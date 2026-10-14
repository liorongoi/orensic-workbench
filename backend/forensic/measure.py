"""Measurement from a single view: height by vanishing geometry, distance and speed on a plane.

Every measurement carries a 95 % interval from Monte Carlo propagation of the
examiner's clicking uncertainty (and, for plane measurements, the uncertainty of
the surveyed reference coordinates). The random generator is seeded, so the
interval is reproduced exactly on re-run.
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

MC_SAMPLES = 2000
SEED = 20240611

METHODS = {
    "height": {
        "name": "Height by single-view metrology",
        "version": "1.0.0",
        "reference": "A. Criminisi, I. Reid and A. Zisserman, 'Single View Metrology', International Journal of "
        "Computer Vision 40(2), 2000.",
        "description": "Uses the vertical vanishing point and the vanishing line (horizon) of the ground plane, "
        "found from lines the examiner traces, plus one object of known height standing on the same plane, to "
        "compute the height of a target standing on that plane.",
        "assumptions": [
            "Reference and target stand on the same plane, which the horizon belongs to.",
            "Lines traced as vertical are vertical in the scene; lines in each horizontal set are parallel in the scene.",
            "Lens distortion has been corrected (apply lens correction in an enhancement chain first if needed).",
            "Base and top points are on the same vertical line in the scene (feet below the head, not posture-shifted).",
        ],
    },
    "distance": {
        "name": "Distance on a plane",
        "version": "1.0.0",
        "reference": "R. Hartley and A. Zisserman, Multiple View Geometry in Computer Vision, 2nd ed., Section 2.7 "
        "(plane homography from four or more correspondences), 2004.",
        "description": "Maps image points to ground coordinates through a homography fitted to at least four "
        "points whose ground positions were surveyed, then measures the distance between two ground points.",
        "assumptions": [
            "All points lie on the surveyed plane (road surface, floor).",
            "Surveyed coordinates are in metres in one planar coordinate system.",
            "Lens distortion has been corrected.",
        ],
    },
    "speed": {
        "name": "Speed on a plane",
        "version": "1.0.0",
        "reference": "R. Hartley and A. Zisserman, Multiple View Geometry in Computer Vision, 2nd ed., Section 2.7, "
        "2004; SWGDE Technical Overview for Speed Determination from Video, 2019.",
        "description": "Maps the position of the same ground-contact point in two frames to ground coordinates "
        "through a surveyed plane homography, and divides the distance travelled by the time between the frames "
        "taken from the video timestamps.",
        "assumptions": [
            "The tracked point is on the ground plane in both frames (e.g. a tyre contact point).",
            "Frame timestamps are accurate; recorder clock drift and dropped frames are not modelled.",
            "Travel between the two frames is a straight line on the plane.",
            "Lens distortion has been corrected.",
        ],
    },
}


class MeasureError(ValueError):
    pass


def _pt(p: Any, name: str) -> np.ndarray:
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        raise MeasureError(f"{name}: expected an [x, y] point") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise MeasureError(f"{name}: point is not finite")
    return np.array([x, y], np.float64)


def _segments(v: Any, name: str, minimum: int = 2) -> np.ndarray:
    """A flat list of points forming segments: [p1, p2, p3, p4, ...] -> (n, 2, 2)."""
    pts = [_pt(p, name) for p in (v or [])]
    if len(pts) % 2 or len(pts) < 2 * minimum:
        raise MeasureError(f"{name}: needs at least {minimum} lines (two points each)")
    return np.array(pts).reshape(-1, 2, 2)


def _h(p: np.ndarray) -> np.ndarray:
    return np.array([p[0], p[1], 1.0])


def vanishing_point(segs: np.ndarray) -> np.ndarray:
    """Least-squares intersection of image lines (homogeneous; may be at infinity)."""
    lines = []
    for a, b in segs:
        l = np.cross(_h(a), _h(b))
        n = np.linalg.norm(l[:2])
        if n == 0:
            raise MeasureError("a traced line has zero length")
        lines.append(l / n)
    _, _, vt = np.linalg.svd(np.array(lines))
    return vt[-1]


def _height_once(pts: dict) -> float:
    v = vanishing_point(pts["vertical"])
    l = np.cross(vanishing_point(pts["horizontal_a"]), vanishing_point(pts["horizontal_b"]))
    l = l / np.linalg.norm(l[:2])

    def ratio(b, t):
        bh, th = _h(b), _h(t)
        return np.linalg.norm(np.cross(bh, th)) / (abs(l @ bh) * np.linalg.norm(np.cross(v, th)))

    r_ref = ratio(pts["ref_base"], pts["ref_top"])
    if r_ref == 0:
        raise MeasureError("reference object has zero height in the image")
    return float(pts["ref_height"] * ratio(pts["target_base"], pts["target_top"]) / r_ref)


def _homography(img_pts: np.ndarray, world: np.ndarray) -> np.ndarray:
    H, _ = cv2.findHomography(img_pts.astype(np.float64), world.astype(np.float64), 0)
    if H is None:
        raise MeasureError("could not fit the plane: check that the surveyed points are not collinear")
    return H


def _to_plane(H: np.ndarray, p: np.ndarray) -> np.ndarray:
    q = H @ _h(p)
    if abs(q[2]) < 1e-12:
        raise MeasureError("point maps to infinity on the plane (it is on or above the horizon)")
    return q[:2] / q[2]


def _plane_inputs(inp: dict) -> tuple[np.ndarray, np.ndarray]:
    img_pts = np.array([_pt(p, "plane image points") for p in inp.get("plane_image_points") or []])
    world = np.array([_pt(p, "plane world points") for p in inp.get("plane_world_points") or []])
    if len(img_pts) < 4 or len(img_pts) != len(world):
        raise MeasureError("the plane needs at least four image points, each with its surveyed ground position (metres)")
    return img_pts, world


def _interval(samples: np.ndarray) -> list[float]:
    s = samples[np.isfinite(samples)]
    if len(s) < MC_SAMPLES // 2:
        return [float("nan"), float("nan")]
    return [float(np.percentile(s, 2.5)), float(np.percentile(s, 97.5))]


def measure(kind: str, inp: dict, frame_times: dict[int, float] | None = None) -> dict:
    """Compute a measurement and its 95 % interval. `inp` holds clicked points and known values."""
    if kind not in METHODS:
        raise MeasureError(f"unknown measurement '{kind}'")
    sigma = float(inp.get("point_sigma", 1.0))
    if not (0 <= sigma <= 20):
        raise MeasureError("point uncertainty must be between 0 and 20 pixels")
    rng = np.random.default_rng(SEED)
    out: dict[str, Any] = {"kind": kind, **{k: METHODS[kind][k] for k in ("name", "version", "reference", "assumptions")}}

    if kind == "height":
        pts = {
            "vertical": _segments(inp.get("vertical"), "vertical lines"),
            "horizontal_a": _segments(inp.get("horizontal_a"), "first set of horizontal lines"),
            "horizontal_b": _segments(inp.get("horizontal_b"), "second set of horizontal lines"),
            "ref_base": _pt(inp.get("ref_base"), "reference base"),
            "ref_top": _pt(inp.get("ref_top"), "reference top"),
            "target_base": _pt(inp.get("target_base"), "target base"),
            "target_top": _pt(inp.get("target_top"), "target top"),
        }
        ref_h = float(inp.get("ref_height", 0))
        if not ref_h > 0:
            raise MeasureError("reference height must be positive (metres)")
        ref_sigma = float(inp.get("ref_height_sigma", 0.0))
        value = _height_once({**pts, "ref_height": ref_h})
        samples = np.empty(MC_SAMPLES)
        for i in range(MC_SAMPLES):
            jit = {k: v + rng.normal(0, sigma, v.shape) for k, v in pts.items()}
            jit["ref_height"] = ref_h + (rng.normal(0, ref_sigma) if ref_sigma else 0.0)
            try:
                samples[i] = _height_once(jit)
            except (MeasureError, np.linalg.LinAlgError):
                samples[i] = np.nan
        out.update(value=value, unit="m", interval_95=_interval(samples))

    else:
        img_pts, world = _plane_inputs(inp)
        world_sigma = float(inp.get("world_sigma", 0.05))
        a = _pt(inp.get("point_a"), "first point")
        b = _pt(inp.get("point_b"), "second point")

        def dist(ip, wp, pa, pb):
            H = _homography(ip, wp)
            return float(np.linalg.norm(_to_plane(H, pa) - _to_plane(H, pb)))

        d = dist(img_pts, world, a, b)
        samples = np.empty(MC_SAMPLES)
        for i in range(MC_SAMPLES):
            try:
                samples[i] = dist(img_pts + rng.normal(0, sigma, img_pts.shape), world + rng.normal(0, world_sigma, world.shape),
                                  a + rng.normal(0, sigma, 2), b + rng.normal(0, sigma, 2))
            except MeasureError:
                samples[i] = np.nan
        H = _homography(img_pts, world)
        resid = [float(np.linalg.norm(_to_plane(H, p) - w)) for p, w in zip(img_pts, world)]
        out["plane_fit_residual_m"] = round(max(resid), 4)
        if kind == "distance":
            out.update(value=d, unit="m", interval_95=_interval(samples))
        else:
            fa, fb = int(inp.get("frame_a", -1)), int(inp.get("frame_b", -1))
            times = frame_times or {}
            if fa not in times or fb not in times:
                raise MeasureError("speed needs two frames of the video with known timestamps")
            dt = abs(times[fb] - times[fa])
            if dt <= 0:
                raise MeasureError("the two frames must be at different times")
            speed = d / dt
            lo, hi = _interval(samples)
            out.update(value=speed, unit="m/s", interval_95=[lo / dt, hi / dt], distance_m=d, time_s=dt,
                       frames=[fa, fb], value_kmh=speed * 3.6, interval_95_kmh=[lo / dt * 3.6, hi / dt * 3.6])
    out["point_sigma_px"] = sigma
    out["monte_carlo_samples"] = MC_SAMPLES
    lo, hi = out["interval_95"]
    if not (math.isfinite(lo) and math.isfinite(hi)):
        out["warning"] = "The uncertainty could not be estimated: the geometry is unstable for these points."
    return out
