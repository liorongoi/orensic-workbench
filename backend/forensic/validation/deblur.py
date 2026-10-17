"""Deblur benchmarks: score the deconvolution filters on public datasets with ground truth.

    python -m forensic.validation.deblur --levin DIR --out DIR
    python -m forensic.validation.deblur --images set3c=DIR --kernels DIR --sigma 2.55 --sigma 7.65 --out DIR
    ... --reference DPIR=DIR      also score another method's outputs with the same measures

Two protocols, each scored the way the papers that use it score it, so the numbers
can be set beside published ones:

* Levin et al. 2009 ("Understanding and evaluating blind deconvolution algorithms",
  CVPR): 4 photographs blurred by real camera shake with 8 measured kernels (32 images,
  255 x 255, grey). PSNR and SSIM after the best shift within +-10 px in 0.25 px steps
  and a 15 px border crop (Levin's comp_upto_shift, as used by later papers).
  Expected layout: im{i}_kernel{j}_img.png, gt/im{i}.png, gt/kernel{j}.png.
* The IRCNN/DPIR synthetic protocol (Zhang et al., CVPR 2017 and TPAMI 2021): sharp
  test images (Set3C, Set12) blurred with Levin's 8 kernels by circular convolution,
  then white Gaussian noise of sigma 2.55 or 7.65 grey levels drawn with NumPy seed 0.
  PSNR on 8-bit images with no border crop, as in DPIR's main_dpir_deblur.py.

Every filter is run with the true kernel (non-blind, which is what the filters do),
over a small grid of its strength setting; the table reports the best setting for
the whole set and also the app's default. On Levin's real blur, the filters are also
run with the closest kernel the app's three blur shapes can describe, and with the
app's own motion-blur estimate, which is what an examiner gets without the true kernel.
"""

from __future__ import annotations

import json
import math
import platform
import re
import time
from datetime import date
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from .. import __version__
from ..filters.base import REGISTRY
from ..filters.deblur import estimate_kernel, estimate_motion_blur, make_psf, richardson_lucy_psf, streak_psf, tv_psf, wiener_psf
from ..filters.multiframe import turbulence_fuse

# Settings each filter is tried at. The app's default is always among them.
GRID: dict[str, list[dict]] = {
    "wiener": [{"nsr": v} for v in (1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 0.1)],
    "richardson_lucy": [{"iterations": n, "tv": t} for n in (20, 50, 150, 300) for t in (0.0, 0.002, 0.005)],
    "tv": [{"regularisation": r, "iterations": 3} for r in (1e-4, 3e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2)],
}
DEFAULT = {"wiener": {"nsr": 0.01}, "richardson_lucy": {"iterations": 20, "tv": 0.0},
           "tv": {"regularisation": 0.002, "iterations": 3}}
NAMES = {"wiener": "Wiener", "richardson_lucy": "Richardson-Lucy", "tv": "Total variation", "blind": "Blind (estimated kernel)",
         "variable": "Variable motion (4 streaks drawn)", "one_streak": "Uniform motion (1 streak drawn)", "single": "One frame", "mean": "Mean of frames", "median": "Median of frames",
         "turbulence": "Turbulence removal"}
BLIND_GRID = [{"smoothness": l, "regularisation": r} for l in (0.004, 0.01, 0.02) for r in (5e-4, 1e-3, 2e-3, 5e-3)]
DEFAULT["blind"] = {"smoothness": 0.01, "regularisation": 0.002}


def deconvolve(method: str, img: np.ndarray, psf: np.ndarray, s: dict) -> np.ndarray:
    """Run one filter on an H x W or H x W x C float image in [0, 1]."""
    x = img[..., None] if img.ndim == 2 else img
    if method == "wiener":
        out = wiener_psf(x, psf, s["nsr"])
    elif method == "richardson_lucy":
        out = richardson_lucy_psf(x, psf, s["iterations"], s["tv"])
    else:
        out = tv_psf(x, psf, s["regularisation"], s["iterations"])
    return out[..., 0] if img.ndim == 2 else out


# Measures -----------------------------------------------------------------------------------


def psnr(a: np.ndarray, b: np.ndarray, peak: float = 1.0) -> float:
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return float("inf") if mse == 0 else 10 * math.log10(peak * peak / mse)


def ssim(a: np.ndarray, b: np.ndarray, peak: float = 1.0) -> float:
    """SSIM of Wang et al. 2004 (11 x 11 Gaussian window, sigma 1.5, valid region), averaged over channels."""
    a, b = np.asarray(a, np.float64) / peak * 255, np.asarray(b, np.float64) / peak * 255
    if a.ndim == 3:
        return float(np.mean([ssim(a[..., c], b[..., c], 255.0) for c in range(a.shape[2])]))
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = cv2.getGaussianKernel(11, 1.5)
    win = g @ g.T

    def f(x):
        return cv2.filter2D(x, -1, win)[5:-5, 5:-5]

    mu1, mu2 = f(a), f(b)
    s11, s22, s12 = f(a * a) - mu1**2, f(b * b) - mu2**2, f(a * b) - mu1 * mu2
    return float(np.mean(((2 * mu1 * mu2 + c1) * (2 * s12 + c2)) / ((mu1**2 + mu2**2 + c1) * (s11 + s22 + c2))))


def align_upto_shift(est: np.ndarray, gt: np.ndarray, maxshift: int = 10, border: int = 15) -> tuple[np.ndarray, np.ndarray]:
    """Levin's comp_upto_shift: crop the border, then shift the estimate (bilinear, 0.25 px steps) to fit best.

    A blind method may return the image shifted by a few pixels with an equally shifted
    kernel; the shift is not an error, so the papers remove it before scoring.
    """
    ref = gt[border:-border, border:-border].astype(np.float32)
    src = est.astype(np.float32)
    h, w = ref.shape

    def shifted(sy, sx):
        m = np.float32([[1, 0, -(border + sx)], [0, 1, -(border + sy)]])
        return cv2.warpAffine(src, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def err(sy, sx):
        return float(np.sum((shifted(sy, sx) - ref) ** 2))

    # Whole pixels first, then quarter pixels around the best one: the same optimum as
    # Levin's exhaustive quarter-pixel search on these images, at a fraction of the cost.
    by, bx = min(((sy, sx) for sy in range(-maxshift, maxshift + 1) for sx in range(-maxshift, maxshift + 1)),
                 key=lambda t: err(*t))
    fine = [(by + dy, bx + dx) for dy in np.arange(-1, 1.001, 0.25) for dx in np.arange(-1, 1.001, 0.25)
            if abs(by + dy) <= maxshift and abs(bx + dx) <= maxshift]
    sy, sx = min(fine, key=lambda t: err(*t))
    return shifted(sy, sx), ref


# Kernels ------------------------------------------------------------------------------------


def _blur(x: np.ndarray, k: np.ndarray, circular: bool) -> np.ndarray:
    """Convolve (true convolution, kernel centred) with circular or symmetric boundaries."""
    if circular:
        out = np.empty_like(x, dtype=np.float64)
        h, w = x.shape[:2]
        pad = np.zeros((h, w))
        pad[: k.shape[0], : k.shape[1]] = k
        K = np.fft.rfft2(np.roll(pad, (-(k.shape[0] // 2), -(k.shape[1] // 2)), (0, 1)))
        xs = x[..., None] if x.ndim == 2 else x
        out = np.dstack([np.fft.irfft2(np.fft.rfft2(xs[..., c]) * K, s=(h, w)) for c in range(xs.shape[2])])
        return out[..., 0] if x.ndim == 2 else out
    return cv2.filter2D(x.astype(np.float64), -1, k[::-1, ::-1].copy(), borderType=cv2.BORDER_REFLECT)


def _kernel_distance(a: np.ndarray, b: np.ndarray) -> float:
    """L2 distance between two kernels at their best relative integer shift."""
    n = a.shape[0] + b.shape[0] + 1
    A, B = np.zeros((n, n)), np.zeros((n, n))
    A[: a.shape[0], : a.shape[1]] = a
    B[: b.shape[0], : b.shape[1]] = b
    corr = np.fft.irfft2(np.fft.rfft2(A) * np.conj(np.fft.rfft2(B)), s=(n, n))
    return float(np.sum(a * a) + np.sum(b * b) - 2 * corr.max())


def _fit_delta(size: int) -> np.ndarray:
    k = np.zeros((size, size))
    k[size // 2, size // 2] = 1.0
    return k


def closest_parametric_psf(k: np.ndarray) -> tuple[np.ndarray, dict]:
    """The motion, defocus or Gaussian blur the app can describe that is closest to kernel k."""
    base = {"psf": "motion", "length": 9.0, "angle": 0.0, "radius": 3.0, "sigma": 1.5}
    cands = [{**base, "psf": "motion", "length": float(L), "angle": float(a)}
             for L in range(1, max(k.shape) + 2) for a in range(0, 180, 3)]
    cands += [{**base, "psf": "defocus", "radius": r} for r in np.arange(0.5, max(k.shape) / 2 + 1, 0.5)]
    cands += [{**base, "psf": "gaussian", "sigma": s} for s in np.arange(0.3, max(k.shape) / 3 + 0.1, 0.1)]
    best = min(cands, key=lambda p: _kernel_distance(make_psf(p), k))
    keys = {"motion": ("length", "angle"), "defocus": ("radius",), "gaussian": ("sigma",)}[best["psf"]]
    return make_psf(best), {"psf": best["psf"], **{key: round(float(best[key]), 2) for key in keys}}


def _read(path: Path) -> np.ndarray:
    im = Image.open(path)
    arr = np.asarray(im.convert("L") if im.mode in ("L", "I;16", "I", "P", "LA") else im.convert("RGB"))
    return arr.astype(np.float64) / 255.0


def _read_kernel(path: Path) -> np.ndarray:
    k = np.load(path) if path.suffix == ".npy" else np.asarray(Image.open(path).convert("L"), np.float64)
    k = np.asarray(k, np.float64)
    return k / k.sum()


def load_kernels(folder: Path) -> list[np.ndarray]:
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".npy", ".png"))
    files.sort(key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.stem)])
    if not files:
        raise SystemExit(f"no kernels (.npy or .png) in {folder}")
    return [_read_kernel(p) for p in files]


# Levin et al. 2009 --------------------------------------------------------------------------


def levin_cases(folder: Path) -> list[dict]:
    """The 32 cases. Each stored kernel is used in the orientation that re-blurs the sharp image to the blurred one.

    Copies of the dataset in circulation differ in whether some kernel images are stored
    rotated by 180 degrees; fitting the orientation to the data removes that ambiguity.
    """
    cases = []
    for blurred in sorted(folder.glob("im*_kernel*_img.png")):
        i, j = (int(v) for v in re.match(r"im(\d+)_kernel(\d+)_img", blurred.stem).groups())
        y, x = _read(blurred), _read(folder / "gt" / f"im{i}.png")
        k = _read_kernel(folder / "gt" / f"kernel{j}.png")

        def fit(kk):
            b = _blur(x, kk, circular=False)
            c = slice(25, -25)
            return min(float(np.mean((np.roll(b, (dy, dx), (0, 1))[c, c] - y[c, c]) ** 2))
                       for dy in range(-6, 7) for dx in range(-6, 7))

        if fit(k[::-1, ::-1]) < fit(k):
            k = k[::-1, ::-1].copy()
        cases.append({"name": f"im{i}_kernel{j}", "blurred": y, "sharp": x, "kernel": k})
    if not cases:
        raise SystemExit(f"no Levin images (im*_kernel*_img.png) in {folder}")
    return cases


def levin_score(est: np.ndarray, sharp: np.ndarray) -> dict:
    a, b = align_upto_shift(est, sharp)
    return {"psnr": psnr(a, b), "ssim": ssim(a, b)}


# IRCNN / DPIR synthetic protocol ------------------------------------------------------------


def synthetic_cases(images: Path, kernels: list[np.ndarray], sigma: float, label: str) -> list[dict]:
    files = sorted(p for p in images.iterdir() if p.suffix.lower() in (".png", ".bmp", ".tif", ".jpg"))
    cases = []
    for ki, k in enumerate(kernels):
        for f in files:
            x8 = (_read(f) * 255).round()
            h, w = x8.shape[:2]
            x8 = x8[: h - h % 8, : w - w % 8]  # DPIR's modcrop(8)
            x = x8 / 255.0
            y = _blur(x, k, circular=True).astype(np.float32)
            y = y + np.random.RandomState(0).normal(0, sigma / 255.0, y.shape)  # seed 0 per image, as DPIR
            cases.append({"name": f"{label}_{f.stem}_k{ki + 1}_s{sigma:g}", "blurred": y.astype(np.float64),
                          "sharp": x, "kernel": k})
    return cases


def synthetic_score(est: np.ndarray, sharp: np.ndarray, border: int = 20) -> dict:
    """DPIR's PSNR on the whole 8-bit image, plus PSNR and SSIM away from the image edge.

    The protocol blurs circularly, as if the image wrapped around, which a filter built for
    real photographs (padding by mirroring) does not assume; the interior score leaves out
    the border where that convention, not the deblurring, decides the result.
    """
    e8, s8 = np.clip(np.round(est * 255), 0, 255), sharp * 255
    i = (slice(border, -border), slice(border, -border))
    return {"psnr": psnr(e8, s8, 255.0), "ssim": ssim(e8, s8, 255.0),
            "psnr_interior": psnr(e8[i], s8[i], 255.0), "ssim_interior": ssim(e8[i], s8[i], 255.0)}


# Running ------------------------------------------------------------------------------------


def summarise(scores: dict[str, dict]) -> dict:
    """Mean of each measure over the cases, and the per-case values."""
    keys = next(iter(scores.values())).keys()
    out = {k: round(float(np.mean([v[k] for v in scores.values()])), 3 if k.startswith("psnr") else 4) for k in keys}
    out["n"] = len(scores)
    out["per_image"] = {name: {k: round(v[k], 3 if k.startswith("psnr") else 4) for k in keys} for name, v in scores.items()}
    return out


def _brief(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "per_image"}


def evaluate(cases: list[dict], score: Callable, psf_for: Callable[[dict], np.ndarray] | None = None,
             methods=("wiener", "richardson_lucy", "tv"), grid=GRID,
             runner: Callable[[str, dict, dict], np.ndarray] | None = None) -> dict:
    """Per method: the best setting over the whole set (by mean PSNR), the app's default, and every setting tried.

    `runner(method, case, setting)` produces an estimate; by default the method's filter
    is run with the PSF that `psf_for(case)` gives (the true kernel unless told otherwise).
    """
    psf_for = psf_for or (lambda c: c["kernel"])
    runner = runner or (lambda m, c, s: deconvolve(m, c["blurred"], psf_for(c), s))
    out = {}
    for m in methods:
        settings = list(grid[m]) + ([DEFAULT[m]] if m in DEFAULT and DEFAULT[m] not in grid[m] else [])
        rows = []
        for s in settings:
            r = summarise({c["name"]: score(runner(m, c, s), c["sharp"]) for c in cases})
            rows.append({"setting": s, **r})
        best = max(rows, key=lambda r: r["psnr"])
        out[m] = {"best": best, "grid": [_brief(r) for r in rows]}
        if m in DEFAULT:
            out[m]["default"] = _brief(next(r for r in rows if r["setting"] == DEFAULT[m]))
    return out


def score_folder(cases: list[dict], score: Callable, folder: Path) -> dict:
    """Score another method's outputs, saved as <case name>.png (or .npy, float in [0, 1])."""
    sc = {}
    for c in cases:
        npy, png = folder / f"{c['name']}.npy", folder / f"{c['name']}.png"
        if npy.exists():
            est = np.load(npy).astype(np.float64)
        elif png.exists():
            est = _read(png)
        else:
            continue
        sc[c["name"]] = score(est, c["sharp"])
    return summarise(sc) if sc else {}


def _save_inputs(cases: list[dict], folder: Path | None, kernels: bool = False):
    if not folder:
        return
    folder.mkdir(parents=True, exist_ok=True)
    for c in cases:
        np.save(folder / f"{c['name']}.npy", c["blurred"].astype(np.float32))
        if kernels:
            np.save(folder / f"{c['name']}.kernel.npy", c["kernel"])


def run_levin(folder: Path, references: dict[str, Path], save_inputs: Path | None = None) -> dict:
    cases = levin_cases(folder)
    _save_inputs(cases, save_inputs, kernels=True)
    res = {"n": len(cases), "blurred": _brief(summarise({c["name"]: levin_score(c["blurred"], c["sharp"]) for c in cases}))}
    res["true_kernel"] = evaluate(cases, levin_score)
    fitted = {}
    for c in cases:
        j = c["name"].split("_")[1]
        if j not in fitted:
            fitted[j] = closest_parametric_psf(c["kernel"])
    res["closest_app_shape"] = evaluate(cases, levin_score, lambda c: fitted[c["name"].split("_")[1]][0])
    res["closest_app_shape"]["shapes"] = {j: v[1] for j, v in sorted(fitted.items())}
    estimates = {}
    for c in cases:
        e = estimate_motion_blur(c["blurred"])
        estimates[c["name"]] = make_psf({"psf": "motion", "length": e["length"], "angle": e["angle"]})
    res["app_motion_estimate"] = evaluate(cases, levin_score, lambda c: estimates[c["name"]])
    res["references"] = {name: score_folder(cases, levin_score, p) for name, p in references.items()}
    res["extra"] = levin_extras(cases)
    return res


def run_synthetic(sets: dict[str, Path], kernels: list[np.ndarray], sigmas: list[float], references: dict[str, Path],
                  save_inputs: Path | None = None) -> dict:
    res = {}
    for sigma in sigmas:
        for label, folder in sets.items():
            cases = synthetic_cases(folder, kernels, sigma, label)
            _save_inputs(cases, save_inputs)
            res[f"{label} sigma {sigma:g}"] = {
                "n": len(cases),
                "blurred": _brief(summarise({c["name"]: synthetic_score(c["blurred"], c["sharp"]) for c in cases})),
                "true_kernel": evaluate(cases, synthetic_score),
                "references": {n: score_folder(cases, synthetic_score, p) for n, p in references.items()}}
    return res


# Blur the examiner does not have to describe -------------------------------------------------


def shake_trajectory(length: float, seed: int, steps: int = 64) -> np.ndarray:
    """A random camera-shake path (Boracchi and Foi 2012): inertia, a pull back to the start, and occasional jerks."""
    rng = np.random.default_rng(seed)
    v = rng.normal(0, 1, 2)
    v /= np.linalg.norm(v)
    p = np.zeros((steps, 2))
    for t in range(1, steps):
        jerk = rng.normal(0, 1.0, 2) if rng.random() < 0.05 else 0.0
        v = v + rng.normal(0, 0.4, 2) - 0.08 * p[t - 1] / max(1.0, np.linalg.norm(p[t - 1])) + jerk
        v /= max(np.linalg.norm(v), 1e-9)
        p[t] = p[t - 1] + v
    seg = np.hypot(*np.diff(p, axis=0).T).sum()
    return p * (length / seg)


def drawn_path(trajectory: np.ndarray, clicks: int = 10, seed: int = 0) -> list[list[float]]:
    """An examiner's clicks along a blur path: evenly spaced along it, each off by up to half a pixel."""
    seg = np.hypot(*np.diff(trajectory, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.linspace(0, cum[-1], clicks)
    pick = np.stack([np.interp(t, cum, trajectory[:, 0]), np.interp(t, cum, trajectory[:, 1])], 1)
    return (pick + np.random.default_rng(seed).uniform(-0.5, 0.5, pick.shape)).tolist()


def run_shake(sets: dict[str, Path], lengths=(15.0, 25.0), shakes: int = 4, noise: float = 1.0) -> dict:
    """Curved camera-shake blur generated along random paths, as the Sun et al. 2013 and Lai et al. 2016 sets are made."""
    cases = []
    for label, folder in sets.items():
        for name, x in _sharp_images(folder):
            for L in lengths:
                for si in range(shakes):
                    traj = shake_trajectory(L, seed=si)
                    k = make_psf({"psf": "path", "path": traj.tolist()})
                    y = _blur(x, k, circular=False) + np.random.RandomState(si).normal(0, noise / 255.0, x.shape)
                    cases.append({"name": f"{label}_{name}_L{L:g}_t{si}", "blurred": np.clip(y, 0, 1), "sharp": x,
                                  "kernel": k, "trajectory": traj})
    paths = {c["name"]: make_psf({"psf": "path", "path": drawn_path(c["trajectory"])}) for c in cases}
    line = {}
    for c in cases:
        key = c["name"].split("_L")[1]
        if key not in line:
            line[key] = closest_parametric_psf(c["kernel"])[0]
    tv = {"tv": [{"regularisation": r, "iterations": 3} for r in (3e-4, 1e-3, 3e-3)]}
    blind_grid = {"blind": [{"smoothness": l, "regularisation": r} for l in (0.004, 0.01) for r in (3e-4, 1e-3, 3e-3)]}
    return {"n": len(cases),
            "blurred": _brief(summarise({c["name"]: _interior_score(c["blurred"], c["sharp"]) for c in cases})),
            "True kernel": evaluate(cases, _interior_score, methods=("tv",), grid=tv),
            "Drawn path (10 clicks)": evaluate(cases, _interior_score, lambda c: paths[c["name"]], methods=("tv",), grid=tv),
            "Closest straight line or disk": evaluate(cases, _interior_score, lambda c: line[c["name"].split("_L")[1]],
                                                      methods=("tv",), grid=tv),
            "Blind": evaluate(cases, _interior_score, methods=("blind",), grid=blind_grid, runner=_blind_runner({}))}


def light_streak(k: np.ndarray, noise: float = 0.005, seed: int = 0) -> np.ndarray:
    """The kernel as an 8-bit photo of a small light would record it: dim background, sensor noise, no saturation."""
    pad = 8
    canvas = np.full((k.shape[0] + 2 * pad, k.shape[1] + 2 * pad), 0.05)
    canvas[pad:-pad, pad:-pad] += 0.85 * k / k.max()
    canvas += np.random.default_rng(seed).normal(0, noise, canvas.shape)
    u8 = np.clip(np.round(canvas * 255), 0, 255) / 255.0
    k = streak_psf(np.dstack([u8] * 3), [[0, 0], [canvas.shape[1], canvas.shape[0]]])
    return k / k.sum()


def _blind_runner(cache: dict):
    def run(m, c, s):
        key = (c["name"], s["smoothness"])
        if key not in cache:
            cache[key] = estimate_kernel(c["blurred"], 31, s["smoothness"], 5)
        return deconvolve("tv", c["blurred"], cache[key], {"regularisation": s["regularisation"], "iterations": 3})
    return run


def levin_extras(cases: list[dict]) -> dict:
    """The blur shapes an examiner can give without knowing the kernel, and the blind estimate."""
    streaks = {c["name"]: light_streak(c["kernel"]) for c in cases}
    two = ("richardson_lucy", "tv")
    return {
        "Light streak": evaluate(cases, levin_score, lambda c: streaks[c["name"]], methods=two),
        "Blind": evaluate(cases, levin_score, methods=("blind",), grid={"blind": BLIND_GRID}, runner=_blind_runner({})),
    }


def _interior_score(est: np.ndarray, sharp: np.ndarray, border: int = 20) -> dict:
    i = (slice(border, -border), slice(border, -border))
    return {"psnr": psnr(est[i], sharp[i]), "ssim": ssim(est[i], sharp[i])}


def _sharp_images(folder: Path) -> list[tuple[str, np.ndarray]]:
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".png", ".bmp", ".tif", ".jpg"))
    out = []
    for f in files:
        x = _read(f)
        out.append((f.stem, x.mean(axis=2) if x.ndim == 3 else x))
    return out


def rotation_blur(x: np.ndarray, degrees: float, centre: tuple[float, float], steps: int = 61) -> np.ndarray:
    """Exposure during a camera roll: the mean of the image rotated through the angle, about a point."""
    h, w = x.shape[:2]
    acc = np.zeros_like(x, dtype=np.float64)
    for a in np.linspace(-degrees / 2, degrees / 2, steps):
        m = cv2.getRotationMatrix2D(centre, float(a), 1.0)
        acc += cv2.warpAffine(x.astype(np.float32), m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return acc / steps


def rotation_streaks(degrees: float, centre: tuple[float, float], h: int, w: int, seed: int = 0) -> list[list[float]]:
    """An examiner's clicks on both ends of the blur streak at four places, each off by up to half a pixel."""
    rng = np.random.default_rng(seed)
    pts = []
    for fy, fx in ((0.2, 0.2), (0.2, 0.8), (0.8, 0.2), (0.8, 0.8)):
        x, y = fx * w, fy * h
        for a in (-degrees / 2, degrees / 2):
            m = cv2.getRotationMatrix2D(centre, float(a), 1.0)
            q = m @ np.array([x, y, 1.0])
            pts.append((q + rng.uniform(-0.5, 0.5, 2)).tolist())
    return pts


def run_variable(sets: dict[str, Path], degrees: float = 3.0, noise: float = 1.0) -> dict:
    """Blur that changes across the frame: a camera roll about a point off the image centre."""
    cases = []
    for label, folder in sets.items():
        for name, x in _sharp_images(folder):
            h, w = x.shape
            centre = (w * 0.3, h * 0.35)
            y = rotation_blur(x, degrees, centre)
            y = y + np.random.RandomState(0).normal(0, noise / 255.0, y.shape)
            cases.append({"name": f"{label}_{name}", "blurred": np.clip(y, 0, 1), "sharp": x,
                          "streaks": rotation_streaks(degrees, centre, h, w)})
    variable, cache = REGISTRY["deblur.variable"], {}

    def run(m, c, s):
        y3 = np.dstack([c["blurred"]] * 3)
        if m == "blind":
            if c["name"] not in cache:
                cache[c["name"]] = estimate_kernel(c["blurred"], 31, DEFAULT["blind"]["smoothness"], 5)
            return tv_psf(y3, cache[c["name"]], s["regularisation"], 3)[..., 0]
        if m == "one_streak":
            seg = np.asarray(c["streaks"][:2])
            v = seg[1] - seg[0]
            psf = make_psf({"psf": "motion", "length": float(np.hypot(*v)), "angle": math.degrees(math.atan2(-v[1], v[0]))})
            return tv_psf(y3, psf, s["regularisation"], 3)[..., 0]
        return variable.fn(y3, variable.validate({"streaks": c["streaks"], **s}))[..., 0]

    regs = (5e-4, 1e-3, 2e-3, 5e-3)
    grid = {"one_streak": [{"regularisation": r} for r in regs], "blind": [{"regularisation": r} for r in regs],
            "variable": [{"tiles": t, "regularisation": r} for t in (4, 6, 8) for r in regs]}
    return {"n": len(cases), "degrees": degrees,
            "blurred": _brief(summarise({c["name"]: _interior_score(c["blurred"], c["sharp"]) for c in cases})),
            "results": evaluate(cases, _interior_score, methods=tuple(grid), grid=grid, runner=run)}


def turbulence_stack(x: np.ndarray, frames: int, amplitude: float, blur: float, noise: float, seed: int) -> np.ndarray:
    """Frames of a static scene seen through turbulence: smooth random local shifts, short-exposure blur, noise."""
    rng = np.random.default_rng(seed)
    h, w = x.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    out = []
    for _ in range(frames):
        d = cv2.GaussianBlur(rng.normal(0, 1, (h, w, 2)).astype(np.float32), (0, 0), 8)
        d *= amplitude / max(float(np.sqrt(np.mean(d ** 2))), 1e-9)
        f = cv2.remap(x.astype(np.float32), gx + d[..., 0], gy + d[..., 1], cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        f = cv2.GaussianBlur(f, (0, 0), blur) + rng.normal(0, noise / 255.0, (h, w))
        out.append(np.dstack([np.clip(f, 0, 1)] * 3))
    return np.stack(out).astype(np.float32)


def run_turbulence(sets: dict[str, Path], frames: int = 20, amplitude: float = 1.5, blur: float = 1.0, noise: float = 2.0) -> dict:
    cases = []
    for label, folder in sets.items():
        for i, (name, x) in enumerate(_sharp_images(folder)):
            cases.append({"name": f"{label}_{name}", "blurred": turbulence_stack(x, frames, amplitude, blur, noise, i), "sharp": x})

    def run(m, c, s):
        st = c["blurred"]
        if m == "single":
            return st[len(st) // 2, ..., 0]
        if m == "mean":
            return st.mean(axis=0)[..., 0]
        if m == "median":
            return np.median(st, axis=0)[..., 0]
        return turbulence_fuse(st, s["passes"], "median", s["residual_blur"], s["regularisation"])[..., 0]

    grid = {"single": [{}], "mean": [{}], "median": [{}],
            "turbulence": [{"passes": 2, "residual_blur": b, "regularisation": r} for b in (0.0, 0.8, 1.0, 1.3) for r in (0.001, 0.003)]}
    return {"n": len(cases), "frames": frames, "amplitude": amplitude, "blur": blur, "noise": noise,
            "results": evaluate(cases, _interior_score, methods=tuple(grid), grid=grid, runner=run)}


def environment() -> dict:
    return {"lucida": __version__, "date": date.today().isoformat(), "python": platform.python_version(),
            "numpy": np.__version__, "machine": platform.machine()}


def summary_markdown(results: dict) -> str:
    def setting(st):
        return ", ".join(f"{k} {v:g}" for k, v in st.items())

    def cells(r, keys):
        return " | ".join(f"{r[k]:.2f}" if k.startswith("psnr") else f"{r[k]:.4f}" for k in keys)

    def table(title, block, keys):
        head = " | ".join({"psnr": "PSNR (dB)", "ssim": "SSIM", "psnr_interior": "PSNR interior", "ssim_interior": "SSIM interior"}[k] for k in keys)
        out = [title, "", f"| Method | {head} | Best setting | PSNR at app default |", "|---|" + "---|" * (len(keys) + 2),
               f"| Blurred input | {cells(block['blurred'], keys)} | | |"]
        for label, group in block["groups"]:
            for m, r in group.items():
                if not isinstance(r, dict) or "best" not in r:
                    continue
                dflt = f"{r['default']['psnr']:.2f}" if "default" in r else ""
                out.append(f"| {label}{NAMES.get(m, m)} | {cells(r['best'], keys)} | {setting(r['best']['setting'])} | {dflt} |")
        for name, r in block["references"].items():
            if r:
                out.append(f"| {name} (run here) | {cells(r, keys)} | | |")
        return out + [""]

    out = []
    if "levin" in results:
        L = results["levin"]
        groups = [(label, L[key]) for label, key in (("True kernel, ", "true_kernel"), ("Closest app blur shape, ", "closest_app_shape"),
                                                     ("App motion estimate, ", "app_motion_estimate")) if key in L]
        groups += [("" if k == "Blind" else f"{k}, ", v) for k, v in L.get("extra", {}).items()]
        out += table(f"### Levin et al. 2009, real camera shake ({L['n']} images)", {"references": {}, **L, "groups": groups}, ["psnr", "ssim"])
    for key, S in results.get("synthetic", {}).items():
        groups = [("", S["true_kernel"])] + [(f"{k}, ", v) for k, v in S.get("extra", {}).items()]
        out += table(f"### {key} (IRCNN/DPIR protocol, {S['n']} images)", {**S, "groups": groups},
                     ["psnr", "ssim", "psnr_interior", "ssim_interior"])
    if "shake" in results:
        S = results["shake"]
        groups = [("" if g == "Blind" else f"{g}, ", S[g]) for g in ("True kernel", "Drawn path (10 clicks)", "Closest straight line or disk", "Blind")]
        out += table(f"### Curved camera shake along random paths ({S['n']} images)", {**S, "groups": groups, "references": {}},
                     ["psnr", "ssim"])
    if "variable" in results:
        V = results["variable"]
        out += table(f"### Variable blur: camera roll of {V['degrees']:g} degrees about an off-centre point ({V['n']} images)",
                     {**V, "groups": [("", V["results"])], "references": {}}, ["psnr", "ssim"])
    if "turbulence" in results:
        T = results["turbulence"]
        out += table(f"### Turbulence: {T['frames']} frames, {T['amplitude']:g} px shimmer, blur {T['blur']:g} px, noise "
                     f"{T['noise']:g} grey levels ({T['n']} scenes)",
                     {**T, "groups": [("", T["results"])], "references": {}, "blurred": T["results"]["single"]["best"]},
                     ["psnr", "ssim"])
    return "\n".join(out)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="python -m forensic.validation.deblur", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levin", type=Path, help="folder with Levin et al.'s im*_kernel*_img.png and gt/")
    ap.add_argument("--images", action="append", default=[], metavar="NAME=DIR", help="sharp test images for the synthetic protocol")
    ap.add_argument("--kernels", type=Path, help="folder of kernels (.npy or .png) for the synthetic protocol")
    ap.add_argument("--sigma", type=float, action="append", help="noise sigma in grey levels (default 2.55 and 7.65)")
    ap.add_argument("--levin-new-only", type=Path, metavar="DIR",
                    help="Levin set: only the light-streak and blind tests (skips the known-kernel filters)")
    ap.add_argument("--shake", action="append", default=[], metavar="NAME=DIR", help="sharp images for the curved camera-shake test")
    ap.add_argument("--variable", action="append", default=[], metavar="NAME=DIR", help="sharp images for the variable-blur test")
    ap.add_argument("--turbulence", action="append", default=[], metavar="NAME=DIR", help="sharp images for the turbulence test")
    ap.add_argument("--reference", action="append", default=[], metavar="NAME=DIR", help="score another method's outputs (<case>.png or .npy)")
    ap.add_argument("--save-inputs", type=Path, help="write the degraded inputs as .npy, for running another method on them")
    ap.add_argument("--out", type=Path, required=True, help="folder for results.json and summary.md")
    a = ap.parse_args(argv)
    refs = dict(r.split("=", 1) for r in a.reference)
    t0 = time.time()
    results = {"environment": environment()}
    if a.levin:
        results["levin"] = run_levin(a.levin, {n: Path(p) / "levin" for n, p in refs.items()},
                                     a.save_inputs / "levin" if a.save_inputs else None)
    if a.images:
        if not a.kernels:
            ap.error("--images needs --kernels")
        sets = {n: Path(p) for n, p in (s.split("=", 1) for s in a.images)}
        results["synthetic"] = run_synthetic(sets, load_kernels(a.kernels), a.sigma or [2.55, 7.65],
                                             {n: Path(p) / "synthetic" for n, p in refs.items()},
                                             a.save_inputs / "synthetic" if a.save_inputs else None)
    if a.levin_new_only:
        cases = levin_cases(a.levin_new_only)
        results["levin"] = {"n": len(cases), "blurred": _brief(summarise({c["name"]: levin_score(c["blurred"], c["sharp"]) for c in cases})),
                            "extra": levin_extras(cases)}
    if a.shake:
        results["shake"] = run_shake({n: Path(p) for n, p in (v.split("=", 1) for v in a.shake)})
    if a.variable:
        results["variable"] = run_variable({n: Path(p) for n, p in (v.split("=", 1) for v in a.variable)})
    if a.turbulence:
        results["turbulence"] = run_turbulence({n: Path(p) for n, p in (v.split("=", 1) for v in a.turbulence)})
    results["environment"]["seconds"] = round(time.time() - t0, 1)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "results.json").write_text(json.dumps(results, indent=1))
    (a.out / "summary.md").write_text(summary_markdown(results))
    print(summary_markdown(results))


if __name__ == "__main__":
    main()
