"""Additional classical filters (v0.4): exposure in stops, contrast stretch, shadows/highlights,
homomorphic illumination correction, neutral-area white balance, saturation, channel mixer,
colour-space channel views, false colour, threshold, edge maps, Laplacian sharpening,
morphology, Fourier spectrum and band filters, adaptive Wiener denoise, deblocking, fisheye
correction, 360° unwrapping, and multi-frame background subtraction, frame difference and
optical flow.

Every filter is a pure function of its input pixels and parameters, deterministic on one or
many threads, and documented with its reference.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from .base import FilterError, Param, clip01, luminance, register

GONZALEZ = "R. C. Gonzalez and R. E. Woods, Digital Image Processing, 4th ed., Pearson, 2018"


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(np.float32)


def _scale_luma(img: np.ndarray, new_l: np.ndarray, old_l: np.ndarray) -> np.ndarray:
    """Change brightness while keeping hue and saturation: multiply RGB by new/old luminance."""
    ratio = (new_l + 1e-4) / (old_l + 1e-4)
    return clip01(img * ratio[..., None])


# Exposure -------------------------------------------------------------------------------------------


@register(
    "exposure.stops",
    name="Exposure (stops)",
    version="1.0.0",
    category="Exposure",
    description="Multiplies scene light by 2^stops: pixels are converted from sRGB to linear light, scaled, "
    "and converted back, the way a longer or shorter camera exposure would change them. Highlights that "
    "were clipped in the source stay clipped; no detail is created.",
    reference="IEC 61966-2-1:1999 (sRGB transfer function); " + GONZALEZ + ", ch. 3.",
    params=[Param("stops", "float", 0.0, "Stops", -6.0, 6.0, 0.1, help="+1 doubles the light, -1 halves it")],
)
def exposure_stops(img, p):
    return _linear_to_srgb(_srgb_to_linear(img) * (2.0 ** p["stops"]))


@register(
    "exposure.contrast_stretch",
    name="Contrast stretch (percentile)",
    version="1.0.0",
    category="Exposure",
    description="Linear stretch that maps the low and high percentiles of the luminance histogram to black "
    "and white (the same mapping for all channels, so colours do not shift), or of each channel separately.",
    reference=GONZALEZ + ", §3.2 (contrast stretching).",
    params=[
        Param("low", "float", 0.5, "Low percentile", 0.0, 49.0, 0.1),
        Param("high", "float", 99.5, "High percentile", 51.0, 100.0, 0.1),
        Param("per_channel", "bool", False, "Per channel", help="Stretch R, G and B separately (shifts colours)"),
    ],
)
def contrast_stretch(img, p):
    if p["per_channel"]:
        out = np.empty_like(img)
        for c in range(3):
            lo, hi = np.percentile(img[..., c], [p["low"], p["high"]])
            out[..., c] = (img[..., c] - lo) / max(hi - lo, 1e-6)
        return clip01(out)
    lo, hi = np.percentile(luminance(img), [p["low"], p["high"]])
    return clip01((img - lo) / max(float(hi - lo), 1e-6))


@register(
    "exposure.shadows_highlights",
    name="Shadows and highlights",
    version="1.0.0",
    category="Exposure",
    description="Lifts dark regions and recovers bright regions using a large-radius Gaussian estimate of "
    "local brightness (the 'mask'): gain = 1 + shadows·(1−m)² in dark areas and 1 − highlights·m² in "
    "bright areas, applied to luminance with hue kept. Only tones present in the source are redistributed.",
    reference="Local tone mapping with a blurred luminance mask; cf. E. Reinhard et al., High Dynamic Range "
    "Imaging, 2nd ed., Morgan Kaufmann, 2010, ch. 8.",
    params=[
        Param("shadows", "float", 0.5, "Shadows", 0.0, 2.0, 0.05),
        Param("highlights", "float", 0.3, "Highlights", 0.0, 1.0, 0.05),
        Param("radius", "float", 30.0, "Mask radius (px)", 2.0, 300.0, 1.0),
    ],
)
def shadows_highlights(img, p):
    L = luminance(img)
    m = cv2.GaussianBlur(L, (0, 0), p["radius"], borderType=cv2.BORDER_REFLECT)
    gain = 1.0 + p["shadows"] * (1.0 - m) ** 2 - p["highlights"] * m ** 2
    newL = np.clip(L * gain, 0, 1)
    return _scale_luma(img, newL, L)


@register(
    "exposure.homomorphic",
    name="Homomorphic illumination correction",
    version="1.0.0",
    category="Exposure",
    description="Separates slowly varying illumination from reflectance: log of luminance, a Gaussian "
    "high-emphasis filter in the frequency domain H = (γH − γL)(1 − exp(−D²/(2·D0²))) + γL, then exp. "
    "Evens out uneven lighting (vignetting, a torch spot) and boosts detail.",
    reference=GONZALEZ + ", §4.9 (homomorphic filtering); A. V. Oppenheim, R. W. Schafer and T. G. Stockham, "
    "'Nonlinear filtering of multiplied and convolved signals', Proc. IEEE 56(8), 1968.",
    params=[
        Param("gamma_low", "float", 0.5, "Low-frequency gain γL", 0.0, 1.0, 0.05),
        Param("gamma_high", "float", 1.5, "High-frequency gain γH", 1.0, 4.0, 0.05),
        Param("cutoff", "float", 0.02, "Cutoff D0 (fraction of size)", 0.002, 0.3, 0.002),
    ],
)
def homomorphic(img, p):
    L = luminance(img).astype(np.float64)
    h, w = L.shape
    ph, pw = cv2.getOptimalDFTSize(h * 2) // 1, cv2.getOptimalDFTSize(w * 2)
    logL = np.log1p(L * 255.0)
    pad = cv2.copyMakeBorder(logL, 0, ph - h, 0, pw - w, cv2.BORDER_REFLECT)
    F = np.fft.fft2(pad)
    fy = np.fft.fftfreq(ph)[:, None]
    fx = np.fft.fftfreq(pw)[None, :]
    D2 = fy ** 2 + fx ** 2
    D0 = p["cutoff"]
    H = (p["gamma_high"] - p["gamma_low"]) * (1 - np.exp(-D2 / (2 * D0 ** 2))) + p["gamma_low"]
    out = np.real(np.fft.ifft2(F * H))[:h, :w]
    newL = np.expm1(out) / 255.0
    lo, hi = np.percentile(newL, [0.1, 99.9])
    newL = np.clip((newL - lo) / max(hi - lo, 1e-9), 0, 1).astype(np.float32)
    return _scale_luma(img, newL, L.astype(np.float32))


# Colour -----------------------------------------------------------------------------------------------


@register(
    "color.white_balance_area",
    name="White balance from a neutral area",
    version="1.0.0",
    category="Colour",
    description="Scales R, G and B so that a region the examiner knows to be neutral (grey, white) has equal "
    "channel means, keeping its brightness (von Kries diagonal model). With no region, the whole frame is "
    "used (grey-world assumption).",
    reference="J. von Kries, 1902, as in M. Ebner, Color Constancy, Wiley, 2007, ch. 6.",
    params=[Param("roi", "points", [], "Neutral area (two corners)", help="Drag a box on something grey or white")],
)
def white_balance_area(img, p):
    region = img
    pts = p["roi"]
    if pts:
        if len(pts) != 2:
            raise FilterError("roi: give two corners")
        (x0, y0), (x1, y1) = pts
        xa, xb = sorted((int(round(x0)), int(round(x1))))
        ya, yb = sorted((int(round(y0)), int(round(y1))))
        region = img[max(0, ya):yb + 1, max(0, xa):xb + 1]
        if region.size == 0:
            raise FilterError("roi: the area is outside the image")
    means = region.reshape(-1, 3).mean(axis=0)
    if means.min() < 1e-4:
        raise FilterError("the neutral area is black in one channel; choose a lighter area")
    target = float(0.299 * means[0] + 0.587 * means[1] + 0.114 * means[2])
    return clip01(img * (target / means).astype(np.float32))


@register(
    "color.saturation",
    name="Saturation",
    version="1.0.0",
    category="Colour",
    description="Scales chroma in CIE L*a*b* (a* and b* multiplied by the factor) so lightness is unchanged.",
    reference="CIE 15:2004 Colorimetry (CIELAB).",
    params=[Param("factor", "float", 1.5, "Saturation factor", 0.0, 4.0, 0.05)],
)
def saturation(img, p):
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2Lab)
    lab[..., 1:] *= p["factor"]
    return clip01(cv2.cvtColor(lab, cv2.COLOR_Lab2RGB))


@register(
    "color.channel_mixer",
    name="Channel mixer",
    version="1.0.0",
    category="Colour",
    description="Each output channel is a weighted sum of the input R, G and B (a 3×3 matrix). Used to "
    "separate inks, stains or a coloured background from a mark.",
    reference=GONZALEZ + ", ch. 6 (colour transformations).",
    params=[Param(f"{o}_{i}", "float", 1.0 if o == i else 0.0, f"{o.upper()} from {i.upper()}", -4.0, 4.0, 0.05)
            for o in "rgb" for i in "rgb"],
)
def channel_mixer(img, p):
    M = np.array([[p[f"{o}_{i}"] for i in "rgb"] for o in "rgb"], np.float32)
    return clip01(img.reshape(-1, 3) @ M.T).reshape(img.shape)


SPACES = {
    "HSV": (cv2.COLOR_RGB2HSV, ("H", "S", "V"), (360.0, 1.0, 1.0)),
    "Lab": (cv2.COLOR_RGB2Lab, ("L*", "a*", "b*"), None),
    "YCrCb": (cv2.COLOR_RGB2YCrCb, ("Y", "Cr", "Cb"), (1.0, 1.0, 1.0)),
    "XYZ": (cv2.COLOR_RGB2XYZ, ("X", "Y", "Z"), None),
    "RGB": (None, ("R", "G", "B"), (1.0, 1.0, 1.0)),
}


@register(
    "color.space_channel",
    name="Colour space channel",
    version="1.0.0",
    category="Colour",
    description="Converts to HSV, CIE L*a*b*, YCrCb, XYZ or RGB and shows one channel as grey, stretched "
    "to the channel's fixed range (not to the image's own range, so items stay comparable). Faint marks "
    "often stand out in a single chroma channel.",
    reference="CIE 15:2004; ITU-R BT.601 (YCrCb); A. R. Smith, 'Color gamut transform pairs', SIGGRAPH 1978 (HSV).",
    params=[
        Param("space", "choice", "Lab", "Colour space", choices=tuple(SPACES)),
        Param("channel", "int", 1, "Channel (1-3)", 1, 3, 1),
    ],
)
def space_channel(img, p):
    code, _, ranges = SPACES[p["space"]]
    conv = img if code is None else cv2.cvtColor(img, code)
    c = conv[..., p["channel"] - 1]
    if p["space"] == "Lab":
        c = c / 100.0 if p["channel"] == 1 else (c + 128.0) / 255.0
    elif p["space"] == "XYZ":
        c = c / 1.1
    else:
        c = c / ranges[p["channel"] - 1]
    c = np.clip(c, 0, 1).astype(np.float32)
    return np.repeat(c[..., None], 3, axis=2)


COLORMAPS = {"inferno": cv2.COLORMAP_INFERNO, "jet": cv2.COLORMAP_JET, "viridis": cv2.COLORMAP_VIRIDIS,
             "hot": cv2.COLORMAP_HOT, "turbo": cv2.COLORMAP_TURBO}


@register(
    "color.false_colour",
    name="False colour",
    version="1.0.0",
    category="Colour",
    description="Maps luminance through a colour table so that small brightness differences become visible "
    "hue differences. A visual aid only: it adds no information.",
    reference="Pseudo-colour image processing, " + GONZALEZ + ", §6.3.",
    params=[Param("map", "choice", "inferno", "Colour table", choices=tuple(COLORMAPS))],
)
def false_colour(img, p):
    g = np.clip(np.rint(luminance(img) * 255), 0, 255).astype(np.uint8)
    return cv2.applyColorMap(g, COLORMAPS[p["map"]])[..., ::-1].astype(np.float32) / 255.0


@register(
    "color.threshold",
    name="Threshold (binary)",
    version="1.0.0",
    category="Colour",
    description="Binary view of luminance: a fixed level, Otsu's automatic level (maximises between-class "
    "variance), or an adaptive local level (mean or Gaussian-weighted mean of a block, minus C).",
    reference="N. Otsu, 'A threshold selection method from gray-level histograms', IEEE SMC 9(1), 1979; "
    + GONZALEZ + ", §10.3.",
    params=[
        Param("method", "choice", "otsu", "Method", choices=("fixed", "otsu", "adaptive_mean", "adaptive_gaussian")),
        Param("level", "float", 0.5, "Level (fixed)", 0.0, 1.0, 0.005),
        Param("block", "int", 31, "Block size (adaptive, odd)", 3, 501, 2),
        Param("c", "float", 0.02, "C (adaptive)", -0.5, 0.5, 0.005),
        Param("invert", "bool", False, "Invert"),
    ],
)
def threshold(img, p):
    g = np.clip(np.rint(luminance(img) * 255), 0, 255).astype(np.uint8)
    mode = cv2.THRESH_BINARY_INV if p["invert"] else cv2.THRESH_BINARY
    if p["method"] == "fixed":
        _, b = cv2.threshold(g, int(round(p["level"] * 255)), 255, mode)
    elif p["method"] == "otsu":
        _, b = cv2.threshold(g, 0, 255, mode | cv2.THRESH_OTSU)
    else:
        if p["block"] % 2 == 0:
            raise FilterError("block size must be odd")
        am = cv2.ADAPTIVE_THRESH_MEAN_C if p["method"] == "adaptive_mean" else cv2.ADAPTIVE_THRESH_GAUSSIAN_C
        b = cv2.adaptiveThreshold(g, 255, am, mode, p["block"], p["c"] * 255)
    b = b.astype(np.float32) / 255.0
    return np.repeat(b[..., None], 3, axis=2)


# Edges and sharpening -----------------------------------------------------------------------------------


@register(
    "edges.detect",
    name="Edge / gradient map",
    version="1.0.0",
    category="Analysis view",
    description="Gradient magnitude by Sobel or Scharr, the absolute Laplacian, or Canny edges (hysteresis "
    "between a low and high threshold). Shows structure and boundaries; useful for checking alignment "
    "and for spotting inconsistent edges.",
    reference="J. Canny, 'A computational approach to edge detection', IEEE TPAMI 8(6), 1986; " + GONZALEZ + ", §10.2.",
    params=[
        Param("method", "choice", "sobel", "Method", choices=("sobel", "scharr", "laplacian", "canny")),
        Param("gain", "float", 2.0, "Display gain", 0.1, 20.0, 0.1),
        Param("low", "float", 0.1, "Canny low", 0.0, 1.0, 0.01),
        Param("high", "float", 0.3, "Canny high", 0.0, 1.0, 0.01),
        Param("overlay", "bool", False, "Overlay on image"),
    ],
)
def edges(img, p):
    g = luminance(img)
    if p["method"] == "canny":
        u = np.clip(np.rint(g * 255), 0, 255).astype(np.uint8)
        e = cv2.Canny(u, p["low"] * 255, max(p["high"], p["low"]) * 255, L2gradient=True).astype(np.float32) / 255
    elif p["method"] == "laplacian":
        e = np.abs(cv2.Laplacian(g, cv2.CV_32F, ksize=3)) * p["gain"]
    else:
        k = -1 if p["method"] == "scharr" else 3
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=k)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=k)
        scale = 1 / 16 if p["method"] == "scharr" else 1 / 4
        e = np.sqrt(gx * gx + gy * gy) * scale * p["gain"]
    e = np.clip(e, 0, 1).astype(np.float32)
    if p["overlay"]:
        out = img * 0.6
        out[..., 1] = np.maximum(out[..., 1], e)
        return clip01(out)
    return np.repeat(e[..., None], 3, axis=2)


@register(
    "sharpen.laplacian",
    name="Laplacian sharpen",
    version="1.0.0",
    category="Sharpen",
    description="Subtracts a multiple of the Laplacian (second derivative) from the image, steepening edges. "
    "Amplifies noise as well; denoise first.",
    reference=GONZALEZ + ", §3.6.",
    params=[Param("amount", "float", 0.5, "Amount", 0.0, 3.0, 0.05)],
)
def laplacian_sharpen(img, p):
    lap = cv2.Laplacian(img, cv2.CV_32F, ksize=3, borderType=cv2.BORDER_REFLECT)
    return clip01(img - p["amount"] * lap)


# Morphology ------------------------------------------------------------------------------------------------

MORPH = {"erode": cv2.MORPH_ERODE, "dilate": cv2.MORPH_DILATE, "open": cv2.MORPH_OPEN, "close": cv2.MORPH_CLOSE,
         "tophat": cv2.MORPH_TOPHAT, "blackhat": cv2.MORPH_BLACKHAT, "gradient": cv2.MORPH_GRADIENT}
SHAPES = {"rect": cv2.MORPH_RECT, "ellipse": cv2.MORPH_ELLIPSE, "cross": cv2.MORPH_CROSS}


@register(
    "morphology.op",
    name="Morphology",
    version="1.0.0",
    category="Analysis view",
    description="Grey-level mathematical morphology with a structuring element: erode, dilate, open, close, "
    "top-hat (bright detail smaller than the element), black-hat (dark detail) and gradient. Top-hat and "
    "black-hat remove uneven backgrounds behind fingerprints and text.",
    reference="J. Serra, Image Analysis and Mathematical Morphology, Academic Press, 1982; " + GONZALEZ + ", ch. 9.",
    params=[
        Param("operation", "choice", "tophat", "Operation", choices=tuple(MORPH)),
        Param("shape", "choice", "ellipse", "Element shape", choices=tuple(SHAPES)),
        Param("size", "int", 15, "Element size (px)", 1, 201, 1),
        Param("iterations", "int", 1, "Iterations", 1, 20, 1),
        Param("gain", "float", 1.0, "Display gain (top/black-hat, gradient)", 0.1, 20.0, 0.1),
    ],
)
def morphology(img, p):
    k = cv2.getStructuringElement(SHAPES[p["shape"]], (p["size"], p["size"]))
    out = cv2.morphologyEx(img, MORPH[p["operation"]], k, iterations=p["iterations"], borderType=cv2.BORDER_REFLECT)
    if p["operation"] in ("tophat", "blackhat", "gradient"):
        out = out * p["gain"]
    return clip01(out)


# Frequency domain ----------------------------------------------------------------------------------------------


@register(
    "fft.spectrum",
    name="Fourier spectrum (view)",
    version="1.0.0",
    category="Frequency",
    description="Shows log(1 + |F|) of the luminance's 2-D discrete Fourier transform, centred and scaled to "
    "the output range. Periodic patterns (screens, interference, moiré) appear as bright off-centre peaks. "
    "A view for analysis: the output is not an image of the scene.",
    reference=GONZALEZ + ", ch. 4.",
    params=[Param("window", "bool", True, "Hann window", help="Reduces the cross artefact from image borders")],
)
def fft_spectrum(img, p):
    g = luminance(img).astype(np.float64)
    if p["window"]:
        g = g * np.outer(np.hanning(g.shape[0]), np.hanning(g.shape[1]))
    mag = np.log1p(np.abs(np.fft.fftshift(np.fft.fft2(g))))
    mag = (mag / max(mag.max(), 1e-9)).astype(np.float32)
    return np.repeat(mag[..., None], 3, axis=2)


@register(
    "fft.band_filter",
    name="Frequency band filter",
    version="1.0.0",
    category="Frequency",
    description="Low-pass, high-pass or band-pass filter in the frequency domain with Gaussian or "
    "Butterworth roll-off (no ringing from ideal cut-offs). Cut-offs are fractions of the Nyquist frequency. "
    "High-pass output is centred on mid-grey.",
    reference=GONZALEZ + ", §4.8-4.10.",
    params=[
        Param("type", "choice", "lowpass", "Type", choices=("lowpass", "highpass", "bandpass")),
        Param("shape", "choice", "gaussian", "Roll-off", choices=("gaussian", "butterworth")),
        Param("low", "float", 0.05, "Low cut-off", 0.001, 1.0, 0.005),
        Param("high", "float", 0.3, "High cut-off", 0.001, 1.0, 0.005),
        Param("order", "int", 2, "Butterworth order", 1, 10, 1),
    ],
)
def fft_band(img, p):
    h, w = img.shape[:2]
    fy = np.fft.fftfreq(h)[:, None] * 2
    fx = np.fft.fftfreq(w)[None, :] * 2
    D = np.sqrt(fy ** 2 + fx ** 2)

    def lp(c):
        if p["shape"] == "gaussian":
            return np.exp(-(D ** 2) / (2 * c ** 2))
        return 1 / (1 + (D / c) ** (2 * p["order"]))

    if p["type"] == "lowpass":
        H = lp(p["high"])
    elif p["type"] == "highpass":
        H = 1 - lp(p["low"])
    else:
        if p["high"] <= p["low"]:
            raise FilterError("high cut-off must be above low cut-off")
        H = lp(p["high"]) - lp(p["low"])
    out = np.empty_like(img)
    for c in range(3):
        out[..., c] = np.real(np.fft.ifft2(np.fft.fft2(img[..., c].astype(np.float64)) * H))
    if p["type"] != "lowpass":
        out = out + 0.5
    return clip01(out.astype(np.float32))


# Noise and compression ------------------------------------------------------------------------------------------


@register(
    "denoise.wiener_adaptive",
    name="Adaptive Wiener denoise",
    version="1.0.0",
    category="Denoise",
    description="Lee's locally adaptive Wiener filter: each pixel is pulled toward its local mean by "
    "(σ²_local − σ²_noise)/σ²_local, so flat areas are smoothed and edges kept. Noise variance is the "
    "given value, or estimated as the mean local variance when 0.",
    reference="J.-S. Lee, 'Digital image enhancement and noise filtering by use of local statistics', IEEE "
    "TPAMI 2(2), 1980; J. S. Lim, Two-Dimensional Signal and Image Processing, Prentice Hall, 1990, §9.2.",
    params=[
        Param("window", "int", 5, "Window (odd)", 3, 31, 2),
        Param("noise_sigma", "float", 0.0, "Noise σ (0 = estimate)", 0.0, 0.3, 0.001),
    ],
)
def wiener_adaptive(img, p):
    if p["window"] % 2 == 0:
        raise FilterError("window must be odd")
    k = (p["window"], p["window"])
    mu = cv2.blur(img, k, borderType=cv2.BORDER_REFLECT)
    var = np.maximum(cv2.blur(img * img, k, borderType=cv2.BORDER_REFLECT) - mu * mu, 0)
    nv = p["noise_sigma"] ** 2 if p["noise_sigma"] > 0 else float(var.mean())
    gain = np.maximum(var - nv, 0) / np.maximum(var, 1e-12)
    return clip01(mu + gain * (img - mu))


@register(
    "denoise.deblock",
    name="Deblocking (8×8)",
    version="1.0.0",
    category="Denoise",
    description="Smooths the steps at 8×8 block boundaries left by JPEG/MPEG compression: across each "
    "boundary the two pixels either side are blended toward each other when the step is smaller than "
    "`threshold` (larger steps are treated as real edges and kept). The grid is aligned to the image origin.",
    reference="Simplified boundary filter after P. List et al., 'Adaptive deblocking filter', IEEE TCSVT 13(7), 2003.",
    params=[
        Param("threshold", "float", 0.06, "Edge threshold", 0.0, 0.5, 0.005),
        Param("strength", "float", 0.5, "Strength", 0.0, 1.0, 0.05),
        Param("block", "int", 8, "Block size", 4, 32, 4),
    ],
)
def deblock(img, p):
    out = img.copy()
    b, t, s = p["block"], p["threshold"], p["strength"]
    for axis in (1, 0):
        n = out.shape[axis]
        for x in range(b, n - 1, b):
            sl = [slice(None)] * 3
            sl[axis] = x - 2
            p1 = out[tuple(sl)].copy()
            sl[axis] = x - 1
            p0 = out[tuple(sl)].copy()
            sl[axis] = x
            q0 = out[tuple(sl)].copy()
            sl[axis] = x + 1 if x + 1 < n else x
            q1 = out[tuple(sl)].copy()
            step = q0 - p0
            mask = (np.abs(step) < t).astype(np.float32)
            d = step * mask * s
            sl[axis] = x - 1
            out[tuple(sl)] = p0 + d / 2
            sl[axis] = x
            out[tuple(sl)] = q0 - d / 2
            sl[axis] = x - 2
            out[tuple(sl)] = p1 + d / 6
            if x + 1 < n:
                sl[axis] = x + 1
                out[tuple(sl)] = q1 - d / 6
    return clip01(out)


# Geometry ------------------------------------------------------------------------------------------------------


@register(
    "geometry.fisheye",
    name="Fisheye correction",
    version="1.0.0",
    category="Geometry",
    description="Undistorts a fisheye image with the Kannala-Brandt equidistant model (θd = θ(1 + k1θ² + k2θ⁴ + "
    "k3θ⁶ + k4θ⁸)), as used by OpenCV's fisheye calibration. Focal length is a fraction of the image width; "
    "`balance` trades cropped corners (0) against keeping the whole field (1). Use calibrated values when "
    "measuring.",
    reference="J. Kannala and S. S. Brandt, 'A generic camera model and calibration method for conventional, "
    "wide-angle, and fish-eye lenses', IEEE TPAMI 28(8), 2006.",
    params=[
        Param("focal", "float", 0.35, "Focal (× width)", 0.05, 3.0, 0.01),
        Param("k1", "float", 0.0, "k1", -1.0, 1.0, 0.001),
        Param("k2", "float", 0.0, "k2", -1.0, 1.0, 0.001),
        Param("k3", "float", 0.0, "k3", -1.0, 1.0, 0.001),
        Param("k4", "float", 0.0, "k4", -1.0, 1.0, 0.001),
        Param("balance", "float", 0.0, "Balance", 0.0, 1.0, 0.05),
    ],
)
def fisheye(img, p):
    h, w = img.shape[:2]
    f = p["focal"] * w
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], np.float64)
    D = np.array([p["k1"], p["k2"], p["k3"], p["k4"]], np.float64).reshape(4, 1)
    newK = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(K, D, (w, h), np.eye(3), balance=p["balance"])
    m1, m2 = cv2.fisheye.initUndistortRectifyMap(K, D, np.eye(3), newK, (w, h), cv2.CV_32FC1)
    return clip01(cv2.remap(img, m1, m2, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT))


@register(
    "geometry.unwrap_360",
    name="360° view (equirectangular to perspective)",
    version="1.0.0",
    category="Geometry",
    description="Renders a normal (rectilinear) camera view from a 360° equirectangular image: choose the "
    "viewing direction (yaw, pitch) and horizontal field of view. Each output pixel's ray is mapped to "
    "longitude/latitude and sampled bicubically.",
    reference="Equirectangular (plate carrée) projection; R. Szeliski, Computer Vision: Algorithms and "
    "Applications, 2nd ed., Springer, 2022, §8.2.",
    params=[
        Param("yaw", "float", 0.0, "Yaw (°)", -180.0, 180.0, 1.0),
        Param("pitch", "float", 0.0, "Pitch (°)", -90.0, 90.0, 1.0),
        Param("fov", "float", 90.0, "Horizontal field of view (°)", 10.0, 160.0, 1.0),
        Param("width", "int", 0, "Output width (0 = half input)", 0, 8192, 1),
        Param("aspect", "float", 0.75, "Height / width", 0.2, 2.0, 0.05),
    ],
)
def unwrap_360(img, p):
    H, W = img.shape[:2]
    ow = p["width"] or max(16, W // 2)
    oh = max(8, int(round(ow * p["aspect"])))
    f = (ow / 2) / math.tan(math.radians(p["fov"]) / 2)
    xs, ys = np.meshgrid(np.arange(ow) - ow / 2 + 0.5, np.arange(oh) - oh / 2 + 0.5)
    dirs = np.stack([xs, ys, np.full_like(xs, f)], axis=-1)
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)
    yaw, pitch = math.radians(p["yaw"]), math.radians(p["pitch"])
    Rx = np.array([[1, 0, 0], [0, math.cos(pitch), -math.sin(pitch)], [0, math.sin(pitch), math.cos(pitch)]])
    Ry = np.array([[math.cos(yaw), 0, math.sin(yaw)], [0, 1, 0], [-math.sin(yaw), 0, math.cos(yaw)]])
    d = dirs @ (Ry @ Rx).T
    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arcsin(np.clip(-d[..., 1], -1, 1))
    mx = ((lon / (2 * math.pi) + 0.5) * W - 0.5).astype(np.float32)
    my = ((0.5 - lat / math.pi) * H - 0.5).astype(np.float32)
    return clip01(cv2.remap(img, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_WRAP))


# Multi-frame analysis ----------------------------------------------------------------------------------------------


@register(
    "stack.background_subtract",
    name="Background subtraction",
    version="1.0.0",
    category="Multi-frame",
    description="Estimates the static background as the per-pixel median of the frame range, then shows each "
    "frame's absolute difference from it (×gain), or a foreground mask above a threshold, or the background "
    "itself. Reveals moving objects and changes; assumes a fixed camera (stabilise first).",
    reference="M. Piccardi, 'Background subtraction techniques: a review', IEEE SMC 2004; temporal median background.",
    input_kind="stack",
    output_kind="stack",
    params=[
        Param("output", "choice", "difference", "Output", choices=("difference", "mask", "overlay", "background")),
        Param("gain", "float", 3.0, "Gain", 0.5, 20.0, 0.1),
        Param("threshold", "float", 0.08, "Mask threshold", 0.0, 1.0, 0.005),
    ],
)
def background_subtract(stack, p):
    from .. import compute

    bg = compute.median0(stack).astype(np.float32)
    if p["output"] == "background":
        return np.repeat(bg[None], stack.shape[0], axis=0)
    d = np.abs(stack - bg[None]).max(axis=3)
    if p["output"] == "difference":
        v = np.clip(d * p["gain"], 0, 1)
        return np.repeat(v[..., None], 3, axis=3).astype(np.float32)
    mask = (d > p["threshold"]).astype(np.float32)
    if p["output"] == "mask":
        return np.repeat(mask[..., None], 3, axis=3)
    out = stack * 0.5
    out[..., 0] = np.maximum(out[..., 0], mask)
    return clip01(out.astype(np.float32))


@register(
    "stack.frame_difference",
    name="Frame difference",
    version="1.0.0",
    category="Multi-frame",
    description="Absolute difference between each frame and the one before it (×gain); the first frame is "
    "black. Shows motion, flicker, and frames that are exact repeats (fully black).",
    reference=GONZALEZ + ", §2.6 (image subtraction).",
    input_kind="stack",
    output_kind="stack",
    params=[Param("gain", "float", 4.0, "Gain", 0.5, 50.0, 0.5)],
)
def frame_difference(stack, p):
    out = np.zeros_like(stack)
    out[1:] = np.abs(stack[1:] - stack[:-1]) * p["gain"]
    return clip01(out)


@register(
    "stack.optical_flow",
    name="Optical flow (view)",
    version="1.0.0",
    category="Multi-frame",
    description="Dense optical flow between consecutive frames by Farnebäck's polynomial expansion, shown as "
    "colour (hue = direction, brightness = speed, saturated at `max_speed` px/frame). The first frame is "
    "black. An analysis view of apparent motion, not of the scene.",
    reference="G. Farnebäck, 'Two-frame motion estimation based on polynomial expansion', SCIA 2003.",
    input_kind="stack",
    output_kind="stack",
    params=[
        Param("max_speed", "float", 8.0, "Speed for full brightness (px/frame)", 0.5, 100.0, 0.5),
        Param("levels", "int", 3, "Pyramid levels", 1, 6, 1),
        Param("window", "int", 15, "Window", 5, 61, 2),
    ],
)
def optical_flow(stack, p):
    g = [np.clip(np.rint(luminance(f) * 255), 0, 255).astype(np.uint8) for f in stack]
    out = np.zeros_like(stack)
    for i in range(1, len(g)):
        flow = cv2.calcOpticalFlowFarneback(g[i - 1], g[i], None, 0.5, p["levels"], p["window"], 3, 5, 1.2, 0)
        mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
        hsv = np.zeros(g[i].shape + (3,), np.float32)
        hsv[..., 0] = ang * 180 / np.pi
        hsv[..., 1] = 1.0
        hsv[..., 2] = np.clip(mag / p["max_speed"], 0, 1)
        out[i] = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    return clip01(out)
