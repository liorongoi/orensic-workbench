"""HTTP API for still-image traffic detection (forensic/traffic). Mounted by api.py under /api and /api/v1."""

from __future__ import annotations

from fastapi import APIRouter, Body, Query
from fastapi.responses import HTMLResponse, Response

from . import api as core

router = APIRouter(prefix="/api")


def _t():
    return core.wb().traffic


def _maybe_train(camera_id: str | None) -> dict | None:
    """Start training on its own once a camera has enough new corrections and none is waiting."""
    if not camera_id or not _t().wants_training(camera_id):
        return None
    busy = core.wb().db.one(
        "SELECT id FROM jobs WHERE kind = 'traffic.train' AND status IN ('queued','running') AND target = ?", (camera_id,))
    if busy:
        return None
    u = core.user()
    return core.jobs().submit("traffic.train", {"camera_id": camera_id}, core.actor_for(u), u["username"])


def _with_training(result: dict, evidence_id: str) -> dict:
    cam = core.wb().db.one("SELECT camera_id FROM traffic_images WHERE evidence_id = ?", (evidence_id,))
    job = _maybe_train(cam["camera_id"] if cam else None)
    return {**result, "training_job": job}


# Cameras ------------------------------------------------------------------------------------------


@router.get("/cases/{case_id}/traffic/cameras")
def cameras(case_id: str):
    return _t().cameras(case_id)


@router.post("/cases/{case_id}/traffic/cameras")
def create_camera(case_id: str, body: dict = Body(...)):
    return _t().create_camera(case_id, str(body.get("label", "")), core.actor(), float(body.get("utc_offset_hours", 0)),
                              str(body.get("location", "")))


@router.get("/traffic/cameras/{camera_id}")
def camera(camera_id: str):
    return _t().config(camera_id)


@router.put("/traffic/cameras/{camera_id}")
def set_camera(camera_id: str, body: dict = Body(...)):
    return _t().set_config(camera_id, core.actor(), utc_offset_hours=body.get("utc_offset_hours"),
                           regions=body.get("regions"), settings=body.get("settings"))


@router.post("/traffic/cameras/{camera_id}/background")
def learn_background(camera_id: str, body: dict = Body(default={})):
    return core.queued("traffic.background", camera_id=camera_id, lighting=body.get("lighting"))


@router.post("/traffic/cameras/{camera_id}/train")
def train(camera_id: str, body: dict = Body(default={})):
    """Queue training now; the examiner is notified when it starts and when it finishes."""
    u = core.user()
    return core.jobs().submit("traffic.train", {"camera_id": camera_id, "rerun": bool(body.get("rerun", True))},
                              core.actor_for(u), u["username"])


@router.get("/traffic/cameras/{camera_id}/profiles")
def profiles(camera_id: str):
    return _t().profiles(camera_id)


# Images -------------------------------------------------------------------------------------------


@router.get("/cases/{case_id}/traffic/images")
def images(case_id: str, camera_id: str | None = None, status: str | None = None):
    return _t().images(case_id, core.actor(), camera_id=camera_id, status=status)


@router.post("/cases/{case_id}/traffic/detect")
def detect_batch(case_id: str, body: dict = Body(default={})):
    """Queue detection on many images (pending ones by default) and return the job at once."""
    u = core.user()
    params = {"case_id": case_id, "only": body.get("only", "pending")}
    for k in ("evidence_ids", "camera_id"):
        if body.get(k):
            params[k] = body[k]
    return core.jobs().submit("traffic.detect_batch", params, core.actor_for(u), u["username"])


@router.get("/traffic/images/{evidence_id}")
def image(evidence_id: str):
    return _t().image(evidence_id)


@router.patch("/traffic/images/{evidence_id}")
def update_image(evidence_id: str, body: dict = Body(...)):
    return _t().set_image(evidence_id, core.actor(), camera_id=body.get("camera_id"), time=body.get("time"),
                          time_is_local=bool(body.get("time_is_local", True)), lighting=body.get("lighting"),
                          regions=body.get("regions"), clear_regions=bool(body.get("clear_regions")))


@router.post("/traffic/images/{evidence_id}/detect")
def detect(evidence_id: str, body: dict = Body(default={})):
    return core.queued("traffic.detect", evidence_id=evidence_id, force=bool(body.get("force")), reason=body.get("reason", ""))


@router.post("/traffic/images/{evidence_id}/detections")
def add_detection(evidence_id: str, body: dict = Body(...)):
    d = _t().add_detection(evidence_id, body.get("box"), str(body.get("cls", "car")), core.actor(), str(body.get("note", "")))
    return _with_training(d, evidence_id)


@router.post("/traffic/images/{evidence_id}/approve")
def approve(evidence_id: str, body: dict = Body(default={})):
    r = _t().approve(evidence_id, core.actor(), bool(body.get("approved", True)))
    return _with_training(r, evidence_id)


@router.get("/traffic/images/{evidence_id}/overlay.png")
def overlay(evidence_id: str, max_side: int = 0):
    return Response(_t().overlay(evidence_id, max_side), media_type="image/png")


@router.patch("/traffic/detections/{detection_id}")
def update_detection(detection_id: int, body: dict = Body(...)):
    d = _t().update_detection(detection_id, core.actor(), box=body.get("box"), cls=body.get("cls"))
    return _with_training(d, d["evidence_id"])


@router.post("/traffic/detections/{detection_id}/false-alarm")
def false_alarm(detection_id: int, body: dict = Body(default={})):
    d = _t().delete_detection(detection_id, core.actor(), str(body.get("note", "")))
    return _with_training(d, d["evidence_id"])


@router.post("/traffic/detections/{detection_id}/restore")
def restore(detection_id: int):
    return _t().restore_detection(detection_id, core.actor())


@router.get("/traffic/runs/{traffic_run_id}")
def run(traffic_run_id: str):
    return _t().get_run(traffic_run_id)


# Notifications, exports, report ------------------------------------------------------------------------


@router.get("/cases/{case_id}/traffic/notifications")
def notifications(case_id: str, unread: bool = False):
    return _t().notifications(case_id, unread)


@router.post("/cases/{case_id}/traffic/notifications/read")
def read_notifications(case_id: str, body: dict = Body(default={})):
    return {"marked": _t().read_notifications(case_id, body.get("ids"))}


@router.get("/cases/{case_id}/traffic/export.{fmt}")
def export(case_id: str, fmt: str):
    data, mime, name = _t().export(case_id, core.actor(), fmt)
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/cases/{case_id}/traffic/dataset.zip")
def dataset(case_id: str):
    data = _t().dataset(case_id, core.actor())
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="traffic-dataset-{case_id}.zip"'})


@router.get("/cases/{case_id}/traffic/report.html")
def report_html(case_id: str, chart: list[str] | None = Query(None)):
    """`chart` (repeatable): charts described in words, e.g. chart=detections by hour and lighting as stacked."""
    from .traffic.report import render_html

    return HTMLResponse(render_html(core.wb(), case_id, core.actor(), chart))


@router.get("/cases/{case_id}/traffic/report.pdf")
def report_pdf(case_id: str, chart: list[str] | None = Query(None)):
    from .traffic.report import render_pdf

    return Response(render_pdf(core.wb(), case_id, core.actor(), chart), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="traffic-report-{case_id}.pdf"'})


@router.get("/cases/{case_id}/traffic/chart")
def chart(case_id: str, q: str):
    """A chart described in words, e.g. 'detections by hour as bar for night'. Returns the parsed
    request and the data; the front end draws it."""
    from .traffic.charts import chart as make

    return make(core.wb(), case_id, q, core.actor())
