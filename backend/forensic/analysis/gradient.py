"""Luminance gradient analysis (LGA): a map of which way brightness changes, everywhere in the image.

Light falling on a scene shades every surface in a direction set by where the light comes
from, so smooth shading in an authentic photograph changes direction gradually and
consistently. A pasted object lit from elsewhere, or a retouched area whose shading was
painted, often stands out as a patch whose colours (directions) do not follow its
surroundings, and pasted edges can show a seam of different sharpness.

The map shows the gradient of a lightly smoothed luma: hue is the direction (red = brighter
to the right, then round the colour wheel anticlockwise) and brightness is the strength,
scaled to the image's own 99th percentile. Nothing is decided automatically: it is a visual
aid for the examiner, like ELA.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..filters.base import Param, luminance
from .base import AnalysisContext, AnalysisResult, register


def gradient_map(img: np.ndarray, sigma: float, gain: float) -> tuple[np.ndarray, dict]:
    y = luminance(img).astype(np.float32) * 255.0
    if sigma > 0:
        y = cv2.GaussianBlur(y, (0, 0), sigma, borderType=cv2.BORDER_REFLECT)
    gx = cv2.Sobel(y, cv2.CV_32F, 1, 0, ksize=3, borderType=cv2.BORDER_REFLECT) / 8.0
    gy = -cv2.Sobel(y, cv2.CV_32F, 0, 1, ksize=3, borderType=cv2.BORDER_REFLECT) / 8.0  # up is positive
    mag = np.hypot(gx, gy)
    ref = float(np.percentile(mag, 99)) or 1.0
    val = np.clip(mag / ref * gain, 0, 1)
    hue = (np.degrees(np.arctan2(gy, gx)) % 360) / 2  # OpenCV hue is 0..180
    hsv = np.stack([hue, np.full_like(hue, 255), val * 255], axis=2).astype(np.uint8)
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB).astype(np.float32) / 255.0
    stats = {"gradient_p99_grey_levels_per_px": round(ref, 3), "median_gradient": round(float(np.median(mag)), 3)}
    return rgb, stats


def _legend(size: int = 96) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) - (size - 1) / 2
    r = np.hypot(xx, yy) / ((size - 1) / 2)
    hue = (np.degrees(np.arctan2(-yy, xx)) % 360) / 2
    hsv = np.stack([hue, np.full_like(hue, 255), np.clip(r, 0, 1) * 255], axis=2).astype(np.uint8)
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB).astype(np.float32) / 255.0
    rgb[r > 1] = 1.0
    return rgb


@register(
    "tamper.luminance_gradient",
    name="Luminance gradient (LGA)",
    version="1.0.0",
    category="Local analysis",
    description="Maps the direction (colour) and strength (brightness) of the change in brightness at every pixel. "
    "Shading in a real scene follows the light, so its direction changes gradually; a pasted or repainted object "
    "often shows shading or edges that do not fit its surroundings.",
    reference="N. Krawetz, 'A Picture's Worth: Digital Image Analysis and Forensics', Black Hat USA, 2007; "
    "M. K. Johnson and H. Farid, 'Exposing Digital Forgeries by Detecting Inconsistencies in Lighting', "
    "ACM Multimedia and Security Workshop, 2005.",
    applies_to=("image",),
    reading_guide="Colour = direction (see the legend wheel), brightness = strength. Compare surfaces that should be lit "
    "alike: two faces, or an object and its shadow side. Texture, noise and JPEG blocks add speckle; raise the "
    "smoothing to see only shading. A visual aid with no automatic decision: support any reading with other analyses.",
    params=[
        Param("smoothing", "float", 1.5, "Smoothing (px)", 0.0, 8.0, 0.5),
        Param("gain", "float", 1.0, "Brightness gain", 0.25, 8.0, 0.25),
    ],
)
def luminance_gradient(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    rgb, stats = gradient_map(ctx.image, p["smoothing"], p["gain"])
    leg = _legend(min(96, max(32, min(rgb.shape[:2]) // 5)))
    rgb[4 : 4 + leg.shape[0], 4 : 4 + leg.shape[1]] = leg
    return AnalysisResult(
        summary="Gradient direction (colour) and strength (brightness) map; legend wheel at the top left.",
        findings=stats | {"colour_key": "hue = direction of increasing brightness (red right, yellow-green up, cyan left, "
                          "blue-purple down); brightness = strength relative to the 99th percentile"},
        maps={"luminance_gradient": rgb},
    )
