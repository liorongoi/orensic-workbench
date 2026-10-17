"""Geometric consistency: shadows, reflections and perspective.

The examiner marks pairs of points on the image; each pair constrains where one
point must lie in the image plane (the projected light source, or a vanishing
point), within an angular tolerance that absorbs the imprecision of clicking.
If no single point satisfies every constraint, the scene's geometry is
inconsistent: some part of it was added, moved or synthesised. If one does, the
geometry is consistent, which is not proof that nothing changed.

The constraints are wedges in the image plane. Their intersection is a union of
convex polygons whose vertices are intersections of wedge boundary lines, so a
point is feasible exactly when one of those intersections (or a corner of a
bounding box far outside the image, which stands in for points at infinity)
satisfies every wedge. The smallest conflicting subset is found by exhaustive
search over small subsets (by Helly's theorem three convex wedges already
decide a conflict for one light orientation).
"""

from __future__ import annotations

import itertools
import math

import cv2
import numpy as np

from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register

EPS_ANGLE = 1e-7
MAX_COMBINATIONS = 20000
FAR = 200.0  # bounding box half-size in image diagonals: stands in for points at infinity


def wedges(points: list[list[float]], tolerance_deg: float, click_px: float = 0.0) -> list[dict]:
    """Pairs of clicked points (apex, through) -> wedge constraints. Each half-angle is the tolerance plus the
    direction error that `click_px` of error at both ends causes on a segment of that length (short ones widen)."""
    if len(points) % 2:
        raise ValueError("points must come in pairs; the last pair is incomplete")
    out = []
    for i in range(0, len(points), 2):
        a = np.array(points[i], np.float64)
        b = np.array(points[i + 1], np.float64)
        v = b - a
        n = float(np.hypot(*v))
        if n < 1.0:
            raise ValueError(f"constraint {i // 2 + 1}: its two points are less than a pixel apart")
        extra = math.atan(2.0 * click_px / n)
        out.append({"apex": a, "dir": v / n, "tol": min(math.radians(tolerance_deg) + extra, math.radians(89.0)),
                    "extra": extra, "end": b})
    return out


def _rot(d: np.ndarray, t: float) -> np.ndarray:
    c, s = math.cos(t), math.sin(t)
    return np.array([c * d[0] - s * d[1], s * d[0] + c * d[1]])


def _inside(pts: np.ndarray, ws: list[dict], sign: float, two_sided: bool) -> np.ndarray:
    """Which points (N x 2) lie in every wedge."""
    ok = np.ones(len(pts), bool)
    for w in ws:
        v = pts - w["apex"]
        r = np.hypot(v[:, 0], v[:, 1])
        cosang = (v @ (sign * w["dir"])) / np.maximum(r, 1e-12)
        if two_sided:
            cosang = np.abs(cosang)
        ok &= (r < 1e-6) | (cosang >= math.cos(w["tol"] + EPS_ANGLE))
    return ok


def _candidates(ws: list[dict], box: tuple[float, float, float, float]) -> np.ndarray:
    lines = []
    for w in ws:
        for t in (w["tol"], -w["tol"]):
            lines.append((w["apex"], _rot(w["dir"], t)))
    x0, y0, x1, y1 = box
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    edges = [(np.array(corners[i], float), np.array(corners[(i + 1) % 4], float) - np.array(corners[i], float)) for i in range(4)]
    pts = [np.array(c, float) for c in corners] + [w["apex"] for w in ws]
    for (p, d), (q, e) in itertools.combinations(lines + edges, 2):
        den = d[0] * e[1] - d[1] * e[0]
        if abs(den) < 1e-12:
            continue
        s = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
        pts.append(p + s * d)
    c = np.array(pts)
    keep = (c[:, 0] >= x0 - 1e-6) & (c[:, 0] <= x1 + 1e-6) & (c[:, 1] >= y0 - 1e-6) & (c[:, 1] <= y1 + 1e-6)
    return c[keep]


def solve(ws: list[dict], two_sided: bool, box: tuple[float, float, float, float]) -> dict | None:
    """A point satisfying every wedge, or None. One-sided wedges are also tried all reversed
    (a light source behind the camera projects on the opposite side of every shadow)."""
    if not ws:
        return None
    cand = _candidates(ws, box)
    for sign in (1.0, -1.0) if not two_sided else (1.0,):
        ok = _inside(cand, ws, sign, two_sided)
        if ok.any():
            pts = cand[ok]
            # report the feasible point nearest the image centre's region (the centroid of the feasible vertices)
            centroid = pts.mean(axis=0)
            if not _inside(centroid[None], ws, sign, two_sided)[0]:
                centroid = pts[0]
            return {"point": centroid, "sign": sign, "vertices": pts}
    return None


def with_tolerance(ws: list[dict], deg: float) -> list[dict]:
    return [w | {"tol": min(math.radians(deg) + w.get("extra", 0.0), math.radians(89.0))} for w in ws]


def minimal_conflict(ws: list[dict], two_sided: bool, box) -> list[int] | None:
    """Indices of the fewest constraints that already conflict (None if all are consistent)."""
    n, tried = len(ws), 0
    for k in range(2, min(n, 6) + 1):
        for combo in itertools.combinations(range(n), k):
            tried += 1
            if tried > MAX_COMBINATIONS:
                return None
            if solve([ws[i] for i in combo], two_sided, box) is None:
                return list(combo)
    return None


def tolerance_boundary(ws: list[dict], two_sided: bool, box, feasible: bool, tol_deg: float) -> float:
    """The tolerance at which the verdict flips: for a conflict, the tolerance up to which it persists;
    for a consistent set, the smallest tolerance at which it stays consistent."""
    lo, hi = (tol_deg, 89.0) if not feasible else (0.0, tol_deg)
    if not feasible and solve(with_tolerance(ws, hi), two_sided, box) is None:
        return hi
    for _ in range(30):
        mid = (lo + hi) / 2
        ok = solve(with_tolerance(ws, mid), two_sided, box) is not None
        if feasible:
            hi = mid if ok else hi
            lo = lo if ok else mid
        else:
            lo = mid if not ok else lo
            hi = hi if not ok else mid
    return round(hi if feasible else lo, 2)


def _draw(img: np.ndarray, ws: list[dict], conflict: list[int] | None, sol: dict | None, two_sided: bool, mark: str) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(1.0, 1600 / max(h, w))
    sh, sw = max(1, int(h * scale)), max(1, int(w * scale))
    base = cv2.resize(img.astype(np.float32), (sw, sh), interpolation=cv2.INTER_AREA) if scale < 1 else img.astype(np.float32).copy()
    gray = base.mean(axis=2, keepdims=True)
    out = np.repeat(gray, 3, axis=2) * 0.55
    diag = math.hypot(sw, sh)
    # feasible region inside the frame
    if sol is not None:
        yy, xx = np.mgrid[0:sh, 0:sw]
        grid = np.stack([xx.ravel() / scale, yy.ravel() / scale], axis=1)
        m = _inside(grid, ws, sol["sign"], two_sided).reshape(sh, sw)
        out[m] = out[m] * 0.5 + np.array([0.15, 0.55, 0.2], np.float32)
    out = (np.clip(out, 0, 1) * 255).astype(np.uint8)  # OpenCV draws text on 8-bit images only
    lw = max(1, int(round(diag / 700)))
    bad = set(conflict or [])
    for i, wd in enumerate(ws):
        col = (242, 64, 51) if i in bad else ((77, 217, 242) if conflict else (102, 230, 115))
        a = wd["apex"] * scale
        sides = (1.0, -1.0) if two_sided else ((sol or {}).get("sign", 1.0),)
        layer = out.copy()
        for s in sides:
            p1 = a + _rot(s * wd["dir"], wd["tol"]) * diag * 3
            p2 = a + _rot(s * wd["dir"], -wd["tol"]) * diag * 3
            poly = np.clip(np.array([a, p1, p2]), -1e5, 1e5).round().astype(np.int32)
            cv2.fillPoly(layer, [poly], col, lineType=cv2.LINE_AA)
            for p in (p1, p2):
                cv2.line(out, tuple(np.clip(a, -1e5, 1e5).round().astype(int)), tuple(np.clip(p, -1e5, 1e5).round().astype(int)), col, lw, cv2.LINE_AA)
        out = cv2.addWeighted(layer, 0.18, out, 0.82, 0)
        b = wd["end"] * scale
        a0 = wd.get("start", wd["apex"]) * scale
        cv2.line(out, tuple(a0.round().astype(int)), tuple(b.round().astype(int)), (255, 255, 255), lw + 1, cv2.LINE_AA)
        for p in (a0, b):
            cv2.circle(out, tuple(p.round().astype(int)), lw * 3, col, -1, cv2.LINE_AA)
        cv2.putText(out, str(i + 1), tuple((a0 + np.array([6, -6]) * lw).round().astype(int)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * lw, (255, 255, 255), lw, cv2.LINE_AA)
    if sol is not None:
        p = sol["point"] * scale
        inside = 0 <= p[0] < sw and 0 <= p[1] < sh
        if inside:
            cv2.drawMarker(out, tuple(p.round().astype(int)), (255, 230, 51), cv2.MARKER_STAR, 18 * lw, lw + 1)
        else:  # arrow from the centre toward the off-frame solution
            c = np.array([sw / 2, sh / 2])
            v = p - c
            t = min((sw / 2 - 12) / max(abs(v[0]), 1e-9), (sh / 2 - 12) / max(abs(v[1]), 1e-9))
            e = c + v * t
            cv2.arrowedLine(out, tuple((c + v * t * 0.8).round().astype(int)), tuple(e.round().astype(int)), (255, 230, 51), lw + 1,
                            cv2.LINE_AA, tipLength=0.4)
        cv2.putText(out, mark, (8, 22 * lw), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * lw, (255, 230, 51), lw, cv2.LINE_AA)
    out = out.astype(np.float32) / 255.0
    return cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR) if scale < 1 else out


def check(img: np.ndarray, points: list[list[float]], tolerance: float, two_sided: bool, what: dict,
          starts: list[list[float]] | None = None, click_px: float = 0.0) -> AnalysisResult:
    """Shared driver: `what` words the result (noun for the constraint and for the common point).
    `starts` replaces each constraint's drawn and reported first point (perspective lines use their midpoint as apex)."""
    ws = wedges(points, tolerance, click_px)
    for wd, s in zip(ws, starts or []):
        wd["start"] = np.array(s, np.float64)
    n = len(ws)
    if n < 2:
        raise ValueError(f"mark at least two {what['plural']} (two points each: {what['how']})")
    h, w = img.shape[:2]
    d = math.hypot(w, h) * FAR
    box = (-d, -d, w + d, h + d)
    sol = solve(ws, two_sided, box)
    feasible = sol is not None
    conflict = None if feasible else minimal_conflict(ws, two_sided, box)
    boundary = tolerance_boundary(ws, two_sided, box, feasible, tolerance)
    findings: dict = {
        "constraints": n,
        "tolerance_deg": tolerance,
        "click_error_px": click_px,
        "half_angles_deg": [round(math.degrees(wd["tol"]), 2) for wd in ws],
        "consistent": feasible,
        "constraint_points": [[[round(float(v), 1) for v in wd.get("start", wd["apex"])], [round(float(v), 1) for v in wd["end"]]]
                              for wd in ws],
    }
    flags: list[dict] = []
    if feasible:
        # best-fit point: the feasible region at the tightest tolerance that still admits one
        tight = solve(with_tolerance(ws, boundary + 0.01), two_sided, box)
        if tight is not None and tight["sign"] == sol["sign"]:
            sol["point"] = tight["point"]
        p = sol["point"]
        far = bool(math.hypot(p[0] - w / 2, p[1] - h / 2) > 10 * math.hypot(w, h))  # effectively at infinity
        findings[what["point_key"]] = None if far else [round(float(p[0]), 1), round(float(p[1]), 1)]
        findings["point_far_outside_frame"] = far
        if not two_sided:
            findings["light_side"] = "in front of the camera" if sol["sign"] > 0 else "behind the camera"
        findings["stays_consistent_down_to_deg"] = boundary
        summary = f"Consistent: one {what['point']} fits all {n} {what['plural']} (±{tolerance:g}°)."
        flags.append({"code": f"{what['code']}.consistent", "level": "info", "text": summary
                      + f" Still consistent at ±{boundary:g}°. Consistent geometry does not prove the image is unaltered."})
    else:
        findings["smallest_conflicting_set"] = [i + 1 for i in conflict] if conflict else None
        findings["conflict_persists_up_to_deg"] = boundary
        names = ", ".join(f"#{i + 1}" for i in conflict) if conflict else "several"
        summary = f"Inconsistent: no single {what['point']} fits all {n} {what['plural']} (±{tolerance:g}°)."
        flags.append({"code": f"{what['code']}.inconsistent", "level": "warning", "text": summary
                      + f" Smallest conflicting set: {names}; the conflict persists up to ±{boundary:g}°."})
    mark = ("consistent" if feasible else "INCONSISTENT")
    maps = {"constraints": _draw(img, ws, conflict, sol, two_sided, mark)}
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps=maps)


_TOL = Param("tolerance", "float", 2.0, "Tolerance (°)", min=0.0, max=30.0, step=0.5,
             help="Angle added to every wedge for pairing and model error.")
_CLICK = Param("click_error", "float", 2.0, "Click error (px)", min=0.0, max=20.0, step=0.5,
               help="How far each click may be from the true point; widens short constraints more.")
_REF = ("E. Kee, J. F. O'Brien and H. Farid, 'Exposing Photo Manipulation with Inconsistent Shadows', ACM Transactions on "
        "Graphics 32(3), 2013; J. F. O'Brien and H. Farid, 'Exposing Photo Manipulation with Inconsistent Reflections', ACM "
        "Transactions on Graphics 31(1), 2012; H. Farid, 'Photo Forensics', MIT Press, 2016, chapters 8 and 9.")
_GUIDE = ("Green is where the common point can lie; red wedges are the smallest set of constraints that cannot all be true "
          "at once. Mark only pairs you can match with confidence, set the tolerance to your clicking uncertainty, and "
          "report the tolerance the conflict persists to. Consistency does not prove authenticity: a careful forger can "
          "match the geometry, and a conflict may come from a wrong pairing or a non-planar surface.")


@register(
    "geometry.shadows",
    name="Shadow consistency",
    version="1.0.0",
    category="Geometric analysis",
    description="Tests whether one light source can cast every marked shadow. For each pair, click a point on a cast "
    "shadow, then the point on the object that casts it. The light's projection must lie inside every wedge; if no "
    "point does, the shadows are inconsistent.",
    reference=_REF,
    applies_to=("image",),
    reading_guide="Works for one dominant, compact light (the sun, a single lamp). " + _GUIDE,
    params=[Param("pairs", "points", [], "Shadow → object pairs", help="Click a shadow point, then the object point casting it; repeat."), _TOL, _CLICK],
)
def shadows(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    return check(ctx.image, p["pairs"], p["tolerance"], False,
                 {"plural": "shadow constraints", "how": "shadow point, then object point", "point": "light position",
                  "point_key": "light_projection_xy", "code": "geom.shadows"}, click_px=p["click_error"])


@register(
    "geometry.reflections",
    name="Reflection consistency",
    version="1.0.0",
    category="Geometric analysis",
    description="Tests whether the reflections in one flat mirror, window or water surface fit together. For each pair, "
    "click a point on an object, then the same point in its reflection. All such lines must meet at one vanishing "
    "point; if no point lies in every wedge, the reflections are inconsistent.",
    reference=_REF,
    applies_to=("image",),
    reading_guide="Needs a single planar reflecting surface; curved or rippled surfaces break the model. " + _GUIDE,
    params=[Param("pairs", "points", [], "Object → reflection pairs", help="Click an object point, then its reflection; repeat."), _TOL, _CLICK],
)
def reflections(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    return check(ctx.image, p["pairs"], p["tolerance"], False,
                 {"plural": "reflection constraints", "how": "object point, then its reflection", "point": "vanishing point",
                  "point_key": "vanishing_point_xy", "code": "geom.reflections"}, click_px=p["click_error"])


@register(
    "geometry.perspective",
    name="Perspective consistency",
    version="1.0.0",
    category="Geometric analysis",
    description="Tests whether lines that are parallel in the scene (building edges, road markings, shelves) meet at "
    "one vanishing point. Click two points along each line. An object pasted in with a different perspective "
    "breaks the common vanishing point.",
    reference=_REF + " R. Hartley and A. Zisserman, 'Multiple View Geometry in Computer Vision', 2nd ed., 2004, ch. 8.",
    applies_to=("image",),
    reading_guide="Use lines from one direction only (one vanishing point per run), and lens-corrected images: barrel "
    "distortion bends straight lines. " + _GUIDE,
    params=[Param("pairs", "points", [], "Line points (2 per line)", help="Click two points along each scene-parallel line."), _TOL, _CLICK],
)
def perspective(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    pts = p["pairs"]
    if len(pts) % 2:
        raise ValueError("points must come in pairs; the last pair is incomplete")
    # apex at each segment's midpoint so the tolerance wedge is centred on the clicked line
    mid = []
    for i in range(0, len(pts), 2):
        mid += [list((np.array(pts[i], float) + np.array(pts[i + 1], float)) / 2), pts[i + 1]]
    return check(ctx.image, mid, p["tolerance"], True,
                 {"plural": "lines", "how": "two points along each line", "point": "vanishing point",
                  "point_key": "vanishing_point_xy", "code": "geom.perspective"}, starts=pts[0::2],
                 click_px=p["click_error"] / 2)  # half-length segments: halve to keep the full line's direction error
