"""Validation of the clone (copy-move) detector and the noise level map.

Same rules as the other experiments: fixed seeds and generated ground truth. Each image from
the source (synthetic scenes, or real photographs with --pristine) is manipulated in a known
place, and the figures show how often the method finds it and how often it raises a false
alarm on the untouched image. They are not real-world accuracy; the cards say so.
"""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np

from ..analysis import ANALYSES
from .run import decode, jpeg, rate


def _run(aid: str, u8: np.ndarray, **params):
    a = ANALYSES[aid]
    return a.fn(SimpleNamespace(image=u8.astype(np.float32) / 255.0), a.validate(params))


def _place(rng, h, w, size, avoid=None):
    for _ in range(200):
        y, x = int(rng.integers(0, h - size)), int(rng.integers(0, w - size))
        if avoid is None or max(abs(y - avoid[0]), abs(x - avoid[1])) > 1.5 * size:
            return y, x
    return None


def _overlap(found: list[dict], truth: tuple[int, int, int, int], key: str) -> bool:
    """Does any reported region's box cover at least 30% of the true box?"""
    tx, ty, tw, th = truth
    for r in found:
        x, y, w, h = r[key]
        ix = max(0, min(x + w, tx + tw) - max(x, tx))
        iy = max(0, min(y + h, ty + th) - max(y, ty))
        if ix * iy >= 0.3 * tw * th:
            return True
    return False


def eval_copy_move(src, n: int) -> dict:
    conds = ("moved", "moved, then JPEG 85", "rotated 15 deg and scaled 1.1", "rotated and scaled, then JPEG 90")
    hits = {c: [] for c in conds}
    shift_ok, fp_plain, fp_jpeg = [], [], []
    for i in range(n):
        rng = np.random.default_rng(9000 + i)
        img = src(9000 + i)
        h, w = img.shape[:2]
        size = int(min(h, w) * 0.18) // 2 * 2
        s = _place(rng, h, w, size)
        d = _place(rng, h, w, size, avoid=s)
        if s is None or d is None:
            continue
        (sy, sx), (dy, dx) = s, d
        moved = img.copy()
        moved[dy : dy + size, dx : dx + size] = img[sy : sy + size, sx : sx + size]
        big = int(size * 1.6)
        cy, cx = min(max(sy + size // 2, big // 2), h - big // 2), min(max(sx + size // 2, big // 2), w - big // 2)
        patch = img[cy - big // 2 : cy - big // 2 + big, cx - big // 2 : cx - big // 2 + big]
        M = cv2.getRotationMatrix2D((big / 2, big / 2), 15, 1.1)
        rp = cv2.warpAffine(patch, M, (big, big), flags=cv2.INTER_CUBIC)
        o = (big - size) // 2
        rotated = img.copy()
        rotated[dy : dy + size, dx : dx + size] = rp[o : o + size, o : o + size]
        cases = {"moved": moved, "moved, then JPEG 85": decode(jpeg(moved, 85)),
                 "rotated 15 deg and scaled 1.1": rotated, "rotated and scaled, then JPEG 90": decode(jpeg(rotated, 90))}
        for c, im in cases.items():
            copies = _run("tamper.copy_move", im).findings["copies"]
            boxes = [{"b": cp["bbox_a_px"]} for cp in copies] + [{"b": cp["bbox_b_px"]} for cp in copies]
            hit = _overlap(boxes, (dx, dy, size, size), "b")
            hits[c].append(hit)
            if c == "moved" and hit:
                d_true = (dx - sx, dy - sy)
                shift_ok.append(any(min(np.hypot(cp["shift_px"][0] - d_true[0], cp["shift_px"][1] - d_true[1]),
                                        np.hypot(cp["shift_px"][0] + d_true[0], cp["shift_px"][1] + d_true[1])) < 3
                                    for cp in copies))
        fp_plain.append(bool(_run("tamper.copy_move", img).findings["copies"]))
        fp_jpeg.append(bool(_run("tamper.copy_move", decode(jpeg(img, 85))).findings["copies"]))
    return {
        "task": "Was an area of the image copied to another place in it (square of 18% of the short side)?",
        "positives": f"{len(hits['moved'])} images, each manipulated four ways (see true_positive_rate_by_condition)",
        "negatives": f"the same {len(fp_plain)} images untouched, as given and re-saved at JPEG 85",
        "detected_means": "the box around one side of a reported copy covers at least 30% of the pasted square",
        "true_positive_rate_by_condition": {c: rate(v) for c, v in hits.items()},
        "false_positive_rate": rate(fp_plain),
        "false_positive_rate_after_jpeg85": rate(fp_jpeg),
        "shift_within_3px_when_found": rate(shift_ok),
    }


def eval_noise_map(src, n: int) -> dict:
    conds = ("smoothed (Gaussian blur, sigma 1.2 px)", "noisier (added noise, sigma 6 grey levels)")
    hits = {c: [] for c in conds}
    fp_plain, fp_jpeg, ratios_s, ratios_n = [], [], [], []
    for i in range(n):
        rng = np.random.default_rng(9500 + i)
        img = src(9500 + i)
        h, w = img.shape[:2]
        size = int(min(h, w) * 0.3) // 8 * 8
        y, x = _place(rng, h, w, size)
        truth = (x, y, size, size)
        sm = img.copy()
        sm[y : y + size, x : x + size] = cv2.GaussianBlur(img, (0, 0), 1.2)[y : y + size, x : x + size]
        no = img.astype(np.float32)
        no[y : y + size, x : x + size] += rng.normal(0, 6.0, (size, size, 3))
        no = np.clip(np.rint(no), 0, 255).astype(np.uint8)
        for c, im, store in ((conds[0], sm, ratios_s), (conds[1], no, ratios_n)):
            regs = _run("tamper.noise_map", im).findings["regions"]
            hit = _overlap(regs, truth, "bbox_px")
            hits[c].append(hit)
            if hit:
                store.append(max((r["noise_ratio"] for r in regs), key=lambda v: abs(np.log(v))))
        fp_plain.append(bool(_run("tamper.noise_map", img).findings["regions"]))
        fp_jpeg.append(bool(_run("tamper.noise_map", decode(jpeg(img, 90))).findings["regions"]))
    return {
        "task": "Does an area (square of 30% of the short side) have noise unlike the rest of the image?",
        "positives": f"{len(fp_plain)} images, each altered two ways (see true_positive_rate_by_condition)",
        "negatives": f"the same {len(fp_plain)} images untouched, as given and re-saved at JPEG 90",
        "detected_means": "an outlined area covers at least 30% of the altered square, at the default ratios (below 0.2x or above 8x)",
        "true_positive_rate_by_condition": {c: rate(v) for c, v in hits.items()},
        "false_positive_rate": rate(fp_plain),
        "false_positive_rate_after_jpeg90": rate(fp_jpeg),
        "median_ratio_when_found": {"smoothed": round(float(np.median(ratios_s)), 2) if ratios_s else None,
                                    "noisier": round(float(np.median(ratios_n)), 2) if ratios_n else None},
    }


def run_phase4(src, quick: bool, log=print) -> dict:
    n = 6 if quick else 60
    log(f"Copy-move detection ({n} images x 4 manipulations)…")
    cm = eval_copy_move(src, n)
    log(f"Noise level map ({n} images x 2 alterations)…")
    nm = eval_noise_map(src, n)
    return {"copy_move": cm, "noise_map": nm}


def phase4_cards(results: dict) -> dict[str, dict]:
    return {"tamper.copy_move": {"detection": results["copy_move"]}, "tamper.noise_map": {"detection": results["noise_map"]}}


FAILURE_MODES = {
    "tamper.copy_move": [
        "Repeated patterns in the scene (tiles, windows, fences, text, logos, symmetric objects) produce genuine matches that are not edits.",
        "Copies into or from flat, textureless areas (sky, walls) carry too few features to be found.",
        "Strong blur, noise or low-quality JPEG applied to the copy, or a copy smaller than about 30 x 30 pixels, is usually missed.",
        "Only copies within one image are found: an area pasted from another photograph needs the noise map, ELA or double-JPEG analyses.",
    ],
    "tamper.noise_map": [
        "Noise varies naturally with focus (out-of-focus backgrounds are smoother), texture and the camera's tone curve; 2-3x differences are common in authentic photographs.",
        "JPEG compression removes noise in flat areas first, so after low-quality saving flat areas look 'clean' and can be outlined.",
        "Small areas (under about three windows across) and areas that were given matching noise after editing are not found.",
        "Added noise of a strength similar to the camera's own is not distinguishable.",
    ],
}
