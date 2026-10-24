"""HTTP API for the platform services: case management, custody and export, video operations,
multi-camera synchronisation, detection/tracking/plates, redaction, batch conversion and the
optional lab sign-in. Mounted by api.py under /api (and /api/v1)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import compute, security
from . import api as core

router = APIRouter(prefix="/api")


def _req(body: dict, key: str, kind=None):
    if key not in body or body[key] in (None, ""):
        raise ValueError(f"'{key}' is required")
    v = body[key]
    return kind(v) if kind else v


def _sec() -> security.Security:
    return security.Security(core.wb().db, core.wb().audit)


def _visible() -> list[str] | None:
    if not security.enabled():
        return None
    return _sec().visible_cases(core.user())


def _role() -> str:
    return core.user()["role"]


# Hardware profile and capabilities ---------------------------------------------------------------


@router.get("/platform/profile")
def profile():
    return {"profile": compute.hardware_profile(), "intelligence": core.wb().intel.capabilities(),
            "auth": "accounts" if security.enabled() else "none"}


# Sign-in (only with FORENSIC_AUTH=accounts) --------------------------------------------------------

_login_hits: dict[str, list[float]] = {}


def _need_accounts():
    if not security.enabled():
        raise HTTPException(404)


@router.get("/auth/status")
def auth_status(request: Request):
    if not security.enabled():
        return {"mode": "none"}
    s = _sec()
    tok = request.cookies.get("lucida_session")
    u = s.user_for_token(tok)
    return {"mode": "accounts", "needs_setup": not s.has_users(), "user": u, "idle_minutes": security.idle_minutes()}


@router.post("/auth/setup")
def auth_setup(body: dict = Body(...)):
    """Create the first administrator. Only possible while there are no accounts."""
    _need_accounts()
    s = _sec()
    if s.has_users():
        raise HTTPException(409, "accounts already exist; ask an administrator")
    return s.create_user(_req(body, "username"), _req(body, "full_name"), "admin", _req(body, "password"))


@router.post("/auth/login")
def auth_login(request: Request, body: dict = Body(...)):
    _need_accounts()
    ip = request.client.host if request.client else ""
    now = time.monotonic()
    hits = [t for t in _login_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 20:
        raise HTTPException(429, "too many sign-in attempts from this address; wait a minute")
    _login_hits[ip] = hits + [now]
    token, u = _sec().login(_req(body, "username"), _req(body, "password"), ip)
    resp = JSONResponse(u)
    resp.set_cookie("lucida_session", token, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=security.MAX_SESSION_HOURS * 3600)
    return resp


@router.post("/auth/logout")
def auth_logout(request: Request):
    _need_accounts()
    _sec().logout(request.cookies.get("lucida_session"))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("lucida_session")
    return resp


@router.get("/users")
def users():
    _need_accounts()
    return _sec().users()


@router.post("/users")
def create_user(body: dict = Body(...)):
    _need_accounts()
    return _sec().create_user(_req(body, "username"), _req(body, "full_name"), _req(body, "role"),
                              _req(body, "password"), core.actor())


@router.patch("/users/{username}")
def update_user(username: str, body: dict = Body(...)):
    _need_accounts()
    return _sec().update_user(username, core.actor(), role=body.get("role"), disabled=body.get("disabled"),
                              password=body.get("password"))


@router.get("/cases/{case_id}/members")
def members(case_id: str):
    _need_accounts()
    return _sec().members(case_id)


@router.post("/cases/{case_id}/members")
def add_member(case_id: str, body: dict = Body(...)):
    _need_accounts()
    core.wb().get_case(case_id)
    return _sec().add_member(case_id, _req(body, "username"), _req(body, "role"), core.actor())


@router.delete("/cases/{case_id}/members/{username}")
def remove_member(case_id: str, username: str):
    _need_accounts()
    return _sec().remove_member(case_id, username, core.actor())


# Case management ---------------------------------------------------------------------------------------


@router.post("/cases/{case_id}/details")
def update_case(case_id: str, body: dict = Body(...)):
    return core.wb().casework.update_case(case_id, body, core.actor())


@router.get("/cases/{case_id}/subcases")
def subcases(case_id: str):
    return core.wb().casework.subcases(case_id)


@router.get("/cases/{case_id}/custody")
def custody(case_id: str, evidence_id: str | None = None):
    return core.wb().casework.custody(case_id, evidence_id)


@router.post("/evidence/{evidence_id}/custody")
def record_custody(evidence_id: str, body: dict = Body(...)):
    return core.wb().casework.record_custody(
        evidence_id, _req(body, "event"), core.actor(), from_party=body.get("from_party", ""),
        to_party=body.get("to_party", ""), location=body.get("location", ""), notes=body.get("notes", ""))


@router.put("/evidence/{evidence_id}/tags")
def set_tags(evidence_id: str, body: dict = Body(...)):
    return {"tags": core.wb().casework.set_tags(evidence_id, list(body.get("tags") or []), core.actor())}


@router.get("/cases/{case_id}/tags")
def case_tags(case_id: str):
    return core.wb().casework.case_tags(case_id)


@router.post("/evidence/{evidence_id}/bookmarks")
def add_bookmark(evidence_id: str, body: dict = Body(...)):
    return core.wb().casework.add_bookmark(evidence_id, _req(body, "frame", int), _req(body, "label"), core.actor(),
                                           end_frame=body.get("end_frame"), note=body.get("note", ""),
                                           kind=body.get("kind", "bookmark"))


@router.get("/cases/{case_id}/bookmarks")
def bookmarks(case_id: str, evidence_id: str | None = None):
    return core.wb().casework.bookmarks(case_id, evidence_id)


@router.delete("/bookmarks/{bookmark_id}")
def delete_bookmark(bookmark_id: str):
    core.wb().casework.delete_bookmark(bookmark_id, core.actor())
    return {"ok": True}


@router.get("/cases/{case_id}/duplicates")
def duplicates(case_id: str, max_distance: int = 8):
    return core.wb().casework.duplicates(case_id, max_distance, core.actor())


@router.get("/search")
def search(q: str = "", media_type: str | None = None, tag: str | None = None, limit: int = 100, offset: int = 0):
    return core.wb().casework.search(q, core.actor(), case_ids=_visible(), media_type=media_type, tag=tag,
                                     limit=limit, offset=offset)


@router.post("/cases/{case_id}/legal-hold")
def legal_hold(case_id: str, body: dict = Body(...)):
    return core.wb().casework.set_legal_hold(case_id, bool(body.get("on")), body.get("reason", ""), core.actor())


@router.post("/cases/{case_id}/retention")
def retention(case_id: str, body: dict = Body(...)):
    return core.wb().casework.set_retention(case_id, body.get("until") or None, core.actor())


@router.post("/cases/{case_id}/dispose")
def dispose(case_id: str, body: dict = Body(...)):
    return core.wb().casework.dispose_case(case_id, core.actor(), reason=body.get("reason", ""),
                                           confirm=body.get("confirm", ""), role=_role())


@router.post("/cases/{case_id}/verify")
def verify_case(case_id: str):
    return core.wb().casework.verify_case(case_id, core.actor())


@router.post("/cases/{case_id}/exports")
def export_package(case_id: str, body: dict = Body(default={})):
    return core.queued("case.export", case_id=case_id, **{k: body[k] for k in
                       ("evidence_ids", "chain_ids", "include_report", "purpose", "recipient") if k in body})


@router.get("/cases/{case_id}/exports")
def exports(case_id: str):
    return core.wb().casework.exports(case_id)


@router.get("/exports/{export_id}/package.zip")
def export_zip(export_id: str):
    data = core.wb().casework.export_bytes(export_id)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="export-{export_id}.zip"'})


@router.post("/exports/verify")
def verify_package(file: UploadFile = File(...)):
    return core.wb().casework.verify_package(file.file.read())


@router.post("/chains/{chain_id}/fork")
def fork_chain(chain_id: str, body: dict = Body(default={})):
    return core.wb().casework.fork_chain(chain_id, core.actor(), upto=body.get("upto"), name=body.get("name", ""))


@router.post("/chains/{chain_id}/preset")
def save_preset(chain_id: str, body: dict = Body(...)):
    return core.wb().casework.save_preset(chain_id, _req(body, "name"), core.actor(), body.get("description", ""))


@router.get("/presets")
def presets():
    return core.wb().casework.presets()


@router.post("/evidence/{evidence_id}/apply-preset")
def apply_preset(evidence_id: str, body: dict = Body(...)):
    ev = core.wb().get_evidence(evidence_id)
    return core.wb().casework.apply_preset(_req(body, "preset_id"), ev["case_id"], evidence_id, core.actor(), body.get("name", ""))


# Video operations ---------------------------------------------------------------------------------------


@router.post("/evidence/{evidence_id}/clip")
def clip(evidence_id: str, body: dict = Body(...)):
    return core.queued("video.clip", evidence_id=evidence_id, start=_req(body, "start", int), end=_req(body, "end", int),
                       mode=body.get("mode", "lossless"), audio=bool(body.get("audio", True)))


@router.post("/evidence/{evidence_id}/frames.zip")
def export_frames(evidence_id: str, body: dict = Body(default={})):
    data, info = core.wb().videoops.export_frames(evidence_id, core.actor(), frames=body.get("frames"),
                                                  start=body.get("start"), end=body.get("end"), step=int(body.get("step", 1)))
    return Response(data, media_type="application/zip", headers={"X-Zip-Sha256": info["zip_sha256"],
                    "Content-Disposition": f'attachment; filename="frames-{evidence_id}.zip"'})


@router.get("/evidence/{evidence_id}/temporal")
def temporal(evidence_id: str, threshold: float = 0.35):
    return core.wb().videoops.temporal_profile(evidence_id, threshold)


@router.post("/evidence/{evidence_id}/contact-sheet")
def contact_sheet(evidence_id: str, body: dict = Body(default={})):
    return core.queued("video.contact_sheet", evidence_id=evidence_id, frames=body.get("frames"), count=body.get("count", 12))


@router.get("/evidence/{evidence_id}/av-timing")
def av_timing(evidence_id: str):
    return core.wb().videoops.av_timing(evidence_id)


@router.post("/cases/{case_id}/sequence")
def sequence(case_id: str, body: dict = Body(...)):
    return core.wb().videoops.sequence_to_video(case_id, _req(body, "evidence_ids"), float(body.get("frame_rate", 25)),
                                                core.actor(), codec=body.get("codec", "ffv1"))


@router.post("/cases/{case_id}/concatenate")
def concatenate(case_id: str, body: dict = Body(...)):
    return core.wb().videoops.concatenate(case_id, _req(body, "evidence_ids"), core.actor(), body.get("mode", "copy"))


@router.post("/chains/{chain_id}/render-video")
def render_video(chain_id: str, body: dict = Body(default={})):
    return core.queued("video.render_chain", chain_id=chain_id, codec=body.get("codec", "ffv1"))


@router.post("/compare")
def compare(body: dict = Body(...)):
    return core.wb().videoops.compare(_req(body, "a"), _req(body, "b"), core.actor(), bool(body.get("align", False)))


# Multi-camera ---------------------------------------------------------------------------------------------


@router.get("/cases/{case_id}/cameras")
def cameras(case_id: str):
    return core.wb().sync.cameras(case_id)


@router.post("/cases/{case_id}/cameras")
def create_camera(case_id: str, body: dict = Body(...)):
    return core.wb().sync.create_camera(case_id, _req(body, "label"), core.actor(), location=body.get("location", ""),
                                        latitude=body.get("latitude"), longitude=body.get("longitude"),
                                        notes=body.get("notes", ""))


@router.post("/evidence/{evidence_id}/camera")
def assign_camera(evidence_id: str, body: dict = Body(...)):
    return core.wb().sync.assign_camera(evidence_id, body.get("camera_id"), core.actor())


@router.get("/evidence/{evidence_id}/sync")
def sync_state(evidence_id: str):
    s = core.wb().sync
    return {"current": s.current(evidence_id), "history": s.history(evidence_id)}


@router.post("/evidence/{evidence_id}/sync")
def sync(evidence_id: str, body: dict = Body(...)):
    s, a, m = core.wb().sync, core.actor(), _req(body, "method")
    if m == "reference":
        return s.set_reference(evidence_id, a, body.get("start_iso"))
    if m == "manual":
        return s.sync_manual(evidence_id, _req(body, "offset_s", float), a, drift_ppm=float(body.get("drift_ppm", 0)),
                             uncertainty_s=body.get("uncertainty_s"), basis=body.get("basis", ""))
    if m == "clock":
        return s.sync_clock(evidence_id, _req(body, "clock_time"), float(body.get("clock_error_s", 1.0)), a,
                            source=body.get("source", ""), frame=int(body.get("frame", 0)))
    if m == "event":
        return s.sync_event(evidence_id, _req(body, "frame", int), _req(body, "reference_id"),
                            _req(body, "reference_frame", int), a, label=body.get("label", ""))
    if m == "two_events":
        return s.sync_two_events(evidence_id, tuple(_req(body, "frames")), _req(body, "reference_id"),
                                 tuple(_req(body, "reference_frames")), a, label=body.get("label", ""))
    if m in ("audio", "visual"):
        return core.queued("sync.correlate", evidence_id=evidence_id, reference_id=_req(body, "reference_id"), signal=m,
                           max_lag_s=float(body.get("max_lag_s", 30)), distance_m=body.get("distance_m"))
    raise ValueError("method must be reference, manual, clock, event, two_events, audio or visual")


@router.get("/cases/{case_id}/timeline")
def timeline(case_id: str):
    return core.wb().sync.timeline(case_id)


@router.post("/cases/{case_id}/synchronized-frames")
def synchronized_frames(case_id: str, body: dict = Body(...)):
    if body.get("evidence_id") and body.get("case_time") is None:
        T, _ = core.wb().sync.global_time(body["evidence_id"], int(body.get("frame", 0)))
    else:
        T = _req(body, "case_time", float)
    return core.wb().sync.synchronized_frames(case_id, T, core.actor(), body.get("evidence_ids"))


# Detection, tracking and plates --------------------------------------------------------------------------


@router.post("/evidence/{evidence_id}/detect")
def detect(evidence_id: str, body: dict = Body(default={})):
    allowed = ("classes", "min_score", "frame_step", "start", "end", "model", "track", "plates", "country",
               "plate_frames_per_track")
    return core.queued("intel.detect", evidence_id=evidence_id, **{k: body[k] for k in allowed if k in body})


@router.get("/cases/{case_id}/detection-runs")
def runs(case_id: str, evidence_id: str | None = None):
    return core.wb().intel.runs(case_id, evidence_id)


@router.get("/detection-runs/{run_id}")
def get_run(run_id: str):
    return core.wb().intel.get_run(run_id)


@router.get("/detection-runs/{run_id}/detections")
def detections(run_id: str, frame: int | None = None):
    return core.wb().intel.detections(run_id, frame)


@router.get("/evidence/{evidence_id}/overlay/{n}.png")
def overlay(evidence_id: str, n: int):
    return Response(core.wb().intel.overlay(evidence_id, n), media_type="image/png")


@router.get("/cases/{case_id}/tracks")
def tracks(case_id: str, evidence_id: str | None = None, run_id: str | None = None):
    return core.wb().intel.tracks(case_id, evidence_id, run_id)


@router.get("/cases/{case_id}/tracks/export")
def export_tracks(case_id: str, fmt: str = "json", evidence_id: str | None = None):
    data, mime = core.wb().intel.export_tracks(case_id, core.actor(), fmt, evidence_id)
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="tracks.{fmt}"'})


@router.patch("/tracks/{track_id}")
def update_track(track_id: str, body: dict = Body(...)):
    return core.wb().intel.update_track(track_id, core.actor(), label=body.get("label"), status=body.get("status"))


@router.post("/tracks/{track_id}/merge")
def merge_track(track_id: str, body: dict = Body(...)):
    return core.wb().intel.merge_tracks(track_id, _req(body, "other_id"), core.actor())


@router.post("/tracks/{track_id}/split")
def split_track(track_id: str, body: dict = Body(...)):
    return core.wb().intel.split_track(track_id, _req(body, "frame", int), core.actor())


@router.post("/tracks/{track_id}/boxes")
def add_track_box(track_id: str, body: dict = Body(...)):
    return core.wb().intel.add_track_box(track_id, _req(body, "frame", int), _req(body, "box"), core.actor())


@router.get("/cases/{case_id}/plates")
def plates(case_id: str, status: str | None = None, evidence_id: str | None = None):
    return core.wb().intel.plates(case_id, status, evidence_id)


@router.get("/cases/{case_id}/plates/review-queue")
def review_queue(case_id: str):
    return core.wb().intel.review_queue(case_id)


@router.get("/plates/{plate_id}")
def get_plate(plate_id: str):
    return core.wb().intel.get_plate(plate_id)


@router.post("/plates/{plate_id}/review")
def review_plate(plate_id: str, body: dict = Body(...)):
    return core.wb().intel.review_plate(plate_id, _req(body, "action"), core.actor(), body.get("text", ""), body.get("note", ""))


@router.post("/evidence/{evidence_id}/plates")
def add_plate(evidence_id: str, body: dict = Body(...)):
    return core.wb().intel.add_plate(evidence_id, _req(body, "frame", int), _req(body, "corners"), core.actor(),
                                     body.get("country", "ANY"))


@router.post("/intel/search")
def intel_search(body: dict = Body(...)):
    vis = _visible()
    cases = body.get("case_ids") or [c["id"] for c in core.wb().list_cases()]
    if vis is not None:
        cases = [c for c in cases if c in vis]
    return core.wb().intel.search(core.actor(), case_ids=cases, plate=body.get("plate"), cls=body.get("cls"),
                                  evidence_ids=body.get("evidence_ids"), min_score=float(body.get("min_score", 0)),
                                  reviewed_only=bool(body.get("reviewed_only")), time_from=body.get("time_from"),
                                  time_to=body.get("time_to"), limit=int(body.get("limit", 200)), offset=int(body.get("offset", 0)))


@router.get("/cases/{case_id}/cross-camera")
def cross_camera(case_id: str, min_similarity: float = 0.6):
    return core.wb().intel.cross_camera_candidates(case_id, core.actor(), min_similarity)


# Annotation and redaction ------------------------------------------------------------------------------


@router.post("/evidence/{evidence_id}/documents")
def save_document(evidence_id: str, body: dict = Body(...)):
    return core.wb().redaction.save(evidence_id, _req(body, "kind"), body.get("name", ""), _req(body, "doc"), core.actor(),
                                    body.get("id"))


@router.get("/cases/{case_id}/documents")
def documents(case_id: str, evidence_id: str | None = None):
    return core.wb().redaction.list(case_id, evidence_id)


@router.get("/documents/{doc_id}")
def get_document(doc_id: str):
    return core.wb().redaction.get(doc_id)


@router.post("/evidence/{evidence_id}/track-regions")
def track_regions(evidence_id: str, body: dict = Body(...)):
    return core.wb().redaction.track_regions(evidence_id, _req(body, "track_ids"), body.get("method", "pixelate"), body.get("part", "whole"))


@router.post("/evidence/{evidence_id}/redaction-preview/{n}.png")
def redaction_preview(evidence_id: str, n: int, body: dict = Body(...)):
    """One frame with a (possibly unsaved) redaction document applied; nothing is stored."""
    from . import media

    w = core.wb()
    ev = w.get_evidence(evidence_id)
    doc = w.redaction.validate("redaction", body, ev)
    out, _ = w.redaction.apply(doc, media.to_uint8(w.source_frame(evidence_id, n)), n)
    return Response(core._fast_png(out.astype("float32") / 255), media_type="image/png")


@router.post("/documents/{doc_id}/render")
def render_document(doc_id: str, body: dict = Body(default={})):
    return core.queued("redaction.render", doc_id=doc_id, codec=body.get("codec", "h264"))


@router.post("/documents/{doc_id}/exhibit")
def render_exhibit(doc_id: str, body: dict = Body(default={})):
    return core.wb().redaction.render_exhibit(doc_id, core.actor(), int(body.get("frame", 0)), body.get("exhibit_id", ""))


# Batch conversion --------------------------------------------------------------------------------------


@router.post("/batches/scan")
def batch_scan(body: dict = Body(...)):
    return core.wb().batch.scan(Path(_req(body, "input_dir")), bool(body.get("recursive", True)), body.get("include", "*"))


@router.post("/batches")
def batch_create(body: dict = Body(...)):
    b = core.wb().batch.create(
        body.get("name", "Batch"), core.actor(), input_dir=body.get("input_dir"), files=body.get("files"),
        output_dir=_req(body, "output_dir"), method=body.get("method", "auto"), output=body.get("output", "copy"),
        frame_rate=float(body.get("frame_rate", 25)), segment_seconds=body.get("segment_seconds"),
        case_id=body.get("case_id"), concurrency=body.get("concurrency"), recursive=bool(body.get("recursive", True)),
        include=body.get("include", "*"))
    if body.get("start", True):
        core.wb().batch.start(b["id"], core.actor())
    return core.wb().batch.get(b["id"])


@router.get("/batches")
def batch_list():
    return core.wb().batch.list()


@router.get("/batches/{batch_id}")
def batch_get(batch_id: str):
    return core.wb().batch.get(batch_id)


@router.post("/batches/{batch_id}/{action}")
def batch_action(batch_id: str, action: str, body: dict = Body(default={})):
    b = core.wb().batch
    if action == "start":
        return b.start(batch_id, core.actor())
    if action == "pause":
        return b.pause(batch_id, core.actor())
    if action == "resume":
        return b.resume(batch_id, core.actor())
    if action == "cancel":
        return b.cancel(batch_id, core.actor())
    if action == "retry":
        return b.retry(batch_id, core.actor(), body.get("item_ids"))
    raise HTTPException(404)


# Live job updates -----------------------------------------------------------------------------------------


@router.get("/jobs-stream")
async def jobs_stream(request: Request):
    """Server-sent events: the job queue overview whenever it changes (checked twice a second)."""

    async def gen():
        last = None
        for _ in range(7200):
            if await request.is_disconnected():
                break
            ov = json.dumps(core.jobs().overview(), default=str, sort_keys=True)
            if ov != last:
                last = ov
                yield f"event: jobs\ndata: {ov}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
