"""Noise reduction and sharpening."""

from __future__ import annotations

import cv2
import numpy as np

from .base import Param, clip01, from_u8, luminance, register, u8


@register(
    "denoise.median",
    name="Median filter",
    version="1.0.0",
    category="Denoise",
    description="Replaces each pixel by the median of its square neighbourhood; removes impulse "
    "(salt-and-pepper) noise while keeping edges.",
    reference="J. W. Tukey, Exploratory Data Analysis, Addison-Wesley, 1977; Gonzalez and Woods, ch. 3.",
    params=[Param("size", "int", 3, "Window size (odd)", 3, 15, 2)],
)
def median(img, p):
    k = p["size"] | 1
    return from_u8(cv2.medianBlur(u8(img), k))


@register(
    "denoise.gaussian",
    name="Gaussian blur",
    version="1.0.0",
    category="Denoise",
    description="Smooths with a Gaussian kernel of the given standard deviation.",
    reference="R. C. Gonzalez and R. E. Woods, Digital Image Processing, 4th ed., 2018, ch. 3.",
    params=[Param("sigma", "float", 1.0, "Sigma (px)", 0.1, 20.0, 0.1)],
)
def gaussian(img, p):
    return cv2.GaussianBlur(img, (0, 0), p["sigma"], borderType=cv2.BORDER_REFLECT)


@register(
    "denoise.bilateral",
    name="Bilateral filter",
    version="1.0.0",
    category="Denoise",
    description="Edge-preserving smoothing that averages only neighbours of similar colour.",
    reference="C. Tomasi and R. Manduchi, 'Bilateral filtering for gray and color images', ICCV 1998.",
    params=[
        Param("diameter", "int", 7, "Diameter (px)", 3, 31, 2),
        Param("sigma_color", "float", 0.1, "Colour sigma", 0.01, 1.0, 0.01),
        Param("sigma_space", "float", 5.0, "Spatial sigma (px)", 0.5, 50.0, 0.5),
    ],
)
def bilateral(img, p):
    return clip01(cv2.bilateralFilter(img, p["diameter"], p["sigma_color"], p["sigma_space"]))


@register(
    "denoise.nlm",
    name="Non-local means",
    version="1.0.0",
    category="Denoise",
    description="Averages pixels whose surrounding patches look alike anywhere in a search window; "
    "strong on Gaussian-like sensor noise.",
    reference="A. Buades, B. Coll and J.-M. Morel, 'A non-local algorithm for image denoising', CVPR 2005.",
    params=[
        Param("strength", "float", 6.0, "Luminance strength", 1.0, 40.0, 0.5),
        Param("color_strength", "float", 6.0, "Colour strength", 0.0, 40.0, 0.5),
        Param("template", "int", 7, "Patch size", 3, 15, 2),
        Param("search", "int", 21, "Search window", 7, 41, 2),
    ],
)
def nlm(img, p):
    out = cv2.fastNlMeansDenoisingColored(
        u8(img), None, p["strength"], p["color_strength"], p["template"] | 1, p["search"] | 1
    )
    return from_u8(out)


@register(
    "denoise.fft_notch",
    name="Periodic noise removal (FFT)",
    version="1.0.0",
    category="Denoise",
    description="Finds isolated peaks in the Fourier spectrum (interference patterns, scan lines, "
    "screen moiré) away from the centre and suppresses them with Gaussian notch filters.",
    reference="Gonzalez and Woods, Digital Image Processing, 4th ed., 2018, sec. 5.4 (notch filters).",
    params=[
        Param("threshold", "float", 6.0, "Peak threshold (x median)", 2.0, 100.0, 0.5),
        Param("min_radius", "float", 0.05, "Protected centre radius", 0.01, 0.5, 0.01,
              help="Fraction of the spectrum around DC that is never touched"),
        Param("notch_sigma", "float", 2.0, "Notch width (px)", 0.5, 20.0, 0.5),
    ],
)
def fft_notch(img, p):
    h, w = img.shape[:2]
    lum = luminance(img).astype(np.float64)
    spec = np.fft.fftshift(np.fft.fft2(lum))
    mag = np.log1p(np.abs(spec))
    # Peaks relative to a smooth background estimate of the spectrum.
    bg = cv2.medianBlur(mag.astype(np.float32), 5).astype(np.float64)
    ratio = (mag - bg) / (np.median(np.abs(mag - bg)) + 1e-9)
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot((yy - h / 2) / (h / 2), (xx - w / 2) / (w / 2))
    peaks = (ratio > p["threshold"]) & (r > p["min_radius"])
    mask = np.ones((h, w), dtype=np.float64)
    ys, xs = np.nonzero(peaks)
    s2 = 2 * p["notch_sigma"] ** 2
    rad = int(np.ceil(3 * p["notch_sigma"]))
    for y, x in zip(ys, xs):
        y0, y1, x0, x1 = max(0, y - rad), min(h, y + rad + 1), max(0, x - rad), min(w, x + rad + 1)
        g = 1 - np.exp(-((yy[y0:y1, x0:x1] - y) ** 2 + (xx[y0:y1, x0:x1] - x) ** 2) / s2)
        mask[y0:y1, x0:x1] *= g
    out = np.empty_like(img)
    for c in range(3):
        f = np.fft.fftshift(np.fft.fft2(img[..., c].astype(np.float64)))
        out[..., c] = np.real(np.fft.ifft2(np.fft.ifftshift(f * mask)))
    return clip01(out.astype(np.float32))


@register(
    "sharpen.unsharp_mask",
    name="Unsharp mask",
    version="1.0.0",
    category="Sharpen",
    description="Adds back the difference between the image and a blurred copy, increasing edge "
    "contrast. Does not recover detail; strong settings create halos.",
    reference="Gonzalez and Woods, Digital Image Processing, 4th ed., 2018, sec. 3.6.",
    params=[
        Param("radius", "float", 1.5, "Radius (sigma, px)", 0.1, 20.0, 0.1),
        Param("amount", "float", 1.0, "Amount", 0.0, 5.0, 0.05),
        Param("threshold", "float", 0.0, "Threshold", 0.0, 0.5, 0.005),
    ],
)
def unsharp_mask(img, p):
    blur = cv2.GaussianBlur(img, (0, 0), p["radius"], borderType=cv2.BORDER_REFLECT)
    detail = img - blur
    if p["threshold"] > 0:
        detail = np.where(np.abs(detail) >= p["threshold"], detail, 0.0)
    return clip01((img + p["amount"] * detail).astype(np.float32))
