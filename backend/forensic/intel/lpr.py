"""Licence-plate recognition: localisation, rectification, OCR candidates and format rules.

The pipeline is deliberately transparent:

1. Localise. Two independent proposers: (a) character grouping — dark-on-light or light-on-dark
   connected components of character size and shape that line up horizontally; (b) the
   morphological "black-hat + horizontal gradient + closing" plate finder. Each candidate is
   scored by how many character-like components it contains and how well its aspect ratio fits
   a plate. An optional learned plate detector (LPD-YuNet) adds proposals when installed; it was
   trained on Chinese plates and is off by default.
2. Rectify. The candidate's four corners are mapped to a fronto-parallel rectangle (planar
   homography), 64 pixels high.
3. Read. The OCR engine runs on several binarisations and scales of the rectified crop; every
   distinct reading is kept with its confidence and how many variants produced it.
4. Format rules. Readings are checked against configurable regular expressions per country.
   Where a reading fails a format only because of commonly confused characters (0/O, 1/I, 5/S,
   8/B, 2/Z, 6/G), the format-fitting alternative is added as a separate, lower-confidence
   candidate marked "format-adjusted".

Nothing here confirms a plate: every observation starts "unreviewed" and keeps its crop and all
alternative readings until an examiner confirms, corrects or rejects it.
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from dataclasses import dataclass, field

import cv2
import numpy as np

VERSION = "1.0.0"

# Plate formats: regular expressions over the reading with spaces and dashes removed.
FORMATS: dict[str, dict] = {
    "IL": {"label": "Israel (7 or 8 digits)", "patterns": [r"\d{7}", r"\d{8}"], "alphabet": "digits"},
    "GB": {"label": "United Kingdom (AB12CDE)", "patterns": [r"[A-Z]{2}\d{2}[A-Z]{3}"], "alphabet": "alnum"},
    "EU": {"label": "Generic EU (4-8 letters and digits)", "patterns": [r"[A-Z0-9]{4,8}"], "alphabet": "alnum"},
    "US": {"label": "Generic US (2-8 letters and digits)", "patterns": [r"[A-Z0-9]{2,8}"], "alphabet": "alnum"},
    "ANY": {"label": "Any (3-10 letters and digits)", "patterns": [r"[A-Z0-9]{3,10}"], "alphabet": "alnum"},
}
TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "T": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "A": "4"}
TO_LETTER = {v: k for k, v in {"O": "0", "I": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "A": "4"}.items()}


@dataclass
class PlateCandidate:
    corners: list[list[float]]  # TL, TR, BR, BL in image pixels
    score: float
    proposer: str
    chars: int = 0


@dataclass
class Reading:
    text: str
    confidence: float  # 0-1
    votes: int
    variants: list[str] = field(default_factory=list)
    format_adjusted: bool = False
    format: str | None = None


def normalise_text(t: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", t.upper())


# 1. Localisation --------------------------------------------------------------------------------


def _order(pts: np.ndarray) -> np.ndarray:
    """Corners in TL, TR, BR, BL order."""
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def _char_components(gray: np.ndarray, invert: bool) -> list[tuple[int, int, int, int]]:
    g = 255 - gray if invert else gray
    h, w = g.shape
    block = max(15, (min(h, w) // 8) | 1)
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 7)
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if ch < 8 or ch > h * 0.5 or cw < 2:
            continue
        r = ch / cw
        fill = area / (cw * ch)
        if 1.0 <= r <= 6.0 and 0.15 <= fill <= 0.95:
            out.append((int(x), int(y), int(cw), int(ch)))
    return out


def _group_rows(comps: list[tuple[int, int, int, int]], min_chars: int = 4) -> list[list[tuple[int, int, int, int]]]:
    """Chains of components of similar height whose centres line up horizontally."""
    comps = sorted(comps, key=lambda c: c[0])
    groups: list[list] = []
    used = set()
    for i, a in enumerate(comps):
        if i in used:
            continue
        chain = [a]
        last = a
        for j in range(i + 1, len(comps)):
            if j in used:
                continue
            b = comps[j]
            gap = b[0] - (last[0] + last[2])
            if gap > 1.5 * max(last[3], b[3]):
                break
            same_h = 0.7 <= b[3] / last[3] <= 1.4
            cy_a, cy_b = last[1] + last[3] / 2, b[1] + b[3] / 2
            if same_h and abs(cy_a - cy_b) < 0.35 * last[3] and gap > -0.3 * last[2]:
                chain.append(b)
                used.add(j)
                last = b
        if len(chain) >= min_chars:
            groups.append(chain)
    return groups


def propose_char_groups(gray: np.ndarray, min_chars: int = 4) -> list[PlateCandidate]:
    cands = []
    for invert in (False, True):
        for chain in _group_rows(_char_components(gray, invert), min_chars):
            pts = np.array([[x, y] for x, y, w, h in chain] + [[x + w, y + h] for x, y, w, h in chain]
                           + [[x + w, y] for x, y, w, h in chain] + [[x, y + h] for x, y, w, h in chain], np.float32)
            rect = cv2.minAreaRect(pts)
            (cx, cy), (rw, rh), ang = rect
            if rw < rh:
                rw, rh, ang = rh, rw, ang + 90
            ch = float(np.median([c[3] for c in chain]))
            # Grow to the plate border: about 0.35 character heights above/below, 0.5 at the ends.
            box = cv2.boxPoints(((cx, cy), (rw + ch * 1.0, rh + ch * 0.7), ang))
            aspect = (rw + ch) / (rh + ch * 0.7)
            fit = np.exp(-((np.log(aspect) - np.log(4.0)) ** 2) / 0.8)
            cands.append(PlateCandidate(_order(box).tolist(), float(min(1.0, len(chain) / 7) * (0.5 + 0.5 * fit)),
                                        "char_groups" + ("_inverted" if invert else ""), len(chain)))
    return cands


def propose_morphology(gray: np.ndarray) -> list[PlateCandidate]:
    h, w = gray.shape
    kw = max(9, int(round(w / 22)) | 1)
    kh = max(3, int(round(kw / 3)) | 1)
    rect = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, kh))
    bh = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, rect)
    gx = np.abs(cv2.Sobel(bh, cv2.CV_32F, 1, 0, ksize=3))
    gx = (255 * gx / max(float(gx.max()), 1.0)).astype(np.uint8)
    gx = cv2.GaussianBlur(gx, (5, 5), 0)
    closed = cv2.morphologyEx(gx, cv2.MORPH_CLOSE, rect)
    _, th = cv2.threshold(closed, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    th = cv2.dilate(cv2.erode(th, None, iterations=2), None, iterations=2)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
        if rw < rh:
            rw, rh, ang = rh, rw, ang + 90
        if rh < 8 or rw < 24 or rw * rh > 0.25 * w * h:
            continue
        aspect = rw / rh
        if not 1.8 <= aspect <= 7.5 or abs(((ang + 90) % 180) - 90) > 30:
            continue
        box = cv2.boxPoints(((cx, cy), (rw * 1.08, rh * 1.3), ang))
        fit = np.exp(-((np.log(aspect) - np.log(4.0)) ** 2) / 0.8)
        out.append(PlateCandidate(_order(box).tolist(), float(0.4 * fit), "morphology"))
    return out


def _text_score(gray: np.ndarray, corners: np.ndarray) -> float:
    """How much a region looks like a line of characters: density of strong vertical edges in its
    middle band, relative to the image as a whole (0-1)."""
    r = rectify(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), corners.tolist(), height=32)
    g = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY).astype(np.float32)
    band = g[8:24]
    gx = np.abs(np.diff(band, axis=1))
    contrast = float(np.percentile(band, 90) - np.percentile(band, 10))
    if contrast < 25:
        return 0.0
    crossings = float((gx > contrast * 0.35).sum(axis=1).mean()) / max(1, band.shape[1]) * 32
    return float(np.clip(crossings / 3.0, 0, 1)) * min(1.0, contrast / 80)


def propose_bright_regions(gray: np.ndarray) -> list[PlateCandidate]:
    """Plates are usually lighter than the bodywork around them (white or yellow reflective sheet)."""
    h, w = gray.shape
    out = []
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    for t in (np.percentile(blur, 70), np.percentile(blur, 80), np.percentile(blur, 90)):
        _, th = cv2.threshold(blur, float(t), 255, cv2.THRESH_BINARY)
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, w // 60), 3)))
        cnts, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
            if rw < rh:
                rw, rh, ang = rh, rw, ang + 90
            if rh < 10 or rw < 30 or not 0.001 * w * h <= rw * rh <= 0.15 * w * h:
                continue
            aspect = rw / rh
            if not 1.8 <= aspect <= 7.5 or abs(((ang + 90) % 180) - 90) > 30:
                continue
            # Rectangular enough: the contour fills most of its rotated box.
            if cv2.contourArea(c) < 0.6 * rw * rh:
                continue
            box = _order(cv2.boxPoints(((cx, cy), (rw * 1.04, rh * 1.08), ang)))
            ts = _text_score(gray, box)
            if ts <= 0.15:
                continue
            fit = np.exp(-((np.log(aspect) - np.log(4.0)) ** 2) / 0.8)
            out.append(PlateCandidate(box.tolist(), float(0.25 + 0.5 * ts + 0.2 * fit), "bright_region"))
    return out


def _iou(a: list, b: list) -> float:
    ra = cv2.boundingRect(np.array(a, np.float32))
    rb = cv2.boundingRect(np.array(b, np.float32))
    x0, y0 = max(ra[0], rb[0]), max(ra[1], rb[1])
    x1, y1 = min(ra[0] + ra[2], rb[0] + rb[2]), min(ra[1] + ra[3], rb[1] + rb[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter / float(ra[2] * ra[3] + rb[2] * rb[3] - inter + 1e-9)


def localise(bgr: np.ndarray, max_candidates: int = 3, upscale_to: int = 640) -> list[PlateCandidate]:
    """Plate candidates in a vehicle crop or a whole frame, best first (corners in input pixels)."""
    h, w = bgr.shape[:2]
    s = max(1.0, upscale_to / max(w, 1)) if w < upscale_to else 1.0
    img = cv2.resize(bgr, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC) if s > 1 else bgr
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(2.0, (8, 8)).apply(gray)
    cands = propose_char_groups(gray) + propose_morphology(gray) + propose_bright_regions(gray)
    for c in cands:
        if c.proposer == "morphology":
            c.score = float(0.2 + 0.6 * _text_score(gray, np.array(c.corners, np.float32)))
    # Morphology candidates that agree with a character group strengthen it.
    for c in cands:
        others = {o.proposer.split("_")[0] for o in cands if o is not c and _iou(o.corners, c.corners) > 0.3}
        others.discard(c.proposer.split("_")[0])
        c.score = min(1.0, c.score + 0.12 * len(others))
    cands.sort(key=lambda c: -c.score)
    kept: list[PlateCandidate] = []
    for c in cands:
        if all(_iou(c.corners, k.corners) < 0.3 for k in kept):
            kept.append(c)
    for c in kept:
        c.corners = (np.array(c.corners) / s).round(2).tolist()
    return kept[:max_candidates]


# 2. Rectification ---------------------------------------------------------------------------------------


def rectify(bgr: np.ndarray, corners: list[list[float]], height: int = 64) -> np.ndarray:
    tl, tr, br, bl = np.asarray(corners, np.float32)
    wpx = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    hpx = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    aspect = max(1.0, wpx / max(hpx, 1e-6))
    W = int(round(height * aspect))
    Hm = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), np.float32([[0, 0], [W, 0], [W, height], [0, height]]))
    return cv2.warpPerspective(bgr, Hm, (W, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# 3. OCR ---------------------------------------------------------------------------------------------


class OcrEngine:
    """Adapter contract: read(plate_bgr) -> list[(text, confidence 0-1)]."""

    id = "none"
    version = "n/a"

    def available(self) -> bool:
        return False

    def read(self, gray: np.ndarray, alphabet: str) -> list[tuple[str, float]]:  # pragma: no cover
        raise NotImplementedError


class TesseractOcr(OcrEngine):
    id = "tesseract"

    def __init__(self, binary: str | None = None):
        self.binary = binary or shutil.which("tesseract")
        self.version = "not installed"
        if self.binary:
            try:
                out = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=10)
                self.version = (out.stdout or out.stderr).splitlines()[0].strip()
            except Exception:  # noqa: BLE001
                self.binary = None

    def available(self) -> bool:
        return bool(self.binary)

    def read(self, gray: np.ndarray, alphabet: str) -> list[tuple[str, float]]:
        # No character whitelist: Tesseract 5 reports zero confidence when one is set. Readings are
        # normalised to A-Z/0-9 afterwards and checked against the plate formats instead.
        ok, png = cv2.imencode(".png", gray)
        out = []
        for psm in ("7", "8"):
            cmd = [self.binary, "stdin", "stdout", "--psm", psm, "--oem", "1", "-l", "eng",
                   "-c", "load_system_dawg=0", "-c", "load_freq_dawg=0", "tsv"]
            p = subprocess.run(cmd, input=png.tobytes(), capture_output=True, timeout=30)
            words, weights = [], []
            for line in p.stdout.decode(errors="replace").splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) == 12 and float(parts[10]) >= 0:
                    t = normalise_text(parts[11])
                    if t:
                        words.append(t)
                        weights.append((len(t), float(parts[10]) / 100))
            text = "".join(words)
            if alphabet == "digits":
                text = "".join(TO_DIGIT.get(c, c) for c in text) if sum(c.isdigit() for c in text) >= len(text) / 2 else text
            if text:
                conf = sum(n * c for n, c in weights) / sum(n for n, _ in weights)
                out.append((text, float(conf)))
        return out


def ocr_variants(rect_bgr: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Several binarisations and scales of the rectified plate: dark-on-light text for the OCR."""
    g = cv2.cvtColor(rect_bgr, cv2.COLOR_BGR2GRAY)
    # Text is the minority: if more than half the pixels are dark, the plate is light-on-dark.
    _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if (otsu == 0).mean() > 0.5:
        g = 255 - g
    out = []
    for scale in (1.0, 1.5):
        gs = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale != 1 else g
        gs = cv2.copyMakeBorder(gs, 10, 10, 10, 10, cv2.BORDER_REPLICATE)
        _, o = cv2.threshold(gs, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        a = cv2.adaptiveThreshold(gs, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
        out += [(f"grey@{scale}", gs), (f"otsu@{scale}", o), (f"adaptive@{scale}", a)]
    return out


def read_plate(rect_bgr: np.ndarray, engine: OcrEngine, country: str = "ANY") -> list[Reading]:
    fmt = FORMATS.get(country) or FORMATS["ANY"]
    votes: dict[str, list[tuple[float, str]]] = {}
    for name, img in ocr_variants(rect_bgr):
        for text, conf in engine.read(img, fmt["alphabet"]):
            votes.setdefault(text, []).append((conf, name))
    n_var = len(ocr_variants(rect_bgr)) * 2
    readings = []
    for text, vs in votes.items():
        conf = max(c for c, _ in vs) * (0.6 + 0.4 * min(1.0, len(vs) / (n_var / 2)))
        readings.append(Reading(text, round(conf, 4), len(vs), sorted({v for _, v in vs}),
                                format=matching_format(text, country)))
    # Format-adjusted alternatives for readings that fail the format.
    extra = []
    for r in readings:
        if r.format is None and country in FORMATS and country != "ANY":
            for alt in format_alternatives(r.text, country):
                if alt not in votes and all(alt != e.text for e in extra):
                    extra.append(Reading(alt, round(r.confidence * 0.7, 4), 0, [f"adjusted from {r.text}"],
                                         format_adjusted=True, format=matching_format(alt, country)))
    readings += extra
    readings.sort(key=lambda r: (r.format is None, -r.confidence))
    return readings[:8]


def matching_format(text: str, country: str) -> str | None:
    fmt = FORMATS.get(country)
    if not fmt:
        return None
    for p in fmt["patterns"]:
        if re.fullmatch(p, text):
            return country
    return None


def format_alternatives(text: str, country: str, limit: int = 4) -> list[str]:
    """Substitute commonly confused characters until the text fits one of the country's patterns."""
    fmt = FORMATS[country]
    out = []
    if fmt["alphabet"] == "digits":
        alt = "".join(TO_DIGIT.get(c, c) for c in text)
        if matching_format(alt, country):
            out.append(alt)
        return out
    for p in fmt["patterns"]:
        # Position-wise class from simple patterns like [A-Z]{2}\d{2}[A-Z]{3}.
        classes = []
        for m in re.finditer(r"(\[A-Z\]|\\d|\[A-Z0-9\])\{(\d+)(?:,(\d+))?\}", p):
            if m.group(3):
                classes = None
                break
            classes += [m.group(1)] * int(m.group(2))
        if not classes or len(classes) != len(text):
            continue
        alt = []
        for c, k in zip(text, classes):
            if k == "\\d" and not c.isdigit():
                c = TO_DIGIT.get(c, c)
            elif k == "[A-Z]" and c.isdigit():
                c = TO_LETTER.get(c, c)
            alt.append(c)
        a = "".join(alt)
        if a != text and matching_format(a, country):
            out.append(a)
    return out[:limit]


# Vehicle colour --------------------------------------------------------------------------------------------

COLOURS = [("white", None), ("silver/grey", None), ("black", None), ("red", 0), ("orange", 20), ("yellow", 30),
           ("green", 60), ("blue", 110), ("purple", 140)]


def vehicle_colour(bgr: np.ndarray) -> dict:
    """Dominant body colour from the central part of a vehicle box (HSV rules). An estimate: lighting,
    white balance and compression shift apparent colour; it is a search aid, not an identification."""
    h, w = bgr.shape[:2]
    roi = bgr[int(h * 0.25):int(h * 0.75), int(w * 0.15):int(w * 0.85)]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    s, v = hsv[:, 1] / 255, hsv[:, 2] / 255
    votes = {}
    chroma = s > 0.35
    votes["black"] = float(((v < 0.22)).mean())
    votes["white"] = float(((v > 0.75) & (s < 0.15)).mean())
    votes["silver/grey"] = float(((v >= 0.22) & (v <= 0.75) & (s < 0.2)).mean())
    hue = hsv[:, 0] * 2
    for name, centre in (("red", 0), ("orange", 30), ("yellow", 55), ("green", 120), ("blue", 220), ("purple", 285)):
        d = np.minimum(np.abs(hue - centre), 360 - np.abs(hue - centre))
        votes[name] = float((chroma & (v > 0.2) & (d < 22)).mean())
    best = max(votes, key=votes.get)
    total = sum(votes.values()) or 1
    return {"colour": best, "share": round(votes[best] / total, 3),
            "scores": {k: round(v / total, 3) for k, v in sorted(votes.items(), key=lambda kv: -kv[1])[:4]},
            "method": "HSV pixel voting over the central 50% x 70% of the vehicle box",
            "limitations": "Apparent colour depends on lighting, white balance and compression; treat as a search aid."}
