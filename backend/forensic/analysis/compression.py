"""Compression-based tamper analyses: Error Level Analysis and JPEG Ghost."""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from ..filters.base import Param, u8
from .base import AnalysisContext, AnalysisResult, register


def recompress(img: np.ndarray, quality: int) -> np.ndarray:
    """Re-encode as baseline 4:4:4 JPEG with libjpeg standard tables, then decode."""
    buf = io.BytesIO()
    Image.fromarray(u8(img)).save(buf, "JPEG", quality=quality, subsampling=0, optimize=False)
    buf.seek(0)
    with Image.open(buf) as im:
        return np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0


def _colormap(x: np.ndarray) -> np.ndarray:
    """[0,1] scalar map -> RGB with a perceptually ordered colour map (inferno)."""
    c = cv2.applyColorMap(np.clip(np.rint(x * 255), 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    return cv2.cvtColor(c, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


@register(
    "tamper.ela",
    name="Error Level Analysis (ELA)",
    version="1.0.0",
    category="Local analysis",
    description="Re-saves the image as JPEG at a known quality and shows the absolute difference, "
    "amplified. Areas compressed differently from the rest (for example pasted in from another "
    "JPEG) can stand out as brighter or darker.",
    reference="N. Krawetz, 'A Picture's Worth: Digital Image Analysis and Forensics', Black Hat USA, 2007.",
    applies_to=("image",),
    reading_guide="ELA is strongly affected by content: edges, texture and saturated areas are bright in "
    "authentic images too. Look for regions whose level differs from similar content elsewhere, and "
    "confirm with other analyses. Not meaningful on images saved losslessly after editing.",
    params=[
        Param("quality", "int", 90, "Re-save quality", 50, 100, 1),
        Param("scale", "float", 20.0, "Amplification", 1.0, 100.0, 1.0),
    ],
)
def ela(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    img = ctx.image
    diff = np.abs(img - recompress(img, p["quality"]))
    amplified = np.clip(diff * p["scale"], 0, 1)
    lum = diff.max(axis=2)
    # Blockwise error level, to show where the level departs from the image median.
    h, w = lum.shape
    bh, bw = h // 8, w // 8
    blocks = lum[: bh * 8, : bw * 8].reshape(bh, 8, bw, 8).mean(axis=(1, 3)) if bh and bw else lum
    med = float(np.median(blocks))
    # Floor the spread at 1/255 so flat synthetic areas do not turn every block into an outlier.
    mad = max(float(np.median(np.abs(blocks - med))), 1.0 / 255.0)
    outlier_share = float(np.mean(np.abs(blocks - med) / mad > 6))
    findings = {
        "mean_error": round(float(lum.mean()) * 255, 3),
        "p99_error": round(float(np.percentile(lum, 99)) * 255, 3),
        "outlier_block_share": round(outlier_share, 4),
    }
    flags = []
    if outlier_share > 0.02:
        flags.append({"level": "info", "text": f"{outlier_share:.1%} of 8x8 blocks have an error level far from the image median; inspect the map, keeping in mind that edges and texture do this naturally."})
    return AnalysisResult(
        summary=f"Mean error level {findings['mean_error']:.2f} (of 255) at quality {p['quality']}.",
        findings=findings,
        flags=flags,
        maps={"ela": amplified.astype(np.float32), "ela_heat": _colormap(np.clip(lum * p["scale"], 0, 1))},
    )


@register(
    "tamper.jpeg_ghost",
    name="JPEG Ghost",
    version="1.0.0",
    category="Local analysis",
    description="Re-saves the image at a range of JPEG qualities and measures the local difference at "
    "each. A region previously compressed at a lower quality than the rest shows a dark 'ghost' (a "
    "local minimum) at that quality. The global curve shows the quality at which the whole image was "
    "most likely last saved.",
    reference="H. Farid, 'Exposing Digital Forgeries from JPEG Ghosts', IEEE Trans. Information Forensics "
    "and Security 4(1), 2009.",
    applies_to=("image",),
    reading_guide="Look for a compact dark region that appears at a quality where the rest of the image is "
    "bright. Works best when the inserted region was saved at a clearly lower quality than the final image; "
    "misalignment of the 8x8 grid weakens it.",
    params=[
        Param("q_min", "int", 50, "Lowest quality", 10, 99, 1),
        Param("q_max", "int", 95, "Highest quality", 11, 100, 1),
        Param("q_step", "int", 5, "Step", 1, 20, 1),
        Param("block", "int", 16, "Averaging block (px)", 1, 64, 1),
    ],
)
def jpeg_ghost(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if p["q_max"] <= p["q_min"]:
        raise ValueError("highest quality must be above the lowest")
    img = ctx.image
    qualities = list(range(p["q_min"], p["q_max"] + 1, p["q_step"]))
    maps, curve = [], []
    b = p["block"]
    for q in qualities:
        d = ((img - recompress(img, q)) ** 2).mean(axis=2)
        d = cv2.blur(d, (b, b), borderType=cv2.BORDER_REFLECT) if b > 1 else d
        curve.append(float(d.mean()))
        lo, hi = float(d.min()), float(d.max())
        maps.append((d - lo) / (hi - lo) if hi > lo else np.zeros_like(d))
    # Second-lowest dip in the curve (other than the highest quality) hints at a prior save quality.
    curve_arr = np.array(curve)
    local_min = [qualities[i] for i in range(1, len(curve) - 1) if curve_arr[i] < curve_arr[i - 1] and curve_arr[i] < curve_arr[i + 1]]
    montage = _montage([_colormap(1 - m) for m in maps], [f"Q{q}" for q in qualities])
    flags = []
    if local_min:
        flags.append({"level": "info", "text": f"Global difference curve has a local minimum at quality {', '.join(map(str, local_min))}; consistent with an earlier save at that quality."})
    return AnalysisResult(
        summary=f"Ghost maps for qualities {qualities[0]} to {qualities[-1]}." + (f" Curve dips at Q{local_min[0]}." if local_min else ""),
        findings={"qualities": qualities, "curve": [round(c * 1e4, 4) for c in curve], "curve_units": "mean squared difference x 1e4", "local_minima": local_min},
        flags=flags,
        maps={"ghost_montage": montage},
    )


def _montage(images: list[np.ndarray], labels: list[str], max_tile: int = 360) -> np.ndarray:
    h, w = images[0].shape[:2]
    s = min(1.0, max_tile / max(h, w))
    th, tw = max(1, int(h * s)), max(1, int(w * s))
    cols = min(4, len(images))
    rows = (len(images) + cols - 1) // cols
    pad = 24
    canvas = np.full((rows * (th + pad), cols * tw, 3), 255, np.uint8)
    for i, (im, label) in enumerate(zip(images, labels)):
        r, c = divmod(i, cols)
        tile = u8(cv2.resize(im, (tw, th), interpolation=cv2.INTER_AREA))
        y = r * (th + pad) + pad
        canvas[y : y + th, c * tw : (c + 1) * tw] = tile
        cv2.putText(canvas, label, (c * tw + 6, y - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return canvas.astype(np.float32) / 255.0
