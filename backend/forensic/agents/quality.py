"""No-reference image quality measures used to rank candidate enhancement chains."""

from __future__ import annotations

import math

import cv2
import numpy as np

_NOISE_K = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float64)


def _luma(img: np.ndarray) -> np.ndarray:
    return (0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]).astype(np.float64)


def metrics(img: np.ndarray) -> dict:
    y = _luma(img)
    gx = cv2.Sobel(y, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(y, cv2.CV_64F, 0, 1, ksize=3)
    h, w = y.shape
    conv = cv2.filter2D(y, cv2.CV_64F, _NOISE_K, borderType=cv2.BORDER_REFLECT)[1:-1, 1:-1]
    noise = math.sqrt(math.pi / 2) * np.abs(conv).sum() / (6.0 * max(1, (w - 2) * (h - 2)))
    return {
        "sharpness": float(np.mean(gx * gx + gy * gy)),  # Tenengrad
        "noise": float(noise),  # Immerkaer fast noise estimate
        "contrast": float(y.std()),
        "clipped": float(np.mean((y <= 0.5 / 255) | (y >= 254.5 / 255))),
    }


def score(before: dict, after: dict) -> float:
    """Higher is better: more edge energy and contrast, penalised for added noise and clipping."""
    eps = 1e-9
    s = math.log((after["sharpness"] + eps) / (before["sharpness"] + eps))
    s += 0.5 * math.log((after["contrast"] + eps) / (before["contrast"] + eps))
    s -= 1.5 * max(0.0, math.log((after["noise"] + eps) / (before["noise"] + eps)))
    s -= 10.0 * max(0.0, after["clipped"] - before["clipped"])
    return round(s, 4)


SCORE_NOTE = ("Ranking aid only: log-ratio of edge energy (Tenengrad) plus half the log-ratio of contrast, minus "
              "1.5x any increase in estimated noise (Immerkaer 1996) and 10x any increase in clipped pixels, "
              "measured on the region of interest. The examiner judges the result.")
