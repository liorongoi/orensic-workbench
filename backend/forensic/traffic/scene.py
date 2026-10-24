"""Scene analysis for still traffic images: lighting (day / twilight / night / night-IR), visibility
indicators (haze or fog, snow) and the detection-only pre-processing chosen from them.

Everything here is a measured indicator with a fixed, versioned rule, not a claim about the world:
an examiner can override the lighting class of any image, and the override is recorded.
Pre-processing is applied to a working copy fed to the detectors only; the evidence is never changed
and the copy is never shown as evidence.
"""

from __future__ import annotations

import cv2
import numpy as np

RULES_VERSION = "1.0.0"

LIGHTING = ("day", "twilight", "night", "night_ir")


def measure(bgr: np.ndarray) -> dict:
    """Global brightness, darkness, saturation and haze statistics of an 8-bit BGR image."""
    small = _small(bgr)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    v = hsv[..., 2].astype(np.float32) / 255.0
    s = hsv[..., 1].astype(np.float32) / 255.0
    lum = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    b, g, r = (small[..., i].astype(np.float32) for i in range(3))
    # Near-identical channels: a monochrome (typically infrared-illuminated) picture saved as colour.
    chroma = float(np.mean(np.abs(r - g) + np.abs(g - b))) / 255.0
    dark = _dark_channel(small.astype(np.float32) / 255.0, 15)
    return {
        "mean_luminance": round(float(lum.mean()), 4),
        "median_luminance": round(float(np.median(lum)), 4),
        "dark_fraction": round(float((lum < 0.15).mean()), 4),
        "bright_fraction": round(float((v > 0.92).mean()), 5),
        "mean_saturation": round(float(s.mean()), 4),
        "chroma": round(chroma, 4),
        "rms_contrast": round(float(lum.std()), 4),
        "dark_channel_mean": round(float(dark.mean()), 4),
        "snow_like_fraction": round(float(((v > 0.75) & (s < 0.12))[small.shape[0] // 2:].mean()), 4),
    }


def classify(stats: dict) -> dict:
    """Lighting class and visibility indicators from `measure`. Fixed rules, see RULES below."""
    m, dark, chroma, sat = stats["mean_luminance"], stats["dark_fraction"], stats["chroma"], stats["mean_saturation"]
    mono = chroma < 0.02 and sat < 0.05
    if m < 0.22 and dark > 0.45:
        lighting = "night"
    elif mono and m < 0.4:
        lighting = "night_ir"
    elif m < 0.33 and dark > 0.3:
        lighting = "twilight"
    else:
        lighting = "day"
    indicators = []
    if lighting in ("day", "twilight") and stats["dark_channel_mean"] > 0.45 and stats["rms_contrast"] < 0.16:
        indicators.append("haze_or_fog")
    if lighting == "day" and stats["snow_like_fraction"] > 0.25:
        indicators.append("snow_likely")
    if lighting != "day":
        indicators.append("low_light")
    return {"lighting": lighting, "indicators": indicators, "monochrome": mono, "rules_version": RULES_VERSION}


RULES = [
    "night: mean luminance < 0.22 and more than 45 % of pixels darker than 0.15",
    "night_ir: otherwise, near-monochrome (chroma < 0.02, saturation < 0.05) and mean luminance < 0.4 "
    "(a monochrome camera in dull daylight can match: override the image)",
    "twilight: otherwise, mean luminance < 0.33 and more than 30 % dark pixels",
    "day: everything else",
    "haze_or_fog indicator: day/twilight, dark-channel mean > 0.45 and RMS contrast < 0.16 (He et al. 2009 prior)",
    "snow_likely indicator: day, more than 25 % of the lower half bright (V > 0.75) and unsaturated (S < 0.12)",
]


def analyse(bgr: np.ndarray, override: str | None = None) -> dict:
    stats = measure(bgr)
    c = classify(stats)
    if override:
        if override not in LIGHTING:
            raise ValueError(f"lighting must be one of {', '.join(LIGHTING)}")
        c["automatic_lighting"] = c["lighting"]
        c["lighting"] = override
        c["overridden"] = True
    return {"stats": stats, **c}


def preprocess_plan(scene: dict, mode: str = "auto") -> list[dict]:
    """The detection-only pre-processing steps, with parameters, for the record."""
    if mode == "none":
        return []
    if mode == "clahe":
        return [{"op": "clahe", "clip": 2.5, "tiles": 8}]
    if mode == "dehaze":
        return [{"op": "dehaze_dcp", "patch": 15, "omega": 0.85, "t0": 0.2}]
    if mode != "auto":
        raise ValueError("preprocess must be auto, none, clahe or dehaze")
    steps = []
    if "haze_or_fog" in scene["indicators"]:
        steps.append({"op": "dehaze_dcp", "patch": 15, "omega": 0.85, "t0": 0.2})
    if scene["lighting"] in ("night", "night_ir", "twilight"):
        steps.append({"op": "clahe", "clip": 2.5, "tiles": 8})
    return steps


def apply_plan(bgr: np.ndarray, steps: list[dict]) -> np.ndarray:
    out = bgr
    for st in steps:
        if st["op"] == "clahe":
            lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
            cl = cv2.createCLAHE(clipLimit=float(st["clip"]), tileGridSize=(int(st["tiles"]),) * 2)
            lab[..., 0] = cl.apply(lab[..., 0])
            out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        elif st["op"] == "dehaze_dcp":
            out = dehaze(out, int(st["patch"]), float(st["omega"]), float(st["t0"]))
        else:
            raise ValueError(f"unknown pre-processing step {st['op']}")
    return out


def dehaze(bgr: np.ndarray, patch: int = 15, omega: float = 0.85, t0: float = 0.2) -> np.ndarray:
    """Dark channel prior dehazing (He, Sun and Tang, CVPR 2009); transmission smoothed with a box filter."""
    img = bgr.astype(np.float32) / 255.0
    dark = _dark_channel(img, patch)
    n = max(1, int(dark.size * 0.001))
    idx = np.argpartition(dark.ravel(), -n)[-n:]
    A = np.maximum(img.reshape(-1, 3)[idx].max(0), 1e-3)
    t = 1.0 - omega * _dark_channel(img / A, patch)
    t = cv2.blur(t, (patch * 2 + 1,) * 2)
    t = np.maximum(t, t0)[..., None]
    J = (img - A) / t + A
    return np.clip(J * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _dark_channel(img: np.ndarray, patch: int) -> np.ndarray:
    return cv2.erode(img.min(axis=2), cv2.getStructuringElement(cv2.MORPH_RECT, (patch, patch)))


def _small(bgr: np.ndarray, side: int = 640) -> np.ndarray:
    h, w = bgr.shape[:2]
    s = side / max(h, w)
    return cv2.resize(bgr, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else bgr
