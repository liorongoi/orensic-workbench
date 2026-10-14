"""Rules for generative (AI) steps in evidential work.

A generative filter can add detail the camera never recorded. Following the plan's
rule for evidential output:

- results of a chain with an enabled generative step are watermarked
  "ILLUSTRATIVE ONLY" wherever they are stored, exported or shown in a report;
- they cannot be measured on;
- they are kept out of the evidential export unless the examiner writes a
  justification on each generative step, which is recorded in the audit log;
- agents can propose generative steps but never justify them, and an examiner
  cannot accept one without a justification.
"""

from __future__ import annotations

import cv2
import numpy as np

from .filters import get_filter

MIN_JUSTIFICATION = 20  # characters: a reason, not a tick box
WATERMARK = "ILLUSTRATIVE ONLY - AI-GENERATED DETAIL - NOT FOR MEASUREMENT"


def generative_nodes(graph: dict) -> list[dict]:
    return [n for n in graph.get("nodes", []) if n.get("enabled", True) and get_filter(n["filter"]).generative]


def ai_nodes(graph: dict) -> list[dict]:
    return [n for n in graph.get("nodes", []) if n.get("enabled", True) and get_filter(n["filter"]).ai]


def justified(node: dict) -> bool:
    return len((node.get("justification") or "").strip()) >= MIN_JUSTIFICATION


def unjustified(graph: dict) -> list[str]:
    return [n["id"] for n in generative_nodes(graph) if not justified(n)]


def status(graph: dict) -> dict:
    gen = generative_nodes(graph)
    return {
        "generative_steps": [n["id"] for n in gen],
        "unjustified_steps": [n["id"] for n in gen if not justified(n)],
        "illustrative_only": bool(gen),
        "evidential_export": not any(not justified(n) for n in gen),
    }


def watermark(img: np.ndarray) -> np.ndarray:
    """Burn a visible banner into a copy of a float RGB image (top and bottom edges)."""
    out = np.clip(np.rint(img * 255.0), 0, 255).astype(np.uint8).copy()
    h, w = out.shape[:2]
    scale = max(0.3, min(1.2, w / 1400))
    thick = max(1, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(WATERMARK, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    if tw > w - 8:
        scale *= (w - 8) / tw
        (tw, th), base = cv2.getTextSize(WATERMARK, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    band = th + base + 8
    for y0 in (0, max(0, h - band)):
        out[y0 : y0 + band] = (out[y0 : y0 + band].astype(np.uint16) * 2 // 5 + np.array([153, 0, 0], np.uint16) * 3 // 5).astype(np.uint8)
        cv2.putText(out, WATERMARK, (4, y0 + th + 4), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thick, cv2.LINE_AA)
    return out.astype(np.float32) / 255.0
