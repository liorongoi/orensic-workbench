"""Multi-frame filters: registration (stabilisation), frame integration and frame picking.

These consume the whole frame range selected in the chain's source.
"""

from __future__ import annotations

import cv2
import numpy as np

from .. import compute
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

    def align(i):
        if i == ref_i:
            return stack[i]
        warp = np.eye(3, 3, dtype=np.float32) if motion == cv2.MOTION_HOMOGRAPHY else np.eye(2, 3, dtype=np.float32)
        try:
            _, warp = cv2.findTransformECC(ref, luminance(stack[i]), warp, motion, criteria, mask, 5)
        except cv2.error:
            return stack[i]
        flags = cv2.INTER_CUBIC + cv2.WARP_INVERSE_MAP
        if motion == cv2.MOTION_HOMOGRAPHY:
            return cv2.warpPerspective(stack[i], warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)
        return cv2.warpAffine(stack[i], warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)

    # Each frame is registered to the reference on its own, so frames can run in parallel.
    out = np.stack(compute.pmap(align, range(n)))
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
        return compute.mean0(stack, np.float64).astype(np.float32)
    if p["method"] == "median":
        return compute.median0(stack).astype(np.float32)

    def clipped(chunk):  # every output pixel depends only on its own column of frames
        s = chunk.astype(np.float64)
        med = np.median(s, axis=0)
        std = s.std(axis=0)
        keep = np.abs(s - med) <= p["sigma"] * np.maximum(std, 1e-9)
        total = np.where(keep, s, 0).sum(axis=0)
        count = np.maximum(keep.sum(axis=0), 1)
        return (total / count).astype(np.float32)

    return compute.by_rows(clipped, stack, np.float32)


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
    warps = compute.pmap(lambda i: np.eye(3) if i == ref_i else _register_to(ref, luminance(stack[i]), motion, criteria, mask), range(n))
    frames, to_lr, to_hr = [], [], []
    for i, Wk in enumerate(warps):
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
    combine = compute.median0 if p["robust"] else compute.mean0
    ks = range(len(frames))
    hr = combine(compute.pmap(lambda k: to_grid(frames[k], k), ks)).astype(np.float32)
    for _ in range(p["iterations"]):
        blurred = blur(hr)
        errs = compute.pmap(lambda k: to_grid(frames[k] - sample_lr(blurred, k), k), ks)
        hr = np.clip(hr + p["step"] * blur(combine(errs).astype(np.float32)), 0, 1).astype(np.float32)
    return hr


@register(
    "stack.denoise_temporal",
    name="Video denoise (motion-compensated)",
    version="1.0.0",
    category="Multi-frame",
    description="Removes noise from each frame by averaging it with its neighbouring frames after following the "
    "motion between them (dense optical flow), then cleans what is left with light single-frame denoising. Noise "
    "falls far more than with single-image denoising and detail is kept, because each pixel is averaged with the "
    "same point of the scene in other frames. Where the flow fails (things appearing, disappearing or moving very "
    "fast) the neighbour does not match and is left out, so moving objects do not leave trails. Works without "
    "stabilising first; frames at the start and end of the range use fewer neighbours.",
    reference="C. Liu and W. T. Freeman, 'A high-quality video denoising algorithm based on reliable motion "
    "estimation', ECCV 2010; T. Kroeger et al., 'Fast optical flow using dense inverse search', ECCV 2016; "
    "A. Buades, B. Coll and J.-M. Morel, 'A non-local algorithm for image denoising', CVPR 2005.",
    input_kind="stack",
    output_kind="stack",
    params=[
        Param("frames", "int", 5, "Frames used per output frame (odd)", 3, 15, 2),
        Param("tolerance", "float", 0.06, "Match tolerance", 0.01, 0.5, 0.01,
              help="How different a motion-compensated neighbour may look and still be averaged; raise it for very noisy footage"),
        Param("strength", "float", 4.0, "Final denoise strength", 0.0, 30.0, 0.5,
              help="Single-frame non-local means applied after averaging; 0 turns it off"),
    ],
)
def denoise_temporal(stack, p):
    _require_stack(stack)
    n = stack.shape[0]
    k = min(p["frames"] | 1, n if n % 2 else n - 1)
    if k < 3:
        raise FilterError("video denoise needs a range of at least three frames")
    half = k // 2
    h, w = stack.shape[1:3]
    grey = compute.pmap(lambda f: cv2.GaussianBlur(cv2.cvtColor(
        np.clip(np.rint(f * 255.0), 0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY), (0, 0), 1.0), stack)
    soft = compute.pmap(lambda f: cv2.GaussianBlur(f, (0, 0), 1.5), stack)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    def one(i):
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        acc = stack[i].astype(np.float32).copy()
        wsum = np.ones((h, w), np.float32)
        for j in range(max(0, i - half), min(n, i + half + 1)):
            if j == i:
                continue
            flow = dis.calc(grey[i], grey[j], None)
            mx, my = xx + flow[..., 0], yy + flow[..., 1]
            warped = cv2.remap(stack[j], mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
            diff = np.abs(cv2.remap(soft[j], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT) - soft[i]).mean(-1)
            wt = np.exp(-((diff / p["tolerance"]) ** 2)).astype(np.float32)
            acc += warped * wt[..., None]
            wsum += wt
        out = np.clip(acc / wsum[..., None], 0, 1)
        if p["strength"] <= 0:
            return out.astype(np.float32)
        u = np.clip(np.rint(out * 255.0), 0, 255).astype(np.uint8)
        return cv2.fastNlMeansDenoisingColored(u, None, p["strength"], p["strength"], 7, 21).astype(np.float32) / 255.0

    return np.stack(compute.pmap(one, range(n)))


def _u8_luma(img: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(luminance(img) * 255.0), 0, 255).astype(np.uint8)


@register(
    "stack.turbulence",
    name="Turbulence removal",
    version="1.0.0",
    category="Multi-frame",
    description="For footage through hot air or over long distances, where the scene shimmers and wobbles from frame to "
    "frame. Builds a steady reference (the per-pixel median), warps every frame onto it with dense optical flow so each "
    "detail sits in its true place, fuses the aligned frames, then removes the remaining soft blur by total-variation "
    "deconvolution. Needs a fixed camera (stabilise first if it moves) and a static scene: moving objects are averaged away.",
    reference="X. Zhu and P. Milanfar, 'Removing Atmospheric Turbulence via Space-Invariant Deconvolution', IEEE TPAMI "
    "35(1), 2013; G. Farnebäck, 'Two-Frame Motion Estimation Based on Polynomial Expansion', SCIA 2003; Y. Wang et al., "
    "SIAM J. Imaging Sciences 1(3), 2008 (final deconvolution).",
    input_kind="stack",
    output_kind="image",
    params=[
        Param("passes", "int", 2, "Registration passes", 1, 5, 1,
              help="Each pass registers the frames to the previous pass's fused image"),
        Param("fusion", "choice", "median", "Fusion", choices=("mean", "median")),
        Param("residual_blur", "float", 1.0, "Remaining blur sigma (px)", 0.0, 5.0, 0.1,
              help="Softness left after fusion, removed by deconvolution; 0 skips this step"),
        Param("regularisation", "float", 0.001, "Regularisation", 1e-5, 0.1, 0.0005),
    ],
)
def turbulence(stack, p):
    _require_stack(stack)
    return turbulence_fuse(stack, p["passes"], p["fusion"], p["residual_blur"], p["regularisation"])


def turbulence_fuse(stack, passes: int, fusion: str, residual_blur: float, regularisation: float) -> np.ndarray:
    from .deblur import make_psf, tv_psf

    n, h, w = stack.shape[:3]
    if n < 3:
        raise FilterError("turbulence removal needs at least 3 frames")
    fuse = compute.median0 if fusion == "median" else (lambda s: compute.mean0(s, np.float64))
    fused = fuse(stack).astype(np.float32)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    for _ in range(passes):
        ref = _u8_luma(fused)

        def align(i):
            flow = cv2.calcOpticalFlowFarneback(ref, _u8_luma(stack[i]), None, 0.5, 3, 15, 3, 5, 1.2, 0)
            return cv2.remap(stack[i], gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

        fused = fuse(np.stack(compute.pmap(align, range(n)))).astype(np.float32)
    if residual_blur > 0:
        psf = make_psf({"psf": "gaussian", "sigma": residual_blur})
        fused = tv_psf(fused, psf, regularisation, 3)
    return np.clip(fused, 0, 1).astype(np.float32)
