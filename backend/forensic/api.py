"""HTTP API. Run with: uvicorn forensic.api:app"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import cv2
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import __version__, formats, media
from . import measure as measure_mod
from .agents import WORKFLOWS, plan_enhancement, triage_case
from .audit import Actor
from .filters import FilterError
from .report import build_bundle, render_html, render_pdf
from .workbench import NotFound, Workbench

app = FastAPI(title="Forensic Workbench", version=__version__)

# Answer only requests addressed to this computer by name. Without this check a web page could
# rebind its own hostname to 127.0.0.1 and use the API as if it were the examiner's browser.
# Add the names a lab uses to reach the workbench (for example behind a TLS proxy) to
# FORENSIC_ALLOWED_HOSTS, comma-separated.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"] + [
    h.strip() for h in os.environ.get("FORENSIC_ALLOWED_HOSTS", "").split(",") if h.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
_wb: Workbench | None = None


def wb() -> Workbench:
    global _wb
    if _wb is None:
        _wb = Workbench()
    return _wb


def actor(name: str | None) -> Actor:
    name = (name or "").strip()
    if not name:
        raise HTTPException(401, "set your examiner name (X-Examiner header) before working on evidence")
    return Actor(name[:120], "human")


@app.exception_handler(NotFound)
async def _nf(_: Request, exc: NotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(PermissionError)
async def _forbidden(_: Request, exc: PermissionError):
    return JSONResponse({"detail": str(exc)}, status_code=403)


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


# Cases ----------------------------------------------------------------------------------


@app.get("/api/cases")
def list_cases():
    return wb().list_cases()


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
        "analyses": w.list_analysis_results(case_id),
        "camera_references": w.list_camera_references(case_id),
        "measurements": w.list_measurements(case_id),
        "triage": w.list_triage(case_id),
        "review": w.latest_review(case_id),
    }


@app.post("/api/cases/{case_id}/triage")
def triage(case_id: str, body: TriageIn, x_examiner: str | None = Header(None)):
    return triage_case(wb(), case_id, body.evidence_ids, actor(x_examiner))


class ReviewIn(BaseModel):
    rerun_analyses: bool = True


@app.post("/api/cases/{case_id}/review")
def review(case_id: str, body: ReviewIn, x_examiner: str | None = Header(None)):
    actor(x_examiner)  # the examiner asks; the reviewer agent re-checks the case
    return wb().review_case(case_id, rerun_analyses=body.rerun_analyses)


@app.get("/api/cases/{case_id}/review")
def latest_review(case_id: str):
    return wb().latest_review(case_id)


@app.post("/api/cases/{case_id}/plan")
def plan(case_id: str, body: PlanIn, x_examiner: str | None = Header(None)):
    actor(x_examiner)  # the examiner asks; the planner agent proposes
    return plan_enhancement(wb(), case_id, body.evidence_id, body.goal, roi=body.roi, frames=body.frames, workflow=body.workflow)


@app.post("/api/cases/{case_id}/camera-references")
def create_reference(case_id: str, body: ReferenceIn, x_examiner: str | None = Header(None)):
    return wb().create_camera_reference(case_id, body.name, body.evidence_ids, actor(x_examiner), camera=body.camera)


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
def frame_png(evidence_id: str, n: int):
    img = wb().source_frame(evidence_id, n)
    return Response(_fast_png(img), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/evidence/{evidence_id}/original")
def original(evidence_id: str):
    w = wb()
    ev = w.get_evidence(evidence_id)
    return FileResponse(w.evidence_path(evidence_id), filename=ev["filename"])


@app.post("/api/evidence/{evidence_id}/preview")
def preview(evidence_id: str, body: PreviewIn):
    img, info = wb().preview(body.graph, evidence_id, upto=body.upto, frame=body.frame)
    return Response(_fast_png(img), media_type="image/png", headers={"X-Preview-Info": json.dumps(info)})


@app.post("/api/evidence/{evidence_id}/convert")
def convert(evidence_id: str, body: ConvertIn, x_examiner: str | None = Header(None)):
    return wb().convert_evidence(evidence_id, body.method, actor(x_examiner), frame_rate=body.frame_rate, codec=body.codec)


@app.post("/api/evidence/{evidence_id}/measurements")
def measure(evidence_id: str, body: MeasureIn, x_examiner: str | None = Header(None)):
    return wb().measure(evidence_id, body.kind, body.inputs, actor(x_examiner), label=body.label,
                        chain_id=body.chain_id, frame=body.frame)


@app.post("/api/evidence/{evidence_id}/smart-report")
def smart_report(evidence_id: str, x_examiner: str | None = Header(None)):
    return wb().smart_report(evidence_id, actor(x_examiner))


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
    return wb().run_analysis(evidence_id, body.analysis, body.params, actor(x_examiner))


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


class AcceptIn(BaseModel):
    node_ids: list[str] | None = None


@app.post("/api/chains/{chain_id}/accept")
def accept(chain_id: str, body: AcceptIn, x_examiner: str | None = Header(None)):
    return wb().accept_proposals(chain_id, actor(x_examiner), body.node_ids)


@app.post("/api/chains/{chain_id}/render")
def render_chain(chain_id: str, x_examiner: str | None = Header(None)):
    return wb().render_chain(chain_id, actor(x_examiner))


@app.post("/api/chains/{chain_id}/verify")
def verify_chain(chain_id: str, x_examiner: str | None = Header(None)):
    return wb().verify_chain(chain_id, actor(x_examiner))


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
    bundle = build_bundle(wb(), case_id, actor(x_examiner or examiner), verify=verify)
    return JSONResponse(bundle, headers={"Content-Disposition": f'attachment; filename="report-{case_id}.json"'})


@app.get("/api/cases/{case_id}/report.html")
def report_html(case_id: str, x_examiner: str | None = Header(None), examiner: str | None = None):
    w = wb()
    return HTMLResponse(render_html(w, build_bundle(w, case_id, actor(x_examiner or examiner))))


@app.get("/api/cases/{case_id}/report.pdf")
def report_pdf(case_id: str, x_examiner: str | None = Header(None), examiner: str | None = None):
    w = wb()
    pdf = render_pdf(w, build_bundle(w, case_id, actor(x_examiner or examiner)))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="report-{case_id}.pdf"'})


def _fast_png(img) -> bytes:
    u8 = cv2.cvtColor(media.to_uint8(img), cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".png", u8, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    return buf.tobytes()


# Front end (built with `npm run build` in ../frontend) --------------------------------------

_dist = Path(os.environ.get("FORENSIC_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(_dist / "index.html")
