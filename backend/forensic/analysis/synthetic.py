"""Synthetic-media analyses: provenance, camera-pipeline traces and learned detectors.

None of these proves that an image is real or generated. Provenance metadata can be
stripped or (without a valid signature) forged; camera traces disappear after JPEG
compression or resizing; learned detectors age as generators change. Each result is
worded "consistent / inconsistent with" and carries its reading guide and card.
"""

from __future__ import annotations

import io
import json
import os
import re

import cv2
import numpy as np
from PIL import Image

from .. import models
from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register
from .compression import _colormap

# IPTC digital source types (https://cv.iptc.org/newscodes/digitalsourcetype/)
AI_SOURCE_TYPES = {
    "trainedAlgorithmicMedia": "created by a trained (generative AI) model",
    "compositeWithTrainedAlgorithmicMedia": "a composite that includes generative AI content",
    "algorithmicMedia": "created by an algorithm without a camera (not necessarily AI)",
    "compositeSynthetic": "a composite that includes synthetic elements",
}
CAMERA_SOURCE_TYPES = {"digitalCapture", "computationalCapture", "negativeFilm", "positiveFilm", "print"}
# Names of image generators and AI tools that appear in metadata fields.
GENERATOR_NAMES = re.compile(
    r"DALL[\-·.\s]?E|Midjourney|Stable[\s_-]?Diffusion|SDXL|Firefly|Imagen|Gemini|Nano[\s-]?Banana|FLUX\.|Flux[\s-]?(?:dev|schnell|pro)|"
    r"Leonardo\.?Ai|NovelAI|ComfyUI|AUTOMATIC1111|InvokeAI|Ideogram|GPT-4o|gpt-image|Sora|Runway|Kling|Pika|Recraft|"
    r"Bing Image Creator|Designer \(Microsoft\)|Meta AI|Grok|Aurora",
    re.I,
)
PNG_GENERATOR_KEYS = {"parameters", "prompt", "workflow", "invokeai_metadata", "sd-metadata", "dream", "Dream"}
MAX_TEXT = 4000


def _c2pa_read(path, data: bytes) -> dict | None:
    """Read and validate a C2PA manifest offline (no certificate or remote-manifest fetching)."""
    try:
        import c2pa
    except ImportError:
        return {"available": False}
    settings = {"verify": {"ocsp_fetch": False, "remote_manifest_fetch": False}}
    anchors = os.environ.get("FORENSIC_C2PA_TRUST_ANCHORS")
    if anchors and os.path.isfile(anchors):
        settings["trust"] = {"trust_anchors": open(anchors).read()}
    try:
        ctx = c2pa.Context(c2pa.Settings.from_dict(settings))
        fmt = _mime(data)
        reader = c2pa.Reader(fmt, io.BytesIO(data), context=ctx) if fmt else c2pa.Reader(str(path), context=ctx)
        store = json.loads(reader.json())
        state = reader.get_validation_state()
    except Exception as e:  # noqa: BLE001 - no manifest, unsupported format or a malformed one
        msg = str(e)
        if "ManifestNotFound" in type(e).__name__ or "no JUMBF" in msg or "not found" in msg.lower():
            return None
        return {"available": True, "error": msg[:300]}
    active = store.get("manifests", {}).get(store.get("active_manifest") or "", {})
    actions = []
    for a in active.get("assertions", []):
        if str(a.get("label", "")).startswith("c2pa.actions"):
            for act in (a.get("data") or {}).get("actions", []):
                actions.append({"action": act.get("action"), "digital_source_type": act.get("digitalSourceType"),
                                "software_agent": act.get("softwareAgent")})
    codes = [s.get("code") for s in store.get("validation_status", []) or []]
    return {
        "available": True,
        "validation_state": str(state),
        "validation_codes": codes,
        "claim_generator": [g.get("name") for g in active.get("claim_generator_info", []) if isinstance(g, dict)]
        or ([active["claim_generator"]] if active.get("claim_generator") else []),
        "signer": active.get("signature_info", {}),
        "title": active.get("title"),
        "actions": actions,
        "ingredients": len(active.get("ingredients", []) or []),
        "manifests": len(store.get("manifests", {})),
    }


def _mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        if brand == b"avif":
            return "image/avif"
        return "video/mp4"
    return None


def _source_type_name(uri: str | None) -> str | None:
    if not uri:
        return None
    return uri.rstrip("/").rsplit("/", 1)[-1]


@register(
    "synthetic.provenance",
    name="Content credentials and AI provenance",
    version="1.0.0",
    category="Synthetic media",
    description="Reads Content Credentials (C2PA manifests) and validates their signatures and content hashes "
    "offline, and looks for declarations of AI generation in metadata: the IPTC digital source type in XMP, "
    "generator parameters stored in PNG text chunks, and names of image generators in metadata fields.",
    reference="C2PA Technical Specification 2.x (Coalition for Content Provenance and Authenticity); IPTC Digital "
    "Source Type vocabulary (cv.iptc.org/newscodes/digitalsourcetype); c2pa-rs / c2pa-python reference "
    "implementation.",
    applies_to=("image", "video"),
    reading_guide="A valid manifest shows who signed the claims and that the content has not changed since; whether "
    "the signer is trusted depends on the trust list configured (none by default, so valid signatures report "
    "'untrusted signer'). A declaration of AI generation is strong evidence of it, but its absence means nothing: "
    "most cameras write no credentials and most platforms strip metadata. Unsigned metadata can be written by "
    "anyone. Text read from the file is shown as the file's own content.",
)
def provenance(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    data = ctx.data
    flags: list[dict] = []
    findings: dict = {}
    declared: list[str] = []

    c2 = _c2pa_read(ctx.path, data)
    has_jumbf = b"jumb" in data and b"c2pa" in data
    if c2 is None and not has_jumbf:
        findings["c2pa"] = {"present": False}
    elif c2 is None or not c2.get("available", True) or c2.get("error"):
        findings["c2pa"] = {"present": True, "validated": False,
                            "note": (c2 or {}).get("error") or "the c2pa library is not installed, so the manifest was not validated"}
        flags.append({"code": "prov.c2pa_unvalidated", "level": "warning",
                      "text": "The file carries Content Credentials (C2PA) that could not be validated."})
    else:
        findings["c2pa"] = {"present": True, "validated": True} | c2
        state = c2["validation_state"].lower()
        if "invalid" in state:
            bad = [c for c in c2["validation_codes"] if c and not c.startswith("signingCredential.untrusted")]
            flags.append({"code": "prov.c2pa_invalid", "level": "warning",
                          "text": "Content Credentials do not validate (" + ", ".join(bad[:4] or ["invalid"])
                          + "): the content or the claims changed after signing, or the manifest is malformed."})
        else:
            trusted = "trusted" in state and "untrusted" not in state and "signingCredential.untrusted" not in c2["validation_codes"]
            flags.append({"code": "prov.c2pa_valid", "level": "info",
                          "text": "Content Credentials validate: the content matches what was signed"
                          + (" by a signer on the configured trust list." if trusted else
                             "; the signer is not on a configured trust list, so who signed it is not established.")})
        for a in c2["actions"]:
            st = _source_type_name(a.get("digital_source_type"))
            if st in AI_SOURCE_TYPES:
                declared.append(f"C2PA action {a.get('action')}: {AI_SOURCE_TYPES[st]}")
        edits = [a["action"] for a in c2["actions"] if a.get("action") and a["action"] not in ("c2pa.created", "c2pa.opened")]
        if edits:
            flags.append({"code": "prov.c2pa_edits", "level": "info",
                          "text": f"Content Credentials record {len(edits)} editing action(s): " + ", ".join(sorted(set(edits))[:6]) + "."})

    # XMP digital source type (unsigned)
    xmp_types = sorted(set(re.findall(rb"digitalsourcetype/([A-Za-z]+)", data[:4_000_000])))
    if xmp_types:
        findings["xmp_digital_source_types"] = [t.decode() for t in xmp_types]
        for t in findings["xmp_digital_source_types"]:
            if t in AI_SOURCE_TYPES and not any(t in d for d in declared):
                declared.append(f"IPTC digital source type in metadata: {AI_SOURCE_TYPES[t]} (unsigned)")

    # Text metadata: PNG chunks, EXIF and XMP fields that name a generator.
    texts: dict[str, str] = {}
    if ctx.probe["media_type"] == "image":
        try:
            with Image.open(io.BytesIO(data)) as im:
                for k, v in (getattr(im, "text", None) or im.info or {}).items():
                    if isinstance(v, (str, bytes)):
                        texts[f"png:{k}" if data[:4] == b"\x89PNG" else f"info:{k}"] = (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)[:MAX_TEXT]
                exif = im.getexif()
                for tag, name in ((0x0131, "Software"), (0x010E, "ImageDescription"), (0x013B, "Artist"), (0x010F, "Make"),
                                  (0x0110, "Model"), (0x9286, "UserComment")):
                    if exif.get(tag):
                        texts[f"exif:{name}"] = str(exif.get(tag))[:MAX_TEXT]
        except Exception:  # noqa: BLE001 - hostile files
            pass
    else:
        for s in [ctx.probe.get("container", {})] + list(ctx.probe.get("streams", [])):
            for k, v in (s.get("tags") or {}).items():
                texts[f"tag:{k}"] = str(v)[:MAX_TEXT]
    xs = data.find(b"<x:xmpmeta")
    if xs >= 0:
        xe = data.find(b"</x:xmpmeta>", xs)
        xmp = data[xs : xe if xe > 0 else xs + 50000].decode("utf-8", "replace")
        for m in re.finditer(r"(CreatorTool|Credit|Description|softwareAgent)[^>]*>([^<]{1,500})<|(CreatorTool|softwareAgent)=\"([^\"]{1,500})\"", xmp):
            key, val = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            texts[f"xmp:{key}"] = val
    gen_keys = sorted(k for k in texts if k.split(":", 1)[1] in PNG_GENERATOR_KEYS and k.startswith("png:"))
    named = sorted({m.group(0) for v in texts.values() for m in GENERATOR_NAMES.finditer(v)}, key=str.lower)
    if gen_keys:
        declared.append("PNG text chunks of the kind image generators write (" + ", ".join(k.split(":", 1)[1] for k in gen_keys) + ")")
    findings["metadata_text"] = texts
    if named:
        findings["generator_names_found"] = named
        flags.append({"code": "prov.ai_named", "level": "warning",
                      "text": f"Metadata names {len(named)} AI image generator(s) or tool(s); see the findings for the file's own text."})

    findings["ai_generation_declared"] = declared
    if declared:
        flags.insert(0, {"code": "prov.ai_declared", "level": "warning",
                         "text": "The file declares AI generation: " + "; ".join(declared) + "."})
    if not findings["c2pa"]["present"] and not declared and not named:
        flags.append({"level": "info", "text": "No Content Credentials or AI-generation metadata. This is normal for most "
                      "cameras and for files that passed through social media; it is not evidence either way."})
    if declared:
        summary = "Consistent with AI generation: the file declares it."
    elif named:
        summary = "Metadata names an AI generator or tool; no signed declaration."
    elif findings["c2pa"]["present"]:
        summary = "Content Credentials present; no AI generation declared."
    else:
        summary = "No provenance information found (not evidence either way)."
    return AnalysisResult(summary=summary, findings=findings, flags=flags)


# Camera-pipeline traces ----------------------------------------------------------------------------

CFA_BLOCK = 32
CFA_MIN_LOG_RATIO = 0.15
CFA_MIN_CONSISTENCY = 0.75
KIRCHNER = np.array([[-0.25, 0.5, -0.25], [0.5, 0.0, 0.5], [-0.25, 0.5, -0.25]])
RESAMPLING_PEAK = 9.0


def cfa_blocks(rgb: np.ndarray, block: int = CFA_BLOCK) -> tuple[np.ndarray, np.ndarray]:
    """Per-block log ratio of green-channel prediction error on the two quincunx lattices, and block texture.

    Bayer demosaicing interpolates half the green samples, which then fit their neighbours far
    better than the measured half: the log ratio is large, with a sign fixed by the Bayer layout.
    """
    g = rgb[..., 1].astype(np.float64) * 255.0
    pred = (np.roll(g, 1, 0) + np.roll(g, -1, 0) + np.roll(g, 1, 1) + np.roll(g, -1, 1)) / 4.0
    e2 = ((g - pred) ** 2)[2:-2, 2:-2]
    h, w = e2.shape
    bh, bw = h // block, w // block
    if bh == 0 or bw == 0:
        return np.zeros((0, 0)), np.zeros((0, 0))
    e2 = e2[: bh * block, : bw * block]
    yy, xx = np.mgrid[0:block, 0:block]
    even = ((yy + xx) % 2 == 0).astype(np.float64)
    blocks = e2.reshape(bh, block, bw, block).transpose(0, 2, 1, 3)
    ve = (blocks * even).sum(axis=(2, 3)) / even.sum() + 1e-3
    vo = (blocks * (1 - even)).sum(axis=(2, 3)) / (1 - even).sum() + 1e-3
    return np.log(ve / vo), ve + vo


def cfa_statistic(rgb: np.ndarray) -> dict:
    r, tex = cfa_blocks(rgb)
    if r.size < 4:
        return {"present": False, "median_log_ratio": 0.0, "consistency": 0.0, "blocks": int(r.size)}
    keep = tex > np.quantile(tex, 0.25)
    rk = r[keep]
    med = float(np.median(rk))
    cons = float(np.mean(np.sign(rk) == np.sign(med))) if med else 0.0
    return {"present": abs(med) >= CFA_MIN_LOG_RATIO and cons >= CFA_MIN_CONSISTENCY, "median_log_ratio": round(med, 4),
            "consistency": round(cons, 4), "blocks": int(rk.size), "map": r, "texture": tex}


def resampling_spectrum(rgb: np.ndarray, exclude_jpeg_grid: bool) -> dict:
    """Kirchner's fixed-predictor p-map and its contrast-normalised spectrum: peaks mean periodic resampling."""
    y = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.float64) * 255.0
    if min(y.shape) > 1024:  # analyse the centre: periodicity is global
        cy, cx = y.shape[0] // 2, y.shape[1] // 2
        y = y[cy - 512 : cy + 512, cx - 512 : cx + 512]
    e = y - cv2.filter2D(y, -1, KIRCHNER, borderType=cv2.BORDER_REFLECT)
    pmap = np.exp(-(e**2) / 2.0)[4:-4, 4:-4]
    h, w = pmap.shape
    win = np.outer(np.hanning(h), np.hanning(w))
    spec = np.abs(np.fft.fftshift(np.fft.fft2((pmap - pmap.mean()) * win)))
    bg = cv2.medianBlur(spec.astype(np.float32), 5).astype(np.float64) + 1e-9
    ratio = spec / bg
    fy = np.fft.fftshift(np.fft.fftfreq(h))
    fx = np.fft.fftshift(np.fft.fftfreq(w))
    FY, FX = np.meshgrid(fy, fx, indexing="ij")
    r = np.hypot(FX, FY)
    mask = (r > 0.03) & (np.abs(FX) < 0.48) & (np.abs(FY) < 0.48)
    if exclude_jpeg_grid:
        tol = 2.0 / max(h, w)
        on_grid = lambda f: np.abs(f * 8 - np.round(f * 8)) < tol * 8  # noqa: E731
        mask &= ~(on_grid(FX) & on_grid(FY))
    masked = np.where(mask, ratio, 0.0)
    i = int(np.argmax(masked))
    peak = float(masked.flat[i])
    f = (float(FY.flat[i]), float(FX.flat[i]))
    vis = np.log1p(np.where(mask, ratio, 1.0))
    vis = np.clip(vis / max(np.log1p(RESAMPLING_PEAK * 2), 1e-9), 0, 1)
    return {"peak_ratio": round(peak, 2), "peak_frequency_yx": [round(f[0], 4), round(f[1], 4)],
            "period_px": round(1.0 / max(np.hypot(*f), 1e-9), 2), "detected": peak >= RESAMPLING_PEAK, "spectrum": vis}


@register(
    "synthetic.camera_traces",
    name="Camera-pipeline traces (CFA and resampling)",
    version="1.0.0",
    category="Synthetic media",
    description="Looks for two traces of how the image was made. Colour filter array (CFA) demosaicing: cameras "
    "measure one colour per pixel and interpolate the rest, which leaves a periodic pattern in the green channel; "
    "generated images and resized ones lack it. Resampling: scaling or rotating after capture leaves periodic "
    "correlations between neighbouring pixels, found as peaks in the spectrum of a linear-predictor residue. "
    "The CFA map shows, block by block, where the pattern is present.",
    reference="A. C. Gallagher and T. Chen, 'Image Authentication by Detecting Traces of Demosaicing', CVPR "
    "Workshops 2008; P. Ferrara, T. Bianchi, A. De Rosa and A. Piva, 'Image Forgery Localization via Fine-Grained "
    "Analysis of CFA Artifacts', IEEE TIFS 7(5), 2012; M. Kirchner, 'Fast and Reliable Resampling Detection by "
    "Spectral Analysis of Fixed Linear Predictor Residue', ACM MM&Sec 2008.",
    applies_to=("image",),
    reading_guide="CFA traces present at native resolution are consistent with a camera capture and inconsistent with "
    "a generated or resized image; blocks without the pattern inside an image that has it elsewhere deserve a "
    "closer look. Their absence means little: JPEG at quality 95 or below, resizing, and many phone pipelines "
    "remove them (see the detector card). A resampling peak is consistent with scaling or rotation after capture; "
    "peaks on the 8-pixel JPEG grid are ignored for JPEG files because compression causes them.",
)
def camera_traces(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    img = ctx.image
    is_jpeg = ctx.data[:3] == b"\xff\xd8\xff"
    cfa = cfa_statistic(img)
    rs = resampling_spectrum(img, exclude_jpeg_grid=is_jpeg)
    flags: list[dict] = []
    findings = {
        "cfa": {k: v for k, v in cfa.items() if k not in ("map", "texture")} | {
            "thresholds": {"min_abs_median_log_ratio": CFA_MIN_LOG_RATIO, "min_consistency": CFA_MIN_CONSISTENCY}},
        "resampling": {k: v for k, v in rs.items() if k != "spectrum"} | {"threshold": RESAMPLING_PEAK,
                                                                          "jpeg_grid_excluded": is_jpeg},
    }
    maps = {}
    if "map" in cfa and cfa["map"].size:
        r, tex = cfa["map"], cfa["texture"]
        sign = np.sign(cfa["median_log_ratio"]) or 1.0
        agree = np.clip(sign * r / 1.0, -1, 1)
        flat = tex <= np.quantile(tex, 0.25)
        vis = np.zeros(r.shape + (3,), np.float32)
        vis[..., 0] = np.clip(-agree, 0, 1) * 0.9 + 0.1
        vis[..., 1] = np.clip(agree, 0, 1) * 0.8 + 0.1
        vis[..., 2] = 0.15
        vis[flat] = 0.35
        h, w = img.shape[:2]
        maps["cfa_blocks"] = cv2.resize(vis, (w, h), interpolation=cv2.INTER_NEAREST)
        if cfa["present"]:
            off = float(np.mean((sign * r[~flat]) < 0)) if (~flat).any() else 0.0
            findings["cfa"]["blocks_against_pattern"] = round(off, 4)
            flags.append({"code": "trace.cfa", "level": "info", "text": "CFA demosaicing traces present: consistent with a "
                          "camera capture at this resolution."})
            if off > 0.05:
                flags.append({"code": "trace.cfa_local", "level": "warning", "text": f"{off:.0%} of textured blocks lack the "
                              "CFA pattern found elsewhere in the image (red in the map)."})
        else:
            flags.append({"level": "info", "text": "No CFA demosaicing traces. Expected after JPEG compression at quality 95 "
                          "or below or after resizing; also consistent with a generated image. Not evidence on its own."})
    maps["resampling_spectrum"] = _colormap(rs["spectrum"].astype(np.float32))
    if rs["detected"]:
        flags.append({"code": "trace.resampled", "level": "warning", "text": f"Periodic resampling correlation (spectral peak "
                      f"{rs['peak_ratio']:.1f}x background, period about {rs['period_px']:.1f} px): consistent with scaling or "
                      "rotation after capture."})
    parts = ["CFA traces " + ("present" if cfa["present"] else "not found"),
             "resampling " + ("detected" if rs["detected"] else "not detected")]
    return AnalysisResult(summary="; ".join(parts) + ".", findings=findings, flags=flags, maps=maps)


# Learned detectors (installed by the lab) ---------------------------------------------------------------------


def _detectors() -> list[dict]:
    return [m for m in models.manifest() if m["kind"] == "detector"]


def _prepare(img: np.ndarray, spec: dict) -> list[tuple[np.ndarray, tuple[int, int, int, int]]]:
    size = int(spec.get("size", 224))
    mode = spec.get("mode", "center_crop")
    h, w = img.shape[:2]
    crops = []
    if mode == "resize":
        crops.append((cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA), (0, 0, w, h)))
    elif mode == "tiles":
        ys = list(range(0, max(1, h - size + 1), size))[:6]
        xs = list(range(0, max(1, w - size + 1), size))[:6]
        for y in ys:
            for x in xs:
                tile = img[y : y + size, x : x + size]
                if tile.shape[:2] != (size, size):
                    tile = cv2.resize(tile, (size, size), interpolation=cv2.INTER_AREA)
                crops.append((tile, (x, y, size, size)))
    else:
        s = size / min(h, w) if min(h, w) < size else 1.0
        im = cv2.resize(img, (max(size, int(round(w * s))), max(size, int(round(h * s)))), interpolation=cv2.INTER_CUBIC) if s != 1.0 else img
        hh, ww = im.shape[:2]
        y0, x0 = (hh - size) // 2, (ww - size) // 2
        crops.append((im[y0 : y0 + size, x0 : x0 + size], (int(x0 / s), int(y0 / s), int(size / s), int(size / s))))
    out = []
    mean = np.array(spec.get("mean", [0.0, 0.0, 0.0]), np.float32)
    std = np.array(spec.get("std", [1.0, 1.0, 1.0]), np.float32)
    scale = float(spec.get("scale", 1.0))
    for c, box in crops:
        x = c.astype(np.float32) * scale
        if spec.get("color", "RGB") == "BGR":
            x = x[..., ::-1]
        x = (x - mean) / std
        x = x.transpose(2, 0, 1)[None] if spec.get("layout", "NCHW") == "NCHW" else x[None]
        out.append((np.ascontiguousarray(x, np.float32), box))
    return out


def _activate(raw: np.ndarray, spec: dict) -> float:
    v = np.asarray(raw, np.float64).ravel()
    act = spec.get("activation", "sigmoid")
    idx = int(spec.get("index", 0))
    if act == "softmax":
        e = np.exp(v - v.max())
        return float((e / e.sum())[idx])
    if act == "sigmoid":
        return float(1.0 / (1.0 + np.exp(-v[idx])))
    return float(v[idx])


@register(
    "synthetic.learned",
    name="Learned AI-image detector (lab-installed)",
    version="1.0.0",
    category="Synthetic media",
    description="Runs a learned AI-generated-image detector that the lab has installed, licensed and validated "
    "(an ONNX model with a model card: training data, training date, version and measured accuracy). The score "
    "and the model's identity and weights hash are recorded. No detector ships with the workbench: published "
    "ones carry non-commercial licences or have not been validated on current generators.",
    reference="Method depends on the installed model; its card's reference is printed with the result (for example "
    "U. Ojha, Y. Li and Y. J. Lee, 'Towards Universal Fake Image Detectors that Generalize Across Generative "
    "Models', CVPR 2023; D. Cozzolino et al., 'Raising the Bar of AI-generated Image Detection with CLIP', CVPR "
    "Workshops 2024).",
    applies_to=("image",),
    reading_guide="A learned detector's score is evidence to weigh with the others, never proof. Detectors age as "
    "generators change: check the training date and the validation on the model card, and treat images from "
    "generators released after it with caution. Compression, resizing and screenshots move scores.",
    params=[Param("model", "text", "", "Detector model id", help="From python -m forensic.models list (kind detector)")],
    ai=True,
    unavailable=lambda: None if _detectors() else "No learned detector installed (see docs/ai-models.md).",
)
def learned(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    dets = _detectors()
    if not dets:
        raise ValueError("no learned detector is installed; see docs/ai-models.md for how a lab installs a validated one")
    mid = p["model"] or dets[0]["id"]
    m = models.get(mid)
    if m["kind"] != "detector":
        raise ValueError(f"{mid} is not a detector model")
    spec = m["input"]
    scores, boxes = [], []
    for x, box in _prepare(ctx.image, spec):
        scores.append(_activate(models.run(mid, x), m["output"]))
        boxes.append(box)
    score = float(np.mean(scores))
    thr = float(m.get("threshold", 0.5))
    positive = m["output"].get("positive", "synthetic")
    flags = []
    if score >= thr:
        flags.append({"code": "learned.synthetic", "level": "warning",
                      "text": f"Score {score:.3f} is at or above the model's threshold {thr}: consistent with '{positive}' by this model."})
    maps = {}
    if len(scores) > 1:
        h, w = ctx.image.shape[:2]
        heat = np.zeros((h, w), np.float32)
        cnt = np.zeros((h, w), np.float32)
        for s, (x, y, bw, bh) in zip(scores, boxes):
            heat[y : y + bh, x : x + bw] += s
            cnt[y : y + bh, x : x + bw] += 1
        maps["tile_scores"] = _colormap(np.where(cnt > 0, heat / np.maximum(cnt, 1), 0))
    findings = {
        "model": models.public(m) | {"verified_sha256": m["sha256"]},
        "score": round(score, 4),
        "threshold": thr,
        "positive_class": positive,
        "crops": len(scores),
        "crop_scores": [round(s, 4) for s in scores],
        "trained_on": m.get("trained_on"),
        "model_validation": m.get("validation"),
    }
    summary = (f"{m['id']} v{m.get('version', '?')}: score {score:.3f} (threshold {thr}): "
               + ("consistent with" if score >= thr else "not consistent with") + f" '{positive}' by this model.")
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps=maps)
