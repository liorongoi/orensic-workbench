"""AI-assisted filters: steps that run a learned model.

They are offered as investigative aids next to the classical filters, never mixed
in silently. Each names the exact model it ran (with its weights hash) in the
report. Filters that can add detail that was never recorded are marked
generative: their output is watermarked "illustrative only", cannot be measured
on, and is kept out of the evidential export unless the examiner writes a
justification into the chain, which is logged.
"""

from __future__ import annotations

import cv2
import numpy as np

from .. import models
from .base import FilterError, Param, register

SR_MODELS = ("fsrcnn", "espcn")
SR_SCALES = ("2", "3", "4")
MAX_OUTPUT_SIDE = 8192
TILE = 384
MARGIN = 16  # wider than either network's receptive field, so tiling does not change the result


def _sr_model_id(p: dict) -> str:
    return f"sr.{p['model']}_x{p['scale']}"


def _tiled_sr(model_id: str, y: np.ndarray, s: int) -> np.ndarray:
    h, w = y.shape
    out = np.empty((h * s, w * s), np.float32)
    for y0 in range(0, h, TILE):
        for x0 in range(0, w, TILE):
            y1, x1 = min(h, y0 + TILE), min(w, x0 + TILE)
            a0, b0 = max(0, y0 - MARGIN), max(0, x0 - MARGIN)
            a1, b1 = min(h, y1 + MARGIN), min(w, x1 + MARGIN)
            res = models.run(model_id, y[a0:a1, b0:b1][None, None])[0, 0]
            out[y0 * s : y1 * s, x0 * s : x1 * s] = res[(y0 - a0) * s : (y1 - a0) * s, (x0 - b0) * s : (x1 - b0) * s]
    return out


@register(
    "ai.super_resolution",
    name="AI super resolution (single image)",
    version="1.0.0",
    category="AI-assisted",
    description="Enlarges one image 2-4x with a small convolutional network (FSRCNN or ESPCN) trained on natural "
    "photographs. The network runs on the brightness channel; colour is enlarged bicubically. It predicts fine "
    "detail from what it learned in training, so detail in the result may not have been recorded by the camera. "
    "Illustrative only: use multi-frame super resolution for evidential work.",
    reference="C. Dong, C. C. Loy and X. Tang, 'Accelerating the Super-Resolution Convolutional Neural Network', "
    "ECCV 2016; W. Shi et al., 'Real-Time Single Image and Video Super-Resolution Using an Efficient Sub-Pixel "
    "Convolutional Neural Network', CVPR 2016.",
    params=[
        Param("model", "choice", "fsrcnn", "Network", choices=SR_MODELS),
        Param("scale", "choice", "2", "Scale factor", choices=SR_SCALES),
    ],
    generative=True,
    ai=True,
    models=lambda p: [_sr_model_id(p)],
)
def ai_super_resolution(img, p):
    s = int(p["scale"])
    h, w = img.shape[:2]
    if max(h, w) * s > MAX_OUTPUT_SIDE:
        raise FilterError(f"the result would be wider than {MAX_OUTPUT_SIDE} pixels: crop to the region of interest first")
    ycc = cv2.cvtColor(np.ascontiguousarray(img, np.float32), cv2.COLOR_RGB2YCrCb)
    y = _tiled_sr(_sr_model_id(p), np.ascontiguousarray(ycc[..., 0]), s)
    crcb = cv2.resize(ycc[..., 1:], (w * s, h * s), interpolation=cv2.INTER_CUBIC)
    out = cv2.cvtColor(np.dstack([np.clip(y, 0, 1), crcb]).astype(np.float32), cv2.COLOR_YCrCb2RGB)
    return np.clip(out, 0.0, 1.0)


def detect_faces(img: np.ndarray, min_score: float = 0.6) -> list[dict]:
    """Faces found by YuNet: box [x, y, w, h], five landmarks and the detector's confidence."""
    h, w = img.shape[:2]
    det = models.face_detector(w, h, min_score)
    bgr = cv2.cvtColor(np.clip(np.rint(img * 255), 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    _, faces = det.detect(bgr)
    out = []
    for f in (faces if faces is not None else []):
        out.append({
            "box": [round(float(v), 1) for v in f[:4]],
            "landmarks": [[round(float(f[4 + 2 * i]), 1), round(float(f[5 + 2 * i]), 1)] for i in range(5)],
            "score": round(float(f[14]), 3),
        })
    out.sort(key=lambda d: (d["box"][1], d["box"][0]))
    return out


@register(
    "redact.faces",
    name="Redact detected faces",
    version="1.0.0",
    category="AI-assisted",
    description="Finds faces with the YuNet detector and blurs, pixelates or blanks them, for disclosure copies. "
    "The detector misses small, turned-away and covered faces, so check every frame of the result before release; "
    "add manual regions for anything it missed.",
    reference="W. Wu, H. Peng and S. Yu, 'YuNet: A Tiny Millisecond-level Face Detector', Machine Intelligence "
    "Research 20, 2023.",
    params=[
        Param("method", "choice", "pixelate", "Method", choices=("pixelate", "blur", "solid")),
        Param("margin", "float", 0.25, "Margin around each face", 0.0, 1.0, 0.05, help="Fraction of the face size"),
        Param("min_score", "float", 0.6, "Detector confidence threshold", 0.3, 0.95, 0.05,
              help="Lower finds more faces and more false alarms"),
        Param("extra", "points", [], "Extra regions (pairs of corners)", help="Regions the detector missed"),
    ],
    ai=True,
    models=lambda p: ["face.yunet"],
)
def redact_faces(img, p):
    h, w = img.shape[:2]
    boxes = []
    for f in detect_faces(img, p["min_score"]):
        x, y, bw, bh = f["box"]
        m = p["margin"]
        boxes.append((x - m * bw, y - m * bh, x + (1 + m) * bw, y + (1 + m) * bh))
    pts = p["extra"]
    if len(pts) % 2:
        raise FilterError("extra regions need pairs of corner points")
    for (ax, ay), (bx, by) in zip(pts[0::2], pts[1::2]):
        boxes.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
    out = img.copy()
    for x0, y0, x1, y1 in boxes:
        x0, y0 = max(0, int(np.floor(x0))), max(0, int(np.floor(y0)))
        x1, y1 = min(w, int(np.ceil(x1))), min(h, int(np.ceil(y1)))
        if x1 <= x0 or y1 <= y0:
            continue
        region = out[y0:y1, x0:x1]
        if p["method"] == "solid":
            region[:] = 0.0
        elif p["method"] == "blur":
            k = max(3, (max(x1 - x0, y1 - y0) // 3) | 1)
            region[:] = cv2.GaussianBlur(region, (k, k), 0, borderType=cv2.BORDER_REPLICATE)
        else:
            cell = max(4, max(x1 - x0, y1 - y0) // 8)
            small = cv2.resize(region, (max(1, (x1 - x0) // cell), max(1, (y1 - y0) // cell)), interpolation=cv2.INTER_AREA)
            region[:] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    return out
