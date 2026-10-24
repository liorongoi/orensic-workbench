"""Batch conversion of recorder exports (many files, one profile).

A batch reads every file under an input folder (or a list of files), identifies each one from its
bytes (not its extension), converts it into a standard, playable file with the chosen profile and
checks the result. The input files are only ever read. Each item records:

- the input's SHA-256 and size, read before conversion (and re-checked after, so a file that
  changed while it was being converted is reported);
- what was detected (container family, codec, closed-format hint);
- the method actually used and the exact command;
- each output's SHA-256, size and probed frame count, and a frame-count check against the input;
- a per-file log, and on failure a failure class and the error.

When the batch finishes, `manifest.json`, `manifest.csv` and `manifest.json.sha256` are written
to the output folder. Outputs are never overwritten: if a file of that name already exists the
item fails with `output_exists`. Two inputs with identical content are converted once; the
second is reported as a duplicate of the first.

Profiles:
- method "auto" (the recorder parser when there is one, else FFmpeg stream copy, else carving),
  or "parse", "remux", "carve";
- output "copy" (keep the compressed frames: MPEG-TS, or Matroska for Motion JPEG), "h264"
  (re-encoded, CRF 16, for easy playback) or "ffv1" (lossless re-encode in Matroska);
- optional segmenting into pieces of N seconds (stream copy cuts at key frames);
- optional ingest of each original and its output into a case, with the output linked as a
  derived item.

Concurrency comes from the hardware profile (batch_workers) unless set.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from . import compute, formats, media
from .audit import Actor
from .db import canonical_json, new_id, now_iso
from .media import MediaError

if TYPE_CHECKING:  # pragma: no cover
    from .workbench import Workbench

VERSION = "1.0.0"
METHODS = ("auto", "parse", "remux", "carve")
OUTPUTS = ("copy", "h264", "ffv1")
ITEM_STATES = ("queued", "running", "done", "failed", "duplicate", "cancelled", "skipped")
FAILURE_CLASSES = {
    "unsupported_format": "closed, encrypted or unrecognised format; no video could be read",
    "corrupt_input": "the file was recognised but could not be read through (damaged or truncated)",
    "conversion_error": "the conversion tool failed",
    "verification_failed": "the output did not pass its checks (frame count, decodability)",
    "input_changed": "the input file changed while it was being converted",
    "output_exists": "a file with the output name already exists; nothing was overwritten",
    "io_error": "the input could not be read or the output could not be written",
}
# Extensions that are never video: listed but skipped.
SKIP = ("*.txt", "*.log", "*.ini", "*.xml", "*.json", "*.csv", "*.pdf", "*.html", "*.htm", "*.dll", "*.ds_store", "thumbs.db",
        "*.sha256", "*.md5")
MAX_FILES = 10000


class BatchError(ValueError):
    pass


class _ItemFailure(Exception):
    def __init__(self, cls: str, msg: str):
        super().__init__(msg)
        self.cls = cls


def file_hashes(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _safe_stem(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in Path(name).stem)
    return keep.strip("._")[:80] or "file"


class Batch:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.db = wb.db
        self.settings = wb.settings
        self._threads: dict[str, threading.Thread] = {}
        self._flags: dict[str, str] = {}  # batch id -> "pause" | "cancel"
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # Scanning -----------------------------------------------------------------------------------------

    def scan(self, folder: Path, recursive: bool = True, include: str = "*") -> list[dict]:
        """List the files under `folder` with what each one looks like (read from its first 64 KB)."""
        folder = Path(folder)
        if not folder.is_dir():
            raise BatchError(f"{folder} is not a folder")
        it = folder.rglob("*") if recursive else folder.glob("*")
        out = []
        for p in sorted(it):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(folder).as_posix()
            if not fnmatch.fnmatch(p.name.lower(), include.lower()):
                continue
            skip = any(fnmatch.fnmatch(p.name.lower(), s) for s in SKIP)
            out.append({"path": str(p), "relative": rel, "size": p.stat().st_size, "skip": skip,
                        "detected": None if skip else self.detect(p)})
            if len(out) > MAX_FILES:
                raise BatchError(f"more than {MAX_FILES} files; choose a smaller folder")
        return out

    def detect(self, path: Path) -> dict:
        try:
            with open(path, "rb") as f:
                head = f.read(1 << 16)
        except OSError as e:
            return {"error": str(e)}
        s = formats.sniff(head)
        return {"family": s["family"], "label": s["label"], "codec": s["elementary_codec"], "parser": s["parser"],
                "closed_format": formats.closed_format_hint(path.name)}

    # Creating and controlling ---------------------------------------------------------------------------

    def create(self, name: str, actor: Actor, *, input_dir: str | None = None, files: list[str] | None = None,
               output_dir: str, method: str = "auto", output: str = "copy", frame_rate: float = 25.0,
               segment_seconds: float | None = None, case_id: str | None = None, concurrency: int | None = None,
               recursive: bool = True, include: str = "*") -> dict:
        if method not in METHODS:
            raise BatchError(f"method must be one of {', '.join(METHODS)}")
        if output not in OUTPUTS:
            raise BatchError(f"output must be one of {', '.join(OUTPUTS)}")
        if not 0 < frame_rate <= 240:
            raise BatchError("frame rate must be between 0 and 240")
        if segment_seconds is not None and not 0.1 <= segment_seconds <= 86400:
            raise BatchError("segments must be between 0.1 seconds and 24 hours")
        if case_id:
            case = self.wb.get_case(case_id)
            if case.get("disposed"):
                raise BatchError("that case has been disposed of")
        out = Path(output_dir).expanduser().resolve()
        if input_dir:
            src = Path(input_dir).expanduser().resolve()
            if out == src or src in out.parents:
                raise BatchError("the output folder must not be inside the input folder (the inputs must stay untouched)")
            listing = [f for f in self.scan(src, recursive, include) if not f["skip"]]
        elif files:
            listing = []
            for f in files:
                p = Path(f).expanduser().resolve()
                if not p.is_file():
                    raise BatchError(f"{f} is not a file")
                if out in p.parents:
                    raise BatchError("an input file is inside the output folder")
                listing.append({"path": str(p), "relative": p.name, "size": p.stat().st_size, "detected": self.detect(p)})
        else:
            raise BatchError("give an input folder or a list of files")
        if not listing:
            raise BatchError("no files to convert")
        out.mkdir(parents=True, exist_ok=True)
        if (out / "manifest.json").exists():
            raise BatchError("the output folder already holds a batch manifest; choose an empty folder")
        prof = compute.hardware_profile()
        workers = int(concurrency or prof["batch_workers"])
        workers = max(1, min(workers, 16))
        bid = new_id()
        profile = {"method": method, "output": output, "frame_rate": frame_rate, "segment_seconds": segment_seconds,
                   "case_id": case_id, "recursive": recursive, "include": include, "input_dir": input_dir,
                   "hardware_profile": prof["name"], "version": VERSION}
        self.db.execute("INSERT INTO batch_jobs (id, name, case_id, profile_json, output_dir, concurrency, status, actor,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (bid, name.strip()[:200] or "Batch", case_id, canonical_json(profile), str(out), workers, "ready",
                         actor.label, now_iso()))
        for i, f in enumerate(listing):
            self.db.execute("INSERT INTO batch_items (id, batch_id, seq, input_path, input_size, detected_json, status)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (new_id(), bid, i + 1, f["path"], f["size"], canonical_json(f["detected"] or {}), "queued"))
        self.wb.audit.append(actor, "batch.create", case_id=case_id, target=bid,
                             params={"name": name, "files": len(listing), "profile": profile, "output_dir": str(out),
                                     "concurrency": workers})
        return self.get(bid)

    def get(self, batch_id: str, items: bool = True) -> dict:
        b = self.db.one("SELECT * FROM batch_jobs WHERE id = ?", (batch_id,))
        if not b:
            from .workbench import NotFound

            raise NotFound(f"batch {batch_id} not found")
        b["profile"] = json.loads(b.pop("profile_json"))
        counts = {s: 0 for s in ITEM_STATES}
        for r in self.db.all("SELECT status, COUNT(*) AS n FROM batch_items WHERE batch_id = ? GROUP BY status", (batch_id,)):
            counts[r["status"]] = r["n"]
        b["counts"] = counts
        b["total"] = sum(counts.values())
        b["running"] = batch_id in self._threads and self._threads[batch_id].is_alive()
        if items:
            b["items"] = self.items(batch_id)
        return b

    def items(self, batch_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM batch_items WHERE batch_id = ? ORDER BY seq", (batch_id,))
        for r in rows:
            for k in ("detected", "outputs", "checks"):
                v = r.pop(f"{k}_json")
                r[k] = json.loads(v) if v else None
        return rows

    def list(self) -> list[dict]:
        return [self.get(r["id"], items=False) for r in self.db.all("SELECT id FROM batch_jobs ORDER BY created_at DESC")]

    def start(self, batch_id: str, actor: Actor, wait: bool = False) -> dict:
        b = self.get(batch_id, items=False)
        with self._lock:
            if b["running"]:
                raise BatchError("the batch is already running")
            if b["status"] in ("done", "cancelled") and not b["counts"]["queued"]:
                raise BatchError("nothing left to convert; retry the failed items first")
            self._flags.pop(batch_id, None)
            self.db.execute("UPDATE batch_jobs SET status = 'running', finished_at = NULL WHERE id = ?", (batch_id,))
            t = threading.Thread(target=self._run, args=(batch_id, actor), daemon=True, name=f"batch-{batch_id}")
            self._threads[batch_id] = t
            t.start()
        self.wb.audit.append(actor, "batch.start", case_id=b["case_id"], target=batch_id, params={"queued": b["counts"]["queued"]})
        if wait:
            t.join()
        return self.get(batch_id, items=False)

    def wait(self, batch_id: str, timeout: float | None = None) -> dict:
        t = self._threads.get(batch_id)
        if t:
            t.join(timeout)
        return self.get(batch_id)

    def pause(self, batch_id: str, actor: Actor) -> dict:
        """Stop starting new items; items already converting finish."""
        b = self.get(batch_id, items=False)
        if not b["running"]:
            raise BatchError("the batch is not running")
        self._flags[batch_id] = "pause"
        self.wb.audit.append(actor, "batch.pause", case_id=b["case_id"], target=batch_id)
        return self.get(batch_id, items=False)

    def resume(self, batch_id: str, actor: Actor) -> dict:
        t = self._threads.get(batch_id)
        if t and t.is_alive():
            t.join()
        return self.start(batch_id, actor)

    def cancel(self, batch_id: str, actor: Actor) -> dict:
        """Cancel the items not yet converted and stop the ones converting now (their partial outputs are removed)."""
        b = self.get(batch_id, items=False)
        self._flags[batch_id] = "cancel"
        with self._lock:
            for key, proc in list(self._procs.items()):
                if key.startswith(batch_id + ":"):
                    proc.kill()
        t = self._threads.get(batch_id)
        if t and t.is_alive():
            t.join(60)
        self.db.execute("UPDATE batch_items SET status = 'cancelled', log = log || ? WHERE batch_id = ? AND status = 'queued'",
                        (f"{now_iso()} cancelled by {actor.label}\n", batch_id))
        self.db.execute("UPDATE batch_jobs SET status = 'cancelled', finished_at = ? WHERE id = ?", (now_iso(), batch_id))
        self.wb.audit.append(actor, "batch.cancel", case_id=b["case_id"], target=batch_id)
        self._write_manifest(batch_id)
        return self.get(batch_id, items=False)

    def retry(self, batch_id: str, actor: Actor, item_ids: list[str] | None = None) -> dict:
        """Queue failed (or cancelled) items again. Their earlier attempts stay in the log."""
        b = self.get(batch_id, items=False)
        if b["running"]:
            raise BatchError("wait for the batch to finish or pause it first")
        q = "UPDATE batch_items SET status = 'queued', error = NULL, error_class = NULL, log = log || ? WHERE batch_id = ?" \
            " AND status IN ('failed','cancelled')"
        args: list = [f"{now_iso()} queued again by {actor.label}\n", batch_id]
        if item_ids:
            q += f" AND id IN ({','.join('?' * len(item_ids))})"
            args += item_ids
        n = self.db.execute(q, tuple(args)).rowcount
        self.db.execute("UPDATE batch_jobs SET status = 'ready' WHERE id = ?", (batch_id,))
        self.wb.audit.append(actor, "batch.retry", case_id=b["case_id"], target=batch_id, params={"items": n})
        return self.get(batch_id, items=False)

    # Running ----------------------------------------------------------------------------------------------

    def _run(self, batch_id: str, actor: Actor) -> None:
        b = self.get(batch_id, items=False)
        try:
            with ThreadPoolExecutor(max_workers=b["concurrency"]) as pool:
                futures = []
                for it in self.db.all("SELECT id FROM batch_items WHERE batch_id = ? AND status = 'queued' ORDER BY seq", (batch_id,)):
                    futures.append(pool.submit(self._guarded, batch_id, it["id"], b, actor))
                for f in futures:
                    f.result()
        finally:
            flag = self._flags.get(batch_id)
            left = self.db.one("SELECT COUNT(*) AS n FROM batch_items WHERE batch_id = ? AND status = 'queued'", (batch_id,))["n"]
            status = "cancelled" if flag == "cancel" else ("paused" if left else "done")
            self.db.execute("UPDATE batch_jobs SET status = ?, finished_at = ? WHERE id = ?",
                            (status, now_iso() if status != "paused" else None, batch_id))
            if status == "done":
                self._write_manifest(batch_id)
                summary = self.get(batch_id, items=False)["counts"]
                self.wb.audit.append(actor, "batch.finish", case_id=b["case_id"], target=batch_id, params=summary,
                                     output_hash=self.get(batch_id, items=False)["manifest_sha256"])

    def _guarded(self, batch_id: str, item_id: str, b: dict, actor: Actor) -> None:
        if self._flags.get(batch_id):
            return  # paused or cancelled: the item stays queued (or is cancelled by cancel())
        row = self.db.one("SELECT * FROM batch_items WHERE id = ?", (item_id,))
        if row["status"] != "queued":
            return
        self.db.execute("UPDATE batch_items SET status = 'running', attempts = attempts + 1, started_at = ?, log = log || ?"
                        " WHERE id = ?", (now_iso(), f"{now_iso()} attempt {row['attempts'] + 1} started\n", item_id))
        log: list[str] = []
        try:
            res = self._convert(batch_id, row, b, actor, log)
            status = res.pop("status", "done")
            self.db.execute("UPDATE batch_items SET status = ?, mode = ?, outputs_json = ?, checks_json = ?, input_sha256 = ?,"
                            " finished_at = ?, log = log || ? WHERE id = ?",
                            (status, res.get("mode"), canonical_json(res.get("outputs", [])), canonical_json(res.get("checks", {})),
                             res.get("input_sha256"), now_iso(), "".join(log), item_id))
        except _ItemFailure as e:
            self._fail(item_id, e.cls, str(e), log)
        except MediaError as e:
            self._fail(item_id, "conversion_error", str(e), log)
        except OSError as e:
            self._fail(item_id, "io_error", str(e), log)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the batch
            log.append(traceback.format_exc(limit=3))
            self._fail(item_id, "conversion_error", f"{type(e).__name__}: {e}", log)

    def _fail(self, item_id: str, cls: str, msg: str, log: list[str]) -> None:
        if self._flags.get(self.db.one("SELECT batch_id FROM batch_items WHERE id = ?", (item_id,))["batch_id"]) == "cancel":
            status, cls, msg = "cancelled", None, "cancelled while converting; partial output removed"
        else:
            status = "failed"
        log.append(f"{now_iso()} {status}: {msg[:2000]}\n")
        self.db.execute("UPDATE batch_items SET status = ?, error_class = ?, error = ?, finished_at = ?, log = log || ? WHERE id = ?",
                        (status, cls, msg[:4000], now_iso(), "".join(log), item_id))

    def _ffmpeg(self, key: str, cmd: list[str]) -> None:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with self._lock:
            self._procs[key] = proc
        try:
            _, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.pop(key, None)
        if proc.returncode != 0:
            raise MediaError(err.decode(errors="replace")[-1500:] or f"ffmpeg exited with {proc.returncode}")

    def _convert(self, batch_id: str, row: dict, b: dict, actor: Actor, log: list[str]) -> dict:
        src = Path(row["input_path"])
        prof = b["profile"]
        out_dir = Path(b["output_dir"])
        if not src.is_file():
            raise _ItemFailure("io_error", "the input file is no longer there")
        sha, size = file_hashes(src)
        log.append(f"{now_iso()} input sha256 {sha} ({size} bytes)\n")
        dup = self.db.one("SELECT seq, input_path FROM batch_items WHERE batch_id = ? AND input_sha256 = ? AND id != ?"
                          " AND status IN ('done','running')", (batch_id, sha, row["id"]))
        if dup:
            log.append(f"{now_iso()} identical to item {dup['seq']} ({dup['input_path']}); not converted again\n")
            return {"status": "duplicate", "input_sha256": sha, "mode": None, "outputs": [],
                    "checks": {"duplicate_of": dup["seq"]}}
        # Claim the hash before converting so a parallel worker sees the duplicate.
        self.db.execute("UPDATE batch_items SET input_sha256 = ? WHERE id = ?", (sha, row["id"]))
        det = json.loads(row["detected_json"] or "{}")
        if det.get("closed_format") and not det.get("family"):
            raise _ItemFailure("unsupported_format", det["closed_format"])
        method = prof["method"]
        if method == "auto":
            method = self._choose(src, det, log)
        log.append(f"{now_iso()} method {method}; output {prof['output']}\n")
        base = f"{row['seq']:04d}_{_safe_stem(src.name)}"
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            tmp = Path(tmp)
            if method in ("parse", "remux", "carve"):
                try:
                    rec = formats.convert(src, tmp / "step1", method, self.settings, frame_rate=prof["frame_rate"])
                except MediaError as e:
                    msg = str(e)
                    cls = "unsupported_format" if ("no dedicated parser" in msg or "no H.264" in msg or "not recognised" in msg) else (
                        "corrupt_input" if method == "parse" else "conversion_error")
                    raise _ItemFailure(cls, msg) from e
                inter = Path(rec.pop("output_path"))
                record = rec
            else:  # standard container: copy streams as they are
                inter = tmp / f"step1{src.suffix.lower() or '.mkv'}"
                shutil.copyfile(src, inter)
                record = {"method": "none", "description": "already a standard container; copied unchanged"}
            if record.get("command"):
                log.append(f"{now_iso()} {record['command']}\n")
            # Second step: re-encode and/or segment.
            final_dir = tmp / "out"
            final_dir.mkdir()
            ext, vargs = {"copy": (inter.suffix.lstrip(".") or "ts", ["-c", "copy"]),
                          "h264": ("mp4", ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
                                           "-c:a", "aac", "-b:a", "128k"]),
                          "ffv1": ("mkv", ["-c:v", "ffv1", "-level", "3", "-g", "1", "-c:a", "flac"])}[prof["output"]]
            if prof["output"] == "copy" and not prof["segment_seconds"]:
                outs = [(inter, f"{base}.{ext}")]
            else:
                seg = ["-f", "segment", "-segment_time", f"{prof['segment_seconds']:g}", "-reset_timestamps", "1"] \
                    if prof["segment_seconds"] else []
                target = final_dir / (f"{base}_%03d.{ext}" if seg else f"{base}.{ext}")
                cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(inter), "-map", "0:v:0", "-map", "0:a?",
                       *vargs, *(["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"] if prof["output"] == "h264" else []),
                       "-fflags", "+bitexact", "-map_metadata", "-1", *seg, str(target)]
                self._ffmpeg(f"{batch_id}:{row['id']}", cmd)
                log.append(f"{now_iso()} {formats._redact(cmd, inter, target)}\n")
                outs = [(p, p.name) for p in sorted(final_dir.iterdir())]
            if not outs:
                raise _ItemFailure("conversion_error", "the conversion produced no output")
            # Checks on the temporary outputs, before anything is placed in the output folder.
            in_frames = self._input_frames(src, inter, record)
            outputs, total_out = [], 0
            for p, name in outs:
                try:
                    pr = media.probe(p, self.settings)
                except MediaError as e:
                    raise _ItemFailure("verification_failed", f"{name} cannot be read back: {e}") from e
                o_sha, o_size = file_hashes(p)
                total_out += int(pr.get("frame_count") or 0)
                outputs.append({"file": name, "sha256": o_sha, "size": o_size, "frames": pr.get("frame_count"),
                                "codec": pr.get("codec"), "width": pr.get("width"), "height": pr.get("height"),
                                "duration": pr.get("duration"), "_tmp": str(p)})
            checks = {"input_frames": in_frames, "output_frames": total_out,
                      "frame_count_ok": in_frames is None or abs(total_out - in_frames) <= (len(outs) - 1),
                      "decodable": True}
            if in_frames is None:
                checks["note"] = "the input's frame count could not be read independently; only the output was counted"
            if not checks["frame_count_ok"]:
                raise _ItemFailure("verification_failed", f"frame count differs: {in_frames} in, {total_out} out")
            sha2, _ = file_hashes(src)
            if sha2 != sha:
                raise _ItemFailure("input_changed", "the input's SHA-256 changed during conversion")
            checks["input_unchanged"] = True
            # Place outputs; never overwrite.
            for o in outputs:
                dest = out_dir / o["file"]
                if dest.exists():
                    raise _ItemFailure("output_exists", f"{dest} already exists")
            for o in outputs:
                dest = out_dir / o["file"]
                shutil.copyfile(o.pop("_tmp"), dest)
                if file_hashes(dest)[0] != o["sha256"]:
                    raise _ItemFailure("io_error", f"{dest} does not match its hash after writing")
                os.chmod(dest, 0o444)
            log.append(f"{now_iso()} wrote {', '.join(o['file'] for o in outputs)}; frames {in_frames} -> {total_out}\n")
        result = {"status": "done", "input_sha256": sha, "mode": method, "outputs": outputs,
                  "checks": {**checks, "conversion": {k: v for k, v in record.items() if k not in ("description",)}}}
        if prof.get("case_id"):
            result["checks"]["case"] = self._ingest(prof["case_id"], src, out_dir, outputs, record, actor, log)
        return result

    def _choose(self, src: Path, det: dict, log: list[str]) -> str:
        fam = det.get("family")
        if det.get("parser"):
            return "parse"
        if fam in ("isobmff", "matroska", "asf"):
            return "none"
        if fam:
            return "remux"
        if det.get("codec"):
            return "carve"
        try:
            media.probe(src, self.settings)
            log.append(f"{now_iso()} no signature matched but FFmpeg can read the file\n")
            return "remux"
        except MediaError:
            raise _ItemFailure("unsupported_format", det.get("closed_format") or "no known container or video codec found") from None

    def _input_frames(self, src: Path, inter: Path, record: dict) -> int | None:
        """Frames in the input, counted independently of the output where possible."""
        t = record.get("timing") or {}
        if record.get("parser_stats", {}).get("frames"):
            return int(record["parser_stats"]["frames"])
        if t.get("frames"):
            return int(t["frames"])
        for p in (src, inter):
            try:
                n = media.probe(p, self.settings).get("frame_count")
                if n:
                    return int(n)
            except MediaError:
                continue
        return None

    def _ingest(self, case_id: str, src: Path, out_dir: Path, outputs: list[dict], record: dict, actor: Actor,
                log: list[str]) -> dict:
        orig = self.wb.ingest_path(case_id, src, actor, source={"batch": True})
        linked = []
        for o in outputs:
            p = out_dir / o["file"]
            with open(p, "rb") as f:
                stored = self.wb.store.ingest(f, p.name)
            info = media.probe(Path(stored["path"]), self.settings)
            ev = self.wb._insert_evidence(case_id, o["file"], stored, info, actor, derived_from=orig["id"],
                                          conversion={**{k: v for k, v in record.items() if k != "description"},
                                                      "batch_output": o["file"], "source_sha256": orig["sha256"],
                                                      "output_sha256": stored["sha256"]})
            linked.append(ev["id"])
        log.append(f"{now_iso()} added to case {case_id}: original {orig['id']}, outputs {', '.join(linked)}\n")
        return {"original_id": orig["id"], "output_ids": linked}

    # Manifest ---------------------------------------------------------------------------------------------

    def _write_manifest(self, batch_id: str) -> None:
        b = self.get(batch_id)
        out = Path(b["output_dir"])
        items = []
        for it in b["items"]:
            items.append({"seq": it["seq"], "input": it["input_path"], "input_sha256": it["input_sha256"],
                          "input_size": it["input_size"], "detected": it["detected"], "status": it["status"],
                          "method": it["mode"], "attempts": it["attempts"], "outputs": it["outputs"] or [],
                          "checks": it["checks"], "error_class": it["error_class"], "error": it["error"],
                          "started": it["started_at"], "finished": it["finished_at"]})
        manifest = {"manifest_version": 1, "batch_id": batch_id, "name": b["name"], "profile": b["profile"],
                    "created_by": b["actor"], "created_at": b["created_at"], "finished_at": now_iso(), "counts": b["counts"],
                    "failure_classes": FAILURE_CLASSES, "items": items,
                    "note": "Inputs were read only. Each output's SHA-256 was computed after it was written."}
        mjson = json.dumps(manifest, indent=2, sort_keys=True).encode()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["seq", "input", "input_sha256", "status", "method", "output", "output_sha256", "output_frames",
                    "input_frames", "frame_count_ok", "error_class", "error"])
        for it in items:
            outs = it["outputs"] or [{}]
            for o in outs:
                c = it["checks"] or {}
                w.writerow([it["seq"], it["input"], it["input_sha256"] or "", it["status"], it["method"] or "",
                            o.get("file", ""), o.get("sha256", ""), o.get("frames", ""), c.get("input_frames", ""),
                            c.get("frame_count_ok", ""), it["error_class"] or "", (it["error"] or "")[:300]])
        for name, data in (("manifest.json", mjson), ("manifest.csv", buf.getvalue().encode()),
                           ("manifest.json.sha256", (hashlib.sha256(mjson).hexdigest() + "  manifest.json\n").encode())):
            p = out / name
            if p.exists():
                os.chmod(p, 0o644)
            p.write_bytes(data)
        self.db.execute("UPDATE batch_jobs SET manifest_sha256 = ? WHERE id = ?", (hashlib.sha256(mjson).hexdigest(), batch_id))
