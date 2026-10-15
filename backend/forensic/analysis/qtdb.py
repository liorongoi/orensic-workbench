"""Match the file's JPEG quantisation tables against the camera and software database."""

from __future__ import annotations

from .. import qtables
from .base import AnalysisContext, AnalysisResult, register
from .jpeg import is_jpeg, parse_markers
from .metadata import read_exif


@register(
    "file.qt_database",
    name="Quantisation tables vs camera database",
    version="1.0.0",
    category="File analysis",
    description="Looks up the file's JPEG quantisation tables in an offline database of tables recorded from "
    "camera originals and from software encoders, and lists every camera model and program known to write exactly "
    "these tables. When EXIF names a camera the database knows, tables that match none of that camera's tables "
    "are flagged: the file was probably re-saved by something else.",
    reference="H. Farid, 'Digital Image Ballistics from JPEG Quantization', Dartmouth TR2006-583, 2006; "
    "E. Kee, M. K. Johnson and H. Farid, 'Digital Image Authentication from JPEG Headers', IEEE TIFS 6(3), 2011.",
    applies_to=("image",),
    reading_guide="A match is consistent with, not proof of, the listed devices: many cameras share tables, and "
    "anyone can re-save with a camera's tables. A mismatch with the EXIF camera is only meaningful when the "
    "database holds that camera's tables for the quality setting used; the number held is shown. Labs grow the "
    "database from their own reference images (python -m forensic.qtables add).",
)
def qt_database(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if not is_jpeg(ctx.data):
        raise ValueError("the quantisation-table lookup needs a JPEG file")
    tables = qtables.file_tables(parse_markers(ctx.data))
    if tables is None:
        raise ValueError("the file has no quantisation table")
    db = qtables.load(ctx.settings.data_dir if ctx.settings else None)
    m = qtables.match(db, *tables)
    ex = read_exif(ctx.data)
    make, model = str(ex["image"].get("Make") or ""), str(ex["image"].get("Model") or "")
    claimed = qtables.camera_name(make, model) if (make or model) else None
    known = qtables.known_tables_for(db, make, model) if claimed else set()
    key = qtables.table_key(*tables)
    flags = []
    if m["cameras"]:
        flags.append({"level": "info", "text": f"Tables match {len(m['cameras'])} camera model(s): {', '.join(m['cameras'][:8])}"
                      + ("…" if len(m["cameras"]) > 8 else "") + "."})
    if m["software"]:
        flags.append({"level": "info", "text": f"Tables match software: {'; '.join(m['software'][:4])}."})
    claimed_match = bool(known) and key in known
    if claimed and known and not claimed_match:
        flags.append({"code": "qt.model_mismatch", "level": "warning",
                      "text": f"EXIF names {claimed}, but the tables match none of the {len(known)} table set(s) recorded for it; "
                      "consistent with re-saving, unless the camera used a setting the database lacks."})
    s = qtables.stats(db)
    findings = {
        "exif_camera": claimed,
        "matches_exif_camera": claimed_match if known else None,
        "tables_recorded_for_exif_camera": len(known),
        "matching_cameras": m["cameras"],
        "matching_software": m["software"],
        "nearest_cameras": m["nearest_cameras"],
        "matched_entries": m["evidence"],
        "database": s | {"built": db.get("built"), "sources": db["sources"]},
    }
    if claimed_match:
        summary = f"Tables match {claimed}, as EXIF claims."
    elif claimed and known:
        summary = f"Tables do not match any recorded for {claimed}."
    elif m["cameras"] or m["software"]:
        summary = f"Tables match {len(m['cameras'])} camera model(s) and {len(m['software'])} program(s)."
    else:
        summary = f"No match among {s['tables']} table sets ({s['camera_models']} camera models)."
    return AnalysisResult(summary=summary, findings=findings, flags=flags)
