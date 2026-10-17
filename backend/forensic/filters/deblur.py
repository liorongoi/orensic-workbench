"""Deconvolution: Wiener and Richardson-Lucy (optionally total-variation regularised).

Both invert a known blur described by a point-spread function (PSF): linear
motion, out-of-focus disk or Gaussian. All arithmetic is NumPy FFT in float64,
which is deterministic, so a re-run reproduces the result bit for bit. The colour
channels are independent and run in parallel.
"""

from __future__ import annotations

import math

import numpy as np

from .. import compute
from .base import FilterError, Param, register

PSF_PARAMS = [
    Param("psf", "choice", "motion", "Blur type", choices=("motion", "defocus", "gaussian"),
          help="motion: straight-line camera or subject motion; defocus: out-of-focus disk; gaussian: general softness"),
    Param("length", "float", 9.0, "Motion length (px)", 1.0, 101.0, 0.5),
    Param("angle", "float", 0.0, "Motion angle (degrees)", -180.0, 180.0, 1.0,
          help="0 is horizontal, positive is counter-clockwise"),
    Param("radius", "float", 3.0, "Defocus radius (px)", 0.5, 50.0, 0.5),
    Param("sigma", "float", 1.5, "Gaussian sigma (px)", 0.3, 20.0, 0.1),
]


def make_psf(p: dict) -> np.ndarray:
    """Normalised PSF on an odd-sized grid, centred. Anti-aliased by 8x supersampling."""
    kind = p["psf"]
    if kind == "motion":
        length, ang = p["length"], math.radians(p["angle"])
        half = int(math.ceil(length / 2)) + 1
        size = 2 * half + 1
        psf = np.zeros((size, size), np.float64)
        # Sample the segment densely and splat bilinearly.
        n = max(2, int(length * 16))
        t = np.linspace(-length / 2, length / 2, n)
        xs = half + t * math.cos(ang)
        ys = half - t * math.sin(ang)
        x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
        fx, fy = xs - x0, ys - y0
        for dx, dy, w in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
            np.add.at(psf, (np.clip(y0 + dy, 0, size - 1), np.clip(x0 + dx, 0, size - 1)), w)
    elif kind == "defocus":
        r = p["radius"]
        half = int(math.ceil(r)) + 1
        size = 2 * half + 1
        ss = 8
        g = (np.arange(size * ss) + 0.5) / ss - half - 0.5
        yy, xx = np.meshgrid(g, g, indexing="ij")
        disk = (xx**2 + yy**2 <= r * r).astype(np.float64)
        psf = disk.reshape(size, ss, size, ss).mean(axis=(1, 3))
    elif kind == "gaussian":
        s = p["sigma"]
        half = int(math.ceil(3 * s))
        ax = np.arange(-half, half + 1, dtype=np.float64)
        g = np.exp(-(ax**2) / (2 * s * s))
        psf = np.outer(g, g)
    else:
        raise FilterError(f"unknown PSF '{kind}'")
    total = psf.sum()
    if total <= 0:
        raise FilterError("PSF is empty")
    return psf / total


def _otf(psf: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Zero-pad the PSF to `shape` with its centre at (0, 0), then FFT."""
    pad = np.zeros(shape, np.float64)
    kh, kw = psf.shape
    pad[:kh, :kw] = psf
    pad = np.roll(pad, (-(kh // 2), -(kw // 2)), axis=(0, 1))
    return np.fft.rfft2(pad)


def _pad(img: np.ndarray, m: int) -> np.ndarray:
    """Symmetric padding so the FFT's circular wrap-around does not ring at the borders."""
    return np.pad(img, ((m, m), (m, m), (0, 0)), mode="symmetric")


@register(
    "deblur.wiener",
    name="Deblur (Wiener deconvolution)",
    version="1.0.0",
    category="Deblur",
    description="Inverts a known blur (motion, defocus or Gaussian) with a Wiener filter. The noise-to-signal "
    "ratio controls how much noise and ringing are traded for sharpness: raise it if the result looks "
    "grainy or shows ripples around edges. Only recovers detail that the blur attenuated; it cannot "
    "recover detail that was never recorded.",
    reference="N. Wiener, Extrapolation, Interpolation, and Smoothing of Stationary Time Series, MIT Press, 1949; "
    "R. C. Gonzalez and R. E. Woods, Digital Image Processing, 4th ed., Section 5.8, 2018.",
    params=PSF_PARAMS + [Param("nsr", "float", 0.01, "Noise-to-signal ratio", 1e-5, 1.0, 0.001)],
)
def wiener(img, p):
    return wiener_psf(img, make_psf(p), p["nsr"])


def wiener_psf(img: np.ndarray, psf: np.ndarray, nsr: float) -> np.ndarray:
    """Wiener deconvolution of an H x W x C image in [0, 1] by any normalised PSF."""
    m = max(psf.shape)
    padded = _pad(img.astype(np.float64), m)
    h, w = padded.shape[:2]
    H = _otf(psf, (h, w))
    G = np.conj(H) / (np.abs(H) ** 2 + nsr)
    out = np.dstack(compute.pmap(lambda c: np.fft.irfft2(np.fft.rfft2(padded[..., c]) * G, s=(h, w)), range(img.shape[2])))
    return np.clip(out[m:-m, m:-m], 0, 1).astype(np.float32)


def _tv_term(u: np.ndarray, eps: float = 1e-3) -> np.ndarray:
    """div(grad u / |grad u|) per channel with forward/backward differences."""
    gx = np.diff(u, axis=1, append=u[:, -1:])
    gy = np.diff(u, axis=0, append=u[-1:, :])
    mag = np.sqrt(gx * gx + gy * gy + eps * eps)
    nx, ny = gx / mag, gy / mag
    return np.diff(nx, axis=1, prepend=nx[:, :1]) + np.diff(ny, axis=0, prepend=ny[:1, :])


@register(
    "deblur.richardson_lucy",
    name="Deblur (Richardson-Lucy)",
    version="1.0.0",
    category="Deblur",
    description="Iterative maximum-likelihood deconvolution for a known blur (motion, defocus or Gaussian). "
    "Preserves positivity and handles low-light noise better than Wiener. More iterations sharpen more but "
    "amplify noise; the optional total-variation term suppresses that noise while keeping edges.",
    reference="W. H. Richardson, 'Bayesian-Based Iterative Method of Image Restoration', JOSA 62(1), 1972; "
    "L. B. Lucy, 'An iterative technique for the rectification of observed distributions', Astron. J. 79, 1974; "
    "N. Dey et al., 'Richardson-Lucy algorithm with total variation regularization for 3D confocal microscope "
    "deconvolution', Microscopy Research and Technique 69(4), 2006.",
    params=PSF_PARAMS + [
        Param("iterations", "int", 20, "Iterations", 1, 300, 1),
        Param("tv", "float", 0.0, "Total-variation weight", 0.0, 0.05, 0.001, help="0 disables regularisation; 0.002 to 0.01 is typical"),
    ],
)
def richardson_lucy(img, p):
    return richardson_lucy_psf(img, make_psf(p), p["iterations"], p["tv"])


def richardson_lucy_psf(img: np.ndarray, psf: np.ndarray, iterations: int, tv: float) -> np.ndarray:
    """Richardson-Lucy deconvolution of an H x W x C image in [0, 1] by any normalised PSF."""
    m = max(psf.shape)
    padded = _pad(img.astype(np.float64), m) + 1e-4
    h, w = padded.shape[:2]
    H = _otf(psf, (h, w))
    Hc = np.conj(H)

    def channel(c):
        g = np.ascontiguousarray(padded[..., c])
        u = g.copy()
        for _ in range(iterations):
            blurred = np.fft.irfft2(np.fft.rfft2(u) * H, s=(h, w))
            ratio = g / np.maximum(blurred, 1e-6)
            corr = np.fft.irfft2(np.fft.rfft2(ratio) * Hc, s=(h, w))
            if tv > 0:
                corr = corr / np.maximum(1.0 - tv * _tv_term(u), 1e-3)
            u = np.clip(u * corr, 0, 2)
        return u

    out = np.dstack(compute.pmap(channel, range(img.shape[2])))
    return np.clip(out[m:-m, m:-m] - 1e-4, 0, 1).astype(np.float32)


def estimate_motion_blur(gray: np.ndarray, max_length: int = 60) -> dict:
    """Estimate linear motion blur length and angle from the cepstrum.

    A uniform motion blur of length L puts periodic zeros in the spectrum, which show
    up in the cepstrum as a strong negative peak at distance L along the blur direction.
    Returned as a suggestion for the examiner to try; it is not applied automatically.
    """
    g = gray.astype(np.float64)
    g = g - g.mean()
    h, w = g.shape
    win = np.outer(np.hanning(h), np.hanning(w))
    spec = np.abs(np.fft.fft2(g * win))
    cep = np.real(np.fft.ifft2(np.log1p(spec)))
    cep = np.fft.fftshift(cep)
    cy, cx = h // 2, w // 2
    best = (0.0, 0.0, 0.0)
    for ang in np.arange(0.0, 180.0, 1.0):
        rad = math.radians(ang)
        for L in range(3, min(max_length, min(h, w) // 2 - 1)):
            x = cx + L * math.cos(rad)
            y = cy - L * math.sin(rad)
            x0, y0 = int(x), int(y)
            fx, fy = x - x0, y - y0
            v = (cep[y0, x0] * (1 - fx) * (1 - fy) + cep[y0, x0 + 1] * fx * (1 - fy)
                 + cep[y0 + 1, x0] * (1 - fx) * fy + cep[y0 + 1, x0 + 1] * fx * fy)
            if v < best[0]:
                best = (v, float(L), float(ang))
    strength = -best[0] / (np.std(cep) + 1e-12)
    return {"length": best[1], "angle": best[2], "strength": round(float(strength), 2),
            "note": "Cepstral estimate; a strength below about 4 means no clear linear motion blur was found."}


@register(
    "deblur.tv",
    name="Deblur (total-variation deconvolution)",
    version="1.0.0",
    category="Deblur",
    description="Inverts a known blur (motion, defocus or Gaussian) while keeping the result piecewise smooth: it "
    "looks for the sharpest image that, blurred, matches the evidence, and penalises noise and ripples. Gives "
    "much less ringing around edges and less noise than Wiener or Richardson-Lucy at the same sharpness, which "
    "suits plates and text. Raise the regularisation if the result looks flat or cartoon-like only where there "
    "is noise; lower it for more detail. Only recovers detail that the blur attenuated.",
    reference="Y. Wang, J. Yang, W. Yin and Y. Zhang, 'A New Alternating Minimization Algorithm for Total Variation "
    "Image Reconstruction', SIAM J. Imaging Sciences 1(3), 2008; L. I. Rudin, S. Osher and E. Fatemi, 'Nonlinear "
    "total variation based noise removal algorithms', Physica D 60, 1992.",
    params=PSF_PARAMS + [
        Param("regularisation", "float", 0.002, "Regularisation", 1e-5, 0.1, 0.0005,
              help="Higher suppresses more noise and ringing; 0.001 to 0.005 suits most CCTV stills"),
        Param("iterations", "int", 3, "Iterations per stage", 1, 20, 1,
              help="Solved in 9 stages of increasing strictness; more iterations refine each stage"),
    ],
)
def tv_deconv(img, p):
    return tv_psf(img, make_psf(p), p["regularisation"], p["iterations"])


def tv_psf(img: np.ndarray, psf: np.ndarray, regularisation: float, iterations: int) -> np.ndarray:
    """FTVd: half-quadratic splitting for isotropic TV, with every sub-problem solved exactly by FFT."""
    m = max(psf.shape)
    padded = _pad(img.astype(np.float64), m)
    h, w = padded.shape[:2]
    K = _otf(psf, (h, w))
    Dx = _otf(np.array([[0.0, 0.0, 0.0], [0.0, -1.0, 1.0], [0.0, 0.0, 0.0]]), (h, w))
    Dy = _otf(np.array([[0.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 1.0, 0.0]]), (h, w))
    K2, D2 = np.abs(K) ** 2, np.abs(Dx) ** 2 + np.abs(Dy) ** 2
    mu = 1.0 / regularisation

    def channel(c):
        f = padded[..., c]
        KtF = np.conj(K) * np.fft.rfft2(f)
        x = f.copy()
        for beta in 2.0 ** np.arange(0, 9):  # continuation: 1, 2, ... 256
            for _ in range(iterations):
                X = np.fft.rfft2(x)
                gx = np.fft.irfft2(Dx * X, s=(h, w))
                gy = np.fft.irfft2(Dy * X, s=(h, w))
                mag = np.sqrt(gx * gx + gy * gy)
                shrink = np.maximum(mag - 1.0 / beta, 0.0) / np.maximum(mag, 1e-12)
                wx, wy = gx * shrink, gy * shrink
                num = np.conj(Dx) * np.fft.rfft2(wx) + np.conj(Dy) * np.fft.rfft2(wy) + (mu / beta) * KtF
                x = np.fft.irfft2(num / (D2 + (mu / beta) * K2), s=(h, w))
        return x

    out = np.dstack(compute.pmap(channel, range(img.shape[2])))
    return np.clip(out[m:-m, m:-m], 0, 1).astype(np.float32)
