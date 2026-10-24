"""Shared job queue for the heavy work several examiners start at the same time.

Renders, analyses, conversions, reviews and reports all run through this queue, so the
machine is never asked to do more at once than it can. Jobs run in two lanes:

- "gpu": jobs that run a learned model. They share the one graphics card, so only
  FORENSIC_GPU_JOBS of them (default 1) run at a time.
- "cpu": everything else, FORENSIC_JOB_WORKERS at a time (default 4).

Within a lane the next job goes to the examiner with the fewest jobs running, then to the one
whose last job started longest ago, then to the oldest request; examiners take turns, so one
examiner's batch cannot hold everyone else up. Each job is recorded in
the database; everyone can see the queue, and the owner or an administrator can cancel a job
that has not started yet.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .audit import Actor
from .db import canonical_json, new_id, now_iso
from .report import build_bundle

LANES = ("cpu", "gpu")
MAX_QUEUED_PER_USER = 50
# How many finished jobs the queue view lists.
HISTORY = 30


class QueueFull(Exception):
    pass


class JobCancelled(Exception):
    pass


@dataclass(frozen=True)
class Kind:
    label: str
    # (wb, params) -> (case_id, target, short description of what the job works on)
    locate: Callable[[Any, dict], tuple[str, str, str]]
    run: Callable[[Any, dict, Actor], Any]
    # (wb, params) -> True when the job runs a learned model and so needs the graphics card
    gpu: Callable[[Any, dict], bool] = lambda wb, p: False
    keep_result: bool = True


def _chain(wb, p):
    ch = wb.get_chain(p["chain_id"])
    return ch["case_id"], ch["id"], ch["name"]


def _evidence(wb, p):
    ev = wb.get_evidence(p["evidence_id"])
    return ev["case_id"], ev["id"], ev["filename"]


def _case(wb, p):
    c = wb.get_case(p["case_id"])
    return c["id"], c["id"], c["name"]


def _analysis_needs_gpu(wb, p) -> bool:
    from .analysis import get_analysis

    return get_analysis(p["analysis"]).ai


def _chain_needs_gpu(wb, p) -> bool:
    return bool(wb.chain_models(wb.get_chain(p["chain_id"])["graph"]))


def _case_needs_gpu(wb, p) -> bool:
    # A review re-runs the case's analyses and a verified report re-renders its chains.
    if not p.get("rerun_analyses", p.get("verify", True)):
        return False
    from .analysis import get_analysis

    cid = p["case_id"]
    return any(wb.chain_models(c["graph"]) for c in wb.list_chains(cid)) or any(
        get_analysis(a["name"]).ai for a in wb.list_analysis_results(cid))


def _traffic_camera(wb, p):
    c = wb.traffic.config(p["camera_id"])
    return c["case_id"], c["camera_id"], c["label"]


def _triage(wb, p, a):
    from .agents import triage_case

    return triage_case(wb, p["case_id"], p.get("evidence_ids"), a)


KINDS: dict[str, Kind] = {
    "chain.render": Kind("Render chain", _chain, lambda wb, p, a: wb.render_chain(p["chain_id"], a), _chain_needs_gpu),
    "chain.verify": Kind("Verify chain", _chain, lambda wb, p, a: wb.verify_chain(p["chain_id"], a), _chain_needs_gpu),
    "analysis.run": Kind(
        "Analysis", _evidence,
        lambda wb, p, a: wb.run_analysis(p["evidence_id"], p["analysis"], p.get("params") or {}, a), _analysis_needs_gpu),
    "evidence.convert": Kind(
        "Convert", _evidence,
        lambda wb, p, a: wb.convert_evidence(p["evidence_id"], p["method"], a, frame_rate=p.get("frame_rate", 25.0),
                                             codec=p.get("codec"))),
    "evidence.smart_report": Kind("Smart report", _evidence, lambda wb, p, a: wb.smart_report(p["evidence_id"], a)),
    "case.triage": Kind("Triage", _case, _triage),
    "case.review": Kind(
        "Review", _case, lambda wb, p, a: wb.review_case(p["case_id"], rerun_analyses=p.get("rerun_analyses", True)),
        _case_needs_gpu),
    "camera_reference.create": Kind(
        "Camera reference", _case,
        lambda wb, p, a: wb.create_camera_reference(p["case_id"], p["name"], p["evidence_ids"], a, camera=p.get("camera", ""))),
    "report.build": Kind(
        "Report", _case, lambda wb, p, a: build_bundle(wb, p["case_id"], a, verify=p.get("verify", True)),
        _case_needs_gpu, keep_result=False),
    # Platform operations ---------------------------------------------------------------
    "video.clip": Kind(
        "Extract clip", _evidence,
        lambda wb, p, a: wb.videoops.extract_clip(p["evidence_id"], int(p["start"]), int(p["end"]), a,
                                                  mode=p.get("mode", "lossless"), audio=p.get("audio", True))),
    "video.render_chain": Kind(
        "Render processed video", _chain, lambda wb, p, a: wb.videoops.render_chain_video(p["chain_id"], a, p.get("codec", "ffv1")),
        _chain_needs_gpu),
    "video.contact_sheet": Kind(
        "Contact sheet", _evidence,
        lambda wb, p, a: wb.videoops.contact_sheet(p["evidence_id"], a, frames=p.get("frames"), count=int(p.get("count", 12)))),
    "intel.detect": Kind(
        "Detect and track", _evidence,
        lambda wb, p, a: wb.intel.run(p["evidence_id"], a, **{k: v for k, v in p.items() if k != "evidence_id"}),
        lambda wb, p: True),
    "traffic.detect": Kind(
        "Traffic detection", _evidence,
        lambda wb, p, a: wb.traffic.detect(p["evidence_id"], a, force=bool(p.get("force")), reason=p.get("reason", "")),
        lambda wb, p: True),
    "traffic.detect_batch": Kind(
        "Traffic detection (batch)", _case,
        lambda wb, p, a: wb.traffic.detect_many(p["case_id"], a, evidence_ids=p.get("evidence_ids"), camera_id=p.get("camera_id"),
                                                only=p.get("only", "pending"), notify=True),
        lambda wb, p: True),
    "traffic.background": Kind(
        "Learn background", _traffic_camera,
        lambda wb, p, a: wb.traffic.learn_background(p["camera_id"], a, p.get("lighting"))),
    "traffic.train": Kind(
        "Train traffic detector", _traffic_camera,
        lambda wb, p, a: wb.traffic.train(p["camera_id"], a, rerun=p.get("rerun", True)), lambda wb, p: True),
    "redaction.render": Kind(
        "Render redaction", lambda wb, p: _doc(wb, p),
        lambda wb, p, a: wb.redaction.render(p["doc_id"], a, p.get("codec", "h264")), lambda wb, p: True),
    "sync.correlate": Kind(
        "Synchronise recordings", _evidence,
        lambda wb, p, a: wb.sync.sync_correlate(p["evidence_id"], p["reference_id"], a, signal=p.get("signal", "audio"),
                                                max_lag_s=float(p.get("max_lag_s", 30)), distance_m=p.get("distance_m"))),
    "case.export": Kind(
        "Export package", _case,
        lambda wb, p, a: wb.casework.export_package(
            p["case_id"], a, evidence_ids=p.get("evidence_ids"), chain_ids=p.get("chain_ids"),
            include_report=p.get("include_report", False), purpose=p.get("purpose", ""), recipient=p.get("recipient", ""))[1]),
}


def _doc(wb, p):
    d = wb.redaction.get(p["doc_id"])
    return d["case_id"], d["id"], d["name"]


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


class JobQueue:
    def __init__(self, wb, cpu_workers: int | None = None, gpu_jobs: int | None = None):
        self.wb = wb
        self.slots = {"cpu": cpu_workers or _env_int("FORENSIC_JOB_WORKERS", 4),
                      "gpu": gpu_jobs or _env_int("FORENSIC_GPU_JOBS", 1)}
        self._cond = threading.Condition()
        self._running: dict[str, str] = {}  # job id -> owner
        self._waiters: dict[str, threading.Event] = {}
        self._outcome: dict[str, tuple[Any, BaseException | None]] = {}
        self._stopped = False
        # Jobs that were running when Lucida last stopped did not finish.
        wb.db.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE status = 'running'",
            (now_iso(), "Lucida was restarted while this job ran; start it again"),
        )
        self._threads = [
            threading.Thread(target=self._worker, args=(lane,), name=f"job-{lane}-{i}", daemon=True)
            for lane in LANES for i in range(self.slots[lane])
        ]
        for t in self._threads:
            t.start()

    # Submitting -------------------------------------------------------------------------

    def submit(self, kind: str, params: dict, actor: Actor, owner: str, *, _wait: bool = False) -> dict:
        """Queue a job and return it at once. Raises NotFound/ValueError for a bad request."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind}; one of {', '.join(KINDS)}")
        k = KINDS[kind]
        try:
            case_id, target, what = k.locate(self.wb, params)
            lane = "gpu" if k.gpu(self.wb, params) else "cpu"
        except KeyError as e:
            raise ValueError(f"{kind} needs the parameter {e.args[0]}") from None
        jid = new_id()
        with self._cond:
            queued = self.wb.db.one("SELECT COUNT(*) AS n FROM jobs WHERE owner = ? AND status = 'queued'", (owner,))["n"]
            if queued >= MAX_QUEUED_PER_USER:
                raise QueueFull(f"you already have {queued} jobs waiting; cancel some or wait for them to finish")
            if _wait:
                self._waiters[jid] = threading.Event()
            self.wb.db.execute(
                "INSERT INTO jobs (id, kind, label, lane, case_id, target, params_json, owner, owner_name, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'queued',?)",
                (jid, kind, f"{k.label}: {what}", lane, case_id, target, canonical_json(params), owner, actor.name, now_iso()),
            )
            self._cond.notify_all()
        return self.get(jid)

    def run(self, kind: str, params: dict, actor: Actor, owner: str) -> Any:
        """Queue a job and wait for its result, raising what the job raised."""
        jid = self.submit(kind, params, actor, owner, _wait=True)["id"]
        self._waiters[jid].wait()
        with self._cond:
            outcome = self._outcome.pop(jid, None)
            self._waiters.pop(jid, None)
        if outcome is None:
            raise JobCancelled(self.get(jid)["error"] or "the job was cancelled")
        result, exc = outcome
        if exc is not None:
            raise exc
        return result

    def cancel(self, job_id: str, username: str, is_admin: bool) -> dict:
        with self._cond:
            job = self.get(job_id)
            if job["owner"] != username and not is_admin:
                raise PermissionError("only the examiner who started a job, or an administrator, can cancel it")
            if job["status"] != "queued":
                raise ValueError(f"the job is {job['status']}; only a job that has not started can be cancelled")
            self.wb.db.execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, error = ? WHERE id = ?",
                               (now_iso(), f"cancelled by {username}", job_id))
            ev = self._waiters.get(job_id)
        if ev:
            ev.set()
        return self.get(job_id)

    # Reading -----------------------------------------------------------------------------

    def get(self, job_id: str) -> dict:
        from .workbench import NotFound

        row = self.wb.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not row:
            raise NotFound(f"no job {job_id}")
        return self._public(row, self._positions())

    def overview(self, history: int = HISTORY) -> dict:
        active = self.wb.db.all("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at")
        done = self.wb.db.all(
            "SELECT * FROM jobs WHERE status NOT IN ('queued','running') ORDER BY finished_at DESC LIMIT ?", (history,))
        pos = self._positions(active)
        return {
            "slots": self.slots,
            "running": {lane: sum(1 for r in active if r["lane"] == lane and r["status"] == "running") for lane in LANES},
            "queued": {lane: sum(1 for r in active if r["lane"] == lane and r["status"] == "queued") for lane in LANES},
            # Running jobs first, then the queue in the order it will start.
            "active": [self._public(r, pos) for r in sorted(
                active, key=lambda r: (r["status"] != "running", r["lane"], pos.get(r["id"], 0), r["created_at"]))],
            "recent": [self._public(r, pos) for r in done],
        }

    def _public(self, row: dict, positions: dict[str, int]) -> dict:
        out = {k: row[k] for k in ("id", "kind", "label", "lane", "case_id", "target", "owner", "owner_name", "status",
                                   "error", "created_at", "started_at", "finished_at")}
        out["params"] = json.loads(row["params_json"])
        out["position"] = positions.get(row["id"])
        out["result"] = json.loads(row["result_json"]) if row.get("result_json") else None
        return out

    def _positions(self, active: list[dict] | None = None) -> dict[str, int]:
        """1-based place in line for each queued job, in the order the lanes will start them."""
        if active is None:
            active = self.wb.db.all("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at")
        out: dict[str, int] = {}
        for lane in LANES:
            running = _count_by_owner(r for r in active if r["status"] == "running")
            last = self._last_started()
            queued = [r for r in active if r["lane"] == lane and r["status"] == "queued"]
            n = 0
            while queued:
                nxt = _pick(queued, running, last)
                queued.remove(nxt)
                n += 1
                out[nxt["id"]] = n
                # Sorts after every real timestamp: this examiner has just had a turn.
                last[nxt["owner"]] = f"~{n:06d}"
        return out

    def _last_started(self) -> dict[str, str]:
        rows = self.wb.db.all("SELECT owner, MAX(started_at) AS t FROM jobs WHERE started_at IS NOT NULL GROUP BY owner")
        return {r["owner"]: r["t"] for r in rows}

    # Running ------------------------------------------------------------------------------

    def _claim(self, lane: str) -> dict | None:
        queued = self.wb.db.all("SELECT * FROM jobs WHERE status = 'queued' AND lane = ? ORDER BY created_at", (lane,))
        if not queued:
            return None
        running = _count_by_owner({"owner": o} for o in self._running.values())
        job = _pick(queued, running, self._last_started())
        self.wb.db.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (now_iso(), job["id"]))
        self._running[job["id"]] = job["owner"]
        return job

    def _worker(self, lane: str) -> None:
        while True:
            with self._cond:
                job = None
                try:
                    while not self._stopped and (job := self._claim(lane)) is None:
                        self._cond.wait(timeout=5)
                except sqlite3.ProgrammingError:
                    return  # the database was closed: this workbench has shut down
                if self._stopped:
                    return
            self._execute(job)

    def _execute(self, job: dict) -> None:
        k = KINDS[job["kind"]]
        result, exc = None, None
        try:
            result = k.run(self.wb, json.loads(job["params_json"]), Actor(job["owner_name"], "human"))
        except BaseException as e:  # noqa: BLE001 - the error is shown to the examiner
            exc = e
        try:
            stored = json.dumps(_jsonable(result), default=str) if (k.keep_result and exc is None) else None
            self.wb.db.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ?, result_json = ? WHERE id = ?",
                ("failed" if exc else "done", now_iso(), _describe(exc) if exc else None, stored, job["id"]),
            )
        finally:
            with self._cond:
                self._running.pop(job["id"], None)
                if job["id"] in self._waiters:
                    self._outcome[job["id"]] = (result, exc)
                    self._waiters[job["id"]].set()
                self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()


def _count_by_owner(rows) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["owner"]] = out.get(r["owner"], 0) + 1
    return out


def _pick(queued: list[dict], running: dict[str, int], last_started: dict[str, str]) -> dict:
    """Fewest jobs running first, then the examiner who waited longest for a turn, then the oldest request."""
    return min(queued, key=lambda r: (running.get(r["owner"], 0), last_started.get(r["owner"], ""), r["created_at"]))


def _jsonable(result: Any) -> Any:
    return result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else str(result)


def _describe(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__
