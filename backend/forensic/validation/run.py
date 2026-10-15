"""Generate the validation corpus, run the detectors and write detector cards.

The synthetic corpus is generated from fixed seeds, so the same code produces the
same numbers. Synthetic scenes are not real photographs: these figures show that
each detector does what its method says under controlled conditions and catch
regressions. Real-world accuracy must be measured on real images: pass a folder of
camera-original JPEGs with --pristine and the same manipulations are derived from
them, and public sets (Columbia, CASIA, DSO-1, Dresden, VISION) can be run the same way.
"""

from __future__ import annotations

import io
import json
import platform
from datetime import date
from pathlib import Path
from typing import Callable, Iterator

import cv2
import numpy as np
from PIL import Image

from .. import __version__, prnu
from ..analysis import double_jpeg as dj
from ..analysis.base import ANALYSES
from .cards import CARD_DIR


# Corpus -------------------------------------------------------------------------------------


def scene(seed: int, h: int = 384, w: int = 512) -> np.ndarray:
    """A photo-like synthetic scene: sky gradient, textured ground, sharp-edged objects, sensor noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w] / max(h, w)
    sky = np.stack([0.55 + 0.2 * yy, 0.65 + 0.15 * yy, 0.85 - 0.1 * xx], axis=2)
    tex = np.zeros((h, w, 3))
    for s, a in ((2, 0.10), (6, 0.20), (24, 0.30)):
        n = rng.normal(0, 1, (h // s + 2, w // s + 2, 3))
        tex += a * cv2.resize(n, (w, h), interpolation=cv2.INTER_CUBIC)
    ground = np.clip(0.4 + 0.15 * tex + rng.uniform(-0.1, 0.1, 3), 0, 1)
    horizon = int(h * rng.uniform(0.25, 0.5))
    img = sky.copy()
    img[horizon:] = ground[horizon:]
    u8 = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    for _ in range(rng.integers(6, 14)):
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(horizon // 2, h))
        if rng.random() < 0.5:
            cv2.rectangle(u8, (x0, y0), (x0 + int(rng.integers(10, 120)), y0 + int(rng.integers(10, 120))), color, -1)
        else:
            cv2.circle(u8, (x0, y0), int(rng.integers(5, 60)), color, -1, lineType=cv2.LINE_AA)
    if rng.random() < 0.5:
        cv2.putText(u8, "".join(rng.choice(list("ABCDEFGH0123456789"), 6)), (int(rng.integers(0, w // 2)), int(rng.integers(h // 2, h - 10))),
                    cv2.FONT_HERSHEY_SIMPLEX, float(rng.uniform(0.6, 1.5)), (20, 20, 20), 2, cv2.LINE_AA)
    out = u8.astype(np.float64) / 255 + rng.normal(0, 0.006, u8.shape)
    return (np.clip(out, 0, 1) * 255).round().astype(np.uint8)


def jpeg(u8: np.ndarray, q: int) -> bytes:
    b = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(u8)).save(b, "JPEG", quality=int(q))
    return b.getvalue()


def decode(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def pristine_source(folder: Path | None) -> Callable[[int], np.ndarray]:
    """Images to build test cases from: real camera JPEGs if given (decoded), else synthetic scenes."""
    if folder is None:
        return scene
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff"))
    if not files:
        raise SystemExit(f"no images found in {folder}")

    def load(i: int) -> np.ndarray:
        arr = np.asarray(Image.open(files[i % len(files)]).convert("RGB"))
        h, w = arr.shape[:2]
        s = min(1.0, 1024 / max(h, w))
        if s < 1:
            arr = cv2.resize(arr, (int(w * s) // 8 * 8, int(h * s) // 8 * 8), interpolation=cv2.INTER_AREA)
        return arr

    return load


# Metrics --------------------------------------------------------------------------------------


def auc(pos: list[float], neg: list[float]) -> float:
    p, n = np.asarray(pos, float), np.asarray(neg, float)
    if not len(p) or not len(n):
        return float("nan")
    allv = np.concatenate([p, n])
    ranks = allv.argsort().argsort() + 1.0
    # average ranks for ties
    for v in np.unique(allv):
        idx = allv == v
        ranks[idx] = ranks[idx].mean()
    return float((ranks[: len(p)].sum() - len(p) * (len(p) + 1) / 2) / (len(p) * len(n)))


def rate(flags: list[bool]) -> float:
    return round(float(np.mean(flags)), 4) if flags else float("nan")


def threshold_at_fpr(neg: list[float], fpr: float = 0.01) -> float:
    return float(np.quantile(np.asarray(neg, float), 1 - fpr)) if neg else float("nan")


# Experiments ---------------------------------------------------------------------------------------


def eval_adjpg(src, n: int) -> dict:
    pos_scores, neg_scores, q_errors = [], [], []
    by_cond: dict[str, list[bool]] = {}
    hi_pos = []  # Q1 > Q2: expected to be hard
    for i in range(n):
        x = src(1000 + i)
        rng = np.random.default_rng(i)
        q2 = int(rng.choice([75, 85, 90, 95]))
        neg = dj.adjpg(jpeg(x, q2))
        neg_scores.append(neg["score"] if neg["supporting_frequencies"] >= 2 else 0.0)
        q1 = int(rng.integers(50, q2 - 4))
        pos = dj.adjpg(jpeg(decode(jpeg(x, q1)), q2))
        s = pos["score"] if pos["supporting_frequencies"] >= 2 else 0.0
        pos_scores.append(s)
        det = s >= dj.ADJPG_THRESHOLD
        by_cond.setdefault(f"Q2={q2}", []).append(det)
        if pos["estimated_first_quality"] is not None:
            q_errors.append(abs(pos["estimated_first_quality"] - q1))
        q1h = int(rng.integers(q2 + 3, 99)) if q2 < 95 else 98
        hp = dj.adjpg(jpeg(decode(jpeg(x, q1h)), q2))
        hi_pos.append((hp["score"] if hp["supporting_frequencies"] >= 2 else 0.0) >= dj.ADJPG_THRESHOLD)
    t = dj.ADJPG_THRESHOLD
    return {
        "task": "Is the JPEG aligned double compressed (decoded and re-saved on the same grid)?",
        "positives": f"{n} images saved at a random first quality Q1 in [50, Q2-5], then at Q2",
        "negatives": f"{n} images saved once at Q2 in {{75, 85, 90, 95}}",
        "threshold": t,
        "true_positive_rate": rate([s >= t for s in pos_scores]),
        "false_positive_rate": rate([s >= t for s in neg_scores]),
        "auc": round(auc(pos_scores, neg_scores), 4),
        "threshold_for_1pct_fpr": round(threshold_at_fpr(neg_scores), 5),
        "tpr_by_condition": {k: rate(v) for k, v in sorted(by_cond.items())},
        "first_quality_within_3": rate([e <= 3 for e in q_errors]),
        "first_quality_mean_abs_error": round(float(np.mean(q_errors)), 2) if q_errors else None,
        "tpr_when_q1_above_q2": rate(hi_pos),
    }


def eval_adjpg_local(src, n: int) -> dict:
    aucs, f1s = [], []
    for i in range(n):
        rng = np.random.default_rng(5000 + i)
        bg = decode(jpeg(src(2000 + i), int(rng.integers(50, 80)))).copy()
        patch = src(3000 + i)
        h, w = bg.shape[:2]
        ph, pw = int(rng.integers(h // 5, h // 2)) // 8 * 8, int(rng.integers(w // 5, w // 2)) // 8 * 8
        y0, x0 = int(rng.integers(0, (h - ph) // 8)) * 8, int(rng.integers(0, (w - pw) // 8)) * 8
        bg[y0 : y0 + ph, x0 : x0 + pw] = patch[y0 : y0 + ph, x0 : x0 + pw]
        res = dj.adjpg(jpeg(bg, int(rng.choice([85, 90, 95]))))
        llr = cv2.blur(res["block_llr"], (3, 3), borderType=cv2.BORDER_REFLECT)
        truth = np.zeros_like(llr, bool)
        truth[y0 // 8 : (y0 + ph) // 8, x0 // 8 : (x0 + pw) // 8] = True
        p_single = 1 / (1 + np.exp(np.clip(llr, -30, 30)))
        aucs.append(auc(p_single[truth].ravel().tolist(), p_single[~truth].ravel().tolist()))
        pred = p_single > 0.8
        tp, fp, fn = (pred & truth).sum(), (pred & ~truth).sum(), (~pred & truth).sum()
        f1s.append(2 * tp / max(1, 2 * tp + fp + fn))
    return {
        "task": "Which 8x8 blocks were pasted in (single compressed) into a double-compressed image?",
        "cases": f"{n} splices: background saved at Q1 in [50, 80) then the composite at Q2 in {{85, 90, 95}}; "
        "pasted rectangle 20-50 % of each side, grid-aligned",
        "per_block_auc_mean": round(float(np.nanmean(aucs)), 4),
        "f1_at_probability_0.8_mean": round(float(np.mean(f1s)), 4),
    }


def eval_nadjpg(src, n: int) -> dict:
    pos, neg, offset_ok = [], [], []
    by_q1: dict[int, list[float]] = {}
    for i in range(n):
        rng = np.random.default_rng(7000 + i)
        x = src(4000 + i)
        q1 = int(rng.choice([60, 75, 90]))
        dx, dy = 0, 0
        while (dx, dy) == (0, 0):
            dx, dy = int(rng.integers(0, 8)), int(rng.integers(0, 8))
        cropped = decode(jpeg(x, q1))[dy:, dx:]
        y = dj.read_luma(jpeg(cropped, 90))
        off, _, z = dj.grid_residual(dj.shift_energy(y).mean(axis=(2, 3)))
        pos.append(z)
        by_q1.setdefault(q1, []).append(z)
        offset_ok.append(tuple(off) == ((8 - dx) % 8, (8 - dy) % 8))
        # negatives: single compression and aligned double compression
        for data in (jpeg(x, int(rng.choice([75, 90, 95]))), jpeg(decode(jpeg(x, q1)), 90)):
            neg.append(dj.grid_residual(dj.shift_energy(dj.read_luma(data)).mean(axis=(2, 3)))[2])
    t = 1.0
    return {
        "task": "Was the JPEG compressed before on a different 8x8 grid (cropped or shifted, then re-saved)?",
        "positives": f"{n} images saved at Q1 in {{60, 75, 90}}, cropped by a random non-zero (dx, dy), saved at Q2 = 90",
        "negatives": f"{2 * n} images: saved once at {{75, 90, 95}}, or aligned double compressed (Q1, then 90)",
        "threshold": t,
        "threshold_note": f"score = dip z-score / {dj.NA_THRESHOLD_OFF_AXIS} for offsets off the final grid's axes, "
        f"/ {dj.NA_THRESHOLD_ON_AXIS} on them",
        "true_positive_rate": rate([z >= t for z in pos]),
        "false_positive_rate": rate([z >= t for z in neg]),
        "auc": round(auc(pos, neg), 4),
        "threshold_for_1pct_fpr": round(threshold_at_fpr(neg), 3),
        "tpr_by_first_quality": {f"Q1={k}": rate([z >= t for z in v]) for k, v in sorted(by_q1.items())},
        "offset_correct_when_detected": rate([ok for ok, z in zip(offset_ok, pos) if z >= t]),
    }


def eval_prnu(n_cams: int, n_test: int, size=(384, 512), k_std: float = 0.005) -> dict:
    h, w = size
    cams = [np.random.default_rng(90000 + c).normal(0, k_std, (h, w)) for c in range(n_cams)]

    def shot(c, seed, flat=False, q=None):
        rng = np.random.default_rng(seed)
        if flat:
            base = 0.7 + 0.05 * cv2.GaussianBlur(rng.normal(0, 1, (h, w, 3)), (0, 0), 20)
        else:
            base = scene(seed, h, w).astype(np.float64) / 255
        img = np.clip(base * (1 + cams[c][..., None]) + rng.normal(0, 0.006, base.shape), 0, 1)
        q = q or int(rng.integers(75, 96))
        return decode(jpeg((img * 255).round().astype(np.uint8), q)).astype(np.float32) / 255

    refs = [prnu.estimate_fingerprint([shot(c, 100000 + 100 * c + i, flat=True, q=95) for i in range(15)])[0] for c in range(n_cams)]
    same, diff, same_crop, diff_crop = [], [], [], []
    for c in range(n_cams):
        for j in range(n_test):
            img = shot(c, 200000 + 100 * c + j)
            other = (c + 1) % n_cams
            same.append(prnu.match(img, refs[c], rotations=False, crop_search=False)["best"]["pce"])
            diff.append(prnu.match(img, refs[other], rotations=False, crop_search=False)["best"]["pce"])
            rng = np.random.default_rng(300000 + 100 * c + j)
            y0, x0 = int(rng.integers(0, h // 3)), int(rng.integers(0, w // 3))
            crop = img[y0 : y0 + h // 2, x0 : x0 + w // 2]
            same_crop.append(prnu.match(crop, refs[c])["best"]["pce"])
            diff_crop.append(prnu.match(crop, refs[other])["best"]["pce"])
    t = prnu.PCE_THRESHOLD
    return {
        "task": "Was the image taken by the camera of the reference?",
        "cameras": f"{n_cams} simulated sensors, PRNU std {k_std} (typical of real sensors), {h}x{w}",
        "references": "15 flat-field images per camera, JPEG quality 95",
        "tests": f"{n_test} scenes per camera, JPEG quality 75-95; each matched to its own and to another camera's reference",
        "threshold": t,
        "full_frame": {"true_positive_rate": rate([p >= t for p in same]), "false_positive_rate": rate([p >= t for p in diff]),
                       "auc": round(auc(same, diff), 4), "median_pce_same": round(float(np.median(same)), 1)},
        "cropped_quarter_frame_with_search": {"true_positive_rate": rate([p >= t for p in same_crop]),
                                              "false_positive_rate": rate([p >= t for p in diff_crop]),
                                              "auc": round(auc(same_crop, diff_crop), 4),
                                              "median_pce_same": round(float(np.median(same_crop)), 1)},
    }


# Cards -------------------------------------------------------------------------------------------------

FAILURE_MODES = {
    "tamper.double_jpeg": [
        "Not detectable when the first quality was higher than the second (see tpr_when_q1_above_q2).",
        "Not detectable after resizing, rotation or off-grid cropping between saves (use the non-aligned grid analysis).",
        "Flat or saturated regions carry no information and can look single-compressed in authentic images.",
        "Synthetic corpus: real-camera figures must be measured with --pristine or public datasets.",
    ],
    "tamper.nonaligned_grid": [
        "Weak when the first save was at high quality (see tpr_by_first_quality).",
        "Periodic scene content (brick, fences, text) can mimic a secondary grid.",
        "Half-block offsets (4 px) are the most prone to false alarms.",
        "Synthetic corpus: real-camera figures must be measured with --pristine or public datasets.",
    ],
    "camera.prnu": [
        "Simulated sensors follow the classical multiplicative PRNU model; modern phones' multi-frame and AI "
        "processing can suppress or imitate it. Validate per phone model before relying on a match.",
        "Strong compression, resizing and denoising weaken the fingerprint; searching crops and scales raises the chance of an accidental match.",
        "A reference built from few or textured images is noisy; 20 or more flat, bright images is recommended.",
    ],
}


def write_cards(results: dict, out_dir: Path, meta: dict) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    from .phase3 import FAILURE_MODES as P3_MODES
    from .phase3 import phase3_cards

    detectors = {}
    if "adjpg" in results:
        detectors = {
            "tamper.double_jpeg": {"detection": results["adjpg"], "localisation": results["adjpg_local"]},
            "tamper.nonaligned_grid": {"detection": results["nadjpg"]},
            "camera.prnu": {"identification": results["prnu"]},
        }
    from .phase4 import FAILURE_MODES as P4_MODES
    from .phase4 import phase4_cards

    if "phase3" in results:
        detectors |= phase3_cards(results["phase3"])
    from .phase5 import FAILURE_MODES as P5_MODES
    from .phase5 import phase5_cards

    if "phase4" in results:
        detectors |= phase4_cards(results["phase4"])
    if "phase5" in results:
        detectors |= phase5_cards(results["phase5"])
    modes = FAILURE_MODES | P3_MODES | P4_MODES | P5_MODES
    for det, res in detectors.items():
        a = ANALYSES[det]
        card = {
            "detector": det,
            "name": a.name,
            "version": a.version,
            "validated_on": meta["date"],
            "corpus": meta["corpus"],
            "seeded": True,
            "results": res,
            "known_failure_modes": modes[det],
            "software": meta["software"],
            "how_to_reproduce": meta["command"],
        }
        p = out_dir / f"{det}.json"
        p.write_text(json.dumps(card, indent=2) + "\n")
        written.append(p)
    return written


def run(quick: bool = False, pristine: Path | None = None, out_dir: Path = CARD_DIR, log=print,
        only: str | None = None, camera_originals: Path | None = None) -> dict:
    """Run the experiments and write their cards; `only` runs one group ("phase4": clone and noise map;
    "phase5": dimples, PRNU map, fusion and quantisation tables)."""
    src = pristine_source(pristine)
    from .phase4 import run_phase4
    from .phase5 import run_phase5

    if only == "phase4":
        return _finish({"phase4": run_phase4(src, quick, log=log)}, quick, pristine, out_dir)
    if only == "phase5":
        return _finish({"phase5": run_phase5(src, quick, camera_originals, log=log)}, quick, pristine, out_dir,
                       camera_originals)
    n = 12 if quick else 120
    log(f"A-DJPG detection ({n} positives, {n} negatives)…")
    adjpg = eval_adjpg(src, n)
    log(f"A-DJPG localisation ({n // 2 if not quick else 6} splices)…")
    adjpg_local = eval_adjpg_local(src, 6 if quick else 60)
    log(f"NA-DJPG grid ({n} positives)…")
    nadjpg = eval_nadjpg(src, n)
    log("PRNU camera identification…")
    pr = eval_prnu(2 if quick else 4, 4 if quick else 20, size=(192, 256) if quick else (384, 512))
    from .phase3 import run_phase3

    results = {"adjpg": adjpg, "adjpg_local": adjpg_local, "nadjpg": nadjpg, "prnu": pr,
               "phase3": run_phase3(src, quick, log=log), "phase4": run_phase4(src, quick, log=log),
               "phase5": run_phase5(src, quick, camera_originals, log=log)}
    return _finish(results, quick, pristine, out_dir, camera_originals)


def _finish(results: dict, quick: bool, pristine: Path | None, out_dir: Path | None,
            camera_originals: Path | None = None) -> dict:
    import PIL
    import pywt

    meta = {
        "date": date.today().isoformat(),
        "corpus": ("real images from --pristine folder (manipulations derived from them)" if pristine else
                   "synthetic photo-like scenes generated from fixed seeds (forensic.validation.run.scene)")
        + ("; quick (CI) size" if quick else ""),
        "software": {"forensic_workbench": __version__, "numpy": np.__version__, "opencv": cv2.__version__,
                     "pillow": PIL.__version__, "pywavelets": pywt.__version__, "python": platform.python_version()},
        "command": "python -m forensic.validation run" + (" --quick" if quick else "") + (" --pristine <folder>" if pristine else "")
        + (" --camera-originals <folder>" if camera_originals else ""),
    }
    if out_dir is not None:
        write_cards(results, out_dir, meta)
    return {"meta": meta, "results": results}


def summary_markdown(out: dict) -> str:
    r = out["results"]
    m = out["meta"]
    if "adjpg" not in r:
        return "\n".join([f"# Detector validation (partial)", "", f"Generated {m['date']} by `{m['command']}` on {m['corpus']}.", ""]
                          + _phase4_rows(r.get("phase4")) + _phase5_rows(r.get("phase5")) + [""])
    lines = [
        "# Detector validation",
        "",
        f"Generated {m['date']} by `{m['command']}` on {m['corpus']}.",
        "",
        "These numbers come from a seeded synthetic corpus. They show each detector behaves as its method predicts "
        "and guard against regressions; they are not real-world accuracy. Run with `--pristine <folder of camera "
        "JPEGs>` (or on public datasets) to measure on real images before relying on a detector in casework.",
        "",
        "| Detector | Task | Threshold | TPR | FPR | AUC |",
        "|---|---|---|---|---|---|",
        f"| A-DJPG | double compression (Q1 < Q2) | {r['adjpg']['threshold']} | {r['adjpg']['true_positive_rate']} | {r['adjpg']['false_positive_rate']} | {r['adjpg']['auc']} |",
        f"| NA-DJPG grid | crop between saves | {r['nadjpg']['threshold']} | {r['nadjpg']['true_positive_rate']} | {r['nadjpg']['false_positive_rate']} | {r['nadjpg']['auc']} |",
        f"| PRNU | same camera, full frame | PCE {r['prnu']['threshold']} | {r['prnu']['full_frame']['true_positive_rate']} | {r['prnu']['full_frame']['false_positive_rate']} | {r['prnu']['full_frame']['auc']} |",
        f"| PRNU | same camera, quarter crop, searched | PCE {r['prnu']['threshold']} | {r['prnu']['cropped_quarter_frame_with_search']['true_positive_rate']} | {r['prnu']['cropped_quarter_frame_with_search']['false_positive_rate']} | {r['prnu']['cropped_quarter_frame_with_search']['auc']} |",
    ] + _phase3_rows(r.get("phase3")) + _phase4_rows(r.get("phase4")) + _phase5_rows(r.get("phase5")) + [
        "",
        f"A-DJPG first-quality estimate within ±3: {r['adjpg']['first_quality_within_3']} (mean error {r['adjpg']['first_quality_mean_abs_error']}). "
        f"Detection when Q1 > Q2 (expected to fail): {r['adjpg']['tpr_when_q1_above_q2']}.",
        "",
        f"A-DJPG splice localisation: mean per-block AUC {r['adjpg_local']['per_block_auc_mean']}, mean F1 {r['adjpg_local']['f1_at_probability_0.8_mean']}.",
        "",
        f"NA-DJPG by first quality: {r['nadjpg']['tpr_by_first_quality']}; offset correct when detected: {r['nadjpg']['offset_correct_when_detected']}.",
        "",
        "Full results, conditions and known failure modes are in the detector cards (`backend/forensic/validation/cards/`), "
        "which the app shows next to each detector and prints in reports.",
        "",
    ]
    return "\n".join(lines)


def _phase3_rows(p3: dict | None) -> list[str]:
    if not p3:
        return []
    rows = [("CFA traces", "demosaicing present (lossless)", p3["cfa"]),
            ("Resampling", "scaled x1.5 after capture", p3["resampling"]),
            ("Face consistency", "region with different noise/detail", p3["faces"]),
            ("VPF", "H.264 double encoding", p3["double_encoding"]),
            ("Frame integrity", "5 frames deleted", p3["frame_integrity"])]
    out = [f"| {n} | {t} | {r['threshold']} | {r['true_positive_rate']} | {r['false_positive_rate']} | {r.get('auc', '–')} |"
           for n, t, r in rows]
    out += [
        "",
        "Phase 3 checks:",
        "",
        f"CFA traces by storage: {p3['cfa']['tpr_by_condition']}; after downscaling to 75 %: {p3['cfa']['present_after_downscaling_to_75pct']}.",
        "",
        f"Resampling by factor: {p3['resampling']['tpr_by_condition']}.",
        "",
        f"Face consistency by manipulation: {p3['faces']['tpr_by_condition']}.",
        "",
        f"Double encoding by condition: {p3['double_encoding']['tpr_by_condition']}; first GOP within ±1 when detected: "
        f"{p3['double_encoding']['first_gop_within_1_when_detected']}.",
        "",
        f"Frame deletion by size: {p3['frame_integrity']['tpr_by_condition']}; repeated segment found: "
        f"{p3['frame_integrity']['repeated_segment_found']} (false positives {p3['frame_integrity']['repeated_segment_false_positive_rate']}).",
    ]
    return out


def _phase4_rows(p4: dict | None) -> list[str]:
    if not p4:
        return []
    cm, nm = p4["copy_move"], p4["noise_map"]
    return [
        "",
        "Clone detection and noise map (TPR by manipulation; FPR on the untouched images):",
        "",
        f"Copy-move: {cm['true_positive_rate_by_condition']}; FPR {cm['false_positive_rate']} "
        f"({cm['false_positive_rate_after_jpeg85']} after JPEG 85).",
        "",
        f"Noise map: {nm['true_positive_rate_by_condition']}; FPR {nm['false_positive_rate']} "
        f"({nm['false_positive_rate_after_jpeg90']} after JPEG 90).",
    ]


def _phase5_rows(p5: dict | None) -> list[str]:
    if not p5:
        return []
    d, pm, fu, qt = p5["dimples"], p5["prnu_map"], p5["fusion"], p5["qtables"]
    out = [
        "",
        "JPEG dimples, PRNU tampering map, fusion map and quantisation tables:",
        "",
        f"Dimples: TPR by bias {d['true_positive_rate_by_bias']}; FPR {d['false_positive_rate']} "
        f"({d['false_positive_rate_double_saved']} double saved); missing-dimple area found {d['localisation']['true_positive_rate']} "
        f"(FPR {d['localisation']['false_positive_rate']}).",
        "",
        f"PRNU map: {pm['true_positive_rate_by_condition']}; FPR {pm['false_positive_rate']}.",
        "",
        f"Fusion: {fu['true_positive_rate_by_condition']}; FPR {fu['false_positive_rate_by_history']}.",
        "",
        f"Quantisation tables: re-save caught {qt['true_positive_rate']}; false mismatch on held-out originals {qt['false_positive_rate']}.",
    ]
    if "real_camera_originals" in d:
        rc = d["real_camera_originals"]
        out += ["", f"Dimples on {rc['images']} real camera originals: {rc['with_dimple']} have one; "
                f"{rc['with_dimple_after_libjpeg_resave_q90']} still do after re-saving at 90."]
    return out
