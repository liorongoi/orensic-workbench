"""Phase 3 validation experiments: camera-pipeline traces, face consistency and video authentication.

Same rules as the phase 1-2 experiments: fixed seeds, generated ground truth, and
figures that show each method behaves as described and where it stops working.
They are not real-world accuracy; the cards say so.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from ..analysis import faces as fc
from ..analysis import synthetic as syn
from ..analysis import video as vid
from ..config import load_settings
from .run import auc, decode, jpeg, rate


def bayer_capture(u8: np.ndarray) -> np.ndarray:
    """Re-shoot a scene through an RGGB colour filter array and OpenCV's bilinear demosaicing."""
    h, w = u8.shape[:2] // np.array([2, 2]) * 2
    s = u8[:h, :w]
    raw = np.zeros((h, w), np.uint8)
    raw[0::2, 0::2] = s[0::2, 0::2, 0]
    raw[0::2, 1::2] = s[0::2, 1::2, 1]
    raw[1::2, 0::2] = s[1::2, 0::2, 1]
    raw[1::2, 1::2] = s[1::2, 1::2, 2]
    return cv2.cvtColor(cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR), cv2.COLOR_BGR2RGB)


def _f(u8: np.ndarray) -> np.ndarray:
    return u8.astype(np.float32) / 255.0


def eval_cfa(src, n: int) -> dict:
    """CFA traces: demosaiced captures at several JPEG qualities against images that never had a CFA."""
    conds = {"lossless": None, "jpeg 98": 98, "jpeg 95": 95, "jpeg 90": 90}
    by: dict[str, list[bool]] = {k: [] for k in conds}
    neg_rendered, neg_resized, pos_s, neg_s = [], [], [], []
    for i in range(n):
        sc = src(6000 + i)
        cap = bayer_capture(sc)
        for k, q in conds.items():
            img = cap if q is None else decode(jpeg(cap, q))
            st = syn.cfa_statistic(_f(img))
            by[k].append(st["present"])
            if q is None:
                pos_s.append(abs(st["median_log_ratio"]))
        st = syn.cfa_statistic(_f(sc))
        neg_rendered.append(st["present"])
        neg_s.append(abs(st["median_log_ratio"]))
        small = cv2.resize(cap, None, fx=0.75, fy=0.75, interpolation=cv2.INTER_AREA)
        neg_resized.append(syn.cfa_statistic(_f(small))["present"])
    return {
        "task": "Does the image carry colour-filter-array demosaicing traces at native resolution?",
        "positives": f"{n} scenes re-shot through an RGGB Bayer mosaic and demosaiced (bilinear), stored lossless",
        "negatives": f"the same {n} scenes never mosaiced (as a renderer or generator outputs them)",
        "threshold": syn.CFA_MIN_LOG_RATIO,
        "true_positive_rate": rate(by["lossless"]),
        "false_positive_rate": rate(neg_rendered),
        "auc": round(auc(pos_s, neg_s), 4),
        "tpr_by_condition": {k: rate(v) for k, v in by.items()},
        "present_after_downscaling_to_75pct": rate(neg_resized),
    }


def eval_resampling(src, n: int) -> dict:
    factors = (1.1, 1.25, 1.5, 2.0, 0.8)
    by: dict[str, list[bool]] = {f"x{f}": [] for f in factors}
    by_jpeg: list[bool] = []
    neg, neg_jpeg, pos_s, neg_s = [], [], [], []
    for i in range(n):
        sc = src(7000 + i)
        r = syn.resampling_spectrum(_f(sc), exclude_jpeg_grid=False)
        neg.append(r["detected"])
        neg_s.append(r["peak_ratio"])
        neg_jpeg.append(syn.resampling_spectrum(_f(decode(jpeg(sc, 90))), exclude_jpeg_grid=True)["detected"])
        for f in factors:
            up = cv2.resize(sc, None, fx=f, fy=f, interpolation=cv2.INTER_LINEAR)
            r = syn.resampling_spectrum(_f(up), exclude_jpeg_grid=False)
            by[f"x{f}"].append(r["detected"])
            if f == 1.5:
                pos_s.append(r["peak_ratio"])
                by_jpeg.append(syn.resampling_spectrum(_f(decode(jpeg(up, 95))), exclude_jpeg_grid=True)["detected"])
    return {
        "task": "Was the image scaled after capture (periodic interpolation correlations)?",
        "positives": f"{n} scenes scaled by 1.5 (bilinear), lossless; other factors in tpr_by_condition",
        "negatives": f"the same {n} scenes unscaled; also each saved once as JPEG 90 (grid peaks excluded)",
        "threshold": syn.RESAMPLING_PEAK,
        "true_positive_rate": rate(by["x1.5"]),
        "false_positive_rate": rate(neg),
        "auc": round(auc(pos_s, neg_s), 4),
        "tpr_by_condition": {k: rate(v) for k, v in by.items()} | {"x1.5 then jpeg 95": rate(by_jpeg)},
        "false_positive_rate_jpeg_90": rate(neg_jpeg),
    }


def eval_faces(src, n: int) -> dict:
    """A 'face' region replaced by content with different noise or detail, against untouched regions."""
    conds = ("smoothed (sigma 1.5)", "smoothed, noise re-added", "sharper (unsharp mask)", "jpeg 60 patch in jpeg 90")
    by: dict[str, list[bool]] = {c: [] for c in conds}
    pos_z, neg_z, neg_flag = [], [], []
    for i in range(n):
        rng = np.random.default_rng(8000 + i)
        base = src(8100 + i).astype(np.float64)
        base += rng.normal(0, 4, base.shape)  # a sensor-like noise floor
        base = np.clip(base, 0, 255).astype(np.uint8)
        h, w = base.shape[:2]
        fw, fh = int(rng.integers(70, 110)), int(rng.integers(90, 130))
        x0, y0 = int(rng.integers(w // 4, w - w // 4 - fw)), int(rng.integers(h // 4, h - h // 4 - fh))
        box = {"box": [x0, y0, fw, fh], "landmarks": None, "score": None}
        sl = (slice(y0, y0 + fh), slice(x0, x0 + fw))
        # Negative: the same place, untouched.
        r = fc.check_faces(_f(base), [box], seed=i)[0]
        z = max(abs(r["noise_z"]), abs(r["detail_z"]))
        neg_z.append(z)
        neg_flag.append(z >= fc.Z_FLAG)
        for c in conds:
            img = base.copy()
            if c.startswith("smoothed"):
                sm = cv2.GaussianBlur(base, (0, 0), 1.5).astype(np.float64)
                if "noise" in c:
                    sm += rng.normal(0, 4, sm.shape)
                img[sl] = np.clip(sm, 0, 255).astype(np.uint8)[sl]
            elif c.startswith("sharper"):
                bl = cv2.GaussianBlur(base, (0, 0), 1.0).astype(np.float64)
                img[sl] = np.clip(base + 1.0 * (base - bl), 0, 255).astype(np.uint8)[sl]
            else:
                low = decode(jpeg(base, 60))
                img[sl] = low[sl]
                img = decode(jpeg(img, 90))
            r = fc.check_faces(_f(img), [box], seed=i)[0]
            z = max(abs(r["noise_z"]), abs(r["detail_z"]))
            by[c].append(z >= fc.Z_FLAG)
            if c == conds[0]:
                pos_z.append(z)
    return {
        "task": "Does a marked face region differ in noise or detail from the rest of the image?",
        "positives": f"{n} scenes with sensor-like noise; a 70-110 x 90-130 px region replaced by a smoothed copy",
        "negatives": f"the same region left untouched in the same {n} scenes",
        "threshold": fc.Z_FLAG,
        "true_positive_rate": rate(by[conds[0]]),
        "false_positive_rate": rate(neg_flag),
        "auc": round(auc(pos_z, neg_z), 4),
        "tpr_by_condition": {c: rate(v) for c, v in by.items()},
    }


# Video ----------------------------------------------------------------------------------------------

SOURCES = ("mandelbrot=size=320x240:rate=25,noise=alls=6:allf=t",
           "testsrc2=size=320x240:rate=25,noise=alls=10:allf=t",
           "life=size=320x240:rate=25:mold=10:ratio=0.5:seed={seed},trim=start_frame=25,setpts=PTS-STARTPTS,noise=alls=6:allf=t",
           "cellauto=size=320x240:rate=25:rule=110:seed={seed},noise=alls=8:allf=t")


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *map(str, args)], check=True)


def _source(i: int) -> str:
    return SOURCES[i % len(SOURCES)].format(seed=1000 + i)


def eval_double_encoding(n: int, settings) -> dict:
    gops = (10, 15, 20, 30)
    by: dict[str, list[bool]] = {}
    gop_ok, pos, negs = [], [], []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for i in range(n):
            src = _source(i)
            g1 = gops[i % len(gops)]
            second = [("gop 250, no B", ["-g", 250, "-bf", 0]), ("gop 100, 2 B", ["-g", 100, "-bf", 2])][i % 2]
            _ff("-f", "lavfi", "-i", src, "-t", 10, "-c:v", "libx264", "-g", g1, "-bf", 0, "-crf", 26, "-pix_fmt", "yuv420p", d / "a.mp4")
            _ff("-i", d / "a.mp4", "-c:v", "libx264", *second[1], "-crf", 22, d / "b.mp4")
            _ff("-f", "lavfi", "-i", src, "-t", 10, "-c:v", "libx264", *second[1], "-crf", 22, "-pix_fmt", "yuv420p", d / "s.mp4")
            res = vid.vpf(vid.macroblock_stats(d / "b.mp4", settings)[-250:])
            by.setdefault(f"first gop {g1}", []).append(res["detected"])
            by.setdefault(f"second {second[0]}", []).append(res["detected"])
            pos.append(res["detected"])
            if res["detected"]:
                gop_ok.append(abs(res["estimated_first_gop"] - g1) <= 1)
            negs.append(vid.vpf(vid.macroblock_stats(d / "s.mp4", settings)[-250:])["detected"])
    return {
        "task": "Was the H.264 video decoded and encoded again (VPF footprint of the first GOP)?",
        "positives": f"{n} 10 s clips from four synthetic sources: first encoded with GOP {list(gops)} at CRF 26, "
        "re-encoded at CRF 22 with GOP 250 (no B-frames) or GOP 100 (2 B-frames)",
        "negatives": f"{n} clips from the same sources encoded once with the second settings",
        "threshold": vid.VPF_Z,
        "true_positive_rate": rate(pos),
        "false_positive_rate": rate(negs),
        "first_gop_within_1_when_detected": rate(gop_ok),
        "tpr_by_condition": {k: rate(v) for k, v in sorted(by.items())},
    }


def eval_frame_integrity(n: int, settings) -> dict:
    deletions = (1, 5, 20)
    by: dict[str, list[bool]] = {f"{k} frame(s) deleted": [] for k in deletions}
    loc_ok, dup, neg_jump, neg_seg = [], [], [], []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for i in range(n):
            _ff("-f", "lavfi", "-i", _source(i), "-t", 8, "-c:v", "libx264", "-crf", 20, "-pix_fmt", "yuv420p", d / "o.mp4")
            th = vid.decode_thumbs(d / "o.mp4", settings, 320, 240, 1000)
            r = vid.frame_integrity_scan(th)
            neg_jump.append(bool(r["jumps"]))
            neg_seg.append(bool(r["repeated_segments"]))
            for k in deletions:
                cut = np.delete(th, np.s_[90 : 90 + k], axis=0)
                r = vid.frame_integrity_scan(cut)
                hit = any(j["between"] == [89, 90] for j in r["jumps"])
                by[f"{k} frame(s) deleted"].append(bool(r["jumps"]))
                loc_ok.append(hit)
            looped = np.concatenate([th[:120], th[40:60], th[120:]])
            r = vid.frame_integrity_scan(looped)
            dup.append(any(s["first"][0] == 40 and s["repeat"][0] == 120 for s in r["repeated_segments"]))
    return {
        "task": "Were frames removed, or a segment repeated?",
        "positives": f"{n} 8 s clips from four synthetic sources with 1, 5 or 20 frames removed at frame 90, and with "
        "frames 40-59 inserted again after frame 119",
        "negatives": f"the same {n} clips unedited",
        "threshold": vid.JUMP_Z,
        "true_positive_rate": rate(by["5 frame(s) deleted"]),
        "false_positive_rate": rate(neg_jump),
        "deletion_located_at_cut": rate(loc_ok),
        "tpr_by_condition": {k: rate(v) for k, v in by.items()},
        "repeated_segment_found": rate(dup),
        "repeated_segment_false_positive_rate": rate(neg_seg),
    }


def run_phase3(src, quick: bool, log=print) -> dict:
    settings = load_settings(Path(tempfile.gettempdir()) / "forensic-validation")
    n_img = 8 if quick else 80
    n_vid = 2 if quick else 16
    log(f"CFA traces ({n_img} scenes)…")
    cfa = eval_cfa(src, n_img)
    log(f"Resampling ({n_img} scenes)…")
    rs = eval_resampling(src, n_img)
    log(f"Face consistency ({n_img} scenes)…")
    faces = eval_faces(src, n_img)
    log(f"Video double encoding ({n_vid} clips)…")
    de = eval_double_encoding(n_vid, settings)
    log(f"Frame deletion and duplication ({n_vid} clips)…")
    fi = eval_frame_integrity(n_vid, settings)
    return {"cfa": cfa, "resampling": rs, "faces": faces, "double_encoding": de, "frame_integrity": fi}


FAILURE_MODES = {
    "synthetic.camera_traces": [
        "CFA traces do not survive JPEG at quality 95 or below, nor resizing (see tpr_by_condition): their absence says "
        "little about most delivered images.",
        "Simulated with bilinear demosaicing; camera pipelines use adaptive demosaicing, denoising and sharpening that "
        "weaken the pattern, and a generator could imitate it. Measure on camera originals before relying on absence.",
        "Resampling detection fails for down-scaling and after JPEG compression at common qualities.",
    ],
    "face.consistency": [
        "Detects a region whose noise or detail differs from the rest; a swap whose noise was matched to the image "
        "(see 'smoothed, noise re-added') or a whole re-encoded deepfake video can pass.",
        "Make-up, focus on the face, motion blur and phone beautification also change local detail: a flag is a "
        "reason to look, not a conclusion.",
        "Synthetic scenes with rectangular regions; no real face swaps were available for this card.",
    ],
    "video.double_encoding": [
        "Simulated with x264 on synthetic content; the second encoding at higher quality than the first is the "
        "easy case. Recorder firmware and platform transcoders behave differently.",
        "Fails when the second GOP is a multiple of the first or when B-frames dominate; a first GOP of 30 was "
        "missed in every 10 s test clip, and two B-frames in the second encoding halve detection.",
        "Re-encoding is not manipulation: it tells you the file is not the recorder's original stream.",
    ],
    "video.frame_integrity": [
        "A jump can be a cut, camera switch, light change or a dropped frame. Static scenes, and busy scenes where "
        "every frame already changes a lot (the cellular-automaton source here), hide deletions.",
        "Single-frame deletions in slow scenes are often missed (see tpr_by_condition).",
        "Synthetic sources only; CCTV with repeated frames needs the duplicates reading first.",
    ],
}


def phase3_cards(results: dict) -> dict[str, dict]:
    return {
        "synthetic.camera_traces": {"detection": results["cfa"], "resampling": results["resampling"]},
        "face.consistency": {"detection": results["faces"]},
        "video.double_encoding": {"detection": results["double_encoding"]},
        "video.frame_integrity": {"detection": results["frame_integrity"]},
    }
