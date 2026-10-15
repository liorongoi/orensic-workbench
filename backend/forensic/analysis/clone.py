"""Copy-move (clone) detection: a region copied from one part of an image to another.

Two complementary detectors, as examiners use them:

- keypoints: SIFT features matched against the image itself with the generalised
  2-nearest-neighbour test, grouped by RANSAC into affine transforms (so a copy that was
  rotated or rescaled is still found), then each transform is checked densely to outline
  the copied regions. Strong on textured regions; finds nothing on flat ones.
- blocks: overlapping blocks described by their low-frequency DCT coefficients, sorted so
  near-identical blocks sit next to each other, with a vote over the offset between
  matching blocks. Finds plain (moved, not rotated) copies in smooth regions too.

Every match is shown with the transform it implies; nothing is reported as proof.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..filters.base import Param, luminance, u8
from .base import AnalysisContext, AnalysisResult, register

MAX_SIDE = 2048  # larger images are analysed at this size; transforms are reported in original pixels


def _gray_u8(img: np.ndarray) -> tuple[np.ndarray, float]:
    g = luminance(img)
    s = min(1.0, MAX_SIDE / max(g.shape))
    if s < 1.0:
        g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return u8(g), s


def _zncc(a: np.ndarray, b: np.ndarray, k: int = 7) -> np.ndarray:
    """Local zero-mean normalised cross-correlation of two float images in a k x k window."""
    box = lambda x: cv2.blur(x, (k, k), borderType=cv2.BORDER_REFLECT)  # noqa: E731
    ma, mb = box(a), box(b)
    va, vb = box(a * a) - ma * ma, box(b * b) - mb * mb
    cov = box(a * b) - ma * mb
    return cov / np.sqrt(np.maximum(va * vb, 1e-10))


def _dense_regions(g: np.ndarray, M: np.ndarray, min_dist: float, min_area: int, seeds: np.ndarray | None):
    """Pixels x where image(x) matches image(M x): the target region and, through M, its source."""
    h, w = g.shape
    f = g.astype(np.float32) / 255.0
    warped = cv2.warpAffine(f, M, (w, h), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP, borderValue=-1.0)
    valid = cv2.warpAffine(np.ones_like(f), M, (w, h), flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP, borderValue=0) > 0
    corr = _zncc(f, warped)
    # Texture matters: two flat patches always correlate weakly, so also require some local contrast.
    contrast = cv2.blur((f - cv2.blur(f, (7, 7))) ** 2, (7, 7))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = M[0, 0] * xx + M[0, 1] * yy + M[0, 2]
    sy = M[1, 0] * xx + M[1, 1] * yy + M[1, 2]
    far = np.hypot(sx - xx, sy - yy) >= min_dist
    mask = ((corr > 0.85) & valid & far & (contrast > 2e-5)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        if seeds is not None and len(seeds):
            sx_i = np.clip(seeds[:, 0].astype(int), 0, w - 1)
            sy_i = np.clip(seeds[:, 1].astype(int), 0, h - 1)
            if not np.any(lab[sy_i, sx_i] == i):
                continue
        keep[i] = True
    target = keep[lab]
    src = cv2.warpAffine(target.astype(np.uint8), np.linalg.inv(np.vstack([M, [0, 0, 1]]))[:2], (w, h),
                         flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP) > 0
    return target, src


def _bbox(mask: np.ndarray, scale: float) -> list[int]:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return [0, 0, 0, 0]
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    return [int(x0 / scale), int(y0 / scale), int((x1 - x0) / scale), int((y1 - y0) / scale)]


def _describe(M: np.ndarray, scale: float) -> dict:
    A = M[:, :2]
    sx, sy = np.hypot(A[0, 0], A[1, 0]), np.hypot(A[0, 1], A[1, 1])
    return {
        "rotation_deg": round(float(np.degrees(np.arctan2(A[1, 0], A[0, 0]))), 1),
        "scale": round(float((sx + sy) / 2), 3),
        "shift_px": [round(float(M[0, 2] / scale), 1), round(float(M[1, 2] / scale), 1)],
    }


def keypoint_matches(g: np.ndarray, min_dist: float, ratio: float = 0.5, max_kp: int = 20000):
    """Self-matches of SIFT keypoints under the g2NN test (Amerini et al. 2011): pairs (p, q)."""
    sift = cv2.SIFT_create(nfeatures=max_kp, contrastThreshold=0.02)
    kps, des = sift.detectAndCompute(g, None)
    if des is None or len(kps) < 3:
        return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32), len(kps or [])
    pts = np.float32([k.pt for k in kps])
    k = min(10, len(kps))
    knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des, des, k=k)
    P, Q, seen = [], [], set()
    for i, ms in enumerate(knn):
        ms = [m for m in ms if m.trainIdx != i]
        for j in range(len(ms) - 1):
            if ms[j].distance >= ratio * ms[j + 1].distance:
                break
            t = ms[j].trainIdx
            if np.hypot(*(pts[i] - pts[t])) < min_dist:
                continue
            a, b = (i, t) if tuple(pts[i]) < tuple(pts[t]) else (t, i)
            if (a, b) not in seen:
                seen.add((a, b))
                P.append(pts[a])
                Q.append(pts[b])
    return np.float32(P).reshape(-1, 2), np.float32(Q).reshape(-1, 2), len(kps)


def keypoint_transforms(P: np.ndarray, Q: np.ndarray, min_inliers: int, max_clusters: int = 6):
    """Affine transforms Q ~ M P found one at a time by RANSAC, removing each one's inliers."""
    out = []
    P, Q = P.copy(), Q.copy()
    for _ in range(max_clusters):
        if len(P) < min_inliers:
            break
        M, inl = cv2.estimateAffine2D(P, Q, method=cv2.RANSAC, ransacReprojThreshold=3.0, maxIters=4000,
                                      confidence=0.995, refineIters=10)
        if M is None:
            break
        inl = inl.ravel().astype(bool)
        if inl.sum() < min_inliers:
            break
        det = np.linalg.det(M[:, :2])
        if 0.25 < abs(det) < 4.0:  # implausible stretches are coincidences, not copies
            out.append((M, P[inl], Q[inl]))
        P, Q = P[~inl], Q[~inl]
    return out


def block_matches(g: np.ndarray, block: int, min_dist: float, min_votes: int, stride: int = 2):
    """Offsets shared by many near-identical blocks (Fridrich et al. 2003, with DCT features)."""
    f = g.astype(np.float32)
    # Low-frequency 2D DCT coefficients of every block, computed for all positions at once
    # by correlating with each DCT basis function (16 features).
    n = np.arange(block)
    basis = lambda u: np.cos(np.pi * (2 * n + 1) * u / (2 * block)).astype(np.float32)  # noqa: E731
    feats = []
    for u in range(4):
        for v in range(4):
            ker = np.outer(basis(u), basis(v)) / block
            r = cv2.filter2D(f, cv2.CV_32F, ker, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT)
            feats.append(r[: f.shape[0] - block + 1 : stride, : f.shape[1] - block + 1 : stride])
    F = np.stack(feats, -1)
    hh, ww = F.shape[:2]
    ys, xs = np.mgrid[0:hh, 0:ww]
    ys, xs = (ys * stride).ravel(), (xs * stride).ravel()
    F = F.reshape(-1, 16)
    # Skip near-flat blocks: they all look alike and only produce false matches.
    std = cv2.blur(f * f, (block, block)) - cv2.blur(f, (block, block)) ** 2
    std = np.sqrt(np.maximum(std, 0))[block // 2 :: stride, block // 2 :: stride][:hh, :ww].ravel()
    ok = std > 4.0
    F, ys, xs = F[ok], ys[ok], xs[ok]
    if len(F) < 2:
        return [], (hh, ww)
    # Sort on coarsely quantised features so similar blocks end up close together, then compare
    # each block with its next few neighbours on the exact features (robust to recompression).
    Qf = np.rint(F / np.array([16.0] + [8.0] * 15, np.float32)).astype(np.int32)
    order = np.lexsort(Qf.T[::-1])
    Fs, Ys, Xs = F[order], ys[order], xs[order]
    votes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for lag in range(1, 9):
        same = np.linalg.norm(Fs[lag:] - Fs[:-lag], axis=1) < 5.0
        idx = np.nonzero(same)[0]
        dx = Xs[idx + lag] - Xs[idx]
        dy = Ys[idx + lag] - Ys[idx]
        far = np.hypot(dx, dy) >= min_dist
        idx, dx, dy = idx[far], dx[far], dy[far]
        flip = (dx < 0) | ((dx == 0) & (dy < 0))
        a = np.where(flip, idx + lag, idx)
        dx, dy = np.where(flip, -dx, dx), np.where(flip, -dy, dy)
        for i, x, y in zip(a, dx, dy):
            votes.setdefault((int(x), int(y)), []).append((int(Xs[i]), int(Ys[i])))
    # Neighbouring offsets (one pixel apart) are the same copy seen through the stride and noise.
    merged: dict[tuple[int, int], list] = {}
    for off in sorted(votes, key=lambda o: -len(votes[o])):
        near = next((m for m in merged if abs(m[0] - off[0]) <= stride and abs(m[1] - off[1]) <= stride), None)
        merged.setdefault(near or off, []).extend(votes[off])
    return [(off, pos) for off, pos in merged.items() if len(pos) >= min_votes], (hh, ww)


def _overlay(img: np.ndarray, regions: list[tuple[np.ndarray, np.ndarray]], lines: list[tuple], scale: float) -> np.ndarray:
    h, w = img.shape[:2]
    base = (np.clip(luminance(img), 0, 1) * 0.6 + 0.2)[..., None].repeat(3, axis=2)
    colours = [(1.0, 0.25, 0.2), (0.2, 0.6, 1.0), (1.0, 0.8, 0.1), (0.3, 0.9, 0.4), (0.9, 0.3, 0.9), (0.2, 0.9, 0.9)]
    out = base.copy()
    for k, (a, b) in enumerate(regions):
        c = np.array(colours[k % len(colours)], np.float32)
        for m, alpha in ((a, 0.55), (b, 0.35)):
            if scale < 1:
                m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
            out[m] = out[m] * (1 - alpha) + c * alpha
    canvas = u8(out)
    for k, (p, q) in enumerate(lines):
        c = tuple(int(255 * v) for v in colours[k % len(colours)])
        for (x0, y0), (x1, y1) in zip(p, q):
            cv2.line(canvas, (int(x0 / scale), int(y0 / scale)), (int(x1 / scale), int(y1 / scale)), c, 1, cv2.LINE_AA)
            cv2.circle(canvas, (int(x0 / scale), int(y0 / scale)), 2, c, -1)
    return canvas.astype(np.float32) / 255.0


@register(
    "tamper.copy_move",
    name="Clone (copy-move) detection",
    version="1.0.0",
    category="Local analysis",
    description="Looks for a part of the image that was copied to another place in the same image, for example "
    "to hide an object or duplicate one. Keypoint matching (SIFT) finds copies even when they were rotated or "
    "rescaled; block matching (DCT features) also finds plain copies in smooth areas. Each copy found is outlined "
    "in colour (strong colour: one side, light colour: the other) with lines joining matching points, and the "
    "rotation, scale and shift between the two areas are listed.",
    reference="I. Amerini, L. Ballan, R. Caldelli, A. Del Bimbo and G. Serra, 'A SIFT-Based Forensic Method for "
    "Copy-Move Attack Detection and Transformation Recovery', IEEE TIFS 6(3), 2011; J. Fridrich, D. Soukal and "
    "J. Lukas, 'Detection of Copy-Move Forgery in Digital Images', DFRWS 2003; V. Christlein, C. Riess, J. Jordan, "
    "C. Riess and E. Angelopoulou, 'An Evaluation of Popular Copy-Move Forgery Detection Approaches', IEEE TIFS 7(6), 2012.",
    applies_to=("image",),
    reading_guide="A match means two areas are near-identical, which is consistent with copy-move but also occurs "
    "naturally with repeated patterns: tiles, windows, fences, printed text, logos and symmetric objects. Judge "
    "each match against the scene. No match does not show the image is unedited: small, heavily blurred or "
    "flat copies and copies taken from another image are not detected.",
    params=[
        Param("method", "choice", "both", "Method", choices=("both", "keypoints", "blocks")),
        Param("min_distance", "float", 40.0, "Minimum distance between copies (px)", 8.0, 500.0, 1.0,
              help="Matches closer than this are ignored (neighbouring pixels are always similar)"),
        Param("min_matches", "int", 8, "Keypoint matches needed per copy", 4, 100, 1),
        Param("block", "int", 16, "Block size (px)", 8, 32, 4),
        Param("min_block_votes", "int", 60, "Matching blocks needed per copy", 10, 2000, 5),
    ],
)
def copy_move(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    img = ctx.image
    g, s = _gray_u8(img)
    min_dist = p["min_distance"] * s
    min_area = max(64, int((p["block"] * s) ** 2))
    copies, regions, lines = [], [], []
    stats = {}
    if p["method"] in ("both", "keypoints"):
        P, Q, n_kp = keypoint_matches(g, min_dist)
        stats |= {"keypoints": n_kp, "keypoint_matches": int(len(P))}
        for M, pi, qi in keypoint_transforms(P, Q, p["min_matches"]):
            tgt, src = _dense_regions(g, np.linalg.inv(np.vstack([M, [0, 0, 1]]))[:2], min_dist, min_area, qi)
            area = int(tgt.sum() / (s * s))
            copies.append({"method": "keypoints", "matches": int(len(pi)), "area_px": area,
                           "bbox_a_px": _bbox(src, s), "bbox_b_px": _bbox(tgt, s)} | _describe(M, s))
            regions.append((tgt, src))
            sel = np.linspace(0, len(pi) - 1, min(len(pi), 60)).astype(int)
            lines.append((pi[sel], qi[sel]))
    if p["method"] in ("both", "blocks"):
        bsz = max(8, int(round(p["block"] * s)))
        found, _ = block_matches(g, bsz, min_dist, p["min_block_votes"])
        for (dx, dy), pos in sorted(found, key=lambda t: -len(t[1]))[:6]:
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            if any(c["method"] == "keypoints" and abs(c["rotation_deg"]) < 2 and abs(c["scale"] - 1) < 0.03
                   and np.hypot(c["shift_px"][0] * s - dx, c["shift_px"][1] * s - dy) < 4 for c in copies):
                continue  # already found by the keypoints
            seeds = np.float32([(x + dx + bsz / 2, y + dy + bsz / 2) for x, y in pos])
            tgt, src = _dense_regions(g, np.float32([[1, 0, -dx], [0, 1, -dy]]), min_dist, min_area, seeds)
            if not tgt.any():
                continue
            copies.append({"method": "blocks", "matching_blocks": len(pos), "area_px": int(tgt.sum() / (s * s)),
                           "bbox_a_px": _bbox(src, s), "bbox_b_px": _bbox(tgt, s)} | _describe(M, s))
            regions.append((tgt, src))
            lines.append(([], []))
    copies = [c for c in copies if c["area_px"] > 0]
    regions = [r for r in regions if r[0].any()]
    flags = []
    if copies:
        flags.append({"level": "warning", "code": "clone.match", "text": f"{len(copies)} pair(s) of near-identical areas found, consistent with "
                      "copy-move; check each against repeated patterns in the scene before drawing any conclusion."})
    summary = (f"{len(copies)} copied area pair(s) found." if copies else "No copied areas found.") + (
        f" Analysed at {g.shape[1]}x{g.shape[0]}." if s < 1 else "")
    return AnalysisResult(
        summary=summary,
        findings=stats | {"copies": copies, "analysed_scale": round(s, 4)},
        flags=flags,
        maps={"copy_move": _overlay(img, regions, lines, s)},
    )
