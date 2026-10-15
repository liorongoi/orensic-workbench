"""JPEG dimples: a faint periodic mark that many camera encoders leave at one pixel of every 8x8 block.

Agarwal and Farid found that the fixed-point DCT in many cameras rounds with a small,
consistent bias. Summed over the 64 frequencies, the bias concentrates at one position of
each block, so a camera-original JPEG carries a tiny bump or dip ("dimple") at that position
throughout the image. libjpeg and most editing software round without bias and leave none.

Measuring it: high-pass the decoded luma (pixel minus its 3x3 mean) and average the result
at each of the 64 positions in the 8x8 grid over all blocks. The blocking of JPEG itself
raises whole rows and columns of that table (block borders), so an additive row + column
model is removed by median polish first; a dimple is then a single outlying cell, scored as
a robust z against the other 63. A file that is cropped keeps its dimple, at a shifted
position; one that is decoded and re-saved at moderate quality loses it.

Locally, the same contrast (dimple cell minus the block's other cells) is averaged over
windows: where the rest of the image has the dimple and a region does not, that region did
not pass through the camera's encoder the way the rest did (pasted, painted or resampled).
"""

from __future__ import annotations

import cv2
import numpy as np

from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register
from .compression import _colormap
from .double_jpeg import block_dct, luma_table, read_luma
from .jpeg import ZIGZAG, is_jpeg

MAD_FLOOR = 0.02
DIMPLE_THRESHOLD = 13.0  # robust z; calibrated on the validation corpus (libjpeg saves reach about 10)


def highpass(y: np.ndarray) -> np.ndarray:
    return y - cv2.blur(y, (3, 3), borderType=cv2.BORDER_REFLECT)


def block_cells(r: np.ndarray) -> np.ndarray:
    """(bh, bw, 64): the high-pass value at each grid position of each 8x8 block."""
    bh, bw = r.shape[0] // 8, r.shape[1] // 8
    return r[: bh * 8, : bw * 8].reshape(bh, 8, bw, 8).transpose(0, 2, 1, 3).reshape(bh, bw, 64)


def dimple_score(cells: np.ndarray) -> tuple[int, float, np.ndarray]:
    """Strongest isolated cell of the mean 8x8 table: (position 0..63 as row*8+col, robust z, residual table)."""
    L = cells.reshape(-1, 64).mean(axis=0).reshape(8, 8)
    r, c = np.zeros(8), np.zeros(8)
    for _ in range(6):
        r = np.median(L - c[None, :], axis=1)
        c = np.median(L - r[:, None], axis=0)
    R = (L - r[:, None] - c[None, :]).ravel()
    k = int(np.argmax(np.abs(R)))
    rest = np.delete(R, k)
    med = float(np.median(rest))
    # Floor the spread at 0.02 grey levels: in very clean tables tiny ripples would otherwise score as outliers.
    mad = max(float(np.median(np.abs(rest - med))) * 1.4826, MAD_FLOOR)
    return k, float((R[k] - med) / mad), R.reshape(8, 8)


def dimples(data: bytes) -> dict:
    """Global test on a JPEG file. Pure function of the file bytes."""
    if not is_jpeg(data):
        raise ValueError("JPEG dimples analysis needs a JPEG file")
    y = read_luma(data)
    if min(y.shape) < 64:
        raise ValueError("image is too small for the dimples analysis")
    cells = block_cells(highpass(y))
    k, z, R = dimple_score(cells)
    return {"position": k, "z": z, "cells": cells, "table": R, "textured": textured_blocks(data, y, cells.shape[:2])}


def textured_blocks(data: bytes, y: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Blocks (and their neighbours) with at least 3 non-zero quantised low-frequency AC coefficients.

    A block quantised to its DC alone (sky, walls, clipped areas) has nothing for the encoder's
    rounding bias to act on, so it carries no dimple in an authentic image either.
    """
    try:
        q = luma_table(data)
    except ValueError:
        return np.ones(shape, bool)
    c = block_dct(y)[: shape[0], : shape[1]]
    nz = np.zeros(shape, np.float32)
    for k in range(1, 16):
        r, cc = divmod(int(ZIGZAG[k]), 8)
        nz += np.rint(c[:, :, r, cc] / q[r, cc]) != 0
    return cv2.blur(nz, (3, 3), borderType=cv2.BORDER_REFLECT) >= 3


def local_strength(cells: np.ndarray, k: int, window_blocks: int, textured: np.ndarray | None = None
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Per-block dimple strength relative to the whole image, averaged over windows.

    Returns (strength, measurable): strength ~1 where the dimple is as strong as in the image
    overall and ~0 where it is absent; measurable is False where a window holds too few
    blocks' worth of signal to tell the two apart (absence would not reach 4 standard errors).
    """
    others = np.delete(cells, k, axis=2).mean(axis=2)
    d = cells[:, :, k] - others
    amp = float(np.mean(d))
    sd = float(np.median(np.abs(d - np.median(d)))) * 1.4826 + 1e-12
    wb = max(2, window_blocks)
    mean_d = cv2.blur(d, (wb, wb), borderType=cv2.BORDER_REFLECT)
    strength = mean_d / amp if abs(amp) > 1e-12 else np.zeros_like(d)
    se = sd / wb
    measurable = np.full(d.shape, abs(amp) / se >= 4.0)
    # Saturated or flat blocks have no texture to quantise, so they carry no dimple.
    if textured is not None:
        # The window's dimple is averaged over its textured blocks only; flat ones say nothing.
        t = textured.astype(np.float64)
        cnt = cv2.blur(t, (wb, wb), borderType=cv2.BORDER_REFLECT)
        mean_d = cv2.blur(d * t, (wb, wb), borderType=cv2.BORDER_REFLECT) / np.maximum(cnt, 1e-9)
        amp = float(np.mean(d[textured])) if textured.any() else amp
        strength = mean_d / amp if abs(amp) > 1e-12 else np.zeros_like(d)
        measurable = textured & (cnt >= 0.5) & (abs(amp) / (sd / (wb * np.sqrt(np.maximum(cnt, 1e-9)))) >= 4.0)
    else:
        texture = cv2.blur(np.abs(others) + np.abs(cells).mean(axis=2), (wb, wb), borderType=cv2.BORDER_REFLECT)
        measurable &= texture > 0.15 * float(np.median(texture))
    return strength, measurable


def weak_area(strength: np.ndarray, ok: np.ndarray, min_share: float = 0.03) -> np.ndarray:
    """Blocks in compact groups that lack the dimple: below a quarter of the image's strength, in a
    connected group of at least `min_share` of the image (scattered weak blocks are content)."""
    weak = ((strength < 0.25) & ok).astype(np.uint8)
    weak = cv2.morphologyEx(weak, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(weak, connectivity=8)
    keep = np.zeros(weak.shape, bool)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_share * weak.size:
            keep |= lab == i
    return keep


@register(
    "tamper.jpeg_dimples",
    name="JPEG dimples",
    version="1.0.0",
    category="Global analysis",
    description="Looks for the faint mark many camera encoders leave at one pixel of every 8x8 JPEG block. Present "
    "throughout: consistent with a camera-original JPEG (or one cropped from it). Absent: the file was written by "
    "software, re-saved, or comes from a camera without dimples. The map shows where the mark is weaker than in "
    "the rest of the image, which can reveal a pasted or retouched region.",
    reference="S. Agarwal and H. Farid, 'Photo Forensics from JPEG Dimples', IEEE Workshop on Information "
    "Forensics and Security (WIFS), 2017.",
    applies_to=("image",),
    reading_guide="Strength is a robust z score; 13 or more is a dimple. Not every camera makes dimples, so "
    "absence alone shows nothing. The map is only shown when the image has a dimple: bright areas lack it. "
    "Flat, dark or saturated areas carry no dimple in authentic images either and are hatched.",
    params=[Param("window", "int", 64, "Window (px)", 32, 256, 8)],
)
def jpeg_dimples(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    res = dimples(ctx.data)
    k, z = res["position"], res["z"]
    present = abs(z) >= DIMPLE_THRESHOLD
    pos = (k % 8, k // 8)
    h, w = ctx.image.shape[:2]
    flags = []
    findings = {
        "dimple_present": present,
        "strength_z": round(abs(z), 2),
        "threshold": DIMPLE_THRESHOLD,
        "polarity": "bright" if z > 0 else "dark",
        "position_in_block_xy": list(pos),
        "grid_aligned": k == 0,
        "mean_8x8_residual": np.round(res["table"], 4).tolist(),
    }
    heat = np.zeros((h, w), np.float32)
    hatch = np.zeros((h, w), bool)
    if present:
        flags.append({"level": "info", "code": "dimples.present",
                      "text": f"Camera JPEG dimple found (strength {abs(z):.0f}) at block position {pos}; consistent with a "
                      "camera-original JPEG" + (" that was cropped off the 8x8 grid." if k != 0 else ".")})
        strength, ok = local_strength(res["cells"], k, p["window"] // 8, res["textured"])
        missing = np.clip(1.0 - strength, 0, 1).astype(np.float32)
        weak = weak_area(strength, ok)
        share = float(weak.mean())
        findings["weak_block_share"] = round(share, 4)
        findings["measurable_share"] = round(float(ok.mean()), 4)
        if 0.04 <= share <= 0.4:
            flags.append({"level": "warning", "code": "dimples.local",
                          "text": f"{share:.0%} of the image lacks the dimple the rest carries; inspect the map for a compact region."})
        bh, bw = missing.shape
        heat[: bh * 8, : bw * 8] = cv2.resize(missing, (bw * 8, bh * 8), interpolation=cv2.INTER_NEAREST)
        hatch[: bh * 8, : bw * 8] = cv2.resize((~ok).astype(np.uint8), (bw * 8, bh * 8), interpolation=cv2.INTER_NEAREST) > 0
    rgb = _colormap(heat)
    yy, xx = np.mgrid[0:h, 0:w]
    rgb[hatch & (((xx + yy) // 4) % 3 == 0)] = 0.5
    summary = (f"Dimple present (strength {abs(z):.1f}, detection at {DIMPLE_THRESHOLD:.0f})." if present
               else f"No camera dimple (strength {abs(z):.1f}, detection at {DIMPLE_THRESHOLD:.0f}).")
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps={"missing_dimple": rgb})
