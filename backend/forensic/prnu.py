"""Camera identification by photo-response non-uniformity (PRNU).

Every sensor has a fixed, faint multiplicative noise pattern K: pixel i records
I_i = I0_i (1 + K_i) + noise. The pattern is estimated from several images taken
by the camera (ideally flat, bright, unsaturated scenes), and an image is matched
by correlating its noise residual with the pattern, scored by the peak-to-
correlation energy (PCE). The method follows the Binghamton reference
implementation: wavelet denoising for the residual, a maximum-likelihood
fingerprint estimate, zero-meaning and Wiener filtering in the DFT domain to
remove non-unique artefacts (CFA interpolation, JPEG blocking).

Everything is float64 NumPy and PyWavelets, so results are deterministic.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
import pywt

SIGMA0 = 3.0  # assumed noise std of the wavelet denoiser, in 0..255 units
PCE_THRESHOLD = 60.0  # Goljan, Fridrich and Filler (2009) decision threshold
WINDOWS = (3, 5, 7, 9)


def _to255(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float64) * 255.0


def luma(img255: np.ndarray) -> np.ndarray:
    if img255.ndim == 2:
        return img255
    return 0.299 * img255[..., 0] + 0.587 * img255[..., 1] + 0.114 * img255[..., 2]


def _local_var(c: np.ndarray, w: int) -> np.ndarray:
    return cv2.boxFilter(c * c, cv2.CV_64F, (w, w), normalize=True, borderType=cv2.BORDER_REFLECT)


def wavelet_residual(channel: np.ndarray, levels: int = 4, sigma0: float = SIGMA0) -> np.ndarray:
    """Noise residual of one channel (0..255): the part the Mihcak wavelet denoiser removes."""
    h, w = channel.shape
    levels = max(1, min(levels, pywt.dwt_max_level(min(h, w), pywt.Wavelet("db8").dec_len)))
    m = 2**levels
    ph, pw = (-h) % m, (-w) % m
    x = np.pad(channel, ((0, ph), (0, pw)), mode="symmetric")
    coeffs = pywt.wavedec2(x, "db8", level=levels, mode="periodization")
    s2 = sigma0 * sigma0
    out = [np.zeros_like(coeffs[0])]
    for detail in coeffs[1:]:
        bands = []
        for c in detail:
            var = np.min([np.maximum(_local_var(c, win) - s2, 0) for win in WINDOWS], axis=0)
            bands.append(c * s2 / (var + s2))  # c - c * var / (var + s2): the noise part
        out.append(tuple(bands))
    res = pywt.waverec2(out, "db8", mode="periodization")
    return res[:h, :w]


def zero_mean_total(x: np.ndarray) -> np.ndarray:
    """Remove row and column means on each of the four 2x2 CFA sub-lattices."""
    out = x.copy()
    for dy in (0, 1):
        for dx in (0, 1):
            s = out[dy::2, dx::2]
            s -= s.mean()
            s -= s.mean(axis=0, keepdims=True)
            s -= s.mean(axis=1, keepdims=True)
            out[dy::2, dx::2] = s
    return out


def wiener_dft(x: np.ndarray) -> np.ndarray:
    """Suppress periodic peaks (non-unique artefacts) by Wiener-filtering the DFT magnitude."""
    sigma = float(x.std())
    F = np.fft.fft2(x)
    mag = np.abs(F) / np.sqrt(x.size)
    s2 = sigma * sigma
    var = np.min([np.maximum(_local_var(mag, w) - s2, 0) for w in WINDOWS], axis=0)
    mag_f = mag * s2 / (var + s2)
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(mag > 0, mag_f / mag, 0.0)
    return np.real(np.fft.ifft2(F * scale))


def noise_residual(img: np.ndarray) -> np.ndarray:
    """Greyscale PRNU residual of a float RGB image in [0, 1]."""
    x = _to255(img)
    if x.ndim == 2:
        W = wavelet_residual(x)
    else:
        W = luma(np.stack([wavelet_residual(x[..., c]) for c in range(3)], axis=2))
    return zero_mean_total(W)


def estimate_fingerprint(images: list[np.ndarray]) -> tuple[np.ndarray, dict]:
    """Maximum-likelihood PRNU estimate K = sum(W I) / sum(I^2) from same-size images."""
    if len(images) < 2:
        raise ValueError("a camera reference needs at least two images (20 or more flat images is recommended)")
    shape = images[0].shape[:2]
    num = np.zeros(shape)
    den = np.zeros(shape)
    for img in images:
        if img.shape[:2] != shape:
            raise ValueError("all images for a camera reference must have the same size and orientation")
        I = luma(_to255(img))
        W = noise_residual(img)
        ok = I < 250  # saturated pixels carry no PRNU
        num += np.where(ok, W * I, 0)
        den += np.where(ok, I * I, 0)
    K = num / (den + 1.0)
    K = wiener_dft(zero_mean_total(K))
    stats = {"images": len(images), "height": shape[0], "width": shape[1], "fingerprint_std": float(K.std())}
    return K.astype(np.float32), stats


def to_bytes(K: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, K.astype(np.float32), allow_pickle=False)
    return buf.getvalue()


def from_bytes(data: bytes) -> np.ndarray:
    return np.load(io.BytesIO(data), allow_pickle=False)


def _pce(C: np.ndarray, allowed: tuple[int, int], neigh: int = 11) -> tuple[float, tuple[int, int], float]:
    """Peak-to-correlation energy. `allowed` = (max dy, max dx) for the peak search."""
    region = C[: allowed[0] + 1, : allowed[1] + 1]
    iy, ix = np.unravel_index(int(np.argmax(region)), region.shape)
    peak = float(C[iy, ix])
    h, w = C.shape
    mask = np.ones_like(C, dtype=bool)
    r = neigh // 2
    ys = np.arange(iy - r, iy + r + 1) % h
    xs = np.arange(ix - r, ix + r + 1) % w
    mask[np.ix_(ys, xs)] = False
    energy = float(np.mean(C[mask] ** 2))
    return (peak * abs(peak) / energy if energy > 0 else 0.0), (int(iy), int(ix)), peak


def match(img: np.ndarray, K: np.ndarray, rotations: bool = True, crop_search: bool = True,
          scales: tuple[float, ...] = (1.0,)) -> dict:
    """Score an image against a fingerprint. Searches 90-degree rotations, cropping and scale."""
    I = luma(_to255(img))
    W = noise_residual(img)
    X0 = W * I
    Kz = K.astype(np.float64)
    Kz = Kz - Kz.mean()
    FK = np.fft.fft2(Kz)
    H, Wd = K.shape
    best = None
    tried = []
    for rot in ((0, 1, 2, 3) if rotations else (0,)):
        Xr = np.rot90(X0, rot)
        for s in scales:
            Xs = Xr if s == 1.0 else cv2.resize(Xr, None, fx=1.0 / s, fy=1.0 / s, interpolation=cv2.INTER_CUBIC)
            h, w = Xs.shape
            if h > H or w > Wd:
                continue
            if not crop_search and (h, w) != (H, Wd):
                continue
            pad = np.zeros((H, Wd))
            pad[:h, :w] = Xs - Xs.mean()
            C = np.real(np.fft.ifft2(np.conj(np.fft.fft2(pad)) * FK))
            allowed = (H - h, Wd - w) if crop_search else (0, 0)
            pce, (dy, dx), peak = _pce(C, allowed)
            # Normalised correlation at the peak, for reference alongside PCE.
            Ks = Kz[dy : dy + h, dx : dx + w]
            ncc = float(np.sum((Xs - Xs.mean()) * Ks) / (np.linalg.norm(Xs - Xs.mean()) * np.linalg.norm(Ks) + 1e-12))
            entry = {"rotation_deg": 90 * rot, "scale": s, "pce": round(pce, 2), "offset_xy": [dx, dy], "ncc": round(ncc, 5)}
            tried.append(entry)
            if best is None or pce > best["pce"]:
                best = entry
    if best is None:
        raise ValueError("the image is larger than the camera reference in every orientation tried; "
                         "PRNU matching needs the reference at the camera's full resolution")
    # Searching many positions inflates PCE by chance; report how many hypotheses were tried.
    best = dict(best)
    best["hypotheses"] = len(tried)
    return {"best": best, "tried": tried}


def _window_sums(a: np.ndarray, win: int, gy: np.ndarray, gx: np.ndarray) -> np.ndarray:
    s = cv2.boxFilter(a, cv2.CV_64F, (win, win), anchor=(0, 0), normalize=True, borderType=cv2.BORDER_CONSTANT)
    return s[np.ix_(gy, gx)]


def _window_corr(W: np.ndarray, S: np.ndarray, win: int, gy: np.ndarray, gx: np.ndarray) -> np.ndarray:
    mw, ms = _window_sums(W, win, gy, gx), _window_sums(S, win, gy, gx)
    cov = _window_sums(W * S, win, gy, gx) - mw * ms
    vw = np.maximum(_window_sums(W * W, win, gy, gx) - mw * mw, 1e-12)
    vs = np.maximum(_window_sums(S * S, win, gy, gx) - ms * ms, 1e-12)
    return cov / np.sqrt(vw * vs)


def tamper_map(img: np.ndarray, K: np.ndarray, window: int = 128, step: int = 16) -> dict:
    """Where in an image from the reference camera is its sensor fingerprint missing?

    After Chen, Fridrich, Goljan and Lukas (2008): the image is first registered to the
    fingerprint (rotation and crop, from `match`). In every window the normalised correlation
    rho between the image's noise residual W and the expected PRNU term I*K is measured. How
    strong rho should be depends on the content (dark, saturated and textured areas carry
    less fingerprint), so a predictor of rho from window brightness, texture and saturation
    is fitted by least squares on the image's own windows, iteratively ignoring windows far
    below it. The spread of rho with no fingerprint (sigma0) is measured by correlating with
    the fingerprint shifted to an unrelated position. A window is flagged when its rho is
    below 3 standard deviations of the prediction and within 3 sigma0 of zero, and only where
    the prediction is high enough for that to be decidable.
    """
    m = match(img, K, rotations=True, crop_search=True)
    best = m["best"]
    rot, (dx, dy) = best["rotation_deg"] // 90, best["offset_xy"]
    imr = np.rot90(img, rot)
    h, w = imr.shape[:2]
    if min(h, w) < 2 * window:
        raise ValueError(f"the image is too small for {window}-pixel windows")
    Kc = K[dy : dy + h, dx : dx + w].astype(np.float64)
    I = luma(_to255(imr))
    W = noise_residual(imr)
    gy, gx = np.arange(0, h - window + 1, step), np.arange(0, w - window + 1, step)
    rho = _window_corr(W, I * Kc, window, gy, gx)
    # Null: the same windows against the fingerprint shifted half a frame (unrelated sensor positions).
    Ks = np.roll(Kc, (h // 2, w // 2), axis=(0, 1))
    rho0 = _window_corr(W, I * Ks, window, gy, gx)
    sigma0 = float(np.median(np.abs(rho0 - np.median(rho0)))) * 1.4826 + 1e-9
    # Features for the predictor (Chen et al. 2008): brightness, texture, saturation and their products.
    mean_i = _window_sums(I, window, gy, gx) / 255.0
    hp = np.abs(I - cv2.blur(I, (3, 3), borderType=cv2.BORDER_REFLECT))
    tex = 1.0 / (1.0 + _window_sums(hp, window, gy, gx))
    sat = _window_sums((I >= 250).astype(np.float64), window, gy, gx)
    X = np.stack([np.ones_like(mean_i), mean_i, mean_i ** 2, tex, mean_i * tex, sat], -1).reshape(-1, 6)
    y = rho.ravel()
    use = np.ones_like(y, bool)
    for _ in range(6):
        coef, *_ = np.linalg.lstsq(X[use], y[use], rcond=None)
        pred = X @ coef
        res = y[use] - pred[use]
        sigma1 = float(np.median(np.abs(res - np.median(res)))) * 1.4826 + 1e-9
        use = y > pred - 2.5 * sigma1
    pred = pred.reshape(rho.shape)
    measurable = pred >= 3 * sigma0 + 3 * sigma1
    tampered = measurable & (rho < pred - 3 * sigma1) & (rho < 3 * sigma0)
    missing = np.where(measurable, np.clip(1 - rho / np.maximum(pred, 1e-9), 0, 1), 0.0)
    return {"match": best, "rotation": rot, "offset_xy": [dx, dy], "shape": (h, w), "step": step, "window": window,
            "rho": rho, "predicted": pred, "sigma0": sigma0, "sigma1": sigma1, "measurable": measurable,
            "tampered": tampered, "missing": missing}
