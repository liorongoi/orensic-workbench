"""Multi-object tracking by detection (SORT-style, deterministic).

Each track keeps a constant-velocity estimate of its box. On every processed frame, predicted
boxes are matched to new detections of the same class greedily by intersection-over-union (highest
first, at least `min_iou`). Unmatched detections start new tracks; tracks unmatched for more than
`max_missed` processed frames end. Tracks shorter than `min_hits` detections are dropped as
likely false alarms.

A track number only says the detector saw boxes that overlap from frame to frame. It is not an
identity: occlusions, crossings and look-alike objects swap or split tracks, which the examiner
corrects by merging or splitting.

Reference: A. Bewley et al., 'Simple Online and Realtime Tracking', ICIP 2016 (here with greedy
matching and an alpha-beta velocity filter instead of the Hungarian algorithm and Kalman filter).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

VERSION = "1.0.0"


def iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    x0, y0 = max(ax0, bx0), max(ay0, by0)
    x1, y1 = min(ax0 + aw, bx0 + bw), min(ay0 + ah, by0 + bh)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return inter / (aw * ah + bw * bh - inter + 1e-9)


@dataclass
class Track:
    number: int
    cls: str
    box: np.ndarray
    vel: np.ndarray = field(default_factory=lambda: np.zeros(4))
    missed: int = 0
    history: list = field(default_factory=list)  # (frame, time, box, score)

    def predict(self, steps: int = 1) -> np.ndarray:
        return self.box + self.vel * steps


class Tracker:
    def __init__(self, min_iou: float = 0.25, max_missed: int = 8, min_hits: int = 3, alpha: float = 0.6, beta: float = 0.2):
        self.min_iou, self.max_missed, self.min_hits = min_iou, max_missed, min_hits
        self.alpha, self.beta = alpha, beta
        self.active: list[Track] = []
        self.finished: list[Track] = []
        self._next = 1

    def params(self) -> dict:
        return {"version": VERSION, "min_iou": self.min_iou, "max_missed": self.max_missed, "min_hits": self.min_hits,
                "alpha": self.alpha, "beta": self.beta, "matching": "greedy IoU, same class"}

    def update(self, frame: int, time: float | None, dets: list[dict]) -> list[tuple[int, dict]]:
        """Returns (track number, detection) for every detection assigned on this frame."""
        pairs = []
        for ti, t in enumerate(self.active):
            p = t.predict(t.missed + 1)
            for di, d in enumerate(dets):
                if d["cls"] == t.cls:
                    v = iou(p, d["box"])
                    if v >= self.min_iou:
                        pairs.append((v, ti, di))
        pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
        used_t, used_d, out = set(), set(), []
        for v, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            t, d = self.active[ti], dets[di]
            z = np.array(d["box"], float)
            steps = t.missed + 1
            pred = t.predict(steps)
            r = z - pred
            t.box = pred + self.alpha * r
            t.vel = t.vel + self.beta * r / steps
            t.missed = 0
            t.history.append((frame, time, [round(float(x), 1) for x in d["box"]], d["score"]))
            out.append((t.number, d))
        for ti, t in enumerate(self.active):
            if ti not in used_t:
                t.missed += 1
        for di, d in enumerate(dets):
            if di not in used_d:
                t = Track(self._next, d["cls"], np.array(d["box"], float))
                t.history.append((frame, time, list(d["box"]), d["score"]))
                self._next += 1
                self.active.append(t)
                out.append((t.number, d))
        still = []
        for t in self.active:
            (self.finished if t.missed > self.max_missed else still).append(t)
        self.active = still
        return out

    def close(self) -> list[Track]:
        tracks = self.finished + self.active
        self.active, self.finished = [], []
        return sorted([t for t in tracks if len(t.history) >= self.min_hits], key=lambda t: t.number)
