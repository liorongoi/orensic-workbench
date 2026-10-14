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


def _register_to(ref: np.ndarray, img: np.ndarray, motion: int, criteria, mask) -> np.ndarray | None:
    """3x3 matrix W with img(W x) ~ ref(x), or None if ECC does not converge."""
    warp = np.eye(3, 3, dtype=np.float32) if motion == cv2.MOTION_HOMOGRAPHY else np.eye(2, 3, dtype=np.float32)
    try:
        _, warp = cv2.findTransformECC(ref, img, warp, motion, criteria, mask, 5)
    except cv2.error:
        return None
    if warp.shape[0] == 2:
        warp = np.vstack([warp, [0, 0, 1]]).astype(np.float32)
    return warp.astype(np.float64)


def _scale_matrix(s: float) -> np.ndarray:
    """Low-res pixel centres -> high-res pixel centres for an integer zoom s."""
    return np.array([[s, 0, (s - 1) / 2], [0, s, (s - 1) / 2], [0, 0, 1]], dtype=np.float64)


@register(
    "stack.super_resolution",
    name="Multi-frame super resolution",
    version="1.0.0",
    category="Multi-frame",
    description="Builds one higher-resolution image from several frames of the same scene that are shifted "
    "by fractions of a pixel (for example a slowly moving car or a hand-held camera). Frames are registered "
    "to the reference frame (ECC), fused on a finer grid (shift-and-add), then refined by iterative "
    "back-projection so that the result, blurred and downsampled, agrees with every observed frame. "
    "This is classical, model-based reconstruction: it does not invent detail, but it can only add "
    "resolution when the frames really carry different sub-pixel samples of the scene. The robust option "
    "uses the median of the frame errors so a frame with a passing object does not spoil the result.",
    reference="M. Irani and S. Peleg, 'Improving Resolution by Image Registration', CVGIP: Graphical Models "
    "and Image Processing 53(3), 1991; S. Farsiu, M. D. Robinson, M. Elad and P. Milanfar, 'Fast and Robust "
    "Multiframe Super Resolution', IEEE Trans. Image Processing 13(10), 2004.",
    input_kind="stack",
    output_kind="image",
    params=[
        Param("scale", "int", 2, "Zoom factor", 2, 4, 1),
        Param("reference", "int", 0, "Reference frame (index in range)", 0, None, 1),
        Param("motion", "choice", "affine", "Motion model", choices=tuple(MOTION)),
        Param("roi", "points", [], "Registration region (2 corners, optional)",
              help="Register on this region only, e.g. the plate; leave empty for the whole frame"),
        Param("iterations", "int", 15, "Back-projection iterations", 0, 100, 1),
        Param("psf_sigma", "float", 0.8, "Camera blur sigma (low-res px)", 0.3, 3.0, 0.1),
        Param("robust", "bool", True, "Robust (median) fusion"),
        Param("step", "float", 1.0, "Back-projection step", 0.1, 2.0, 0.1),
    ],
)
def super_resolution(stack, p):
    _require_stack(stack)
    n, h, w = stack.shape[:3]
    if n < 2:
        raise FilterError("super resolution needs at least two frames")
    ref_i = p["reference"]
    if ref_i >= n:
        raise FilterError(f"reference frame {ref_i} is outside the range of {n} frames")
    s = p["scale"]
    H, W = h * s, w * s
    mask = None
    if p["roi"]:
        if len(p["roi"]) != 2:
            raise FilterError("registration region needs exactly two corner points")
        (x0, y0), (x1, y1) = p["roi"]
        x0, x1 = sorted((int(round(x0)), int(round(x1))))
        y0, y1 = sorted((int(round(y0)), int(round(y1))))
        mask = np.zeros((h, w), np.uint8)
        mask[max(0, y0) : min(h, y1), max(0, x0) : min(w, x1)] = 255
    motion = MOTION[p["motion"]]
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-7)
    ref = luminance(stack[ref_i])
    S = _scale_matrix(s)
    Sinv = np.linalg.inv(S)
    frames, to_lr, to_hr = [], [], []
    for i in range(n):
        Wk = np.eye(3) if i == ref_i else _register_to(ref, luminance(stack[i]), motion, criteria, mask)
        if Wk is None:
            continue  # frames that cannot be registered would only add blur
        frames.append(stack[i].astype(np.float32))
        to_lr.append(S @ np.linalg.inv(Wk))  # LR frame pixel u -> HR position
        to_hr.append(Wk @ Sinv)  # HR pixel X -> LR frame position
    flags = cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP
    border = cv2.BORDER_REPLICATE

    def sample_lr(hr, k):
        return cv2.warpPerspective(hr, to_lr[k], (w, h), flags=flags, borderMode=border)

    def to_grid(lr, k):
        up = cv2.warpPerspective(lr, to_hr[k], (W, H), flags=cv2.INTER_CUBIC + cv2.WARP_INVERSE_MAP, borderMode=border)
        return up

    ksz = int(2 * np.ceil(3 * p["psf_sigma"] * s) + 1)
    blur = lambda x: cv2.GaussianBlur(x, (ksz, ksz), p["psf_sigma"] * s, borderType=cv2.BORDER_REFLECT)  # noqa: E731
    combine = (lambda xs: np.median(np.stack(xs), axis=0)) if p["robust"] else (lambda xs: np.mean(np.stack(xs), axis=0))
    hr = combine([to_grid(f, k) for k, f in enumerate(frames)]).astype(np.float32)
    for _ in range(p["iterations"]):
        blurred = blur(hr)
        errs = [to_grid(frames[k] - sample_lr(blurred, k), k) for k in range(len(frames))]
        hr = np.clip(hr + p["step"] * blur(combine(errs).astype(np.float32)), 0, 1).astype(np.float32)
    return hr
