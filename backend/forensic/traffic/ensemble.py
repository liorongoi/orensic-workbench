"""Unifies the members' candidates into one list of vehicle detections.

1. Region filter: a candidate counts only if its anchor point (bottom centre of the box, where a
   vehicle meets the road) lies inside a scan area (when any is drawn) and outside every mask.
2. Clustering: candidates from all members overlapping by IoU >= `match_iou` (or a lamp box lying
   mostly inside a vehicle box) form one object.
3. Fusion: the box is the score-weighted mean of the cluster's boxes from the most reliable member
   present (learned detector > change > lamps); the score is a noisy-OR of member scores times
   member weights: 1 - prod(1 - w_m * s_m). Class is the best-scoring vehicle class in the cluster.
4. Optional learned re-scorer (trained from the examiner's corrections, see learn.py) replaces the
   fused score with a calibrated probability.
5. Detections with a final score >= `threshold` are reported.

All parameters live in a versioned profile; the default profile is DEFAULT_PROFILE.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from .detectors import iou

VERSION = "1.0.0"

DEFAULT_PROFILE = {
    "weights": {"yolox": 1.0, "lights": 0.7, "change": 0.45},
    "threshold": 0.4,
    "match_iou": 0.3,
    "member_min_score": 0.05,
    "rescorer": None,
}

PRIORITY = {"yolox": 0, "change": 1, "lights": 2}


# Regions --------------------------------------------------------------------------------------------


def region_mask(shape: tuple[int, int], regions: list[dict]) -> np.ndarray | None:
    """Boolean mask of where detection is allowed, or None when no region is drawn."""
    if not regions:
        return None
    H, W = shape
    include = [r for r in regions if r.get("kind") == "include"]
    exclude = [r for r in regions if r.get("kind") == "exclude"]
    m = np.zeros((H, W), np.uint8) if include else np.ones((H, W), np.uint8)
    for r in include:
        cv2.fillPoly(m, [_poly(r, W, H)], 1)
    for r in exclude:
        cv2.fillPoly(m, [_poly(r, W, H)], 0)
    return m.astype(bool)


def validate_regions(regions: list[dict]) -> list[dict]:
    out = []
    for r in regions or []:
        kind = r.get("kind")
        if kind not in ("include", "exclude"):
            raise ValueError("each region must be kind 'include' (scan area) or 'exclude' (mask)")
        pts = r.get("points") or []
        if len(pts) < 3:
            raise ValueError("a region needs at least three points")
        pts = [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in pts]
        out.append({"kind": kind, "label": str(r.get("label", ""))[:100], "points": pts})
    return out


def _poly(r: dict, W: int, H: int) -> np.ndarray:
    return np.array([[min(max(p[0], 0), W - 1), min(max(p[1], 0), H - 1)] for p in r["points"]], np.int32)


def anchor(box) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] * 0.92


def inside(mask: np.ndarray | None, box) -> bool:
    if mask is None:
        return True
    x, y = anchor(box)
    H, W = mask.shape
    return bool(mask[min(max(int(y), 0), H - 1), min(max(int(x), 0), W - 1)])


# Fusion ------------------------------------------------------------------------------------------------


def _contained(small, big, frac=0.6) -> bool:
    iw = max(0.0, min(small[0] + small[2], big[0] + big[2]) - max(small[0], big[0]))
    ih = max(0.0, min(small[1] + small[3], big[1] + big[3]) - max(small[1], big[1]))
    a = small[2] * small[3]
    return a > 0 and iw * ih / a >= frac


def _related(a: dict, b: dict, match_iou: float) -> bool:
    if iou(a["box"], b["box"]) >= match_iou:
        return True
    if a["member"] == "lights" and b["member"] != "lights":
        return _contained(a["attrs"]["lamps"][0] if a["attrs"].get("lamps") else a["box"], b["box"], 0.8)
    if b["member"] == "lights" and a["member"] != "lights":
        return _contained(b["attrs"]["lamps"][0] if b["attrs"].get("lamps") else b["box"], a["box"], 0.8)
    return False


def fuse(candidates: list[dict], profile: dict, mask: np.ndarray | None, context: dict) -> list[dict]:
    """Cluster, fuse, re-score and threshold. Returns every fused object with its final score and a
    `kept` flag, so that the record shows what was below threshold or outside the regions."""
    p = {**DEFAULT_PROFILE, **(profile or {})}
    w = p["weights"]
    cands = [c for c in candidates if c["score"] >= p["member_min_score"] and w.get(c["member"], 0) > 0]
    cands.sort(key=lambda c: (PRIORITY.get(c["member"], 9), -c["score"], c["box"][0], c["box"][1]))
    clusters: list[list[int]] = []
    owner = [-1] * len(cands)
    for i, c in enumerate(cands):
        best, best_k = 0.0, -1
        for k, cl in enumerate(clusters):
            for j in cl:
                if _related(c, cands[j], p["match_iou"]):
                    s = iou(c["box"], cands[j]["box"]) + 0.01
                    if s > best:
                        best, best_k = s, k
        if best_k < 0:
            clusters.append([i])
            owner[i] = len(clusters) - 1
        else:
            clusters[best_k].append(i)
            owner[i] = best_k
    out = []
    for cl in clusters:
        members = [cands[i] for i in cl]
        top = min(PRIORITY.get(m["member"], 9) for m in members)
        lead = [m for m in members if PRIORITY.get(m["member"], 9) == top]
        sw = sum(m["score"] for m in lead) or 1.0
        box = [round(sum(m["box"][k] * m["score"] for m in lead) / sw, 1) for k in range(4)]
        per_member: dict[str, float] = {}
        for m in members:
            per_member[m["member"]] = max(per_member.get(m["member"], 0.0), m["score"])
        fused = 1.0 - math.prod(1.0 - min(1.0, w.get(k, 0) * s) for k, s in per_member.items())
        vehicle = [m for m in members if m["member"] == "yolox"]
        cls = max(vehicle, key=lambda m: m["score"])["cls"] if vehicle else max(members, key=lambda m: m["score"])["cls"]
        obj = {"cls": cls, "box": box, "fused_score": round(fused, 4), "members": {k: round(v, 4) for k, v in sorted(per_member.items())},
               "n_candidates": len(members), "in_region": inside(mask, box),
               "lamps": [lb for m in members if m["member"] == "lights" for lb in m["attrs"].get("lamps", [])]}
        obj["features"] = features(obj, context)
        if p.get("rescorer"):
            obj["score"] = round(rescore(p["rescorer"], obj["features"]), 4)
        else:
            obj["score"] = obj["fused_score"]
        obj["kept"] = bool(obj["in_region"] and obj["score"] >= p["threshold"])
        out.append(obj)
    out.sort(key=lambda o: (-o["score"], o["box"][0], o["box"][1]))
    return out


# Learned re-scorer ----------------------------------------------------------------------------------------

FEATURES = ("yolox", "lights", "change", "fused", "n_members", "log_area", "aspect", "cx", "cy", "night",
            "box_brightness", "edge_density")


def features(obj: dict, ctx: dict) -> list[float]:
    """A fixed-length description of a fused object used by the learned re-scorer."""
    W, H = ctx["width"], ctx["height"]
    x, y, w, h = obj["box"]
    m = obj["members"]
    gray, edges = ctx.get("gray"), ctx.get("edges")
    xi0, yi0 = int(max(0, x)), int(max(0, y))
    xi1, yi1 = int(min(W, x + w)), int(min(H, y + h))
    if gray is not None and xi1 > xi0 and yi1 > yi0:
        bright = float(gray[yi0:yi1, xi0:xi1].mean()) / 255.0
        ed = float(edges[yi0:yi1, xi0:xi1].mean()) / 255.0
    else:
        bright = ed = 0.0
    return [m.get("yolox", 0.0), m.get("lights", 0.0), m.get("change", 0.0), obj["fused_score"], float(len(m)),
            math.log(max(1.0, w * h) / (W * H)) / 10.0, min(4.0, w / max(1.0, h)) / 4.0, (x + w / 2) / W,
            (y + h / 2) / H, 1.0 if ctx.get("night") else 0.0, bright, ed]


def context(bgr: np.ndarray, night: bool) -> dict:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return {"width": bgr.shape[1], "height": bgr.shape[0], "night": night, "gray": gray,
            "edges": cv2.Canny(gray, 60, 160)}


def rescore(model: dict, f: list[float]) -> float:
    x = (np.asarray(f, np.float64) - np.asarray(model["mean"])) / np.asarray(model["scale"])
    z = float(np.dot(model["coef"], x) + model["intercept"])
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))
