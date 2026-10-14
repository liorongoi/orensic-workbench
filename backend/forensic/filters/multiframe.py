"""Multi-frame filters: registration (stabilisation), frame integration and frame picking.

These consume the whole frame range selected in the chain's source.
"""

from __future__ import annotations

import cv2
import numpy as np

from .base import FilterError, Param, luminance, register

MOTION = {
    "translation": cv2.MOTION_TRANSLATION,
    "euclidean": cv2.MOTION_EUCLIDEAN,
    "affine": cv2.MOTION_AFFINE,
    "homography": cv2.MOTION_HOMOGRAPHY,
}


def _require_stack(stack):
    if stack.ndim != 4:
        raise FilterError("this filter needs a frame range as its source")


@register(
    "stack.stabilise",
    name="Stabilise / register frames",
    version="1.0.0",
    category="Multi-frame",
    description="Aligns every frame to a reference frame by maximising the enhanced correlation "
    "coefficient (ECC), optionally using only a region of interest (e.g. a plate or face). "
    "Frames that fail to converge are left unaligned and reported.",
    reference="G. D. Evangelidis and E. Z. Psarakis, 'Parametric Image Alignment Using Enhanced "
    "Correlation Coefficient Maximization', IEEE TPAMI 30(10), 2008.",
    input_kind="stack",
    output_kind="stack",
    params=[
        Param("reference", "int", 0, "Reference frame (index in range)", 0, None, 1),
        Param("motion", "choice", "euclidean", "Motion model", choices=tuple(MOTION)),
        Param("roi", "points", [], "Region of interest (2 corners, optional)",
              help="Leave empty to use the whole frame"),
        Param("iterations", "int", 200, "Max iterations", 10, 5000, 10),
        Param("epsilon", "float", 1e-6, "Convergence epsilon", 1e-10, 1e-2),
    ],
)
def stabilise(stack, p):
    _require_stack(stack)
    n, h, w = stack.shape[:3]
    ref_i = p["reference"]
    if ref_i >= n:
        raise FilterError(f"reference frame {ref_i} is outside the range of {n} frames")
    mask = None
    if p["roi"]:
        if len(p["roi"]) != 2:
            raise FilterError("region of interest needs exactly two corner points")
        (x0, y0), (x1, y1) = p["roi"]
        x0, x1 = sorted((int(round(x0)), int(round(x1))))
        y0, y1 = sorted((int(round(y0)), int(round(y1))))
        mask = np.zeros((h, w), np.uint8)
        mask[max(0, y0) : min(h, y1), max(0, x0) : min(w, x1)] = 255
    motion = MOTION[p["motion"]]
    ref = luminance(stack[ref_i])
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, p["iterations"], p["epsilon"])
    out = np.empty_like(stack)
    for i in range(n):
        if i == ref_i:
            out[i] = stack[i]
            continue
        warp = np.eye(3, 3, dtype=np.float32) if motion == cv2.MOTION_HOMOGRAPHY else np.eye(2, 3, dtype=np.float32)
        try:
            _, warp = cv2.findTransformECC(ref, luminance(stack[i]), warp, motion, criteria, mask, 5)
        except cv2.error:
            out[i] = stack[i]
            continue
        flags = cv2.INTER_CUBIC + cv2.WARP_INVERSE_MAP
        if motion == cv2.MOTION_HOMOGRAPHY:
            out[i] = cv2.warpPerspective(stack[i], warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)
        else:
            out[i] = cv2.warpAffine(stack[i], warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)
    return np.clip(out, 0, 1)


@register(
    "stack.integrate",
    name="Frame integration",
    version="1.0.0",
    category="Multi-frame",
    description="Combines aligned frames into one image to reduce random noise: mean, median, or a "
    "sigma-clipped mean that ignores outliers such as passing objects. Noise falls roughly with the "
    "square root of the number of frames; motion between frames causes blur, so stabilise first.",
    reference="Scientific Working Group on Imaging Technology (SWGIT), Section 5: Recommendations and "
    "Guidelines for Image Processing, 2010; standard astronomical image stacking.",
    input_kind="stack",
    output_kind="image",
    params=[
        Param("method", "choice", "mean", "Method", choices=("mean", "median", "sigma_clip")),
        Param("sigma", "float", 2.5, "Clip at sigma", 0.5, 10.0, 0.1),
    ],
)
def integrate(stack, p):
    _require_stack(stack)
    if p["method"] == "mean":
        return stack.mean(axis=0, dtype=np.float64).astype(np.float32)
    if p["method"] == "median":
        return np.median(stack, axis=0).astype(np.float32)
    s = stack.astype(np.float64)
    med = np.median(s, axis=0)
    std = s.std(axis=0)
    keep = np.abs(s - med) <= p["sigma"] * np.maximum(std, 1e-9)
    total = np.where(keep, s, 0).sum(axis=0)
    count = np.maximum(keep.sum(axis=0), 1)
    return (total / count).astype(np.float32)


@register(
    "stack.pick_frame",
    name="Pick frame",
    version="1.0.0",
    category="Multi-frame",
    description="Takes a single frame out of the current frame range (e.g. after stabilisation).",
    reference="Exact pixel copy; no reference needed.",
    input_kind="stack",
    output_kind="image",
    params=[Param("index", "int", 0, "Frame (index in range)", 0, None, 1)],
)
def pick_frame(stack, p):
    _require_stack(stack)
    if p["index"] >= stack.shape[0]:
        raise FilterError(f"frame {p['index']} is outside the range of {stack.shape[0]} frames")
    return np.ascontiguousarray(stack[p["index"]])
