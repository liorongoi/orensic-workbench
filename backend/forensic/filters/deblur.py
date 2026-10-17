"""Deconvolution: Wiener, Richardson-Lucy, total variation, blind and spatially variable.

The non-blind filters invert a known blur described by a point-spread function (PSF):
straight-line motion, out-of-focus disk, Gaussian, a drawn motion path, or the streak a
small light left in the image. The blind filter estimates the PSF from the image itself. All arithmetic is NumPy FFT in float64,
which is deterministic, so a re-run reproduces the result bit for bit. The colour
channels are independent and run in parallel.
"""

from __future__ import annotations

import math

import numpy as np

from .. import compute
from .base import FilterError, Param, register

PSF_PARAMS = [
    Param("psf", "choice", "motion", "Blur type", choices=("motion", "defocus", "gaussian", "path", "streak"),
          help="motion: straight-line camera or subject motion; defocus: out-of-focus disk; gaussian: general softness; "
          "path: a curved or uneven motion you draw; streak: the smear a small light (lamp, reflection) left, used as the blur"),
    Param("length", "float", 9.0, "Motion length (px)", 1.0, 101.0, 0.5),
    Param("angle", "float", 0.0, "Motion angle (degrees)", -180.0, 180.0, 1.0,
          help="0 is horizontal, positive is counter-clockwise"),
    Param("radius", "float", 3.0, "Defocus radius (px)", 0.5, 50.0, 0.5),
    Param("sigma", "float", 1.5, "Gaussian sigma (px)", 0.3, 20.0, 0.1),
    Param("path", "points", [], "Motion path (blur type path)",
          help="Click along the blur's trajectory in order, for example along the streak of a small light; "
          "only the shape matters, not where it is drawn"),
    Param("streak", "points", [], "Light streak box (blur type streak, 2 corners)",
          help="A box tightly around the smear of one small light on a dark background; it must not be saturated"),
]


def _centre(psf: np.ndarray) -> np.ndarray:
    """Place a kernel on an odd square grid with its centre of mass at the middle pixel."""
    ys, xs = np.nonzero(psf)
    if len(ys) == 0:
        raise FilterError("PSF is empty")
    w = psf[ys, xs]
    cy, cx = float(np.sum(ys * w) / w.sum()), float(np.sum(xs * w) / w.sum())
    half = int(math.ceil(max(cy - ys.min(), ys.max() - cy, cx - xs.min(), xs.max() - cx))) + 1
    size = 2 * half + 1
    out = np.zeros((size, size), np.float64)
    oy, ox = half - int(round(cy)), half - int(round(cx))
    out[ys + oy, xs + ox] = w
    return out


def _path_psf(points: list) -> np.ndarray:
    """Uniform-speed motion along a polyline, splatted bilinearly at 1/16 px steps."""
    pts = np.asarray(points, np.float64)
    if len(pts) < 2:
        raise FilterError("a motion path needs at least two points")
    seg = np.diff(pts, axis=0)
    lens = np.hypot(seg[:, 0], seg[:, 1])
    total = float(lens.sum())
    if total < 1.0:
        raise FilterError("the motion path is shorter than one pixel")
    if total > 300:
        raise FilterError("the motion path is longer than 300 pixels")
    t = np.linspace(0.0, total, max(2, int(total * 16)))
    cum = np.concatenate([[0.0], np.cumsum(lens)])
    xs = np.interp(t, cum, pts[:, 0])
    ys = np.interp(t, cum, pts[:, 1])
    xs, ys = xs - xs.min() + 1, ys - ys.min() + 1
    grid = np.zeros((int(ys.max()) + 3, int(xs.max()) + 3), np.float64)
    x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
    fx, fy = xs - x0, ys - y0
    for dx, dy, w in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
        np.add.at(grid, (y0 + dy, x0 + dx), w)
    return _centre(grid)


def streak_psf(img: np.ndarray, box: list) -> np.ndarray:
    """The blur kernel read from the smear of a point light: background removed, faint noise cut."""
    if len(box) != 2:
        raise FilterError("the light streak box needs exactly two corner points")
    (xa, ya), (xb, yb) = box
    x0, x1 = sorted((int(round(xa)), int(round(xb))))
    y0, y1 = sorted((int(round(ya)), int(round(yb))))
    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        raise FilterError("the light streak box is smaller than 3 x 3 pixels")
    if x1 - x0 > 301 or y1 - y0 > 301:
        raise FilterError("the light streak box is larger than 301 pixels")
    g = img[y0:y1, x0:x1]
    g = (0.299 * g[..., 0] + 0.587 * g[..., 1] + 0.114 * g[..., 2]) if g.ndim == 3 else g
    g = g.astype(np.float64)
    # A thin streak covers a small part of its box, so the box's median is the background and its spread the noise.
    bg = float(np.median(g))
    k = np.maximum(g - bg - 3 * 1.4826 * float(np.median(np.abs(g - bg))), 0.0)
    if k.max() <= 0:
        raise FilterError("no light streak found in the box: it should be brighter than its surroundings")
    k[k < 0.05 * k.max()] = 0.0
    # Keep the streak itself: drop specks, and anything reaching the box edge that is not part of the brightest blob.
    import cv2

    n, lab = cv2.connectedComponents((k > 0).astype(np.uint8), connectivity=8)
    peak = lab[np.unravel_index(np.argmax(k), k.shape)]
    edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0, peak}
    for i in edge:
        k[lab == i] = 0.0
    return _centre(_largest_components(k))


def make_psf(p: dict, img: np.ndarray | None = None) -> np.ndarray:
    """Normalised PSF on an odd-sized grid, centred. Anti-aliased by 8x supersampling."""
    kind = p["psf"]
    if kind == "path":
        psf = _path_psf(p.get("path") or [])
    elif kind == "streak":
        if img is None:
            raise FilterError("a light-streak blur is read from the image")
        psf = streak_psf(img, p.get("streak") or [])
    elif kind == "motion":
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
    version="1.1.0",
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
    return wiener_psf(img, make_psf(p, img), p["nsr"])


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
    version="1.1.0",
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
    return richardson_lucy_psf(img, make_psf(p, img), p["iterations"], p["tv"])


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
    version="1.1.0",
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
    return tv_psf(img, make_psf(p, img), p["regularisation"], p["iterations"])


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


# Blind deconvolution ------------------------------------------------------------------------

GAMMA = 2.0
_DX = np.array([[0.0, 0.0, 0.0], [0.0, -1.0, 1.0], [0.0, 0.0, 0.0]])
_DY = np.array([[0.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 1.0, 0.0]])


def _l0_restore(y: np.ndarray, K: np.ndarray, Dx: np.ndarray, Dy: np.ndarray, lam: float) -> np.ndarray:
    """Sharp image with few, strong edges: min |k*x - y|^2 + lam |grad x|_0 (half-quadratic splitting).

    It throws away texture and noise and keeps the salient edges, which is what the kernel
    is estimated from (Xu, Zheng and Jia 2013; Pan, Hu, Su and Yang 2014).
    """
    h, w = y.shape
    Y = np.fft.rfft2(y)
    KtY, K2, D2 = np.conj(K) * Y, np.abs(K) ** 2, np.abs(Dx) ** 2 + np.abs(Dy) ** 2
    x, beta = y, 2 * lam
    while beta < 1e5:
        X = np.fft.rfft2(x)
        gx, gy = np.fft.irfft2(Dx * X, s=(h, w)), np.fft.irfft2(Dy * X, s=(h, w))
        keep = (gx * gx + gy * gy) >= lam / beta
        gx, gy = gx * keep, gy * keep
        num = KtY + beta * (np.conj(Dx) * np.fft.rfft2(gx) + np.conj(Dy) * np.fft.rfft2(gy))
        x = np.fft.irfft2(num / (K2 + beta * D2), s=(h, w))
        beta *= 2
    return x


def _kernel_step(x: np.ndarray, y: np.ndarray, Dx: np.ndarray, Dy: np.ndarray, size: int, gamma: float,
                 margin: int) -> np.ndarray:
    """Least-squares kernel in the gradient domain, then cut to size, non-negative, sparse and centred."""
    h, w = y.shape
    X, Y = np.fft.rfft2(x), np.fft.rfft2(y)
    # Fit only away from the padded border, where the FFT's wrap-around puts edges that are not in the scene.
    m = np.zeros((h, w))
    m[margin:h - margin, margin:w - margin] = 1.0

    def grads(F):
        return (np.fft.rfft2(np.fft.irfft2(Dx * F, s=(h, w)) * m), np.fft.rfft2(np.fft.irfft2(Dy * F, s=(h, w)) * m))

    (Xx, Xy), (Yx, Yy) = grads(X), grads(Y)
    k = np.fft.irfft2((np.conj(Xx) * Yx + np.conj(Xy) * Yy) / (np.abs(Xx) ** 2 + np.abs(Xy) ** 2 + gamma), s=(h, w))
    half = size // 2
    k = np.roll(k, (half, half), axis=(0, 1))[:size, :size]
    k = np.maximum(k, 0.0)
    if k.max() <= 0:
        return _delta(size)
    k[k < 0.05 * k.max()] = 0.0
    k = _largest_components(k)
    return _fit(_centre(k / k.sum()), size)


def _largest_components(k: np.ndarray) -> np.ndarray:
    """Drop isolated specks: keep connected parts holding at least 10 % of the kernel's mass."""
    import cv2

    n, lab = cv2.connectedComponents((k > 0).astype(np.uint8), connectivity=8)
    if n <= 2:
        return k
    mass = np.array([k[lab == i].sum() for i in range(1, n)])
    keep = np.isin(lab, 1 + np.nonzero(mass >= 0.1 * mass.max())[0])
    return k * keep


def _delta(size: int) -> np.ndarray:
    k = np.zeros((size, size))
    k[size // 2, size // 2] = 1.0
    return k


def _fit(k: np.ndarray, size: int) -> np.ndarray:
    """Crop or pad a centred odd kernel to size x size."""
    if k.shape[0] > size:
        c = (k.shape[0] - size) // 2
        k = k[c:c + size, c:c + size]
    elif k.shape[0] < size:
        p = (size - k.shape[0]) // 2
        k = np.pad(k, p)
    return k / k.sum()


def estimate_kernel(gray: np.ndarray, size: int, smoothness: float = 0.01, iterations: int = 5) -> np.ndarray:
    """Blind estimate of a spatially uniform blur kernel, coarse to fine.

    At each scale it alternates between a sharp, edges-only image (_l0_restore) and the
    kernel that best maps that image's gradients to the evidence's (_kernel_step); the
    kernel found at one scale starts the next. Deterministic: plain NumPy FFTs, no random start.
    """
    import cv2

    size = int(size) | 1
    g = gray.astype(np.float64)
    scales = max(1, int(math.floor(math.log(size / 5.0) / math.log(math.sqrt(2)))) + 1)
    k = None
    lam = smoothness
    for si in range(scales - 1, -1, -1):
        f = math.sqrt(0.5) ** si
        ks = max(3, int(round(size * f)) | 1)
        ys = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else g
        ys = np.pad(ys, ks, mode="symmetric")
        h, w = ys.shape
        Dx, Dy = _otf(_DX, (h, w)), _otf(_DY, (h, w))
        k = _delta(ks) if k is None else _fit(np.maximum(cv2.resize(k, (ks, ks), interpolation=cv2.INTER_LINEAR), 0), ks)
        for _ in range(iterations):
            x = _l0_restore(ys, _otf(k, (h, w)), Dx, Dy, lam)
            k = _kernel_step(x, ys, Dx, Dy, ks, GAMMA, margin=2 * ks)
            lam = max(lam / 1.1, 1e-4)
    return k


def _roi(img: np.ndarray, roi: list) -> np.ndarray:
    if not roi:
        return img
    if len(roi) != 2:
        raise FilterError("the estimation region needs exactly two corner points")
    (xa, ya), (xb, yb) = roi
    x0, x1 = sorted((int(round(xa)), int(round(xb))))
    y0, y1 = sorted((int(round(ya)), int(round(yb))))
    out = img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if min(out.shape[:2]) < 32:
        raise FilterError("the estimation region must be at least 32 x 32 pixels")
    return out


def _gray(img: np.ndarray) -> np.ndarray:
    return (0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]) if img.ndim == 3 and img.shape[2] == 3 else img[..., 0]


@register(
    "deblur.blind",
    name="Deblur (blind, estimated kernel)",
    version="1.0.0",
    category="Deblur",
    description="For blur whose shape is not known: camera shake, curved or uneven motion. Estimates the blur kernel "
    "from the image's own strong edges, then removes it with total-variation deconvolution. Works best with a region "
    "that has clear edges (a plate, window frames, text) and a kernel size a little larger than the visible smear. "
    "Assumes the blur is the same across the region; for blur that changes across the frame use the variable deblur.",
    reference="L. Xu, S. Zheng and J. Jia, 'Unnatural L0 Sparse Representation for Natural Image Deblurring', CVPR 2013; "
    "J. Pan, Z. Hu, Z. Su and M.-H. Yang, 'Deblurring Text Images via L0-Regularized Intensity and Gradient Prior', "
    "CVPR 2014; Y. Wang, J. Yang, W. Yin and Y. Zhang, SIAM J. Imaging Sciences 1(3), 2008 (final deconvolution).",
    params=[
        Param("kernel_size", "int", 25, "Kernel size (px)", 5, 101, 2,
              help="A little larger than the longest smear in the image; too large slows it and adds noise to the estimate"),
        Param("roi", "points", [], "Estimation region (2 corners, optional)",
              help="Region with clear edges to estimate the blur from; the whole image is deblurred with it"),
        Param("smoothness", "float", 0.01, "Edge selection", 0.0005, 0.05, 0.0005,
              help="Higher uses only the strongest edges; raise it for noisy images"),
        Param("regularisation", "float", 0.002, "Regularisation", 1e-5, 0.1, 0.0005,
              help="Of the final deconvolution: higher suppresses more noise and ringing"),
        Param("iterations", "int", 5, "Iterations per scale", 1, 20, 1),
    ],
)
def blind(img, p):
    k = estimate_kernel(_gray(_roi(img, p["roi"])), p["kernel_size"], p["smoothness"], p["iterations"])
    return tv_psf(img, k, p["regularisation"], 3)


@register(
    "deblur.variable",
    name="Deblur (motion that varies across the frame)",
    version="1.0.0",
    category="Deblur",
    description="For motion blur whose length or direction changes across the image: a camera that rolled or turned, "
    "a vehicle turning, perspective. Draw short lines along the blur streaks at two or more places (each pair of clicks "
    "is one streak, from one end to the other); the blur is interpolated between them, and each tile of the image is "
    "deblurred with its own motion blur and blended smoothly with its neighbours.",
    reference="M. Hirsch, S. Sra, B. Schölkopf and S. Harmeling, 'Efficient Filter Flow for Space-Variant Multiframe "
    "Blind Deconvolution', CVPR 2010 (overlapping-patch model of varying blur); Y. Wang, J. Yang, W. Yin and Y. Zhang, "
    "SIAM J. Imaging Sciences 1(3), 2008 (deconvolution of each tile).",
    params=[
        Param("streaks", "points", [], "Blur streaks (pairs of clicks)",
              help="Click both ends of a blur streak, then both ends of another one elsewhere; at least two streaks"),
        Param("tiles", "int", 8, "Tiles per side", 2, 12, 1, help="More tiles follow faster changes in the blur"),
        Param("regularisation", "float", 0.005, "Regularisation", 1e-5, 0.1, 0.0005),
    ],
)
def variable(img, p):
    pts = p["streaks"]
    if len(pts) < 4 or len(pts) % 2:
        raise FilterError("draw at least two blur streaks, two clicks each")
    seg = np.asarray(pts, np.float64).reshape(-1, 2, 2)
    centres, vecs = seg.mean(axis=1), seg[:, 1] - seg[:, 0]
    # A streak has no direction: flip each one to agree with the first, so interpolation does not cancel them.
    vecs = np.where((vecs @ vecs[0] < 0)[:, None], -vecs, vecs)

    def psf_at(y, x):
        d2 = np.sum((centres - (x, y)) ** 2, axis=1)
        if d2.min() < 1e-6:
            v = vecs[int(np.argmin(d2))]
        else:
            wgt = 1.0 / d2
            v = (wgt[:, None] * vecs).sum(0) / wgt.sum()
        length = float(np.hypot(*v))
        if length < 1.0:
            return _delta(3)
        return make_psf({"psf": "motion", "length": length, "angle": math.degrees(math.atan2(-v[1], v[0]))})

    return tiled_deconv(img, p["tiles"], psf_at, p["regularisation"])


def tiled_deconv(img: np.ndarray, tiles: int, psf_at, regularisation: float) -> np.ndarray:
    """Deconvolve overlapping tiles, each with the PSF at its centre, blended with windows that sum to one."""
    h, w = img.shape[:2]
    th, tw = h / tiles, w / tiles
    if min(th, tw) < 24:
        raise FilterError(f"tiles of {int(th)} x {int(tw)} px are too small: use fewer tiles")
    acc = np.zeros(img.shape, np.float64)
    wsum = np.zeros((h, w), np.float64)
    jobs = []
    for i in range(tiles):
        for j in range(tiles):
            y0, y1 = int(max(0, (i - 0.5) * th)), int(min(h, (i + 1.5) * th))
            x0, x1 = int(max(0, (j - 0.5) * tw)), int(min(w, (j + 1.5) * tw))
            jobs.append(((i + 0.5) * th, (j + 0.5) * tw, y0, y1, x0, x1))

    def run(job):
        cy, cx, y0, y1, x0, x1 = job
        return tv_psf(img[y0:y1, x0:x1], psf_at(cy, cx), regularisation, 3)

    for (cy, cx, y0, y1, x0, x1), out in zip(jobs, compute.pmap(run, jobs)):
        wy = np.clip(1 - np.abs(np.arange(y0, y1) + 0.5 - cy) / th, 0, 1)
        wx = np.clip(1 - np.abs(np.arange(x0, x1) + 0.5 - cx) / tw, 0, 1)
        win = np.outer(np.sin(wy * math.pi / 2) ** 2, np.sin(wx * math.pi / 2) ** 2)
        acc[y0:y1, x0:x1] += out * win[..., None]
        wsum[y0:y1, x0:x1] += win
    return np.clip(acc / np.maximum(wsum, 1e-9)[..., None], 0, 1).astype(np.float32)
