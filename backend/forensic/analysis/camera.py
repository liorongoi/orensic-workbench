"""Camera identification: match an image against a PRNU camera reference."""

from __future__ import annotations

import numpy as np

from .. import prnu
from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register


@register(
    "camera.prnu",
    name="Camera identification (PRNU)",
    version="1.0.0",
    category="Camera identification",
    description="Correlates the image's sensor-noise residual with a camera reference fingerprint built in this "
    "case from images known to come from that camera. Searches 90-degree rotations and, optionally, cropping "
    "and down-scaling. The score is the peak-to-correlation energy (PCE).",
    reference="J. Lukas, J. Fridrich and M. Goljan, 'Digital Camera Identification from Sensor Pattern Noise', "
    "IEEE TIFS 1(2), 2006; M. Goljan, J. Fridrich and T. Filler, 'Large Scale Test of Sensor Fingerprint Camera "
    "Identification', Proc. SPIE 7254, 2009; M. Goljan and J. Fridrich, 'Camera Identification from Cropped and "
    "Scaled Images', Proc. SPIE 6819, 2008.",
    applies_to=("image",),
    reading_guide="PCE above 60 is consistent with the image having been taken by the reference camera (the "
    "threshold from large-scale tests, with a false-alarm rate around 1 in 100,000 for full-size images). PCE "
    "well below 60 is not consistent with it, or the fingerprint is not recoverable (strong compression, "
    "resizing, heavy processing). Modern phones' computational photography can weaken or fake a match: "
    "validate on images from the same phone model before relying on it. Searching crops and scales tries "
    "more hypotheses and makes chance matches likelier; the number tried is reported.",
    params=[
        Param("reference_id", "text", "", "Camera reference", help="Build one from known images in this case first"),
        Param("crop_search", "bool", True, "Search cropping"),
        Param("scale_search", "bool", False, "Search down-scaling (50 to 100 %)"),
    ],
)
def prnu_match(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if not p["reference_id"]:
        raise ValueError("choose a camera reference")
    if ctx.resolve is None:
        raise ValueError("camera references are not available here")
    ref, K = ctx.resolve("prnu_reference", p["reference_id"])
    scales = tuple(np.round(np.arange(1.0, 0.49, -0.05), 2)) if p["scale_search"] else (1.0,)
    res = prnu.match(ctx.image, K, rotations=True, crop_search=p["crop_search"], scales=scales)
    best = res["best"]
    consistent = best["pce"] >= prnu.PCE_THRESHOLD
    flags = []
    if consistent:
        flags.append({"level": "info", "code": "prnu.match",
                      "text": f"Consistent with the camera of reference '{ref['name']}' (PCE {best['pce']:.0f})."})
    findings = {
        "reference": {"id": ref["id"], "name": ref["name"], "camera": ref["camera"], "images": ref["stats"]["images"],
                      "fingerprint_sha256": ref["fingerprint_sha256"]},
        "pce": best["pce"],
        "threshold": prnu.PCE_THRESHOLD,
        "consistent": consistent,
        "rotation_deg": best["rotation_deg"],
        "scale": best["scale"],
        "crop_offset_xy": best["offset_xy"],
        "normalised_correlation": best["ncc"],
        "hypotheses_tried": best["hypotheses"],
    }
    summary = (f"PCE {best['pce']:.1f}: " + ("consistent with" if consistent else "not consistent with")
               + f" camera reference '{ref['name']}'.")
    return AnalysisResult(summary=summary, findings=findings, flags=flags)
