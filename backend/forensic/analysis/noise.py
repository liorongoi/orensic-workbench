"""Noise level map: local sensor-noise strength, to find areas whose noise differs from the rest.

Every camera adds noise of a fairly uniform strength for a given brightness. An area pasted
from another photograph, or smoothed, sharpened or retouched, often carries noise of a
different strength. The estimate in each window is the principal-component method: the
smallest eigenvalues of the covariance of the window's small patches measure the noise,
because image structure (edges, texture) concentrates in the largest ones. Windows that are
very textured, dark or saturated are still marked unreliable.
"""

from __future__ import annotations

import cv2
import numpy as np

from .. import compute
from ..filters.base import Param, luminance
from .base import AnalysisContext, AnalysisResult, register
from .compression import _colormap

PATCH = 5


def noise_level_map(img: np.ndarray, window: int = 32, step: int = 8):
    """Local noise sigma (8-bit units) on a grid of windows, and a reliability mask.

    Returns (sigma, reliable, brightness, step) with sigma[i, j] for the window whose top-left corner is
    (i * step, j * step) in image pixels. For every window, the covariance of all PATCH x PATCH
    patches inside it is built from box sums of pixel products (so the cost does not grow with
    the window), and sigma^2 is the mean of its smallest fifth of eigenvalues.
    """
    y = luminance(img).astype(np.float64) * 255.0
    H, W = y.shape
    if H < window or W < window:
        raise ValueError(f"the image is too small for {window}-pixel windows")
    # Remove the local mean first so the sums stay small and exact enough in float64.
    y = y - cv2.blur(y, (window, window), borderType=cv2.BORDER_REFLECT)
    n = window - PATCH + 1  # patch positions per window side
    hp, wp = H - PATCH + 1, W - PATCH + 1
    shifts = [(dy, dx) for dy in range(PATCH) for dx in range(PATCH)]
    views = [y[dy : dy + hp, dx : dx + wp] for dy, dx in shifts]
    gy = np.arange(0, H - window + 1, step)
    gx = np.arange(0, W - window + 1, step)

    def boxsum(a):
        s = cv2.boxFilter(a, cv2.CV_64F, (n, n), anchor=(0, 0), normalize=False, borderType=cv2.BORDER_CONSTANT)
        return s[np.ix_(gy, gx)]

    d = PATCH * PATCH
    means = np.stack(compute.pmap(boxsum, views), -1) / (n * n)
    pairs = [(i, j) for i in range(d) for j in range(i, d)]
    sums = compute.pmap(lambda ij: boxsum(views[ij[0]] * views[ij[1]]), pairs)
    C = np.empty(means.shape[:2] + (d, d))
    for (i, j), sm in zip(pairs, sums):
        C[..., i, j] = C[..., j, i] = sm / (n * n) - means[..., i] * means[..., j]
    ev = np.linalg.eigvalsh(C)
    sigma = np.sqrt(np.maximum(ev[..., : max(1, d // 5)].mean(-1), 0)).astype(np.float32)
    # Reliability: the largest eigenvalue measures structure; the most textured 20% are unreliable.
    structure = ev[..., -1]
    reliable = structure <= np.percentile(structure, 80)
    # Saturated windows (clipped highlights, black shadows) carry no noise at all.
    lum = cv2.blur(luminance(img).astype(np.float32) * 255, (window, window), anchor=(0, 0),
                   borderType=cv2.BORDER_REFLECT)[np.ix_(gy, gx)]
    reliable &= (lum > 12) & (lum < 243)
    return sigma, reliable, lum, step


def expected_noise(sigma: np.ndarray, reliable: np.ndarray, lum: np.ndarray, bins: int = 6) -> np.ndarray:
    """The image's own noise level function: typical sigma as a function of brightness.

    Sensor noise grows with signal (shot noise) and is reshaped by the camera's tone curve, so
    each window is compared with windows of similar brightness, not with the whole image.
    """
    ok = reliable if reliable.sum() >= bins * 4 else np.ones_like(reliable)
    edges = np.unique(np.percentile(lum[ok], np.linspace(0, 100, bins + 1)))
    if len(edges) < 3:
        return np.full_like(sigma, max(float(np.median(sigma[ok])), 0.25))
    centres, levels = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = ok & (lum >= a) & (lum <= b)
        if sel.sum() >= 4:
            centres.append(float(np.median(lum[sel])))
            levels.append(float(np.median(sigma[sel])))
    if not centres:
        return np.full_like(sigma, max(float(np.median(sigma[ok])), 0.25))
    return np.maximum(np.interp(lum, centres, levels), 0.25).astype(np.float32)


def inconsistent_regions(sigma: np.ndarray, reliable: np.ndarray, lum: np.ndarray, low: float, high: float,
                         min_windows: int):
    """Connected groups of reliable windows whose noise is below `low` or above `high` times that of
    windows of the same brightness elsewhere in the image."""
    ref = float(np.median(sigma[reliable])) if reliable.any() else float(np.median(sigma))
    lr = np.log(np.maximum(sigma, 1e-3) / expected_noise(sigma, reliable, lum))
    odd = ((lr < np.log(low)) | (lr > np.log(high))) & reliable
    odd = cv2.morphologyEx(odd.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(odd, connectivity=8)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_windows:
            continue
        sel = lab == i
        out.append({"windows": int(stats[i, cv2.CC_STAT_AREA]), "bbox_windows": [int(v) for v in stats[i, :4]],
                    "noise_ratio": round(float(np.exp(np.median(lr[sel]))), 2), "mask": sel})
    return ref, lr, out


@register(
    "tamper.noise_map",
    name="Noise level map",
    version="1.0.0",
    category="Local analysis",
    description="Measures the strength of the fine-grain sensor noise in small windows across the image and "
    "maps it. Areas pasted from another photograph, or smoothed, sharpened or retouched, often have noise "
    "clearly stronger or weaker than their surroundings; they are outlined when they differ by more than the "
    "chosen ratios. In the map, dark means less noise than elsewhere at that brightness and bright yellow more. "
    "Hatched areas are too textured, too dark or too bright to measure.",
    reference="B. Mahdian and S. Saic, 'Using noise inconsistencies for blind image forensics', Image and Vision "
    "Computing 27(10), 2009; D. L. Donoho and I. M. Johnstone, 'Ideal spatial adaptation by wavelet shrinkage', "
    "Biometrika 81(3), 1994.",
    applies_to=("image",),
    reading_guide="Noise naturally varies with brightness (darker areas of many cameras are noisier) and is "
    "reduced by focus blur and by strong compression, so compare areas of similar brightness and sharpness. "
    "A compact area with clearly different noise, whose outline follows an object, is consistent with local "
    "editing; noise that changes gradually across the frame is not. JPEG at low quality hides the differences.",
    params=[
        Param("window", "int", 32, "Window (px)", 16, 128, 8),
        Param("less_noise", "float", 0.2, "Outline areas with less noise than (x)", 0.05, 0.9, 0.05,
              help="Smoothing, retouching and areas pasted from a cleaner photo lower the noise; below 0.2x is rarely natural"),
        Param("more_noise", "float", 8.0, "Outline areas with more noise than (x)", 1.5, 20.0, 0.5,
              help="Shadows, texture and the tone curve raise the noise naturally, often 3-6x in processed photos"),
        Param("min_area", "int", 12, "Smallest area (windows)", 1, 500, 1),
    ],
)
def noise_map(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    img = ctx.image
    h, w = img.shape[:2]
    step = max(4, p["window"] // 4)
    sigma, reliable, lum, step = noise_level_map(img, p["window"], step)
    ref, lr, regions = inconsistent_regions(sigma, reliable, lum, p["less_noise"], p["more_noise"], p["min_area"])
    # Map: log ratio to the level expected at that brightness, dark (less noise) to yellow (more noise).
    v = np.clip(lr / np.log(4.0), -1, 1)
    heat = _colormap((v + 1) / 2)
    heat = cv2.resize(heat, (w, h), interpolation=cv2.INTER_NEAREST)
    unrel = cv2.resize((~reliable).astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
    yy, xx = np.mgrid[0:h, 0:w]
    hatch = unrel & (((xx + yy) // 4) % 3 == 0)
    heat[hatch] = 0.5
    canvas = np.clip(np.rint(heat * 255), 0, 255).astype(np.uint8)
    found = []
    for r in regions:
        m = cv2.resize(r.pop("mask").astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(canvas, cnts, -1, (255, 255, 255), 2)
        x, y, bw, bh = r.pop("bbox_windows")
        r["bbox_px"] = [x * step, y * step, bw * step + p["window"], bh * step + p["window"]]
        r["area_share"] = round(r["windows"] / max(1, sigma.size), 4)
        found.append(r)
    findings = {
        "typical_noise_sigma": round(ref, 3),
        "noise_units": "standard deviation in 8-bit grey levels, from the finest diagonal wavelet band",
        "reliable_share": round(float(reliable.mean()), 3),
        "regions": found,
    }
    flags = []
    if found:
        ratios = ", ".join(f"{r['noise_ratio']}x" for r in found[:5])
        flags.append({"level": "info", "text": f"{len(found)} area(s) with noise {ratios} "
                      "the typical level; compare with areas of similar brightness and sharpness before drawing conclusions."})
    if reliable.mean() < 0.3:
        flags.append({"level": "info", "text": "Most of the image is too textured, dark or bright to measure its noise reliably."})
    return AnalysisResult(
        summary=f"Typical noise {ref:.2f} grey levels; " + (f"{len(found)} area(s) outside {p['less_noise']}x to {p['more_noise']}x." if found else "no area differs clearly."),
        findings=findings,
        flags=flags,
        maps={"noise_map": canvas.astype(np.float32) / 255.0},
    )
