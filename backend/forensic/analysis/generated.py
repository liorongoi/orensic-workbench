"""Bundled learned detector for fully generated images (GAN and diffusion models).

Community Forensics (Park and Owens, CVPR 2025): a ViT-Small/16 trained on 2.7 million images
from 4,803 generators (diffusion models, GANs and other architectures) plus real photographs.
MIT licence, shipped in the offline package; inference follows the authors' evaluation
protocol exactly (shorter side resized to 440 with bilinear filtering, centre crop 384,
ImageNet normalisation, sigmoid of one logit), and the converted model reproduces the authors'
published scores on their example images.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .. import models
from ..filters.base import Param
from ..validation import load_card
from .base import AnalysisContext, AnalysisResult, register

MODEL_ID = "detector.commfor_vit_s16_384"
RESIZE, CROP = 440, 384
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
UNCERTAIN = (0.2, 0.8)  # scores in this band are reported as uncertain either way


def preprocess(rgb: np.ndarray) -> np.ndarray:
    """float RGB in [0, 1] (H x W x 3) -> 1 x 3 x 384 x 384, as torchvision Resize(440) + CenterCrop(384)."""
    u8 = (np.clip(rgb[..., :3], 0, 1) * 255.0 + 0.5).astype(np.uint8)
    im = Image.fromarray(u8, "RGB")
    w, h = im.size
    if w <= h:
        size = (RESIZE, int(RESIZE * h / w))
    else:
        size = (int(RESIZE * w / h), RESIZE)
    im = im.resize(size, Image.BILINEAR)
    w, h = im.size
    top, left = int(round((h - CROP) / 2.0)), int(round((w - CROP) / 2.0))
    im = im.crop((left, top, left + CROP, top + CROP))
    x = (np.asarray(im, np.float32) / 255.0 - MEAN) / STD
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None], np.float32)


def score(rgb: np.ndarray) -> float:
    logit = float(np.asarray(models.run(MODEL_ID, preprocess(rgb))).ravel()[0])
    return float(1.0 / (1.0 + np.exp(-logit)))


def _unavailable() -> str | None:
    try:
        v = models.verify(models.get(MODEL_ID))
    except models.ModelError:
        return "The generated-image detector model is not installed."
    return None if v["ok"] else f"Detector weights failed verification: {v['problem']}."


@register(
    "synthetic.generated",
    name="Generated-image detector (GAN and diffusion)",
    version="1.0.0",
    category="Synthetic media",
    description="A learned detector for images made entirely by a generative model (diffusion models such as Stable "
    "Diffusion, FLUX, DALL-E and Midjourney, and GANs). It was trained on images from 4,803 generators and real "
    "photographs. It scores the whole image; it is not built to find a generated region pasted into a real photo.",
    reference="J. Park and A. Owens, 'Community Forensics: Using Thousands of Generators to Train Fake Image "
    "Detectors', CVPR 2025 (arXiv 2411.04125); model OwensLab/commfor-model-384, MIT licence.",
    applies_to=("image",),
    reading_guide="Scores at or above 0.5 are consistent with a generated image by this model; between 0.2 and 0.8 the "
    "result is uncertain either way. A learned score is one piece of evidence, never proof: generators released after "
    "the model was trained, heavy compression, screenshots and resizing move it, and heavily edited or computational "
    "photos can score high. Read it with the camera-trace, provenance and compression results, and check the "
    "detector card for measured accuracy.",
    params=[Param("flip_check", "bool", True, "Also score the mirrored image",
                  help="Scores the left-right mirror too and reports both; a large difference means the score is unstable.")],
    ai=True,
    unavailable=_unavailable,
)
def generated(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    m = models.get(MODEL_ID)
    img = ctx.image
    s = score(img)
    scores = {"original": round(s, 4)}
    if p["flip_check"]:
        scores["mirrored"] = round(score(img[:, ::-1]), 4)
    lo, hi = UNCERTAIN
    flags: list[dict] = []
    if s >= 0.5:
        verdict = "consistent with a generated image" + (" (uncertain)" if s < hi else "")
    else:
        verdict = "not consistent with a generated image" + (" (uncertain)" if s > lo else "")
    if s >= 0.5:
        flags.append({"code": "gen.generated", "level": "warning",
                      "text": f"Score {s:.3f}: {verdict} by this model. Screening, not a verdict."})
    else:
        flags.append({"level": "info", "text": f"Score {s:.3f}: {verdict} by this model. Unseen generators can be missed."})
    if "mirrored" in scores and abs(scores["mirrored"] - s) > 0.3:
        flags.append({"code": "gen.unstable", "level": "warning",
                      "text": f"The mirrored image scores {scores['mirrored']:.3f}: the score is unstable on this image."})
    h, w = img.shape[:2]
    if min(h, w) < 256:
        flags.append({"level": "info", "text": f"Small image ({w}x{h}): it is enlarged before scoring, which weakens the traces."})
    card = load_card("synthetic.generated")
    findings = {
        "score": round(s, 4),
        "threshold": 0.5,
        "uncertain_band": list(UNCERTAIN),
        "scores": scores,
        "verdict": verdict,
        "model": models.public(m) | {"verified_sha256": m["sha256"]},
        "device": models.device(MODEL_ID),
        "preprocessing": f"shorter side resized to {RESIZE} (bilinear), centre crop {CROP}x{CROP}, ImageNet normalisation",
        "validated_on": card.get("validated_on") if card else None,
    }
    return AnalysisResult(summary=f"Score {s:.3f}: {verdict}.", findings=findings, flags=flags)
