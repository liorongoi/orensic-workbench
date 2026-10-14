"""Smart Report: a fixed battery of analyses and transparent rules that sort evidence
into green / yellow / red for triage.

The rules are deliberately simple and printed in full in the report, so an
examiner can see exactly why an item was sorted where it was. A colour is a
prompt for where to look first, never a finding: green means no check in the
battery found an inconsistency, not that the item is authentic.
"""

from __future__ import annotations

RULES_VERSION = "1.0.0"

# Analyses run per media type, with their parameters (fixed, so triage is reproducible).
BATTERY: dict[str, list[tuple[str, dict]]] = {
    "image": [("file.metadata", {}), ("tamper.double_jpeg", {}), ("tamper.nonaligned_grid", {})],
    "video": [("file.metadata", {})],
    "container": [],
}

# Flag code -> (level, short reason). Red: a local or structural inconsistency worth examining first.
# Yellow: a processing history (re-saving, editing software, missing metadata) that limits what can be said.
RULES: dict[str, tuple[str, str]] = {
    "djpg.local": ("red", "a region is inconsistent with the image's double-compression history"),
    "nadjpg.local": ("red", "a region carries a JPEG grid offset from the rest"),
    "meta.thumbnail": ("red", "the embedded thumbnail does not match the image"),
    "meta.dimensions": ("red", "EXIF dimensions differ from the image"),
    "meta.structure": ("red", "the JPEG structure is irregular"),
    "video.timestamps": ("red", "frame timestamps go backwards"),
    "meta.editor": ("yellow", "the Software tag names an image editor"),
    "meta.xmp_history": ("yellow", "XMP carries editing history"),
    "meta.date_mismatch": ("yellow", "modification time differs from capture time"),
    "meta.no_exif": ("yellow", "there is no EXIF metadata"),
    "meta.thumbnail_bad": ("yellow", "the embedded thumbnail cannot be decoded"),
    "djpg.double": ("yellow", "the JPEG was decoded and saved again"),
    "nadjpg.global": ("yellow", "the image was cropped or re-gridded between JPEG saves"),
    "video.gaps": ("yellow", "there are gaps in the frame timestamps"),
}

RANK = {"green": 0, "yellow": 1, "red": 2}


def rules_table() -> list[dict]:
    return [{"code": c, "level": lvl, "reason": r} for c, (lvl, r) in RULES.items()]


def assess(media_type: str, runs: list[dict]) -> dict:
    reasons = []
    for run in runs:
        for f in run["result"].get("flags", []):
            code = f.get("code")
            if code in RULES:
                lvl, short = RULES[code]
                reasons.append({"code": code, "level": lvl, "reason": short, "detail": f["text"], "analysis": run["name"],
                                "analysis_id": run["id"]})
    if media_type == "container":
        reasons.append({"code": "container", "level": "yellow", "reason": "the recorder export must be converted before analysis",
                        "detail": "Convert it (stream copy or carving) and triage the working copy.", "analysis": None, "analysis_id": None})
    level = max((r["level"] for r in reasons), key=RANK.get, default="green")
    reasons.sort(key=lambda r: -RANK[r["level"]])
    if level == "green":
        line = "No inconsistency found by the triage checks; this is not a finding of authenticity."
    else:
        top = [r["reason"] for r in reasons if r["level"] == level][:2]
        line = ("Examine first: " if level == "red" else "Processing history: ") + "; ".join(top) + "."
    return {
        "level": level,
        "line": line,
        "reasons": reasons,
        "analyses": [{"id": r["id"], "name": r["name"], "version": r["version"], "output_hash": r["output_hash"]} for r in runs],
        "rules_version": RULES_VERSION,
    }
