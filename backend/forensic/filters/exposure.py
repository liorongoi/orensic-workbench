"""Exposure, contrast and colour filters."""

from __future__ import annotations

import cv2
import numpy as np

from .base import FilterError, Param, clip01, from_u8, luminance, register, u8

GONZALEZ = "R. C. Gonzalez and R. E. Woods, Digital Image Processing, 4th ed., Pearson, 2018, ch. 3."


@register(
    "exposure.levels",
    name="Levels",
    version="1.0.0",
    category="Exposure",
    description="Maps the input range [black, white] linearly onto the output range, with a gamma "
    "correction in between. Pixels outside the input range are clipped.",
    reference=GONZALEZ,
    params=[
        Param("in_black", "float", 0.0, "Input black", 0.0, 1.0, 0.005),
        Param("in_white", "float", 1.0, "Input white", 0.0, 1.0, 0.005),
        Param("gamma", "float", 1.0, "Gamma", 0.1, 10.0, 0.01, help=">1 brightens mid-tones"),
        Param("out_black", "float", 0.0, "Output black", 0.0, 1.0, 0.005),
        Param("out_white", "float", 1.0, "Output white", 0.0, 1.0, 0.005),
    ],
)
def levels(img, p):
    if p["in_white"] <= p["in_black"]:
        raise FilterError("input white must be above input black")
    x = np.clip((img - p["in_black"]) / (p["in_white"] - p["in_black"]), 0.0, 1.0)
    x = np.power(x, 1.0 / p["gamma"], dtype=np.float32)
    return clip01(p["out_black"] + x * (p["out_white"] - p["out_black"]))


@register(
    "exposure.brightness_contrast",
    name="Brightness and contrast",
    version="1.0.0",
    category="Exposure",
    description="Adds a constant brightness offset and scales contrast around mid-grey.",
    reference=GONZALEZ,
    params=[
        Param("brightness", "float", 0.0, "Brightness", -1.0, 1.0, 0.01),
        Param("contrast", "float", 1.0, "Contrast", 0.0, 5.0, 0.01),
    ],
)
def brightness_contrast(img, p):
    return clip01((img - 0.5) * p["contrast"] + 0.5 + p["brightness"])


@register(
    "exposure.curves",
    name="Curves",
    version="1.0.0",
    category="Exposure",
    description="Applies a tone curve through the given control points using monotone cubic "
    "(PCHIP) interpolation, so the curve never reverses tonal order.",
    reference="F. N. Fritsch and R. E. Carlson, 'Monotone Piecewise Cubic Interpolation', "
    "SIAM J. Numer. Anal. 17(2), 1980.",
    params=[
        Param(
            "points", "points", [[0.0, 0.0], [0.25, 0.3], [0.75, 0.8], [1.0, 1.0]], "Control points",
            help="Input/output pairs in [0, 1], increasing input",
        ),
        Param("channel", "choice", "rgb", "Channel", choices=("rgb", "red", "green", "blue")),
    ],
)
def curves(img, p):
    pts = sorted(p["points"])
    xs = np.array([a for a, _ in pts], dtype=np.float64)
    ys = np.array([b for _, b in pts], dtype=np.float64)
    if len(xs) < 2 or np.any(np.diff(xs) <= 0) or xs.min() < 0 or xs.max() > 1:
        raise FilterError("curve points need at least two distinct inputs inside [0, 1]")
    lut_x = np.linspace(0.0, 1.0, 4096)
    lut = np.clip(_pchip(xs, ys, lut_x), 0.0, 1.0).astype(np.float32)
    idx = np.clip(np.rint(img * 4095.0), 0, 4095).astype(np.int32)
    out = img.copy()
    chans = {"rgb": (0, 1, 2), "red": (0,), "green": (1,), "blue": (2,)}[p["channel"]]
    for c in chans:
        out[..., c] = lut[idx[..., c]]
    return out


def _pchip(x, y, xq):
    h = np.diff(x)
    delta = np.diff(y) / h
    n = len(x)
    d = np.zeros(n)
    if n == 2:
        d[:] = delta[0]
    else:
        for k in range(1, n - 1):
            if delta[k - 1] * delta[k] > 0:
                w1 = 2 * h[k] + h[k - 1]
                w2 = h[k] + 2 * h[k - 1]
                d[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])
        d[0] = _end_slope(h[0], h[1], delta[0], delta[1])
        d[-1] = _end_slope(h[-1], h[-2], delta[-1], delta[-2])
    k = np.clip(np.searchsorted(x, xq, side="right") - 1, 0, n - 2)
    t = (xq - x[k]) / h[k]
    h00 = 2 * t**3 - 3 * t**2 + 1
    h10 = t**3 - 2 * t**2 + t
    h01 = -2 * t**3 + 3 * t**2
    h11 = t**3 - t**2
    return h00 * y[k] + h10 * h[k] * d[k] + h01 * y[k + 1] + h11 * h[k] * d[k + 1]


def _end_slope(h0, h1, d0, d1):
    s = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if np.sign(s) != np.sign(d0):
        return 0.0
    if np.sign(d0) != np.sign(d1) and abs(s) > abs(3 * d0):
        return 3 * d0
    return s


@register(
    "exposure.clahe",
    name="Local contrast (CLAHE)",
    version="1.0.0",
    category="Exposure",
    description="Contrast-limited adaptive histogram equalisation on the lightness channel (CIE L*a*b*), "
    "which lifts detail in dark and bright areas without changing colour.",
    reference="K. Zuiderveld, 'Contrast Limited Adaptive Histogram Equalization', Graphics Gems IV, 1994.",
    params=[
        Param("clip_limit", "float", 2.0, "Clip limit", 0.5, 40.0, 0.1),
        Param("tiles", "int", 8, "Tiles per side", 1, 64, 1),
    ],
)
def clahe(img, p):
    lab = cv2.cvtColor(u8(img), cv2.COLOR_RGB2LAB)
    eq = cv2.createCLAHE(clipLimit=p["clip_limit"], tileGridSize=(p["tiles"], p["tiles"]))
    lab[..., 0] = eq.apply(lab[..., 0])
    return from_u8(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


@register(
    "exposure.histogram_equalization",
    name="Histogram equalisation",
    version="1.0.0",
    category="Exposure",
    description="Global histogram equalisation of the lightness channel.",
    reference=GONZALEZ,
)
def histogram_equalization(img, p):
    lab = cv2.cvtColor(u8(img), cv2.COLOR_RGB2LAB)
    lab[..., 0] = cv2.equalizeHist(lab[..., 0])
    return from_u8(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


@register(
    "color.channel_gains",
    name="Colour balance",
    version="1.0.0",
    category="Colour",
    description="Multiplies each colour channel by a gain. In 'grey world' mode the gains are computed "
    "so the image's average colour becomes neutral grey.",
    reference="G. Buchsbaum, 'A spatial processor model for object colour perception', "
    "J. Franklin Inst. 310(1), 1980.",
    params=[
        Param("mode", "choice", "manual", "Mode", choices=("manual", "grey_world")),
        Param("red", "float", 1.0, "Red gain", 0.0, 8.0, 0.01),
        Param("green", "float", 1.0, "Green gain", 0.0, 8.0, 0.01),
        Param("blue", "float", 1.0, "Blue gain", 0.0, 8.0, 0.01),
    ],
)
def channel_gains(img, p):
    if p["mode"] == "grey_world":
        means = img.reshape(-1, 3).mean(axis=0, dtype=np.float64)
        gains = means.mean() / np.maximum(means, 1e-6)
    else:
        gains = np.array([p["red"], p["green"], p["blue"]])
    return clip01(img * gains.astype(np.float32))


@register(
    "color.grayscale",
    name="Greyscale / channel",
    version="1.0.0",
    category="Colour",
    description="Converts to Rec. 601 luma, or isolates one colour channel, shown as greyscale.",
    reference="ITU-R Recommendation BT.601-7, 2011.",
    params=[Param("source", "choice", "luma", "Source", choices=("luma", "red", "green", "blue"))],
)
def grayscale(img, p):
    if p["source"] == "luma":
        g = luminance(img)
    else:
        g = img[..., {"red": 0, "green": 1, "blue": 2}[p["source"]]]
    return np.repeat(g[..., None], 3, axis=2).astype(np.float32)


@register(
    "color.invert",
    name="Invert",
    version="1.0.0",
    category="Colour",
    description="Replaces every value v with 1 - v (photographic negative).",
    reference=GONZALEZ,
)
def invert(img, p):
    return (1.0 - img).astype(np.float32)


@register(
    "exposure.range_expand",
    name="Video range to full range",
    version="1.0.0",
    category="Exposure",
    description="Stretches 'video range' footage, where black is stored as 16 and white as 235 (of 255), to the full "
    "0-255 range. Many DVRs and exports are decoded with the wrong range flag, so the picture looks washed out and "
    "low in contrast; this restores the intended blacks and whites. Use it only when the darkest and brightest "
    "areas sit near 16 and 235; on footage that is already full range it clips.",
    reference="ITU-R Recommendation BT.601-7 (2011) and BT.709-6 (2015): 8-bit studio range, black 16, white 235 "
    "(Y) and 240 (Cb, Cr).",
    params=[Param("chroma", "bool", True, "Also expand colour", help="Applies the colour range (16-240) as well, in YCbCr")],
)
def range_expand(img, p):
    lo, hi = 16.0 / 255.0, 235.0 / 255.0
    if not p["chroma"]:
        return clip01((img - lo) / (hi - lo))
    ycc = cv2.cvtColor(np.ascontiguousarray(img, np.float32), cv2.COLOR_RGB2YCrCb)
    ycc[..., 0] = (ycc[..., 0] - lo) / (hi - lo)
    ycc[..., 1:] = (ycc[..., 1:] - 0.5) * (255.0 / 224.0) + 0.5
    return clip01(cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB))


@register(
    "exposure.low_light",
    name="Low-light enhancement (illumination map)",
    version="1.0.0",
    category="Exposure",
    description="Brightens dark footage by estimating how strongly each area was lit (the brightest colour channel, "
    "smoothed while keeping edges) and dividing the image by that illumination, raised to a power. Dark areas are "
    "lifted a lot and bright areas (headlights, lamps) hardly at all, so detail appears in the shadows without "
    "blowing out the lights, and colour ratios are kept. Lifting the shadows also lifts their noise, so the "
    "lift is capped and the result is denoised in proportion to how much each area was brightened. Best on scenes lit "
    "unevenly (a lamp or headlights and dark surroundings); on evenly dark footage, levels does as well. "
    "It changes local brightness, so do not use the result to judge lighting or colour.",
    reference="X. Guo, Y. Li and H. Ling, 'LIME: Low-Light Image Enhancement via Illumination Map Estimation', "
    "IEEE Trans. Image Processing 26(2), 2017; C. Tomasi and R. Manduchi, 'Bilateral filtering for gray and color "
    "images', ICCV 1998.",
    params=[
        Param("strength", "float", 0.8, "Strength (gamma)", 0.1, 1.0, 0.05,
              help="How far the illumination is divided out: 1 lifts the most, 0.5 is gentler"),
        Param("smoothness", "float", 3.0, "Illumination smoothness (px)", 0.5, 30.0, 0.5,
              help="Larger keeps more local contrast; smaller flattens more"),
        Param("max_gain", "float", 6.0, "Largest brightening (x)", 1.0, 40.0, 0.5,
              help="Caps the lift of the darkest areas, where there is mostly noise to lift"),
        Param("denoise", "float", 10.0, "Shadow denoise", 0.0, 30.0, 0.5,
              help="Non-local means strength (colour twice this) blended in where the image was brightened most; 0 turns it off"),
    ],
)
def low_light(img, p):
    t0 = np.ascontiguousarray(img.max(axis=2), np.float32)
    t = cv2.bilateralFilter(t0, 0, 0.08, 12)
    t = np.maximum(cv2.GaussianBlur(t, (0, 0), p["smoothness"], borderType=cv2.BORDER_REFLECT), 1e-3)
    gain = np.minimum(t ** -p["strength"], p["max_gain"])
    out = clip01((img * gain[..., None]).astype(np.float32))
    if p["denoise"] <= 0:
        return out
    u = np.clip(np.rint(out * 255.0), 0, 255).astype(np.uint8)
    den = cv2.fastNlMeansDenoisingColored(u, None, p["denoise"], 2 * p["denoise"], 7, 21).astype(np.float32) / 255.0
    w = (1.0 / gain)[..., None]  # as LIME: keep lit areas, take the denoised result where the gain was high
    return clip01((out * w + den * (1.0 - w)).astype(np.float32))
