"""File-structure and metadata analysis for images and video."""

from __future__ import annotations

import io
import re
from collections import Counter

import numpy as np
from PIL import ExifTags, Image

from .. import media
from .base import AnalysisContext, AnalysisResult, register
from .jpeg import estimate_quality, is_jpeg, parse_markers

GPS_TAGS = ExifTags.GPSTAGS
EXIF_TAGS = ExifTags.TAGS


def _clean(value):
    """Make EXIF values JSON-safe and readable; binary blobs become a length note."""
    if isinstance(value, bytes):
        if len(value) <= 64 and all(32 <= b < 127 for b in value.rstrip(b"\x00")):
            return value.rstrip(b"\x00").decode("ascii")
        return f"<{len(value)} bytes>"
    if isinstance(value, tuple):
        return [_clean(v) for v in value]
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        try:
            return round(float(value), 6)
        except (ZeroDivisionError, ValueError):
            return None
    if isinstance(value, str):
        return value.rstrip("\x00").strip()
    return value


def _gps_decimal(gps: dict) -> dict | None:
    try:
        def dms(v):
            d, m, s = (float(x) for x in v)
            return d + m / 60 + s / 3600

        lat = dms(gps["GPSLatitude"]) * (-1 if gps.get("GPSLatitudeRef") == "S" else 1)
        lon = dms(gps["GPSLongitude"]) * (-1 if gps.get("GPSLongitudeRef") == "W" else 1)
        return {"lat": round(lat, 7), "lon": round(lon, 7)}
    except (KeyError, TypeError, ValueError):
        return None


def read_exif(data: bytes) -> dict:
    out: dict[str, dict] = {"image": {}, "exif": {}, "gps": {}, "interop": {}, "thumbnail_ifd": {}}
    try:
        with Image.open(io.BytesIO(data)) as im:
            exif = im.getexif()
            for k, v in exif.items():
                out["image"][EXIF_TAGS.get(k, f"0x{k:04X}")] = _clean(v)
            for ifd_id, key, names in (
                (ExifTags.IFD.Exif, "exif", EXIF_TAGS),
                (ExifTags.IFD.GPSInfo, "gps", GPS_TAGS),
                (ExifTags.IFD.Interop, "interop", EXIF_TAGS),
                (ExifTags.IFD.IFD1, "thumbnail_ifd", EXIF_TAGS),
            ):
                try:
                    for k, v in exif.get_ifd(ifd_id).items():
                        name = names.get(k, f"0x{k:04X}")
                        out[key][name] = _clean(v) if name != "MakerNote" else f"<{len(v)} bytes>"
                except (KeyError, ValueError, TypeError):
                    pass
    except Exception as e:  # noqa: BLE001 - hostile files can fail in many ways
        out["error"] = f"could not read EXIF: {e}"
    return out


def _thumbnail(data: bytes, markers: list[dict]) -> bytes | None:
    for m in markers:
        if m["name"] == "APP1" and m.get("identifier", "").startswith("Exif"):
            seg = data[m["offset"] + 4 : m["offset"] + 2 + m["length"]]
            soi = seg.find(b"\xff\xd8\xff", 8)
            if soi < 0:
                return None
            eoi = seg.find(b"\xff\xd9", soi)
            return seg[soi : eoi + 2] if eoi > 0 else None
    return None


@register(
    "file.metadata",
    name="File structure and metadata",
    version="1.0.0",
    category="File analysis",
    description="Reads EXIF, GPS and XMP metadata, walks the JPEG marker structure, extracts the "
    "quantisation tables and embedded thumbnail, and checks them for internal consistency. For video "
    "it reports container and stream data and the per-frame type (I/P/B) sequence.",
    reference="JEITA CP-3451 (Exif 2.3); ITU-T T.81 (JPEG); SWGDE Best Practices for Image Authentication, 2018; "
    "H. Farid, Photo Forensics, MIT Press, 2016, ch. 2-3.",
    applies_to=("image", "video"),
    reading_guide="Metadata is easy to edit, so it can support but never prove authenticity. Warnings are "
    "inconsistencies worth explaining, not proof of manipulation.",
)
def file_metadata(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if ctx.probe["media_type"] == "video":
        return _video_metadata(ctx)
    data = ctx.data
    flags: list[dict] = []
    findings: dict = {"format": ctx.probe.get("format"), "width": ctx.probe["width"], "height": ctx.probe["height"]}
    exif = read_exif(data)
    findings["exif"] = exif
    tags = {**exif["image"], **exif["exif"]}
    if not exif["image"] and not exif["exif"]:
        flags.append({"level": "warning", "text": "No EXIF metadata. Common after editing software, screenshots or social-media upload."})
    if sw := tags.get("Software"):
        flags.append({"level": "info", "text": f"Software tag: '{sw}'."})
        if re.search(r"photoshop|gimp|lightroom|snapseed|picsart|affinity|paint", str(sw), re.I):
            flags.append({"level": "warning", "text": f"Software tag names an image editor ('{sw}')."})
    if tags.get("Make") or tags.get("Model"):
        findings["camera"] = f"{tags.get('Make', '')} {tags.get('Model', '')}".strip()
    dt, dto = tags.get("DateTime"), tags.get("DateTimeOriginal")
    if dt and dto and dt != dto:
        flags.append({"level": "warning", "text": f"Modification time ({dt}) differs from capture time ({dto})."})
    px, py = tags.get("ExifImageWidth"), tags.get("ExifImageHeight")
    if isinstance(px, int) and isinstance(py, int) and (px, py) != (findings["width"], findings["height"]):
        flags.append({"level": "warning", "text": f"EXIF dimensions {px}x{py} differ from the image ({findings['width']}x{findings['height']})."})
    if exif["gps"]:
        findings["gps"] = _gps_decimal(exif["gps"])
        if findings["gps"]:
            flags.append({"level": "info", "text": f"GPS position {findings['gps']['lat']}, {findings['gps']['lon']}."})
    xmp_start = data.find(b"<x:xmpmeta")
    if xmp_start >= 0:
        xmp_end = data.find(b"</x:xmpmeta>", xmp_start)
        xmp = data[xmp_start : xmp_end + 12 if xmp_end > 0 else xmp_start + 20000].decode("utf-8", "replace")
        findings["xmp"] = xmp[:20000]
        if m := re.search(r'(?:xmp:CreatorTool|CreatorTool)\s*=\s*"([^"]+)"|<xmp:CreatorTool>([^<]+)<', xmp):
            tool = m.group(1) or m.group(2)
            flags.append({"level": "info", "text": f"XMP creator tool: '{tool}'."})
        if "photoshop:" in xmp or "History" in xmp:
            flags.append({"level": "warning", "text": "XMP contains editing history or Photoshop fields."})
    if b"c2pa" in data[:200000] or b"jumb" in data[:200000]:
        flags.append({"level": "info", "text": "File appears to carry a C2PA / JUMBF manifest (Content Credentials). Validation comes in a later release."})

    if is_jpeg(data):
        jp = parse_markers(data)
        findings["jpeg"] = jp
        q = estimate_quality(jp["dqt"])
        findings["quantisation"] = q
        if q["standard_ijg_match"]:
            flags.append({"level": "info", "text": f"Quantisation tables match the standard libjpeg tables at quality {q['standard_ijg_match']}; common in software encoders and many apps."})
        elif q["estimated_quality"]:
            flags.append({"level": "info", "text": f"Non-standard quantisation tables (closest libjpeg quality {q['estimated_quality']}); typical of camera firmware or specific software."})
        for w in jp["warnings"]:
            flags.append({"level": "warning", "text": f"JPEG structure: {w}."})
        names = [m["name"] for m in jp["markers"]]
        findings["marker_sequence"] = " ".join(names)
        thumb = _thumbnail(data, jp["markers"])
        if thumb:
            try:
                with Image.open(io.BytesIO(thumb)) as t:
                    tw, th = t.size
                findings["thumbnail"] = {"width": tw, "height": th, "bytes": len(thumb)}
                main_ar = findings["width"] / findings["height"]
                if abs(tw / th - main_ar) / main_ar > 0.02 and abs(th / tw - main_ar) / main_ar > 0.02:
                    flags.append({"level": "warning", "text": f"Embedded thumbnail aspect ratio ({tw}x{th}) does not match the image; the image may have been cropped after capture."})
                thumb_img = media.to_float(np.asarray(Image.open(io.BytesIO(thumb)).convert("RGB")))
                maps = {"thumbnail": thumb_img}
            except Exception:  # noqa: BLE001
                flags.append({"level": "warning", "text": "Embedded thumbnail could not be decoded."})
                maps = {}
        else:
            maps = {}
            if exif["image"]:
                flags.append({"level": "info", "text": "No embedded EXIF thumbnail."})
    else:
        maps = {}

    n_warn = sum(1 for f in flags if f["level"] == "warning")
    summary = f"{n_warn} warning(s) in file structure and metadata." if n_warn else "No inconsistencies found in file structure and metadata."
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps=maps)


def _video_metadata(ctx: AnalysisContext) -> AnalysisResult:
    pr = ctx.probe
    flags: list[dict] = []
    frames = media.frame_table(ctx.path, ctx.settings)
    types = Counter(f["type"] for f in frames)
    key_idx = [f["n"] for f in frames if f["key"]]
    gops = [b - a for a, b in zip(key_idx, key_idx[1:])]
    gaps = []
    times = [f["time"] for f in frames if f["time"] is not None]
    if len(times) > 2:
        deltas = [b - a for a, b in zip(times, times[1:])]
        typical = sorted(deltas)[len(deltas) // 2]
        gaps = [{"after_frame": i, "gap_s": round(d, 4)} for i, d in enumerate(deltas) if typical > 0 and d > 1.9 * typical]
        if gaps:
            flags.append({"level": "warning", "text": f"{len(gaps)} timestamp gap(s) larger than twice the typical frame interval; possible dropped or removed frames, or a variable-rate recorder."})
        if any(d <= 0 for d in deltas):
            flags.append({"level": "warning", "text": "Timestamps are not strictly increasing."})
    if len(set(gops)) > 1:
        flags.append({"level": "info", "text": f"Irregular GOP lengths ({min(gops)} to {max(gops)} frames)."})
    tags = pr.get("container", {}).get("tags", {})
    if enc := tags.get("encoder"):
        flags.append({"level": "info", "text": f"Encoder tag: '{enc}'."})
    if pr.get("field_order") not in (None, "progressive", "unknown"):
        flags.append({"level": "info", "text": f"Interlaced video ({pr['field_order']}); consider deinterlacing before measurement."})
    findings = {
        "container": pr.get("container"),
        "streams": pr.get("streams"),
        "frame_count": len(frames),
        "frame_types": dict(types),
        "gop_lengths": sorted(set(gops)),
        "timestamp_gaps": gaps[:200],
        "frames": frames[:5000],
    }
    n_warn = sum(1 for f in flags if f["level"] == "warning")
    summary = f"{len(frames)} frames ({', '.join(f'{k}: {v}' for k, v in sorted(types.items()))}); {n_warn} warning(s)."
    return AnalysisResult(summary=summary, findings=findings, flags=flags)
