"""Double JPEG compression analyses.

* Aligned double JPEG (A-DJPG): when a JPEG is decoded and saved again on the same
  8x8 grid, each DCT coefficient histogram carries a periodic trace of the first
  quantisation step. Fitting that trace per frequency gives a global test, an
  estimate of the first quality, and a per-block likelihood map: a region pasted in
  from elsewhere usually lacks the trace the rest of the image has (or vice versa).
* Non-aligned grid analysis (NA-DJPG): when a JPEG is cropped (or a region pasted
  off-grid) and saved again, the blocking grid of the first compression survives at
  an offset from the final grid. Measuring blockiness at all 64 offsets finds it.

Coefficients are recovered by taking the 8x8 DCT of the decoded luma plane, read
from the file in its native YCbCr (no colour conversion), divided by the file's own
quantisation table. The IDCT rounding makes them near-integers, which is enough.
"""

from __future__ import annotations

import io
import math

import cv2
import numpy as np
from PIL import Image

from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register
from .compression import _colormap
from .jpeg import STD_LUMA, ZIGZAG, ijg_table, is_jpeg, parse_markers

# Orthonormal 8-point DCT-II matrix; JPEG's FDCT is exactly this transform in 2-D.
_k = np.arange(8)
DCT8 = np.sqrt(2 / 8) * np.cos((2 * _k[None, :] + 1) * _k[:, None] * np.pi / 16)
DCT8[0] /= np.sqrt(2)


def read_luma(data: bytes) -> np.ndarray:
    """The decoded Y plane (0..255 float64) without any colour conversion."""
    with Image.open(io.BytesIO(data)) as im:
        if im.format not in ("JPEG", "MPO"):
            raise ValueError("double JPEG analysis needs a JPEG file")
        if im.mode in ("RGB", "YCbCr"):
            im.draft("YCbCr", im.size)
            if im.mode == "YCbCr":
                return np.asarray(im, dtype=np.float64)[..., 0]
            return np.asarray(im.convert("YCbCr"), dtype=np.float64)[..., 0]
        return np.asarray(im.convert("L"), dtype=np.float64)


def block_dct(y: np.ndarray, dy: int = 0, dx: int = 0) -> np.ndarray:
    """8x8 DCT of the image on a grid starting at (dy, dx): shape (bh, bw, 8, 8)."""
    y = y[dy:, dx:]
    bh, bw = y.shape[0] // 8, y.shape[1] // 8
    blocks = (y[: bh * 8, : bw * 8] - 128.0).reshape(bh, 8, bw, 8).transpose(0, 2, 1, 3)
    return DCT8 @ blocks @ DCT8.T


def luma_table(data: bytes) -> np.ndarray:
    info = parse_markers(data)
    dqt = info["dqt"]
    if not dqt:
        raise ValueError("no quantisation table found in the file")
    tid = "0"
    if info.get("frame") and info["frame"].get("components"):
        tid = str(info["frame"]["components"][0]["table"])
    return np.array(dqt.get(tid) or next(iter(dqt.values())), dtype=np.float64)


def _n_of_x(xs: np.ndarray, q1: int, q2: float) -> np.ndarray:
    """How many first-quantisation bins (step q1) land on each second-quantisation value x (step q2)."""
    hi = np.ceil(q2 * (xs + 0.5) / q1 - 1e-9)
    lo = np.ceil(q2 * (xs - 0.5) / q1 - 1e-9)
    return np.maximum(hi - lo, 0)


def _fit_frequency(x_aligned: np.ndarray, x_shifted: np.ndarray, q2: float, q1_max: int):
    """Fit the A-DJPG mixture for one DCT frequency.

    Returns (q1, alpha, gain_per_coefficient, log_lr_lookup) or None if uninformative.
    """
    lim = 40
    xa = np.clip(x_aligned, -lim, lim).astype(int)
    xs = np.clip(x_shifted, -lim, lim).astype(int)
    support = np.arange(-lim, lim + 1)
    h = np.bincount(xa + lim, minlength=2 * lim + 1).astype(np.float64)
    h0 = np.bincount(xs + lim, minlength=2 * lim + 1).astype(np.float64)
    if h.sum() < 200 or np.count_nonzero(h[np.abs(support) >= 1]) < 3:
        return None  # nearly all zeros: nothing to measure at this frequency
    # Smoothed single-compression model from the misaligned grid (Laplace-like, never zero).
    p0 = (h0 + 0.5) / (h0.sum() + 0.5 * len(h0))
    n_total = h.sum()
    best = None
    for q1 in range(1, q1_max + 1):
        nx = _n_of_x(support.astype(np.float64), q1, q2)
        if np.all(nx[np.abs(support) <= 6] == nx[lim]):
            continue  # identical to single compression on the populated bins: indistinguishable
        p1 = nx * p0
        z = p1.sum()
        if z <= 0:
            continue
        p1 = p1 / z
        for alpha in np.linspace(0.05, 1.0, 20):
            ll = float(np.sum(h * np.log(alpha * p1 + (1 - alpha) * p0 + 1e-12)))
            if best is None or ll > best[0]:
                best = (ll, q1, float(alpha), p1)
    if best is None:
        return None
    ll0 = float(np.sum(h * np.log(p0 + 1e-12)))
    ll, q1, alpha, p1 = best
    gain = (ll - ll0) / n_total
    log_lr = np.log((p1 + 1e-9) / (p0 + 1e-9))
    return q1, alpha, gain, log_lr


def adjpg(data: bytes, n_freq: int = 15, q1_max: int = 40) -> dict:
    """Run the aligned double-JPEG fit and per-block likelihood. Pure function of the file bytes."""
    if not is_jpeg(data):
        raise ValueError("double JPEG analysis needs a JPEG file")
    q2_table = luma_table(data)
    y = read_luma(data)
    coef = block_dct(y)
    shifted = block_dct(y, 4, 4)
    bh, bw = coef.shape[:2]
    if bh < 4 or bw < 4:
        raise ValueError("image is too small for double JPEG analysis")
    llr = np.zeros((bh, bw), np.float64)
    nonzero = np.zeros((bh, bw), np.int32)  # informative coefficients per block (flat blocks have none)
    per_freq = []
    zz_pos = [divmod(int(ZIGZAG[k]), 8) for k in range(1, 1 + n_freq)]
    for (r, c) in zz_pos:
        q2 = float(q2_table[r, c])
        xa = np.rint(coef[:, :, r, c] / q2)
        xs = np.rint(shifted[:, :, r, c] / q2)
        nonzero += (xa != 0)
        fit = _fit_frequency(xa.ravel(), xs.ravel(), q2, q1_max)
        entry = {"freq": [r, c], "q2": int(q2)}
        if fit is None:
            entry["informative"] = False
            per_freq.append(entry)
            continue
        q1, alpha, gain, log_lr = fit
        entry.update({"informative": True, "q1": q1, "alpha": round(alpha, 3), "gain": round(gain, 5)})
        per_freq.append(entry)
        if alpha >= 0.3 and gain > 0.002:
            idx = np.clip(xa, -40, 40).astype(int) + 40
            llr += log_lr[idx]
    informative = [f for f in per_freq if f.get("informative")]
    supporting = [f for f in informative if f["alpha"] >= 0.3 and f["gain"] > 0.002]
    score = float(sum(f["gain"] * f["alpha"] for f in supporting))
    # Estimate the first quality from the frequencies that carry a clear trace.
    est_q1 = None
    if len(supporting) >= 3:
        errs = []
        for q in range(1, 101):
            t = ijg_table(STD_LUMA, q)
            errs.append(sum(abs(t[f["freq"][0], f["freq"][1]] - f["q1"]) for f in supporting))
        est_q1 = int(np.argmin(errs) + 1)
    return {
        "q2_table": q2_table.astype(int).tolist(),
        "frequencies": per_freq,
        "supporting_frequencies": len(supporting),
        "score": score,
        "estimated_first_quality": est_q1,
        "block_llr": llr,
        "block_nonzero": nonzero,
    }


ADJPG_THRESHOLD = 0.05  # calibrated on the validation corpus; see forensic/validation


@register(
    "tamper.double_jpeg",
    name="Aligned double JPEG (A-DJPG)",
    version="1.0.0",
    category="Local analysis",
    description="Tests whether the JPEG was compressed twice on the same 8x8 grid (decoded and re-saved), "
    "estimates the first quality, and maps, block by block, how likely each area is to share that history. "
    "In an image that was re-saved, a region pasted in from another source typically lacks the "
    "double-compression trace and shows up bright in the map.",
    reference="T. Bianchi and A. Piva, 'Image Forgery Localization via Block-Grained Analysis of JPEG Artifacts', "
    "IEEE Trans. Information Forensics and Security 7(3), 2012; Z. Lin, J. He, X. Tang and C.-K. Tang, "
    "'Fast, automatic and fine-grained tampered JPEG image detection via DCT coefficient analysis', "
    "Pattern Recognition 42(11), 2009.",
    applies_to=("image",),
    reading_guide="The map is only meaningful when the global test finds double compression. Bright blocks "
    "are less consistent with the double-compression history of the rest of the image. Flat areas (sky, "
    "walls) carry little information and can look bright in authentic images; look for a compact bright "
    "region over textured content. Undetectable when the first quality was higher than the second, or "
    "when the image was resized or cropped off-grid between saves (use the non-aligned grid analysis).",
    params=[Param("frequencies", "int", 15, "DCT frequencies used", 3, 30, 1)],
)
def double_jpeg(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    res = adjpg(ctx.data, n_freq=p["frequencies"])
    llr = res.pop("block_llr")
    res.pop("block_nonzero")
    double = res["score"] >= ADJPG_THRESHOLD and res["supporting_frequencies"] >= 2
    flags = []
    prob_single = 1.0 / (1.0 + np.exp(np.clip(cv2.blur(llr, (3, 3), borderType=cv2.BORDER_REFLECT), -30, 30)))
    tampered_share = None
    if double:
        tampered_share = float(np.mean(prob_single > 0.8))
        q = res["estimated_first_quality"]
        flags.append({"level": "info", "code": "djpg.double",
                      "text": "Consistent with aligned double JPEG compression (the file was decoded and saved again)"
                      + (f"; first save around quality {q}." if q else ".")})
        if 0.01 <= tampered_share <= 0.4:
            flags.append({"level": "warning", "code": "djpg.local",
                          "text": f"{tampered_share:.1%} of blocks are inconsistent with the double-compression history "
                          "of the rest of the image; inspect the map for a compact region."})
    h, w = ctx.image.shape[:2]
    heat = cv2.resize(prob_single.astype(np.float32), (prob_single.shape[1] * 8, prob_single.shape[0] * 8), interpolation=cv2.INTER_NEAREST)
    full = np.zeros((h, w), np.float32)
    full[: heat.shape[0], : heat.shape[1]] = heat
    findings = {
        "double_compressed": bool(double),
        "score": round(res["score"], 5),
        "threshold": ADJPG_THRESHOLD,
        "supporting_frequencies": res["supporting_frequencies"],
        "estimated_first_quality": res["estimated_first_quality"],
        "inconsistent_block_share": None if tampered_share is None else round(tampered_share, 4),
        "frequencies": res["frequencies"],
    }
    summary = ("Consistent with double compression" if double else "No aligned double-compression trace found") + f" (score {res['score']:.4f}, detection at {ADJPG_THRESHOLD})."
    return AnalysisResult(summary=summary, findings=findings, flags=flags,
                          maps={"single_compression_probability": _colormap(full) if double else _colormap(full * 0)})


_HF = np.add.outer(np.arange(8), np.arange(8)) >= 4  # mid and high frequencies


def shift_energy(y: np.ndarray) -> np.ndarray:
    """Per-block mean |DCT| over mid/high frequencies on each of the 64 grid offsets.

    Returns an array (8, 8, bh, bw): offsets (dy, dx), then blocks. A grid on which the
    image was once quantised has many coefficients pushed to zero, so its energy dips.
    """
    bh, bw = (y.shape[0] - 7) // 8, (y.shape[1] - 7) // 8
    out = np.empty((8, 8, bh, bw), np.float64)
    for dy in range(8):
        for dx in range(8):
            c = block_dct(y, dy, dx)[:bh, :bw]
            out[dy, dx] = np.abs(c[:, :, _HF]).mean(axis=2)
    return out


# Offsets in the same row or column as the final grid share half its alignment and dip more
# in authentic images, so they need a stronger dip. Both calibrated on the validation corpus.
NA_THRESHOLD_OFF_AXIS = 10.0
NA_THRESHOLD_ON_AXIS = 30.0


def grid_residual(E: np.ndarray) -> tuple[tuple[int, int], float, float]:
    """Find a secondary grid in an 8x8 energy table.

    Shifts that share rows or columns with the final grid have lower energy anyway, so an
    additive row + column model of log-energy (median polish, ignoring the final grid at
    (0, 0)) is removed first; a first-compression grid then stands out as an isolated dip.
    Returns ((dx, dy), robust z-score of the dip, score) where score >= 1 means detected:
    the z-score divided by the threshold for its kind of offset (on or off the final grid's axes).
    """
    L = np.log(np.maximum(E, 1e-9))
    r, c = np.zeros(8), np.zeros(8)
    for _ in range(6):
        r = np.array([np.median((L[i] - c)[1:] if i == 0 else L[i] - c) for i in range(8)])
        c = np.array([np.median((L[:, j] - r)[1:] if j == 0 else L[:, j] - r) for j in range(8)])
    R = (L - r[:, None] - c[None, :]).ravel()
    best = None
    for on_axis, thr in ((False, NA_THRESHOLD_OFF_AXIS), (True, NA_THRESHOLD_ON_AXIS)):
        cand = [k for k in range(1, 64) if ((k // 8 == 0) or (k % 8 == 0)) == on_axis]
        k = min(cand, key=lambda j: R[j])
        rest = np.delete(R, [0, k])
        med = float(np.median(rest))
        mad = float(np.median(np.abs(rest - med))) * 1.4826 + 1e-9
        z = (med - float(R[k])) / mad
        if best is None or z / thr > best[2]:
            dy, dx = divmod(k, 8)
            best = ((dx, dy), z, z / thr)
    return best


def local_grid(E: np.ndarray, window: int, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Secondary-grid score of sliding windows (half-window step), and a full-size map of it.

    The map paints each window's score, halved and clipped to [0, 1], on its centre cell.
    """
    wb = max(4, window // 8)
    step = max(1, wb // 2)
    bh, bw = E.shape[2:]
    rows, cols = max(1, (bh - wb) // step + 1), max(1, (bw - wb) // step + 1)
    local = np.zeros((rows, cols), np.float32)
    for i in range(rows):
        for j in range(cols):
            sub = E[:, :, i * step : i * step + wb, j * step : j * step + wb].mean(axis=(2, 3))
            local[i, j] = grid_residual(sub)[2]
    h, w = shape
    heat = np.zeros((h, w), np.float32)
    cell = cv2.resize(np.clip(local / 2, 0, 1), (cols * step * 8, rows * step * 8), interpolation=cv2.INTER_NEAREST)
    o = (wb - step) * 4
    hh, ww = min(cell.shape[0], h - o), min(cell.shape[1], w - o)
    heat[o : o + hh, o : o + ww] = cell[:hh, :ww]
    return local, heat


@register(
    "tamper.nonaligned_grid",
    name="Non-aligned JPEG grid (NA-DJPG)",
    version="1.0.0",
    category="Local analysis",
    description="Takes the 8x8 DCT of the image on all 64 grid offsets and measures how much mid- and "
    "high-frequency energy each leaves. A grid on which the image was once compressed leaves an energy dip. "
    "A dip at an offset other than the final grid is consistent with the image (or part of it) having been "
    "compressed before, then cropped or pasted off-grid and saved again. The map shows that dip window by window.",
    reference="W. Luo, Z. Qu, J. Huang and G. Qiu, 'A Novel Method for Detecting Cropped and Recompressed Image "
    "Block', IEEE ICASSP, 2007; T. Bianchi and A. Piva, 'Detection of Nonaligned Double JPEG Compression Based "
    "on Integer Periodicity Maps', IEEE Trans. Information Forensics and Security 7(2), 2012.",
    applies_to=("image",),
    reading_guide="A secondary grid over the whole image suggests the picture was cropped from a JPEG and "
    "re-saved. A secondary grid confined to one region suggests that region came from another JPEG. Strong "
    "periodic content (brick, fences, text) can mimic a grid, and the test weakens when the first save was "
    "at high quality. Offsets of exactly half a block (4 px) are the most prone to false alarms.",
    params=[Param("window", "int", 96, "Window size (px)", 48, 256, 8)],
)
def nonaligned_grid(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if not is_jpeg(ctx.data):
        raise ValueError("non-aligned grid analysis needs a JPEG file")
    y = read_luma(ctx.data)
    E = shift_energy(y)
    if E.shape[2] < 4 or E.shape[3] < 4:
        raise ValueError("image is too small for grid analysis")
    offset, z, score = grid_residual(E.mean(axis=(2, 3)))
    detected = score >= 1.0
    local, heat = local_grid(E, p["window"], y.shape)
    share = float(np.mean(local >= 1.0))
    flags = []
    if detected:
        flags.append({"level": "warning", "code": "nadjpg.global",
                      "text": f"A secondary JPEG grid at offset {offset} (x, y) is present; consistent with cropping "
                      "or re-saving after an earlier compression on a different grid."})
    elif 0.02 <= share <= 0.5:
        flags.append({"level": "warning", "code": "nadjpg.local",
                      "text": f"{share:.0%} of windows show a secondary JPEG grid; inspect the map for a compact region."})
    findings = {
        "secondary_grid": bool(detected),
        "offset_xy": list(offset),
        "z_score": round(z, 2),
        "score": round(score, 3),
        "threshold": 1.0,
        "z_thresholds": {"off_axis": NA_THRESHOLD_OFF_AXIS, "on_axis": NA_THRESHOLD_ON_AXIS},
        "local_window_share": round(share, 4),
    }
    summary = (f"Secondary grid found at offset {offset}" if detected else "No secondary JPEG grid found") + f" (score {score:.2f}, detection at 1.00)."
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps={"secondary_grid_strength": _colormap(heat)})
