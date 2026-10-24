"""HTTP API. Run with: uvicorn forensic.api:app"""

from __future__ import annotations

import contextvars
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import cv2
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import APP_NAME, __version__, formats, media
from . import measure as measure_mod
from . import security
from .accounts import Accounts, actor_for
from .agents import WORKFLOWS, plan_enhancement
from .audit import Actor
from .filters import FilterError
from .jobs import JobCancelled, JobQueue, QueueFull
from .report import render_html, render_pdf
from .workbench import NotFound, Workbench

_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("user", default=None)


_PUBLIC = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"}


async def _authenticate(request: Request) -> None:
    """By default Lucida has no sign-in: every request acts as the built-in local examiner.

    With FORENSIC_AUTH=accounts (a shared lab server) every API request needs a session, and
    the user's role and case membership decide what it may do (see security.py).

    Runs on the event loop, so the user it sets is visible to the endpoint (FastAPI copies the
    context into the worker thread that runs a plain `def` endpoint).
    """
    path = request.url.path
    if not path.startswith("/api/"):
        _user.set(None)
        return
    if not security.enabled():
        _user.set(accounts().local_user())
        return
    if path in _PUBLIC:
        _user.set(None)
        return
    sec = security.Security(wb().db, wb().audit)
    auth = request.headers.get("authorization", "")
    token = request.cookies.get("lucida_session") or (auth[7:] if auth.lower().startswith("bearer ") else None)
    u = sec.user_for_token(token)
    if not u:
        raise HTTPException(401, "sign in first")
    sec.authorize(u, request.method, path, dict(request.path_params))
    _user.set(u)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # Requests for queued work wait in a worker thread until their job finishes; leave room for
    # many of them so waiting requests never starve quick ones (frames, previews, the queue view).
    anyio.to_thread.current_default_thread_limiter().total_tokens = 200
    yield


app = FastAPI(title=APP_NAME, version=__version__, dependencies=[Depends(_authenticate)],
              lifespan=_lifespan)

# Answer only requests addressed to this computer by name. Without this check a web page could
# rebind its own hostname to 127.0.0.1 and use the API as if it were the examiner's browser.
# Add the names a lab uses to reach the workbench (for example behind a TLS proxy) to
# FORENSIC_ALLOWED_HOSTS, comma-separated.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"] + [
    h.strip() for h in os.environ.get("FORENSIC_ALLOWED_HOSTS", "").split(",") if h.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
_wb: Workbench | None = None
_accounts: Accounts | None = None
_jobs: JobQueue | None = None


_wb_lock = threading.Lock()


def wb() -> Workbench:
    # Requests run on a thread pool; the first few must not each open their own Workbench.
    global _wb
    if _wb is None:
        with _wb_lock:
            if _wb is None:
                _wb = Workbench()
    return _wb


def accounts() -> Accounts:
    global _accounts
    if _accounts is None or _accounts.db is not wb().db:
        _accounts = Accounts(wb().db)
    return _accounts


def jobs() -> JobQueue:
    global _jobs
    w = wb()
    if _jobs is None or _jobs.wb is not w:
        if _jobs is not None:
            _jobs.stop()
        _jobs = JobQueue(w)
    return _jobs


def user() -> dict:
    u = _user.get()
    if u:
        return u
    if security.enabled():
        raise HTTPException(401, "sign in first")
    return accounts().local_user()


def actor(_legacy_name: str | None = None) -> Actor:
    """The local examiner, as recorded in the audit log. A name the browser sends is ignored."""
    return actor_for(user())


def queued(kind: str, **params):
    """Run heavy work through the shared job queue and wait for its result."""
    u = user()
    return jobs().run(kind, params, actor_for(u), u["username"])


@app.exception_handler(NotFound)
async def _nf(_: Request, exc: NotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(PermissionError)
async def _forbidden(_: Request, exc: PermissionError):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(QueueFull)
@app.exception_handler(JobCancelled)
async def _conflict(_: Request, exc: Exception):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(FilterError)
@app.exception_handler(media.MediaError)
@app.exception_handler(ValueError)
async def _bad(_: Request, exc: Exception):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# Models -----------------------------------------------------------------------


class CaseIn(BaseModel):
    name: str
    examiner: str
    notes: str = ""


class ChainIn(BaseModel):
    evidence_id: str
    name: str = "Enhancement"
    graph: dict[str, Any] | None = None


class ChainUpdate(BaseModel):
    graph: dict[str, Any]
    name: str | None = None


class PreviewIn(BaseModel):
    graph: dict[str, Any]
    upto: str | None = None
    frame: int = 0


class AnalysisIn(BaseModel):
    analysis: str
    params: dict[str, Any] = Field(default_factory=dict)


class NoteIn(BaseModel):
    text: str


class ConvertIn(BaseModel):
    method: str
    frame_rate: float = 25.0
    codec: str | None = None


class ReferenceIn(BaseModel):
    name: str
    camera: str = ""
    evidence_ids: list[str]


class MeasureIn(BaseModel):
    kind: str
    inputs: dict[str, Any]
    label: str = ""
    chain_id: str | None = None
    frame: int = 0


class TriageIn(BaseModel):
    evidence_ids: list[str] | None = None


class PlanIn(BaseModel):
    evidence_id: str
    goal: str
    roi: list[int] | None = None
    frames: dict[str, int] | None = None
    workflow: str | None = None


class BlurIn(BaseModel):
    graph: dict[str, Any] | None = None
    upto: str | None = None
    frame: int = 0
    roi: list[int] | None = None


# Catalogue and health --------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, "environment": wb().environment()}


@app.get("/api/filters")
def filters():
    return wb().filters()


@app.get("/api/analyses")
def analyses():
    return wb().analyses()


@app.get("/api/models")
def models_list():
    """Learned models with their pinned weights hashes, sources, licences and a fresh hash check."""
    return wb().model_catalogue()


@app.get("/api/catalogue")
def catalogue():
    """Everything else the UI offers: conversion methods, measurement methods, planner workflows, triage rules."""
    from .triage import RULES_VERSION, rules_table

    return {
        "conversion_methods": formats.METHODS,
        "container_families": [{"family": f, "label": l, "notes": n} for f, l, _, n in formats.SIGNATURES],
        "measurements": measure_mod.METHODS,
        "planner_workflows": WORKFLOWS,
        "triage_rules": {"version": RULES_VERSION, "rules": rules_table()},
    }


# The examiner ---------------------------------------------------------------------------


class ExaminerIn(BaseModel):
    full_name: str


@app.get("/api/me")
def me():
    """The examiner every action is recorded under."""
    return user()


@app.patch("/api/me")
def rename_me(body: ExaminerIn):
    before = user()
    u = accounts().rename(body.full_name)
    if u["full_name"] != before["full_name"]:
        wb().audit.append(actor_for(u), "examiner.rename", target=u["username"],
                          params={"from": before["full_name"], "to": u["full_name"]})
    return u


# Job queue -------------------------------------------------------------------------------


class JobIn(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/jobs")
def list_jobs():
    """Everything queued or running for every examiner, and the most recent finished jobs."""
    user()
    return jobs().overview()


@app.post("/api/jobs")
def submit_job(body: JobIn):
    """Queue a job and return at once; follow it with GET /api/jobs/{id}."""
    u = user()
    return jobs().submit(body.kind, body.params, actor_for(u), u["username"])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    user()
    return jobs().get(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    u = user()
    return jobs().cancel(job_id, u["username"], u["role"] == "admin")


# Cases ----------------------------------------------------------------------------------


@app.get("/api/cases")
def list_cases():
    cases = wb().list_cases()
    if security.enabled():
        vis = security.Security(wb().db).visible_cases(user())
        if vis is not None:
            cases = [c for c in cases if c["id"] in vis]
    return cases


@app.post("/api/cases")
def create_case(body: CaseIn, x_examiner: str | None = Header(None)):
    return wb().create_case(body.name, body.examiner, actor(x_examiner or body.examiner), body.notes)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    w = wb()
    return {
        "case": w.get_case(case_id),
        "evidence": w.list_evidence(case_id),
        "removed_evidence": w.list_evidence(case_id, removed=True),
        "chains": w.list_chains(case_id),
        "removed_chains": w.list_chains(case_id, removed=True),
        "analyses": w.list_analysis_results(case_id),
        "camera_references": w.list_camera_references(case_id),
        "measurements": w.list_measurements(case_id),
        "triage": w.list_triage(case_id),
        "review": w.latest_review(case_id),
    }


@app.post("/api/cases/{case_id}/triage")
def triage(case_id: str, body: TriageIn, x_examiner: str | None = Header(None)):
    return queued("case.triage", case_id=case_id, evidence_ids=body.evidence_ids)


class ReviewIn(BaseModel):
    rerun_analyses: bool = True


@app.post("/api/cases/{case_id}/review")
def review(case_id: str, body: ReviewIn, x_examiner: str | None = Header(None)):
    # The examiner asks; the reviewer agent re-checks the case.
    return queued("case.review", case_id=case_id, rerun_analyses=body.rerun_analyses)


@app.get("/api/cases/{case_id}/review")
def latest_review(case_id: str):
    return wb().latest_review(case_id)


@app.post("/api/cases/{case_id}/plan")
def plan(case_id: str, body: PlanIn, x_examiner: str | None = Header(None)):
    actor(x_examiner)  # the examiner asks; the planner agent proposes
    return plan_enhancement(wb(), case_id, body.evidence_id, body.goal, roi=body.roi, frames=body.frames, workflow=body.workflow)


@app.post("/api/cases/{case_id}/camera-references")
def create_reference(case_id: str, body: ReferenceIn, x_examiner: str | None = Header(None)):
    return queued("camera_reference.create", case_id=case_id, name=body.name, evidence_ids=body.evidence_ids,
                  camera=body.camera)


@app.post("/api/cases/{case_id}/notes")
def add_note(case_id: str, body: NoteIn, x_examiner: str | None = Header(None)):
    return wb().note(case_id, body.text, actor(x_examiner))


@app.get("/api/cases/{case_id}/audit")
def audit(case_id: str):
    w = wb()
    return {"verification": w.audit.verify(), "entries": w.audit_entries(case_id)}


# Evidence ---------------------------------------------------------------------------------


@app.post("/api/cases/{case_id}/evidence")
def upload(case_id: str, file: UploadFile = File(...), x_examiner: str | None = Header(None)):
    return wb().ingest(case_id, file.file, file.filename or "evidence", actor(x_examiner))


class LocalFileIn(BaseModel):
    token: str


@app.post("/api/cases/{case_id}/evidence/local")
def ingest_local(case_id: str, body: LocalFileIn):
    """Add a file the examiner chose in the desktop app's Open dialog, read from where it is
    (no upload copy). Only paths the dialog offered, by single-use token, are accepted."""
    from .desktop import local_files

    path = local_files.take(body.token)
    if path is None:
        raise HTTPException(404, "that file is no longer offered: choose it again")
    if not path.is_file():
        raise HTTPException(404, f"{path.name} is no longer there")
    return wb().ingest_path(case_id, path, actor())


@app.get("/api/system")
def system():
    """What this computer gives Lucida: cores, RAM caches, graphics card and model device."""
    from . import compute

    return compute.summary()


@app.get("/api/evidence/{evidence_id}")
def get_evidence(evidence_id: str):
    return wb().get_evidence(evidence_id)


@app.post("/api/evidence/{evidence_id}/verify")
def verify_evidence(evidence_id: str, x_examiner: str | None = Header(None)):
    return wb().verify_evidence(evidence_id, actor(x_examiner))


class RemoveIn(BaseModel):
    reason: str = ""


@app.post("/api/evidence/{evidence_id}/remove")
def remove_evidence(evidence_id: str, body: RemoveIn, x_examiner: str | None = Header(None)):
    return wb().remove_evidence(evidence_id, actor(x_examiner), body.reason)


@app.post("/api/evidence/{evidence_id}/restore")
def restore_evidence(evidence_id: str, x_examiner: str | None = Header(None)):
    return wb().restore_evidence(evidence_id, actor(x_examiner))


@app.get("/api/evidence/{evidence_id}/frames")
def frames(evidence_id: str):
    return wb().frame_table(evidence_id)


@app.get("/api/evidence/{evidence_id}/frame/{n}.png")
def frame_png(evidence_id: str, n: int, request: Request):
    img = wb().source_frame(evidence_id, n)
    data, media_type = _display_image(img, request)
    return Response(data, media_type=media_type, headers={"Cache-Control": "max-age=3600"})


# The PNG route comes first: "{n}" would otherwise also match "12.png".
@app.get("/api/evidence/{evidence_id}/codec/{n}.png")
def codec_png(evidence_id: str, n: int, layer: str = "type", mv: bool = False, grid: bool = False):
    img = wb().codec_view(evidence_id, n, layer=layer, mv=mv, grid=grid)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_PNG_COMPRESSION, 1])
    return Response(buf.tobytes(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/evidence/{evidence_id}/codec/{n}")
def codec_info(evidence_id: str, n: int):
    return wb().codec_info(evidence_id, n)


@app.get("/api/evidence/{evidence_id}/original")
def original(evidence_id: str):
    w = wb()
    ev = w.get_evidence(evidence_id)
    return FileResponse(w.evidence_path(evidence_id), filename=ev["filename"])


@app.post("/api/evidence/{evidence_id}/preview")
def preview(evidence_id: str, body: PreviewIn, request: Request):
    img, info = wb().preview(body.graph, evidence_id, upto=body.upto, frame=body.frame)
    data, media_type = _display_image(img, request)
    return Response(data, media_type=media_type, headers={"X-Preview-Info": json.dumps(info)})


@app.post("/api/evidence/{evidence_id}/convert")
def convert(evidence_id: str, body: ConvertIn, x_examiner: str | None = Header(None)):
    return queued("evidence.convert", evidence_id=evidence_id, method=body.method, frame_rate=body.frame_rate,
                  codec=body.codec)


@app.post("/api/evidence/{evidence_id}/measurements")
def measure(evidence_id: str, body: MeasureIn, x_examiner: str | None = Header(None)):
    return wb().measure(evidence_id, body.kind, body.inputs, actor(x_examiner), label=body.label,
                        chain_id=body.chain_id, frame=body.frame)


@app.post("/api/evidence/{evidence_id}/smart-report")
def smart_report(evidence_id: str, x_examiner: str | None = Header(None)):
    return queued("evidence.smart_report", evidence_id=evidence_id)


@app.post("/api/evidence/{evidence_id}/estimate-blur")
def estimate_blur(evidence_id: str, body: BlurIn):
    """Suggest motion-blur length and angle for the deblur filters (not stored, not evidential)."""
    from .filters.deblur import estimate_motion_blur
    from .filters.base import luminance

    w = wb()
    graph = body.graph or {"source": {"frames": None}, "nodes": []}
    img, _ = w.preview(graph, evidence_id, upto=body.upto, frame=body.frame)
    if body.roi:
        x, y, rw, rh = body.roi
        img = img[max(0, y) : y + rh, max(0, x) : x + rw]
    if min(img.shape[:2]) < 32:
        raise ValueError("region is too small to estimate blur (at least 32 pixels each side)")
    return estimate_motion_blur(luminance(img))


@app.post("/api/evidence/{evidence_id}/analyses")
def run_analysis(evidence_id: str, body: AnalysisIn, x_examiner: str | None = Header(None)):
    return queued("analysis.run", evidence_id=evidence_id, analysis=body.analysis, params=body.params)


# Chains ------------------------------------------------------------------------------------


@app.post("/api/cases/{case_id}/chains")
def create_chain(case_id: str, body: ChainIn, x_examiner: str | None = Header(None)):
    return wb().create_chain(case_id, body.evidence_id, body.name, actor(x_examiner), body.graph)


@app.get("/api/chains/{chain_id}")
def get_chain(chain_id: str):
    return wb().get_chain(chain_id)


@app.put("/api/chains/{chain_id}")
def update_chain(chain_id: str, body: ChainUpdate, x_examiner: str | None = Header(None)):
    return wb().update_chain(chain_id, body.graph, actor(x_examiner), body.name)


@app.post("/api/chains/{chain_id}/remove")
def remove_chain(chain_id: str, body: RemoveIn, x_examiner: str | None = Header(None)):
    return wb().remove_chain(chain_id, actor(x_examiner), body.reason)


@app.post("/api/chains/{chain_id}/restore")
def restore_chain(chain_id: str, x_examiner: str | None = Header(None)):
    return wb().restore_chain(chain_id, actor(x_examiner))


class AcceptIn(BaseModel):
    node_ids: list[str] | None = None


@app.post("/api/chains/{chain_id}/accept")
def accept(chain_id: str, body: AcceptIn, x_examiner: str | None = Header(None)):
    return wb().accept_proposals(chain_id, actor(x_examiner), body.node_ids)


@app.post("/api/chains/{chain_id}/render")
def render_chain(chain_id: str, x_examiner: str | None = Header(None)):
    return queued("chain.render", chain_id=chain_id)


@app.post("/api/chains/{chain_id}/verify")
def verify_chain(chain_id: str, x_examiner: str | None = Header(None)):
    return queued("chain.verify", chain_id=chain_id)


@app.get("/api/chains/{chain_id}/export.png")
def export_chain(chain_id: str, frame: int = 0, x_examiner: str | None = Header(None), examiner: str | None = None):
    png, info = wb().export_chain(chain_id, actor(x_examiner or examiner), frame=frame)
    return Response(png, media_type="image/png", headers={
        "Content-Disposition": f'attachment; filename="chain-{chain_id}-frame{frame}.png"',
        "X-Output-Hash": info["output_hash"], "X-PNG-SHA256": info["png_sha256"]})


# Derived files and reports -------------------------------------------------------------------


@app.get("/api/derived/{digest}.png")
def derived(digest: str):
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        raise HTTPException(400, "bad digest")
    path = wb().store.derived_path(digest, "png")
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=31536000, immutable"})


@app.get("/api/cases/{case_id}/report.json")
def report_json(case_id: str, verify: bool = True, x_examiner: str | None = Header(None), examiner: str | None = None):
    bundle = queued("report.build", case_id=case_id, verify=verify)
    return JSONResponse(bundle, headers={"Content-Disposition": f'attachment; filename="report-{case_id}.json"'})


@app.get("/api/cases/{case_id}/report.html")
def report_html(case_id: str, x_examiner: str | None = Header(None), examiner: str | None = None):
    w = wb()
    return HTMLResponse(render_html(w, queued("report.build", case_id=case_id, verify=True)))


@app.get("/api/cases/{case_id}/report.pdf")
def report_pdf(case_id: str, x_examiner: str | None = Header(None), examiner: str | None = None):
    w = wb()
    pdf = render_pdf(w, queued("report.build", case_id=case_id, verify=True))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="report-{case_id}.pdf"'})


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _display_image(img, request: Request) -> tuple[bytes, str]:
    """Lossless pixels for the viewer. On this computer (the desktop window, or a browser on
    127.0.0.1) an uncompressed BMP: a 4K frame encodes in about 15 ms instead of 0.3-1 s as PNG,
    which is what makes a slider feel live. Over a network, a fast PNG, about a third the size."""
    host = request.client.host if request.client else ""
    if host in _LOOPBACK:
        ok, buf = cv2.imencode(".bmp", cv2.cvtColor(media.to_uint8(img), cv2.COLOR_RGB2BGR))
        return buf.tobytes(), "image/bmp"
    return _fast_png(img), "image/png"


def _fast_png(img) -> bytes:
    u8 = cv2.cvtColor(media.to_uint8(img), cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".png", u8, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    return buf.tobytes()


# Platform services (case management, video, sync, detection, redaction, batch, sign-in) -------

from .api_platform import router as _platform  # noqa: E402

app.include_router(_platform)


class _VersionedPaths:
    """/api/v1/... is the same API as /api/... (a stable, versioned prefix for scripts)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/api/v1/"):
            scope = dict(scope, path="/api/" + scope["path"][len("/api/v1/"):])
            scope["raw_path"] = scope["path"].encode()
        await self.inner(scope, receive, send)


app.add_middleware(_VersionedPaths)


# Front end (built with `npm run build` in ../frontend) --------------------------------------

_dist = Path(os.environ.get("FORENSIC_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(_dist / "index.html")
