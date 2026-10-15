"""Validation of JPEG dimples, the PRNU tampering map, the fusion map and the quantisation-table database.

Same rules as the other experiments: fixed seeds and generated ground truth. Camera
dimples are emulated by an encoder whose DCT carries a small constant bias before
quantisation (the mechanism Agarwal and Farid describe); real cameras are measured
separately when a folder of camera originals is given (--camera-originals). The
quantisation-table figures are measured on the built-in database itself, file by file.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image

from .. import prnu, qtables
from ..analysis import ANALYSES
from ..analysis import dimples as dm
from ..analysis.camera import _prnu_tamper_result
from ..analysis.double_jpeg import DCT8
from ..analysis.jpeg import STD_CHROMA, STD_LUMA, ijg_table
from .phase4 import _overlap
from .run import decode, jpeg, rate, scene


def emulated_camera_jpeg(u8: np.ndarray, quality: int, bias: float) -> bytes:
    """JPEG whose 8x8 DCT coefficients were all biased by `bias` before quantisation, as a dimple-making
    camera encoder does. The result is written losslessly enough (quality 100, 4:4:4) to keep it."""
    ycc = np.asarray(Image.fromarray(u8).convert("YCbCr"), np.float64)
    out = ycc.copy()
    h, w = ycc.shape[:2]
    bh, bw = h // 8, w // 8
    for ch in range(3):
        t = ijg_table(STD_LUMA if ch == 0 else STD_CHROMA, quality).astype(np.float64)
        b = (ycc[: bh * 8, : bw * 8, ch] - 128).reshape(bh, 8, bw, 8).transpose(0, 2, 1, 3)
        c = np.rint((DCT8 @ b @ DCT8.T - bias) / t) * t
        out[: bh * 8, : bw * 8, ch] = (DCT8.T @ c @ DCT8 + 128).transpose(0, 2, 1, 3).reshape(bh * 8, bw * 8)
    rgb = Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8), "YCbCr").convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=100, subsampling=0)
    return buf.getvalue()


def _run(aid: str, data: bytes, **params):
    a = ANALYSES[aid]
    ctx = SimpleNamespace(data=data, image=decode(data).astype(np.float32) / 255.0, settings=None)
    return a.fn(ctx, a.validate(params))


def _paste(rng, bg: np.ndarray, donor: np.ndarray, frac: float = 0.3):
    h, w = bg.shape[:2]
    ph, pw = int(h * frac) // 8 * 8, int(w * frac) // 8 * 8
    y, x = int(rng.integers(0, (h - ph) // 8)) * 8, int(rng.integers(0, (w - pw) // 8)) * 8
    out = bg.copy()
    out[y : y + ph, x : x + pw] = donor[y : y + ph, x : x + pw]
    return out, (x, y, pw, ph)


def eval_dimples(src, n: int) -> dict:
    biases = (0.25, 0.5, 1.0)
    pos = {b: [] for b in biases}
    neg_single, neg_double = [], []
    loc_hit, loc_fp = [], []
    for i in range(n):
        rng = np.random.default_rng(11000 + i)
        x = src(11000 + i)
        q = int(rng.choice([75, 85, 95]))
        for b in biases:
            pos[b].append(abs(dm.dimples(emulated_camera_jpeg(x, q, b))["z"]) >= dm.DIMPLE_THRESHOLD)
        neg_single.append(abs(dm.dimples(jpeg(x, q))["z"]) >= dm.DIMPLE_THRESHOLD)
        neg_double.append(abs(dm.dimples(jpeg(decode(jpeg(x, int(rng.integers(50, 90)))), 90))["z"]) >= dm.DIMPLE_THRESHOLD)
        # Localisation: a camera original (bias 0.5) with an area pasted from a software-saved JPEG, re-saved at 90-95.
        cam = decode(emulated_camera_jpeg(x, q, 0.5))
        donor = decode(jpeg(src(12000 + i), int(rng.choice([80, 90]))))
        spliced, truth = _paste(rng, cam, donor)
        q2 = int(rng.choice([90, 95]))
        r = _run("tamper.jpeg_dimples", jpeg(spliced, q2))
        hit = False
        if r.findings["dimple_present"] and "weak_block_share" in r.findings:
            res = dm.dimples(jpeg(spliced, q2))
            strength, ok = dm.local_strength(res["cells"], res["position"], 8, res["textured"])
            weak = dm.weak_area(strength, ok)
            tx, ty, tw, th = truth
            inside = weak[ty // 8 : (ty + th) // 8, tx // 8 : (tx + tw) // 8]
            hit = any(f.get("code") == "dimples.local" for f in r.flags) and inside.mean() >= 0.3
        loc_hit.append(hit)
        clean = _run("tamper.jpeg_dimples", jpeg(cam, q2))
        loc_fp.append(any(f.get("code") == "dimples.local" for f in clean.flags))
    return {
        "task": "Does the JPEG carry a camera encoder's dimple? Where is it missing?",
        "positives": f"{n} images through an emulated dimple-making encoder (constant DCT bias before quantisation, "
                     "quality 75/85/95), at three bias strengths",
        "negatives": f"the same {n} images saved once by libjpeg, and decoded and saved twice by libjpeg",
        "threshold": dm.DIMPLE_THRESHOLD,
        "true_positive_rate_by_bias": {str(b): rate(v) for b, v in pos.items()},
        "true_positive_rate": rate(pos[0.5]),
        "false_positive_rate": rate(neg_single),
        "false_positive_rate_double_saved": rate(neg_double),
        "localisation": {
            "cases": f"{n} emulated camera originals (bias 0.5) with 30% x 30% pasted from a libjpeg-saved photo, re-saved at 90 or 95",
            "detected_means": "the local warning is raised and at least 30% of the pasted area is marked as lacking the dimple",
            "true_positive_rate": rate(loc_hit),
            "false_positive_rate": rate(loc_fp),
        },
    }


def eval_dimples_real(folder: Path) -> dict:
    """Observed on real camera originals: how many carry a dimple, and how many keep it after libjpeg re-saving."""
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
    orig, q90, q75, models = [], [], [], set()
    for p in files:
        data = p.read_bytes()
        try:
            z = abs(dm.dimples(data)["z"])
        except ValueError:
            continue
        im = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
        orig.append(z >= dm.DIMPLE_THRESHOLD)
        q90.append(abs(dm.dimples(jpeg(im, 90))["z"]) >= dm.DIMPLE_THRESHOLD)
        q75.append(abs(dm.dimples(jpeg(im, 75))["z"]) >= dm.DIMPLE_THRESHOLD)
        models.add(p.name.split("__")[0] + " " + (p.name.split("__")[1] if "__" in p.name else ""))
    return {
        "images": len(orig),
        "camera_models": len(models),
        "with_dimple": rate(orig),
        "with_dimple_after_libjpeg_resave_q90": rate(q90),
        "with_dimple_after_libjpeg_resave_q75": rate(q75),
        "note": "No ground truth: shows how common dimples are among these cameras and that re-saving does not reliably remove them.",
    }


def eval_prnu_map(n: int, size=(384, 512), k_std: float = 0.005) -> dict:
    h, w = size
    cams = [np.random.default_rng(91000 + c).normal(0, k_std, (h, w)) for c in range(2)]

    def shot(c, seed, flat=False):
        rng = np.random.default_rng(seed)
        base = (0.7 + 0.05 * cv2.GaussianBlur(rng.normal(0, 1, (h, w, 3)), (0, 0), 20)) if flat else scene(seed, h, w) / 255.0
        img = np.clip(base * (1 + cams[c][..., None]) + rng.normal(0, 0.006, base.shape), 0, 1)
        return (img * 255).round().astype(np.uint8)

    K = prnu.estimate_fingerprint([decode(jpeg(shot(0, 120000 + i, flat=True), 95)).astype(np.float32) / 255 for i in range(15)])[0]
    conds = ("pasted from another camera", "copied from elsewhere in the same image")
    hits = {c: [] for c in conds}
    fp = []
    for i in range(n):
        rng = np.random.default_rng(13000 + i)
        a = shot(0, 130000 + i)
        other, truth = _paste(rng, a, shot(1, 140000 + i), 0.33)
        x, y, tw, th = truth
        moved = a.copy()
        sy, sx = (y + h // 2) % (h - th), (x + w // 2) % (w - tw)
        moved[y : y + th, x : x + tw] = a[sy : sy + th, sx : sx + tw]
        q = int(rng.choice([85, 90, 95]))
        for c, im in zip(conds, (other, moved)):
            r = _prnu_tamper_result(decode(jpeg(im, q)).astype(np.float32) / 255, K, 128)
            hits[c].append(_overlap(r.findings["regions"], truth, "bbox_px"))
        fp.append(bool(_prnu_tamper_result(decode(jpeg(a, q)).astype(np.float32) / 255, K, 128).findings["regions"]))
    return {
        "task": "Which area of an image from the reference camera lacks its sensor fingerprint?",
        "cameras": f"2 simulated sensors, PRNU std {k_std}, {h}x{w}; reference from 15 flat images at JPEG 95",
        "positives": f"{n} images, each altered two ways in a third of each side (see true_positive_rate_by_condition), JPEG 85-95",
        "negatives": f"the same {n} images untouched",
        "detected_means": "an outlined area covers at least 30% of the altered rectangle (128-px windows)",
        "true_positive_rate_by_condition": {c: rate(v) for c, v in hits.items()},
        "true_positive_rate": rate(hits[conds[0]]),
        "false_positive_rate": rate(fp),
    }


def eval_fusion(src, n: int) -> dict:
    conds = ("one trace: uncompressed area pasted into a JPEG saved before (A-DJPG only)",
             "two traces: the pasted area is also smoothed (A-DJPG and noise)",
             "two traces: pasted into a camera original with dimples (A-DJPG and dimples)")
    hits = {c: [] for c in conds}
    fps = {"single save": [], "double save": [], "camera original with dimples, re-saved": []}
    for i in range(n):
        rng = np.random.default_rng(14000 + i)
        x, donor = src(14000 + i), src(15000 + i)
        q1 = int(rng.integers(50, 80))
        bg = decode(jpeg(x, q1))
        s1, t1 = _paste(rng, bg, donor)
        s2 = bg.copy()
        tx, ty, tw, th = t1
        s2[ty : ty + th, tx : tx + tw] = cv2.GaussianBlur(donor, (0, 0), 1.2)[ty : ty + th, tx : tx + tw]
        cam = decode(emulated_camera_jpeg(x, q1, 0.5))
        s3, t3 = _paste(rng, cam, donor)
        q2 = int(rng.choice([90, 95]))
        for c, im, t in ((conds[0], s1, t1), (conds[1], s2, t1), (conds[2], s3, t3)):
            hits[c].append(_overlap(_run("tamper.fusion", jpeg(im, q2)).findings["regions"], t, "bbox_px"))
        for k, im in (("single save", x), ("double save", bg), ("camera original with dimples, re-saved", cam)):
            fps[k].append(bool(_run("tamper.fusion", jpeg(im, q2)).findings["regions"]))
    return {
        "task": "Which area was pasted in? (fusion of A-DJPG, NA-DJPG, dimples and noise)",
        "positives": f"{n} images per condition: first save at JPEG 50-79, an uncompressed area of 30% x 30% pasted, final save at 90 or 95",
        "negatives": f"the same {n} images untouched in three histories (see false_positive_rate_by_history)",
        "detected_means": "an outlined area (belief above 0.55) covers at least 30% of the pasted rectangle",
        "true_positive_rate_by_condition": {c: rate(v) for c, v in hits.items()},
        "true_positive_rate": rate([h for v in hits.values() for h in v]),
        "false_positive_rate_by_history": {k: rate(v) for k, v in fps.items()},
        "false_positive_rate": rate([f for v in fps.values() for f in v]),
    }


def eval_qtables() -> dict:
    db = qtables.load(None)
    builtin = [e for e in db["entries"] if e.get("layer") == "built-in" and e["kind"] == "camera"]
    files = [(e, f) for e in builtin for f in e.get("files", [e["file"]])]
    # Leave one file out: is its table still recorded for its model, and would the mismatch warning fire?
    matched, flagged = [], []
    for e, f in files:
        same_model = [o for o in builtin if qtables.model_key(o["make"], o["model"]) == qtables.model_key(e["make"], e["model"])]
        others = {qtables.table_key(o["luma"], o["chroma"]) for o in same_model if o is not e or len(o.get("files", [])) > 1}
        if not others:
            continue  # model unknown once this file is removed: no claim either way
        k = qtables.table_key(e["luma"], e["chroma"])
        matched.append(k in others)
        flagged.append(k not in others)
    # Re-saved by libjpeg at a common quality: does the mismatch warning fire for the claimed model?
    detect = []
    models = {}
    for e in builtin:
        models.setdefault(qtables.model_key(e["make"], e["model"]), set()).add(qtables.table_key(e["luma"], e["chroma"]))
    for keys in models.values():
        for q in (70, 75, 80, 85, 90, 95, 100):
            k = qtables.table_key(ijg_table(STD_LUMA, q).ravel().tolist(), ijg_table(STD_CHROMA, q).ravel().tolist())
            detect.append(k not in keys)
    s = qtables.stats(db)
    return {
        "task": "Do the file's tables match a camera named in EXIF? Is a re-saved file caught?",
        "database": f"{s['camera_models']} camera models, {s['tables']} distinct table sets (built-in plus generated libjpeg)",
        "true_positive_rate": rate(detect),
        "true_positive_means": f"files re-saved by libjpeg at quality 70-100 with EXIF still naming one of {len(models)} known models: mismatch flagged",
        "false_positive_rate": rate(flagged),
        "false_positive_means": f"{len(flagged)} camera-original files from models with other recorded files, held out one at a time: "
                                "mismatch wrongly flagged because the database lacks that quality setting",
        "exif_camera_matched_when_held_out": rate(matched),
    }


def run_phase5(src, quick: bool, camera_originals: Path | None = None, log=print) -> dict:
    n = 6 if quick else 40
    log(f"JPEG dimples ({n} images)…")
    out = {"dimples": eval_dimples(src, n)}
    if camera_originals:
        log("JPEG dimples on real camera originals…")
        out["dimples"]["real_camera_originals"] = eval_dimples_real(camera_originals)
    log(f"PRNU tampering map ({n} images)…")
    out["prnu_map"] = eval_prnu_map(n)
    log(f"Fusion map ({n} images)…")
    out["fusion"] = eval_fusion(src, n)
    log("Quantisation-table database…")
    out["qtables"] = eval_qtables()
    return out


def phase5_cards(results: dict) -> dict[str, dict]:
    return {"tamper.jpeg_dimples": {"detection": results["dimples"]},
            "tamper.prnu_map": {"detection": results["prnu_map"]},
            "tamper.fusion": {"detection": results["fusion"]},
            "file.qt_database": {"detection": results["qtables"]}}


FAILURE_MODES = {
    "tamper.jpeg_dimples": [
        "Many cameras make no dimple; absence alone shows nothing.",
        "On real cameras the dimple often survives re-saving by software, so presence does not show the file is the camera original (see real_camera_originals).",
        "Synthetic positives come from an emulated encoder; real dimple strengths vary by camera model.",
        "Flat, saturated and very dark areas carry no dimple and are hatched in the map.",
    ],
    "tamper.prnu_map": [
        "Needs a camera reference built from images of the same camera at the same resolution, and the whole image must match it (PCE above 60).",
        "Areas smaller than about one window (128 px) are missed; dark, saturated and flat areas cannot be decided.",
        "Simulated sensors follow the classical PRNU model; phones' multi-frame processing can weaken it locally.",
        "Resizing, rotation by other than 90 degrees, or strong compression after editing defeat it.",
    ],
    "tamper.fusion": [
        "Only as good as its inputs: when no input's condition holds (single-saved file, no dimples) only the weak noise source remains.",
        "Reliabilities are fixed, not learned; they are chosen from the inputs' own cards.",
        "Inherits every input's false alarms on texture, flat areas and periodic content.",
    ],
    "file.qt_database": [
        "The built-in database covers a few hundred camera models, far fewer than commercial databases; labs should add their own reference images.",
        "Cameras have one table set per quality setting; a missing setting causes a false mismatch (see false_positive_rate).",
        "Many devices and programs share tables (libjpeg at a given quality is used by countless apps); a match narrows, never proves, the source.",
        "Anyone can re-save with a camera's tables and edit EXIF.",
    ],
}
