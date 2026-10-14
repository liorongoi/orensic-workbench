"""Geometric filters: crop, rotation, scaling, perspective, lens distortion, deinterlacing."""

from __future__ import annotations

import cv2
import numpy as np

from .base import FilterError, Param, register

INTERP = {
    "nearest": cv2.INTER_NEAREST,
    "bilinear": cv2.INTER_LINEAR,
    "bicubic": cv2.INTER_CUBIC,
    "lanczos": cv2.INTER_LANCZOS4,
}
INTERP_CHOICES = tuple(INTERP)
HARTLEY = "R. Hartley and A. Zisserman, Multiple View Geometry in Computer Vision, 2nd ed., Cambridge, 2004."
KEYS = "R. Keys, 'Cubic convolution interpolation for digital image processing', IEEE TASSP 29(6), 1981."


@register(
    "geometry.crop",
    name="Crop",
    version="1.0.0",
    category="Geometry",
    description="Keeps the rectangle starting at (x, y) with the given width and height. No resampling.",
    reference="Exact pixel copy; no reference needed.",
    params=[
        Param("x", "int", 0, "X", 0, None, 1),
        Param("y", "int", 0, "Y", 0, None, 1),
        Param("width", "int", 0, "Width (0 = to edge)", 0, None, 1),
        Param("height", "int", 0, "Height (0 = to edge)", 0, None, 1),
    ],
)
def crop(img, p):
    h, w = img.shape[:2]
    x, y = p["x"], p["y"]
    cw = p["width"] or (w - x)
    ch = p["height"] or (h - y)
    if x >= w or y >= h or cw <= 0 or ch <= 0:
        raise FilterError("crop rectangle is outside the image")
    return np.ascontiguousarray(img[y : min(h, y + ch), x : min(w, x + cw)])


@register(
    "geometry.rotate",
    name="Rotate",
    version="1.0.0",
    category="Geometry",
    description="Rotates by the given angle (degrees, counter-clockwise). Multiples of 90° are exact "
    "pixel moves; other angles are resampled and the canvas is enlarged to keep every pixel.",
    reference=KEYS,
    params=[
        Param("angle", "float", 0.0, "Angle (°)", -360.0, 360.0, 0.1),
        Param("interpolation", "choice", "bicubic", "Interpolation", choices=INTERP_CHOICES),
    ],
)
def rotate(img, p):
    a = p["angle"] % 360.0
    if a == 0:
        return img
    if a in (90.0, 180.0, 270.0):
        return np.ascontiguousarray(np.rot90(img, int(a // 90)))
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), a, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(np.ceil(h * sin + w * cos)), int(np.ceil(h * cos + w * sin))
    m[0, 2] += nw / 2.0 - w / 2.0
    m[1, 2] += nh / 2.0 - h / 2.0
    out = cv2.warpAffine(img, m, (nw, nh), flags=INTERP[p["interpolation"]], borderMode=cv2.BORDER_CONSTANT)
    return np.clip(out, 0, 1)


@register(
    "geometry.flip",
    name="Flip",
    version="1.0.0",
    category="Geometry",
    description="Mirrors the image horizontally or vertically (for example to correct a mirror or "
    "reflection). Exact pixel move.",
    reference="Exact pixel copy; no reference needed.",
    params=[Param("axis", "choice", "horizontal", "Axis", choices=("horizontal", "vertical"))],
)
def flip(img, p):
    return np.ascontiguousarray(img[:, ::-1] if p["axis"] == "horizontal" else img[::-1])


@register(
    "geometry.resize",
    name="Resize",
    version="1.0.0",
    category="Geometry",
    description="Scales the image. Resampling cannot add detail that was not recorded; nearest "
    "neighbour keeps original pixel values for inspection.",
    reference=KEYS,
    params=[
        Param("scale_x", "float", 2.0, "Horizontal scale", 0.05, 16.0, 0.05),
        Param("scale_y", "float", 2.0, "Vertical scale", 0.05, 16.0, 0.05),
        Param("interpolation", "choice", "bicubic", "Interpolation", choices=INTERP_CHOICES),
    ],
)
def resize(img, p):
    h, w = img.shape[:2]
    nw, nh = max(1, round(w * p["scale_x"])), max(1, round(h * p["scale_y"]))
    return np.clip(cv2.resize(img, (nw, nh), interpolation=INTERP[p["interpolation"]]), 0, 1)


@register(
    "geometry.aspect_ratio",
    name="Correct aspect ratio",
    version="1.0.0",
    category="Geometry",
    description="Restores the display aspect ratio of footage stored with non-square pixels "
    "(common in CCTV, e.g. 704x576 shown at 4:3) by rescaling the width.",
    reference=KEYS,
    params=[
        Param("ratio_w", "float", 4.0, "Display width", 0.1, 100.0, 0.1),
        Param("ratio_h", "float", 3.0, "Display height", 0.1, 100.0, 0.1),
        Param("interpolation", "choice", "bicubic", "Interpolation", choices=INTERP_CHOICES),
    ],
)
def aspect_ratio(img, p):
    h, w = img.shape[:2]
    nw = max(1, round(h * p["ratio_w"] / p["ratio_h"]))
    if nw == w:
        return img
    return np.clip(cv2.resize(img, (nw, h), interpolation=INTERP[p["interpolation"]]), 0, 1)


@register(
    "geometry.perspective",
    name="Perspective correction",
    version="1.0.0",
    category="Geometry",
    description="Maps the quadrilateral marked by four points (top-left, top-right, bottom-right, "
    "bottom-left) onto a rectangle of the given size, e.g. to view a licence plate or document "
    "front-on. Uses a planar homography.",
    reference=HARTLEY,
    params=[
        Param("points", "points", [[0, 0], [100, 0], [100, 50], [0, 50]], "Corners (pixels)", count=4,
              help="Click four corners on the image: top-left, top-right, bottom-right, bottom-left"),
        Param("width", "int", 0, "Output width (0 = auto)", 0, 8192, 1),
        Param("height", "int", 0, "Output height (0 = auto)", 0, 8192, 1),
        Param("interpolation", "choice", "bicubic", "Interpolation", choices=INTERP_CHOICES),
    ],
)
def perspective(img, p):
    src = np.array(p["points"], dtype=np.float32)
    w = p["width"] or int(round(max(np.linalg.norm(src[1] - src[0]), np.linalg.norm(src[2] - src[3]))))
    h = p["height"] or int(round(max(np.linalg.norm(src[3] - src[0]), np.linalg.norm(src[2] - src[1]))))
    if w < 2 or h < 2:
        raise FilterError("the four points enclose too small an area")
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    return np.clip(cv2.warpPerspective(img, m, (w, h), flags=INTERP[p["interpolation"]]), 0, 1)


@register(
    "geometry.lens_undistort",
    name="Lens distortion",
    version="1.0.0",
    category="Geometry",
    description="Removes radial (barrel / pincushion) and tangential lens distortion with the "
    "Brown-Conrady model. Focal length is expressed relative to the image's larger side.",
    reference="D. C. Brown, 'Decentering distortion of lenses', Photogrammetric Engineering 32(3), 1966; "
    "Z. Zhang, 'A flexible new technique for camera calibration', IEEE TPAMI 22(11), 2000.",
    params=[
        Param("k1", "float", -0.2, "k1 (radial)", -2.0, 2.0, 0.001),
        Param("k2", "float", 0.0, "k2 (radial)", -2.0, 2.0, 0.001),
        Param("k3", "float", 0.0, "k3 (radial)", -2.0, 2.0, 0.001),
        Param("p1", "float", 0.0, "p1 (tangential)", -0.5, 0.5, 0.0005),
        Param("p2", "float", 0.0, "p2 (tangential)", -0.5, 0.5, 0.0005),
        Param("focal", "float", 1.0, "Focal length (x larger side)", 0.1, 10.0, 0.01),
        Param("interpolation", "choice", "bicubic", "Interpolation", choices=INTERP_CHOICES),
    ],
)
def lens_undistort(img, p):
    h, w = img.shape[:2]
    f = p["focal"] * max(w, h)
    k = np.array([[f, 0, (w - 1) / 2.0], [0, f, (h - 1) / 2.0], [0, 0, 1]], dtype=np.float64)
    dist = np.array([p["k1"], p["k2"], p["p1"], p["p2"], p["k3"]], dtype=np.float64)
    m1, m2 = cv2.initUndistortRectifyMap(k, dist, None, k, (w, h), cv2.CV_32FC1)
    return np.clip(cv2.remap(img, m1, m2, INTERP[p["interpolation"]]), 0, 1)


@register(
    "geometry.deinterlace",
    name="Deinterlace",
    version="1.0.0",
    category="Geometry",
    description="Separates the two fields of an interlaced frame. 'Field' keeps one field and "
    "interpolates the missing lines (each output is a single moment in time); 'blend' averages "
    "adjacent lines; 'split' places both fields one above the other for inspection.",
    reference="G. de Haan and E. B. Bellers, 'Deinterlacing - an overview', Proc. IEEE 86(9), 1998.",
    params=[
        Param("mode", "choice", "field", "Mode", choices=("field", "blend", "split")),
        Param("field", "choice", "top", "Field", choices=("top", "bottom")),
    ],
)
def deinterlace(img, p):
    h = img.shape[0]
    if h < 4:
        raise FilterError("image too small to deinterlace")
    top, bottom = img[0::2], img[1::2]
    if p["mode"] == "split":
        return np.ascontiguousarray(np.concatenate([top, bottom], axis=0))
    if p["mode"] == "blend":
        out = img.copy()
        out[:-1] = (img[:-1] + img[1:]) / 2.0
        return out
    keep = top if p["field"] == "top" else bottom
    out = np.empty_like(img)
    offset = 0 if p["field"] == "top" else 1
    out[offset::2] = keep[: len(out[offset::2])]
    # Fill missing lines with the average of the lines above and below (line averaging).
    for y in range(1 - offset, h, 2):
        above = out[y - 1] if y - 1 >= 0 else out[y + 1]
        below = out[y + 1] if y + 1 < h else out[y - 1]
        out[y] = (above + below) / 2.0
    return out
