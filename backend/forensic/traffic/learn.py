"""Training from the examiner's corrections.

Ground truth comes only from images the examiner reviewed (edited or approved): the active
detections left on such an image are taken as the complete list of vehicles in its regions.
Every fused object the ensemble produced for that image (kept or not, so including the ones below
threshold) is labelled:

- positive if it overlaps a final detection (IoU >= 0.5);
- negative otherwise (a deleted false alarm, or a weak candidate the examiner did not add).

A vehicle the examiner added by hand with no fused object anywhere near it is an *unrecoverable
miss*: no member proposed it, so re-scoring cannot find it. Those are counted, kept with their crops,
and exported for detector fine-tuning (dataset export), which needs a GPU training run outside this
application.

What is trained:
1. a logistic-regression re-scorer over the object features (ensemble.FEATURES), standardised,
   L2-regularised, fitted by Newton-Raphson (deterministic: same data -> same coefficients);
2. the decision threshold maximising F1 over image-grouped cross-validation predictions (the middle
   of the best range when several thresholds tie).

A new profile is adopted only when its cross-validated F1 is at least the current profile's F1 on the
same reviewed images; otherwise it is stored as `rejected`, the current one stays, and the examiner
is told why. With too few labels (fewer than MIN_POS positives or MIN_NEG negatives) only the
threshold over the fused score is tuned.

These metrics are measured on the examiner's own reviewed images; they are an internal consistency
check, not a validation of the detector for other cameras or conditions.
"""

from __future__ import annotations

import numpy as np

from .detectors import iou
from .ensemble import FEATURES

VERSION = "1.0.0"
MIN_POS = 8
MIN_NEG = 4
MATCH_IOU = 0.5
L2 = 1.0


def label_objects(objects: list[dict], finals: list[dict]) -> tuple[list[int], int]:
    """Label each in-region object 1/0 against the final detections; count finals with no object."""
    labels = []
    matched_final = set()
    for o in objects:
        best, bj = 0.0, -1
        for j, f in enumerate(finals):
            v = iou(o["box"], f["box"])
            if v > best:
                best, bj = v, j
        if best >= MATCH_IOU:
            labels.append(1)
            matched_final.add(bj)
        else:
            labels.append(0)
    return labels, len(finals) - len(matched_final)


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = L2, iters: int = 50) -> dict:
    mean = X.mean(0)
    scale = X.std(0)
    scale[scale < 1e-6] = 1.0
    Z = np.c_[(X - mean) / scale, np.ones(len(X))]
    w = np.zeros(Z.shape[1])
    R = np.eye(Z.shape[1]) * l2
    R[-1, -1] = 0.0  # the intercept is not penalised
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -40, 40)))
        g = Z.T @ (p - y) + R @ w
        Hs = (Z * (p * (1 - p))[:, None]).T @ Z + R + np.eye(len(w)) * 1e-9
        step = np.linalg.solve(Hs, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return {"kind": "logistic", "features": list(FEATURES), "mean": mean.round(8).tolist(),
            "scale": scale.round(8).tolist(), "coef": w[:-1].round(8).tolist(), "intercept": round(float(w[-1]), 8),
            "l2": l2}


def predict(model: dict, X: np.ndarray) -> np.ndarray:
    Z = (X - np.asarray(model["mean"])) / np.asarray(model["scale"])
    return 1 / (1 + np.exp(-np.clip(Z @ np.asarray(model["coef"]) + model["intercept"], -40, 40)))


def prf(scores: np.ndarray, y: np.ndarray, thr: float, unrecoverable: int) -> dict:
    pred = scores >= thr
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum()) + unrecoverable
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"threshold": round(float(thr), 4), "tp": tp, "fp": fp, "fn": fn, "precision": round(p, 4),
            "recall": round(r, 4), "f1": round(f, 4)}


def best_threshold(scores: np.ndarray, y: np.ndarray, unrecoverable: int) -> dict:
    """Threshold with the best F1 on a 0.05-0.95 grid; among equally good thresholds the middle one,
    which leaves the widest margin on both sides."""
    grid = np.round(np.arange(0.05, 0.951, 0.01), 2)
    ms = [prf(scores, y, float(t), unrecoverable) for t in grid]
    top = max(m["f1"] for m in ms)
    tied = [m for m in ms if abs(m["f1"] - top) <= 1e-12]
    return tied[(len(tied) - 1) // 2]


def train(samples: list[dict], current: dict) -> dict:
    """samples: [{"image": id, "features": [...], "fused": float, "score_current": float, "label": 0/1}],
    plus `unrecoverable` misses passed in `current["unrecoverable"]`. Returns the proposed profile
    parameters, metrics and a decision."""
    unrec = int(current.get("unrecoverable", 0))
    if not samples:
        return {"decision": "no_data", "reason": "no reviewed images with detector output to learn from"}
    X = np.array([s["features"] for s in samples], np.float64)
    y = np.array([s["label"] for s in samples], np.int64)
    imgs = [s["image"] for s in samples]
    fused = np.array([s["fused"] for s in samples])
    cur_scores = np.array([s["score_current"] for s in samples])
    baseline = prf(cur_scores, y, current["threshold"], unrec)
    n_pos, n_neg = int(y.sum()), int((y == 0).sum())
    uniq = sorted(set(imgs))
    out = {"samples": len(samples), "positives": n_pos, "negatives": n_neg, "images": len(uniq),
           "unrecoverable_misses": unrec, "baseline": baseline}
    if n_pos >= MIN_POS and n_neg >= MIN_NEG and len(uniq) >= 2:
        k = min(5, len(uniq))
        fold_of = {im: i % k for i, im in enumerate(uniq)}
        folds = np.array([fold_of[im] for im in imgs])
        cv = np.zeros(len(y))
        for f in range(k):
            tr, te = folds != f, folds == f
            if len(set(y[tr].tolist())) < 2:
                cv[te] = fused[te]
                continue
            cv[te] = predict(fit_logistic(X[tr], y[tr]), X[te])
        chosen = best_threshold(cv, y, unrec)
        model = fit_logistic(X, y)
        out |= {"method": "logistic re-scorer + threshold (image-grouped %d-fold CV)" % k, "rescorer": model,
                "threshold": chosen["threshold"], "cross_validated": chosen}
    else:
        chosen = best_threshold(fused, y, unrec)
        out |= {"method": "threshold on fused score (too few labels for a re-scorer: need %d positives and %d negatives)"
                % (MIN_POS, MIN_NEG), "rescorer": None, "threshold": chosen["threshold"], "cross_validated": chosen}
    better = chosen["f1"] >= baseline["f1"] - 1e-12
    out["decision"] = "adopted" if better else "rejected"
    if not better:
        out["reason"] = f"cross-validated F1 {chosen['f1']:.3f} is below the current profile's {baseline['f1']:.3f}"
    return out
