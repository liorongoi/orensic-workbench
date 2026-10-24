"""Ensemble members for vehicle detection in still images from fixed or mobile cameras.

Each member is independent, deterministic and returns candidates in source pixels:
`{"member", "cls", "score", "box": [x, y, w, h], "attrs": {...}}`.

- `yolox`: the bundled YOLOX COCO detector (Apache-2.0), run on the whole picture and, for
  long-range scenes, on overlapping full-resolution tiles so that small, distant vehicles reach the
  network at a usable size (slicing-aided inference, Akyon et al. 2022).
- `lights`: vehicles at night from their lamps. Bright compact blobs are found by thresholding;
  pairs of similar blobs side by side are a vehicle's headlights (white / yellow) or tail-lights
  (red). Blobs where the camera's learned background shows a light (street lamps, signs, windows)
  are discarded. A single unpaired lamp far from the camera is kept with a low score.
- `change`: day or night difference against the camera's learned background picture (per-pixel
  median of earlier images in the same lighting); compact changed regions are vehicle candidates.

Members propose; the ensemble (ensemble.py) decides.
"""

from __future__ import annotations

import cv2
import numpy as np

VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle", "bicycle")
LIGHT_CLASSES = ("vehicle_headlights", "vehicle_taillights", "vehicle_lamp")
MEMBERS = ("yolox", "lights", "change")

MEMBER_INFO = {
    "yolox": {"label": "Learned detector (YOLOX, COCO)", "version": "1.0.0",
              "reference": "Ge et al., 'YOLOX: Exceeding YOLO Series in 2021', arXiv:2107.08430; tiling after "
                           "Akyon et al., 'Slicing Aided Hyper Inference', ICIP 2022."},
    "lights": {"label": "Vehicle lamps (night)", "version": "1.0.0",
               "reference": "Blob detection and symmetric lamp pairing, after O'Malley, Jones and Glavin, "
                            "'Rear-lamp vehicle detection and tracking in low-exposure color video', IEEE T-ITS 2010."},
    "change": {"label": "Change against learned background", "version": "1.0.0",
               "reference": "Per-pixel temporal median background and robust (median + k*MAD) thresholding."},
}


# Learned detector -----------------------------------------------------------------------------------


def yolox_member(detector, bgr: np.ndarray, *, classes=VEHICLE_CLASSES, min_score: float = 0.05,
                 tile: bool | str = "auto", tile_overlap: float = 0.25) -> tuple[list[dict], dict]:
    """Whole-image pass plus overlapping tiles. Returns candidates and a description of the passes."""
    h, w = bgr.shape[:2]
    S = detector.size
    passes = [{"kind": "whole", "box": [0, 0, w, h]}]
    query = tuple(classes) + (("person",) if "person" not in classes else ())
    found = [dict(d, attrs={"pass": "whole"}) for d in detector.detect(bgr, query, min_score)]
    do_tile = tile is True or (tile == "auto" and max(h, w) > 1.4 * S)
    if do_tile:
        T = S if max(h, w) > 2 * S else max(S // 2, int(max(h, w) / 1.8))
        step = max(32, int(T * (1 - tile_overlap)))
        for y0 in _starts(h, T, step):
            for x0 in _starts(w, T, step):
                crop = bgr[y0:y0 + T, x0:x0 + T]
                passes.append({"kind": "tile", "box": [x0, y0, crop.shape[1], crop.shape[0]]})
                for d in detector.detect(crop, query, min_score):
                    bx = d["box"]
                    # Boxes cut by a tile edge are kept only from the pass that sees them whole.
                    if _touches_inner_edge(bx, crop.shape, x0, y0, w, h):
                        continue
                    found.append({**d, "box": [bx[0] + x0, bx[1] + y0, bx[2], bx[3]], "attrs": {"pass": "tile"}})
    # People are found too, only to stop the other members counting a pedestrian as a vehicle.
    people = [d["box"] for d in _nms([d for d in found if d["cls"] == "person"], 0.5) if d["score"] >= 0.4]
    out = _nms([d for d in found if d["cls"] != "person"], 0.5)
    for d in out:
        d["member"] = "yolox"
    return out, {"passes": len(passes), "tiled": do_tile, "input_size": S, "people": people}


def veto_people(cands: list[dict], people: list[list[float]]) -> tuple[list[dict], int]:
    """Drop change and lamp candidates that are mostly a detected person."""
    if not people:
        return cands, 0
    keep = []
    for c in cands:
        x, y, w, h = c["box"]
        hit = False
        for p in people:
            iw = max(0.0, min(x + w, p[0] + p[2]) - max(x, p[0]))
            ih = max(0.0, min(y + h, p[1] + p[3]) - max(y, p[1]))
            if iw * ih >= 0.5 * max(1.0, w * h) or iou(c["box"], p) >= 0.4:
                hit = True
                break
        if not hit:
            keep.append(c)
    return keep, len(cands) - len(keep)


def _starts(n: int, T: int, step: int) -> list[int]:
    if n <= T:
        return [0]
    s = list(range(0, n - T, step))
    s.append(n - T)
    return sorted(set(s))


def _touches_inner_edge(bx, shape, x0, y0, W, H, margin: float = 2.0) -> bool:
    th, tw = shape[:2]
    x, y, w, h = bx
    return ((x <= margin and x0 > 0) or (y <= margin and y0 > 0) or
            (x + w >= tw - margin and x0 + tw < W) or (y + h >= th - margin and y0 + th < H))


# Vehicle lamps ----------------------------------------------------------------------------------------


def lamp_blobs(bgr: np.ndarray, *, bright: float = 0.0, min_area: int = 3, max_area_frac: float = 0.01) -> list[dict]:
    """Compact bright blobs with their colour class (white / red / other)."""
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    v = hsv[..., 2]
    thr = bright or max(200.0, float(np.percentile(v, 99.7)))
    b, g, r = (bgr[..., i].astype(np.int16) for i in range(3))
    # Red lamps saturate less than white ones: a separate, lower threshold on red dominance.
    redness = (r - np.maximum(g, b))
    red_mask = (redness > 60) & (r > 120)
    core = (v >= thr) | red_mask
    core = cv2.morphologyEx(core.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)) if min(h, w) > 400 else core.astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(core, connectivity=8)
    out = []
    max_area = max_area_frac * h * w
    for i in range(1, n):
        x, y, bw, bh, area = (int(s) for s in stats[i])
        if area < min_area or area > max_area:
            continue
        if bw > 6 * bh or bh > 4 * bw:  # lines, reflections on the road, poles
            continue
        m = lab[y:y + bh, x:x + bw] == i
        rr = float(redness[y:y + bh, x:x + bw][m].mean())
        colour = "red" if rr > 40 else "white"
        out.append({"box": [x, y, bw, bh], "area": area, "c": (float(cent[i][0]), float(cent[i][1])),
                    "d": float(np.sqrt(area / np.pi) * 2), "colour": colour, "label": i})
    return out


def lights_member(bgr: np.ndarray, *, background_lights: np.ndarray | None = None, bg_threshold: float = 0.5,
                  allowed: np.ndarray | None = None, min_score: float = 0.05) -> tuple[list[dict], dict]:
    blobs = lamp_blobs(bgr)
    n_all = len(blobs)
    removed = 0
    if background_lights is not None:
        keep = []
        for bl in blobs:
            x, y, w, h = bl["box"]
            frac = float(background_lights[y:y + h, x:x + w].mean()) if w and h else 0.0
            if frac >= bg_threshold:
                removed += 1
            else:
                keep.append(bl)
        blobs = keep
    if allowed is not None:
        blobs = [bl for bl in blobs if allowed[min(int(bl["c"][1]), allowed.shape[0] - 1), min(int(bl["c"][0]), allowed.shape[1] - 1)]]
    used: set[int] = set()
    pairs = []
    for i, a in enumerate(blobs):
        for j in range(i + 1, len(blobs)):
            b = blobs[j]
            if a["colour"] != b["colour"]:
                continue
            d = (a["d"] + b["d"]) / 2
            dx = abs(a["c"][0] - b["c"][0])
            dy = abs(a["c"][1] - b["c"][1])
            size_ratio = max(a["area"], b["area"]) / max(1, min(a["area"], b["area"]))
            if dy > 0.6 * d + 2 or size_ratio > 3.0 or not (1.6 * d <= dx <= 14 * d + 6):
                continue
            # Symmetry: similar size and level; spacing typical of lamp pairs.
            sym = 1.0 - min(1.0, (size_ratio - 1) / 2.0) * 0.5 - min(1.0, dy / (d + 1)) * 0.3
            pairs.append((sym, i, j, dx, d))
    pairs.sort(key=lambda p: -p[0])
    out = []
    H, W = bgr.shape[:2]
    for sym, i, j, dx, d in pairs:
        if i in used or j in used:
            continue
        used |= {i, j}
        a, b = blobs[i], blobs[j]
        x0 = min(a["box"][0], b["box"][0]) - 0.25 * d
        x1 = max(a["box"][0] + a["box"][2], b["box"][0] + b["box"][2]) + 0.25 * d
        cy = (a["c"][1] + b["c"][1]) / 2
        span = x1 - x0
        # A car body is roughly as tall as 0.6x its lamp span, mostly above the lamps.
        y0, y1 = cy - 0.45 * span, cy + 0.2 * span
        box = _clip([x0, y0, x1 - x0, y1 - y0], W, H)
        cls = "vehicle_taillights" if a["colour"] == "red" else "vehicle_headlights"
        score = round(float(np.clip(0.35 + 0.5 * sym, 0, 0.9)), 4)
        if score >= min_score:
            out.append({"member": "lights", "cls": cls, "score": score, "box": box,
                        "attrs": {"lamps": [a["box"], b["box"]], "colour": a["colour"], "symmetry": round(sym, 3)}})
    # Unpaired lamps: at long range both headlights merge into one glare spot.
    for k, bl in enumerate(blobs):
        if k in used or bl["area"] < 6:
            continue
        x, y, w, h = bl["box"]
        s = max(w, h)
        box = _clip([x - s, y - s, w + 2 * s, h + 1.5 * s], W, H)
        score = 0.2 if bl["colour"] == "white" else 0.25
        if score >= min_score:
            out.append({"member": "lights", "cls": "vehicle_lamp", "score": score, "box": box,
                        "attrs": {"lamps": [bl["box"]], "colour": bl["colour"], "single": True}})
    return out, {"blobs": n_all, "background_lights_removed": removed, "pairs": len(pairs)}


# Change against background ------------------------------------------------------------------------------


def change_member(bgr: np.ndarray, background: np.ndarray | None, *, k: float = 6.0, min_area_frac: float = 0.0002,
                  allowed: np.ndarray | None = None, min_score: float = 0.05) -> tuple[list[dict], dict]:
    if background is None or background.shape != bgr.shape:
        return [], {"used": False, "reason": "no learned background for this camera and lighting"}
    a = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32), (5, 5), 0)
    b = cv2.GaussianBlur(cv2.cvtColor(background, cv2.COLOR_BGR2LAB).astype(np.float32), (5, 5), 0)
    # Lightness differences are halved: global exposure and shadow changes are mostly in L.
    diff = np.sqrt(0.25 * (a[..., 0] - b[..., 0]) ** 2 + (a[..., 1] - b[..., 1]) ** 2 + (a[..., 2] - b[..., 2]) ** 2)
    med = float(np.median(diff))
    mad = float(np.median(np.abs(diff - med))) * 1.4826 + 1e-3
    thr = med + k * mad
    mask = (diff > thr).astype(np.uint8)
    if allowed is not None:
        mask &= allowed.astype(np.uint8)
    H, W = mask.shape
    ker = max(3, int(round(min(H, W) / 200)) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((ker, ker), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((ker * 3, ker * 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    min_area = max(12, int(min_area_frac * H * W))
    for i in range(1, n):
        x, y, w, h, area = (int(s) for s in stats[i])
        if area < min_area or w > 0.6 * W or h > 0.6 * H:
            continue
        ar = w / max(1, h)
        if not 0.25 <= ar <= 5.0 or area < 0.3 * w * h:
            continue
        strength = float(diff[y:y + h, x:x + w][lab[y:y + h, x:x + w] == i].mean())
        score = round(float(np.clip((strength - thr) / (4 * thr + 1e-6) + 0.2, 0.05, 0.7)), 4)
        if score >= min_score:
            out.append({"member": "change", "cls": "vehicle", "score": score, "box": [x, y, w, h],
                        "attrs": {"strength": round(strength, 2)}})
    return out, {"used": True, "threshold": round(thr, 3), "regions": len(out)}


# Helpers -------------------------------------------------------------------------------------------------


def _clip(b, W, H) -> list[float]:
    x0, y0 = max(0.0, b[0]), max(0.0, b[1])
    x1, y1 = min(float(W), b[0] + b[2]), min(float(H), b[1] + b[3])
    return [round(x0, 1), round(y0, 1), round(max(0.0, x1 - x0), 1), round(max(0.0, y1 - y0), 1)]


def iou(a, b) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    u = a[2] * a[3] + b[2] * b[3] - inter
    return inter / u if u > 0 else 0.0


def _nms(dets: list[dict], thr: float) -> list[dict]:
    dets = sorted(dets, key=lambda d: (-d["score"], d["box"][0], d["box"][1]))
    keep: list[dict] = []
    for d in dets:
        if all(iou(d["box"], k["box"]) < thr for k in keep):
            keep.append(d)
    return keep
