"""Camera identification: match an image against a PRNU camera reference."""

from __future__ import annotations

import cv2
import numpy as np

from .. import prnu
from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register
from .compression import _colormap


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


def _paint(grid: np.ndarray, step: int, window: int, shape: tuple[int, int]) -> np.ndarray:
    """Each window's value painted on its central step x step cell, so the map lines up with the image."""
    h, w = shape
    out = np.zeros((h, w), np.float32)
    cell = cv2.resize(grid.astype(np.float32), (grid.shape[1] * step, grid.shape[0] * step), interpolation=cv2.INTER_NEAREST)
    o = (window - step) // 2
    hh, ww = min(cell.shape[0], h - o), min(cell.shape[1], w - o)
    out[o : o + hh, o : o + ww] = cell[:hh, :ww]
    return out


@register(
    "tamper.prnu_map",
    name="PRNU tampering map",
    version="1.0.0",
    category="Local analysis",
    description="For an image from a camera whose reference fingerprint is in this case, measures window by window "
    "whether the camera's sensor fingerprint is present. A region pasted from another photograph, from elsewhere in "
    "the same photograph, or painted over lacks the fingerprint at that position and is outlined.",
    reference="M. Chen, J. Fridrich, M. Goljan and J. Lukas, 'Determining Image Origin and Integrity Using Sensor "
    "Noise', IEEE TIFS 3(1), 2008; G. Chierchia, G. Poggi, C. Sansone and L. Verdoliva, 'A Bayesian-MRF Approach for "
    "PRNU-Based Image Forgery Detection', IEEE TIFS 9(4), 2014.",
    applies_to=("image",),
    reading_guide="Only meaningful when the whole image matches the reference (PCE above 60, shown). Bright = fingerprint "
    "weaker than predicted for that content. Dark, saturated, very textured or flat-blue-sky areas carry too little "
    "fingerprint to decide and are hatched. Regions smaller than about a window are missed.",
    params=[
        Param("reference_id", "text", "", "Camera reference", help="Build one from known images in this case first"),
        Param("window", "int", 128, "Window (px)", 64, 256, 16),
    ],
)
def prnu_tamper_map(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if not p["reference_id"]:
        raise ValueError("choose a camera reference")
    if ctx.resolve is None:
        raise ValueError("camera references are not available here")
    ref, K = ctx.resolve("prnu_reference", p["reference_id"])
    return _prnu_tamper_result(ctx.image, K, p["window"], ref)


def _prnu_tamper_result(img: np.ndarray, K: np.ndarray, window: int, ref: dict | None = None) -> AnalysisResult:
    r = prnu.tamper_map(img, K, window=window, step=max(8, window // 8))
    pce = r["match"]["pce"]
    matched = pce >= prnu.PCE_THRESHOLD
    # Isolated single windows are dropped: a region must make at least two neighbouring windows fail.
    nb = cv2.filter2D(r["tampered"].astype(np.float32), -1, np.ones((3, 3), np.float32), borderType=cv2.BORDER_CONSTANT)
    tam = r["tampered"] & (nb >= 2)
    shape = r["shape"]
    heat = _paint(r["missing"], r["step"], window, shape)
    unmeas = _paint((~r["measurable"]).astype(np.float32), r["step"], window, shape) > 0.5
    # A flagged window lacks the fingerprint almost throughout (rho near zero), so the area is the union of whole windows.
    mask = np.zeros(shape, np.uint8)
    for i, j in zip(*np.nonzero(tam)):
        mask[i * r["step"] : i * r["step"] + window, j * r["step"] : j * r["step"] + window] = 1
    mask = mask > 0
    rot = r["rotation"]
    heat, unmeas, mask = (np.rot90(a, -rot) for a in (heat, unmeas, mask))  # back to the image's orientation
    h, w = heat.shape
    rgb = _colormap(heat if matched else heat * 0)
    yy, xx = np.mgrid[0:h, 0:w]
    rgb[unmeas & (((xx + yy) // 4) % 3 == 0)] = 0.5
    canvas = np.clip(np.rint(rgb * 255), 0, 255).astype(np.uint8)
    regions = []
    if matched:
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            x, y, bw, bh, area = (int(v) for v in stats[i])
            regions.append({"bbox_px": [x, y, bw, bh], "area_share": round(area / (h * w), 4)})
            cnts, _ = cv2.findContours((lab == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(canvas, cnts, -1, (255, 255, 255), 2)
    flags = []
    if not matched:
        flags.append({"level": "info", "text": f"The image does not match the reference as a whole (PCE {pce:.0f}, below "
                      f"{prnu.PCE_THRESHOLD:.0f}), so the map cannot show local tampering."})
    elif regions:
        flags.append({"level": "warning", "code": "prnu.local",
                      "text": f"{len(regions)} area(s) lack the camera's sensor fingerprint that the rest of the image carries."})
    findings = {
        "pce": pce, "threshold": prnu.PCE_THRESHOLD, "whole_image_matches": matched,
        "rotation_deg": 90 * rot, "crop_offset_xy": r["offset_xy"], "window_px": window,
        "sigma_no_fingerprint": round(r["sigma0"], 5), "prediction_error_sigma": round(r["sigma1"], 5),
        "measurable_share": round(float(r["measurable"].mean()), 3), "regions": regions,
    }
    if ref:
        findings["reference"] = {"id": ref["id"], "name": ref["name"], "fingerprint_sha256": ref["fingerprint_sha256"]}
    summary = (f"PCE {pce:.0f}; " + (f"{len(regions)} area(s) without the fingerprint." if regions else "fingerprint present wherever measurable.")
               if matched else f"Whole image does not match the reference (PCE {pce:.0f}); no map.")
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps={"prnu_missing": canvas.astype(np.float32) / 255.0})
