"""Background learning for a stationary camera.

From several images of one camera taken in the same lighting class, two pictures are estimated:

- the background image: the per-pixel median (vehicles that pass are outvoted, so they vanish
  when each place is empty in more than half of the images);
- the background-lights map: for each pixel, the fraction of images in which it is part of a bright
  lamp blob. Street lamps, illuminated signs and windows light up in nearly every night image;
  vehicle lamps move and light any one place rarely. A lamp blob whose area has a background-lights
  value >= 0.5 is treated as scenery by the lamps member.

Limits: a vehicle parked in the same place in most images becomes background; a camera that moved
between images blurs both pictures (the pixel-wise spread is reported as `instability`). Lights that
switch on in some images only (a flashing sign) are learned only when lit in more than half of them;
examiners teach the rest by deleting the false alarm, which records the place as a static light.
"""

from __future__ import annotations

import cv2
import numpy as np

from .detectors import lamp_blobs

VERSION = "1.0.0"
MIN_IMAGES = 3
MAX_IMAGES = 40


def learn(images: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, dict]:
    if len(images) < MIN_IMAGES:
        raise ValueError(f"background learning needs at least {MIN_IMAGES} images of the camera in the same lighting")
    shape = images[0].shape
    if any(im.shape != shape for im in images):
        raise ValueError("all images used for a background must have the same size")
    stack = np.stack(images[:MAX_IMAGES])
    bg = np.median(stack, axis=0).astype(np.uint8)
    H, W = shape[:2]
    counts = np.zeros((H, W), np.float32)
    for im in images[:MAX_IMAGES]:
        m = np.zeros((H, W), np.uint8)
        for bl in lamp_blobs(im):
            x, y, w, h = bl["box"]
            pad = max(2, int(0.5 * max(w, h)))
            cv2.rectangle(m, (x - pad, y - pad), (x + w + pad, y + h + pad), 1, -1)
        counts += m
    lights = counts / len(stack)
    spread = float(np.median(np.abs(stack.astype(np.int16) - bg[None].astype(np.int16))))
    stats = {"images": int(len(stack)), "instability": round(spread, 3),
             "light_pixels": int((lights >= 0.5).sum()), "version": VERSION}
    return bg, lights, stats


def lights_to_png(lights: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.clip(lights * 255 + 0.5, 0, 255).astype(np.uint8))
    return buf.tobytes()


def lights_from_png(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.0


def add_static_lights(lights: np.ndarray | None, shape: tuple[int, int], boxes: list[list[float]]) -> np.ndarray | None:
    """Mark places an examiner identified as scenery lights (from deleted lamp false alarms)."""
    if not boxes:
        return lights
    out = np.zeros(shape, np.float32) if lights is None else lights.copy()
    for x, y, w, h in boxes:
        cv2.rectangle(out, (int(x), int(y)), (int(x + w), int(y + h)), 1.0, -1)
    return out
