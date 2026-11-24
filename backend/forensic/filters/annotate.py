"""Annotation and measurement overlays: marks drawn on the picture at any point in a chain.

Each mark is drawn as a coverage mask (0-255, anti-aliased) in one colour and blended onto the picture, so every
pixel the mark does not touch keeps its exact value. Positions are pixels of the picture as it enters the step,
so a mark added after a crop, rotation or perspective correction is placed on the corrected picture.

Text is drawn with a TrueType font found on the machine (Segoe UI or Arial on Windows, DejaVu Sans on Linux),
falling back to the font bundled with Pillow; the font used can change how text looks between machines, but a
re-run on the same machine is identical.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .base import FilterError, Param, register

COLOURS = {
    "yellow": (1.0, 0.85, 0.1),
    "red": (0.95, 0.15, 0.15),
    "green": (0.2, 0.85, 0.3),
    "cyan": (0.2, 0.8, 1.0),
    "magenta": (0.95, 0.3, 0.9),
    "white": (1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0),
}
COLOUR_CHOICES = tuple(COLOURS)
FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
REF = "Drawing only: marks are blended over the picture and do not change any pixel they do not cover."
SS = 4  # sub-pixel precision of shape coordinates (cv2 shift bits = 2)


@lru_cache(maxsize=32)
def _font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _auto(v: int | float, img: np.ndarray, frac: float, lo: int) -> int:
    return int(v) if v else max(lo, int(round(max(img.shape[:2]) * frac)))


def _blend(img: np.ndarray, mask: np.ndarray, colour, opacity: float = 1.0) -> np.ndarray:
    a = (mask.astype(np.float32) / 255.0 * opacity)[..., None]
    return (img * (1.0 - a) + np.asarray(colour, np.float32) * a).astype(np.float32)


def _mask(img: np.ndarray) -> np.ndarray:
    return np.zeros(img.shape[:2], np.uint8)


def _pt(p) -> tuple[int, int]:
    return int(round(p[0] * SS)), int(round(p[1] * SS))


def _line(mask, a, b, t):
    cv2.line(mask, _pt(a), _pt(b), 255, t, cv2.LINE_AA, shift=2)


def _arrow(mask, a, b, t):
    _line(mask, a, b, t)
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length < 1:
        return
    head = min(length * 0.5, max(8.0, t * 4.0))
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    pts = [b] + [(b[0] - head * math.cos(ang + s * 0.45), b[1] - head * math.sin(ang + s * 0.45)) for s in (1, -1)]
    cv2.fillPoly(mask, [np.array([_pt(p) for p in pts], np.int32)], 255, cv2.LINE_AA, shift=2)


def _text(img, xy, text, size, colour, background: bool, anchor="ls"):
    """Draws text with its baseline-left (anchor 'ls') at xy, on a dark box when asked."""
    if not text:
        return img
    font = _font(size)
    h, w = img.shape[:2]
    m = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(m)
    box = draw.textbbox(xy, text, font=font, anchor=anchor)
    if background:
        pad = max(2, size // 6)
        bg = Image.new("L", (w, h), 0)
        ImageDraw.Draw(bg).rectangle((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad), fill=255)
        img = _blend(img, np.asarray(bg), (0.0, 0.0, 0.0) if colour != COLOURS["black"] else (1.0, 1.0, 1.0), 0.6)
    draw.text(xy, text, fill=255, font=font, anchor=anchor)
    return _blend(img, np.asarray(m), colour)


def _two(points, what) -> tuple[list[float], list[float]]:
    if len(points) < 2:
        raise FilterError(f"{what} needs two points on the image")
    return points[0], points[1]


def _corners(a, b, w, h):
    x0, x1 = sorted((int(round(a[0])), int(round(b[0]))))
    y0, y1 = sorted((int(round(a[1])), int(round(b[1]))))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise FilterError("the marked area is outside the image or too small")
    return x0, y0, x1, y1


def _zoom(img, p, colour, t):
    h, w = img.shape[:2]
    a, b = _two(p["position"], "Zoom")
    circle = p["zoom_area"] == "circle"
    if circle:
        r = math.hypot(b[0] - a[0], b[1] - a[1])
        a, b = (a[0] - r, a[1] - r), (a[0] + r, a[1] + r)
    x0, y0, x1, y1 = _corners(a, b, w, h)
    patch = img[y0 : y1 + 1, x0 : x1 + 1]
    ph, pw = patch.shape[:2]
    # The enlarged copy may take at most half the picture in each direction.
    factor = min(p["zoom"], (w * 0.5) / pw, (h * 0.5) / ph)
    if factor < 1:
        raise FilterError("the area is too large to show enlarged; mark a smaller area")
    zw, zh = max(1, int(round(pw * factor))), max(1, int(round(ph * factor)))
    interp = cv2.INTER_NEAREST if p["zoom_interpolation"] == "nearest" else cv2.INTER_CUBIC
    big = np.clip(cv2.resize(patch, (zw, zh), interpolation=interp), 0, 1)
    margin = max(4, t * 2)
    corner = p["zoom_position"]
    if corner == "auto":
        # The corner farthest from the marked area, so the enlargement does not cover it.
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        corner = ("bottom" if cy < h / 2 else "top") + "_" + ("right" if cx < w / 2 else "left")
    ix = w - zw - margin if corner.endswith("right") else margin
    iy = h - zh - margin if corner.startswith("bottom") else margin
    out = img.copy()
    if circle:
        cm = np.zeros((zh, zw), np.uint8)
        cv2.ellipse(cm, _pt(((zw - 1) / 2, (zh - 1) / 2)), _pt(((zw - 1) / 2, (zh - 1) / 2)), 0, 0, 360, 255, -1, cv2.LINE_AA, shift=2)
        full = _mask(img)
        full[iy : iy + zh, ix : ix + zw] = cm
        canvas = np.zeros_like(img)
        canvas[iy : iy + zh, ix : ix + zw] = big
        a_ = (full.astype(np.float32) / 255.0)[..., None]
        out = (out * (1 - a_) + canvas * a_).astype(np.float32)
    else:
        out[iy : iy + zh, ix : ix + zw] = big
    # Outline of the source area and of the enlargement, joined by a line between their nearest points.
    m = _mask(img)
    if circle:
        cv2.ellipse(m, _pt(((x0 + x1) / 2, (y0 + y1) / 2)), _pt(((x1 - x0) / 2, (y1 - y0) / 2)), 0, 0, 360, 255, t, cv2.LINE_AA, shift=2)
        cv2.ellipse(m, _pt((ix + (zw - 1) / 2, iy + (zh - 1) / 2)), _pt(((zw - 1) / 2, (zh - 1) / 2)), 0, 0, 360, 255, t, cv2.LINE_AA, shift=2)
    else:
        cv2.rectangle(m, (x0, y0), (x1, y1), 255, t, cv2.LINE_AA)
        cv2.rectangle(m, (ix, iy), (ix + zw - 1, iy + zh - 1), 255, t, cv2.LINE_AA)
    src_c = ((x0 + x1) / 2, (y0 + y1) / 2)
    dst_c = (ix + zw / 2, iy + zh / 2)
    _line(m, _edge_point(src_c, dst_c, x0, y0, x1, y1), _edge_point(dst_c, src_c, ix, iy, ix + zw - 1, iy + zh - 1), max(1, t // 2))
    out = _blend(out, m, colour)
    label = p["text"] or f"×{factor:.3g}"
    size = _auto(p["text_size"], img, 0.025, 12)
    return _text(out, (ix + margin, iy + zh - margin), label, size, colour, True)


def _edge_point(c, toward, x0, y0, x1, y1):
    """Where the line from the box centre c toward a point leaves the box."""
    dx, dy = toward[0] - c[0], toward[1] - c[1]
    hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    s = min(hw / abs(dx) if dx else math.inf, hh / abs(dy) if dy else math.inf)
    s = min(s, 1.0) if math.isfinite(s) else 0.0
    return c[0] + dx * s, c[1] + dy * s


@register(
    "annotate.mark",
    name="Annotation",
    version="1.0.0",
    category="Annotate",
    description="Draws a mark on the picture for a report or a briefing: an arrow, a line, a rectangle, a circle, "
    "a text label, or an enlarged copy (zoom) of a selected area placed in a corner and joined to the area it "
    "comes from. Add the step once for each mark. Arrow and line: click start, then end. Rectangle: click two "
    "opposite corners. Circle: click the centre, then a point on the edge. Text: click where the text starts. "
    "Zoom: two corners of the area (rectangle) or its centre and edge (circle). The text, if any, is written next "
    "to the mark. Marks cover what is under them, so keep them off the detail that matters, and keep an "
    "unannotated export for examination.",
    reference=REF,
    params=[
        Param("shape", "choice", "arrow", "Mark", choices=("arrow", "line", "rectangle", "circle", "text", "zoom")),
        Param("position", "points", [], "Position on the image", help="Click on the image (see the description for how many points each mark takes)"),
        Param("text", "text", "", "Text", help="Label written next to the mark; for a text mark, the text itself"),
        Param("colour", "choice", "yellow", "Colour", choices=COLOUR_CHOICES),
        Param("thickness", "int", 0, "Line thickness (0 = auto)", 0, 100, 1),
        Param("text_size", "int", 0, "Text size in pixels (0 = auto)", 0, 500, 1),
        Param("text_background", "bool", True, "Dark box behind text", help="Keeps text readable on any background"),
        Param("filled", "bool", False, "Filled shape", help="Rectangle and circle: fill at 35% opacity instead of an outline"),
        Param("zoom", "float", 3.0, "Zoom factor", 1.0, 16.0, 0.5, help="Zoom mark: how much to enlarge the area (reduced if it would not fit)"),
        Param("zoom_area", "choice", "rectangle", "Zoom area shape", choices=("rectangle", "circle")),
        Param("zoom_position", "choice", "auto", "Zoom placed at", choices=("auto", "top_left", "top_right", "bottom_left", "bottom_right")),
        Param("zoom_interpolation", "choice", "nearest", "Zoom resampling", choices=("nearest", "bicubic"),
              help="Nearest shows the original pixels as blocks; bicubic is smoother but invents in-between values"),
    ],
)
def mark(img, p):
    pts = p["position"]
    if not pts:
        return img  # nothing marked yet
    img = np.ascontiguousarray(img, np.float32)
    colour = COLOURS[p["colour"]]
    t = _auto(p["thickness"], img, 0.004, 2)
    size = _auto(p["text_size"], img, 0.03, 12)
    shape = p["shape"]
    if shape == "zoom":
        return _zoom(img, p, colour, t)
    if shape == "text":
        if not p["text"]:
            raise FilterError("type the text to write")
        return _text(img, tuple(pts[0]), p["text"], size, colour, p["text_background"])
    a, b = _two(pts, shape.capitalize())
    m = _mask(img)
    fill = -1 if p["filled"] and shape in ("rectangle", "circle") else t
    if shape == "arrow":
        _arrow(m, a, b, t)
        label_at = (a[0], a[1] - t * 2)
    elif shape == "line":
        _line(m, a, b, t)
        label_at = (min(a[0], b[0]), min(a[1], b[1]) - t * 2)
    elif shape == "rectangle":
        cv2.rectangle(m, _pt((min(a[0], b[0]), min(a[1], b[1]))), _pt((max(a[0], b[0]), max(a[1], b[1]))), 255, fill, cv2.LINE_AA, shift=2)
        label_at = (min(a[0], b[0]), min(a[1], b[1]) - t * 2)
    else:  # circle
        r = math.hypot(b[0] - a[0], b[1] - a[1])
        cv2.circle(m, _pt(a), int(round(r * SS)), 255, fill, cv2.LINE_AA, shift=2)
        label_at = (a[0] - r, a[1] - r - t * 2)
    out = _blend(img, m, colour, 0.35 if fill == -1 else 1.0)
    if p["text"]:
        out = _text(out, (max(0.0, label_at[0]), max(float(size), label_at[1])), p["text"], size, colour, p["text_background"])
    return np.clip(out, 0, 1)


UNIT_CHOICES = ("px", "mm", "cm", "m", "in", "ft")


@register(
    "annotate.measure",
    name="Measurement",
    version="1.0.0",
    category="Annotate",
    description="Measures straight-line distances and angles on the picture as it is at this step, and writes "
    "them on it. Add it after the steps that change the picture (crop, resize, rotation, perspective "
    "correction...) so you measure the corrected picture. Click pairs of points: each pair is one measured line. "
    "Lengths are in pixels, or in real units when you mark a reference of known length (two points) and enter its "
    "length: every line is then scaled by the same factor. A real-unit reading is valid only for lines in the "
    "same flat plane as the reference, seen front-on (straighten the plane with Perspective correction first); "
    "for heights and distances in a 3D scene use the Measure tab. The uncertainty printed is from a ±1 pixel "
    "error at each clicked point.",
    reference="Planar scaling from a reference of known length; uncertainty by first-order propagation of the "
    "clicking error (JCGM 100:2008, Guide to the Expression of Uncertainty in Measurement).",
    params=[
        Param("lines", "points", [], "Lines to measure (pairs of points)", help="Click the two ends of each line; Esc to stop"),
        Param("reference", "points", [], "Reference of known length (2 points)", help="Click the two ends of something whose length you know"),
        Param("reference_length", "float", 0.0, "Reference length (0 = measure in pixels)", 0.0, None, 0.01),
        Param("unit", "choice", "cm", "Unit of the reference", choices=UNIT_CHOICES),
        Param("show_angle", "bool", False, "Also show each line's angle", help="Angle from horizontal, in degrees"),
        Param("colour", "choice", "yellow", "Colour", choices=COLOUR_CHOICES),
        Param("thickness", "int", 0, "Line thickness (0 = auto)", 0, 100, 1),
        Param("text_size", "int", 0, "Text size in pixels (0 = auto)", 0, 500, 1),
    ],
)
def measure(img, p):
    lines = p["lines"]
    ref = p["reference"]
    if not lines and not ref:
        return img  # nothing marked yet
    if len(lines) % 2:
        lines = lines[:-1]  # the last line is still being clicked
    img = np.ascontiguousarray(img, np.float32)
    colour = COLOURS[p["colour"]]
    t = _auto(p["thickness"], img, 0.003, 1)
    size = _auto(p["text_size"], img, 0.025, 12)
    scale, unit, ref_px = 1.0, "px", None
    if p["reference_length"] > 0:
        if len(ref) != 2:
            raise FilterError("mark the two ends of the reference to measure in real units")
        ref_px = math.dist(ref[0], ref[1])
        if ref_px < 2:
            raise FilterError("the reference is too short; mark its two ends further apart")
        scale, unit = p["reference_length"] / ref_px, p["unit"]
    m = _mask(img)
    labels = []

    def ticks(a, b):
        _line(m, a, b, t)
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) + math.pi / 2
        k = max(4.0, t * 3.0)
        for q in (a, b):
            _line(m, (q[0] - k * math.cos(ang), q[1] - k * math.sin(ang)), (q[0] + k * math.cos(ang), q[1] + k * math.sin(ang)), t)

    if len(ref) == 2:
        ticks(ref[0], ref[1])
        name = f"ref {p['reference_length']:g} {unit}" if ref_px else f"ref {math.dist(ref[0], ref[1]):.1f} px"
        labels.append((ref[0], ref[1], name))
    for i in range(0, len(lines), 2):
        a, b = lines[i], lines[i + 1]
        ticks(a, b)
        d_px = math.dist(a, b)
        # ±1 px per clicked point on both the line and (when scaled) the reference, combined in quadrature.
        rel = math.sqrt(2) / max(d_px, 1e-9)
        if ref_px:
            rel = math.hypot(rel, math.sqrt(2) / ref_px)
        d = d_px * scale
        dec = 0 if unit == "px" and d >= 100 else 1 if d >= 10 else 2
        text = f"{i // 2 + 1}: {d:.{dec}f} ± {d * rel:.{dec}f} {unit}"
        if p["show_angle"]:
            text += f", {round(math.degrees(math.atan2(-(b[1] - a[1]), b[0] - a[0])), 1) + 0.0:.1f}°"
        labels.append((a, b, text))
    out = _blend(img, m, colour)
    for a, b, text in labels:
        mid = ((a[0] + b[0]) / 2 + t * 3, (a[1] + b[1]) / 2 - t * 3)
        out = _text(out, (max(0.0, mid[0]), max(float(size), mid[1])), text, size, colour, True)
    return np.clip(out, 0, 1)
