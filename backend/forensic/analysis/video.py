"""Video authentication: double encoding (VPF) and frame integrity (duplication, deletion, discontinuities).

Re-encoding a video after editing leaves traces. Frames that were intra-coded (I) in the
first encoding and became predicted (P) in the second are predicted unusually badly from
their neighbours, so the second encoder spends more intra macroblocks and fewer skipped
ones on them: the variation of prediction footprint. Removing, inserting or repeating
frames leaves jumps and repeats in the frame-to-frame difference sequence.
"""

from __future__ import annotations

import json
import re
import subprocess

import cv2
import numpy as np

from .. import media
from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register

INTRA = set("PAiI")  # FFmpeg debug mb_type symbols: PCM, AC-predicted intra, intra 4x4, intra 16x16
SKIP = set("Sdg")  # skipped, direct-skip, GMC-skip
VPF_Z = 4.0
VPF_MIN_FRAMES = 3
VPF_CODECS = ("h264", "mpeg2video", "mpeg4", "mpeg1video", "h263")


def macroblock_stats(path, settings, max_frames: int = 3000) -> list[dict]:
    """Per decoded frame: picture type and counts of intra and skipped macroblocks (FFmpeg -debug mb_type)."""
    proc = subprocess.run(
        [settings.ffmpeg, "-nostdin", "-hide_banner", "-threads", "1", "-debug", "mb_type", "-i", str(path),
         "-map", "0:v:0", "-frames:v", str(max_frames), "-f", "null", "-"],
        capture_output=True, check=False,
    )
    frames: list[dict] = []
    cur = None
    for raw in proc.stderr.decode("latin-1").splitlines():
        m = re.search(r"New frame, type: (\w)", raw)
        if m:
            cur = {"type": m.group(1), "intra": 0, "skip": 0, "mbs": 0}
            frames.append(cur)
            continue
        if cur is None or not raw.startswith("[") or "] " not in raw:
            continue
        body = raw.split("] ", 1)[1]
        if not body.strip() or re.fullmatch(r"[ 0-9]+", body.strip()):
            continue
        for k in range(0, len(body) - 1, 3):
            t = body[k]
            if t == " ":
                continue
            cur["mbs"] += 1
            if t in INTRA:
                cur["intra"] += 1
            elif t in SKIP:
                cur["skip"] += 1
    if proc.returncode != 0 and not frames:
        raise media.MediaError(proc.stderr.decode(errors="replace")[-500:])
    return frames


def _robust_z(x: np.ndarray, k: int = 6) -> np.ndarray:
    """Deviation from the median of the k neighbours on each side, in robust standard deviations."""
    n = len(x)
    out = np.zeros(n)
    if n < 5:
        return out
    pad = np.pad(x, k, mode="edge")
    med = np.array([np.median(np.r_[pad[i : i + k], pad[i + k + 1 : i + 2 * k + 1]]) for i in range(n)])
    r = x - med
    mad = np.median(np.abs(r)) * 1.4826 + 0.01
    return r / mad


def vpf(frames: list[dict]) -> dict:
    """Find P-frames whose intra share jumps up and skip share drops: candidate first-encoding I-frames."""
    types = np.array([f["type"] for f in frames])
    mbs = np.array([max(f["mbs"], 1) for f in frames], np.float64)
    intra = np.array([f["intra"] for f in frames]) / mbs
    skip = np.array([f["skip"] for f in frames]) / mbs
    p_idx = np.where(types == "P")[0]
    if len(p_idx) < 12:
        return {"p_frames": int(len(p_idx)), "footprint_frames": [], "feature_frames": [], "feature": [], "detected": False,
                "estimated_first_gop": None, "note": "too few P-frames to look for a footprint"}
    feat = _robust_z(intra[p_idx]) + _robust_z(-skip[p_idx])
    after_i = set((np.where(types == "I")[0] + 1).tolist())
    cand = [int(i) for i, z in zip(p_idx, feat) if z >= VPF_Z and int(i) not in after_i]
    spacing = np.diff(cand) if len(cand) > 1 else np.array([], int)
    period, regular = None, 0.0
    if len(spacing):
        period = int(np.median(spacing))
        regular = float(np.mean(np.abs(spacing - period) <= max(1, 0.25 * period)))
    detected = len(cand) >= VPF_MIN_FRAMES and regular >= 0.6
    gop = np.diff(np.where(types == "I")[0])
    return {
        "p_frames": int(len(p_idx)),
        "footprint_frames": cand,
        "footprint_scores": [round(float(z), 2) for i, z in zip(p_idx, feat) if int(i) in cand],
        "spacing": spacing.tolist(),
        "estimated_first_gop": period if detected else None,
        "spacing_regularity": round(regular, 3),
        "current_gop": int(np.median(gop)) if len(gop) else None,
        "detected": bool(detected),
        "feature_frames": p_idx.tolist(),
        "feature": [round(float(v), 3) for v in feat],
    }


def _plot(n_frames: int, xs: list[int], ys: list[float], marks: list[int], threshold: float, title: str,
          types: list[str] | None = None, w: int = 1000, h: int = 260) -> np.ndarray:
    img = np.full((h, w, 3), 255, np.uint8)
    pad_l, pad_b, pad_t = 40, 24, 24
    ph, pw = h - pad_b - pad_t, w - pad_l - 10
    lo = min(min(ys, default=0.0), 0.0)
    hi = max(max(ys, default=1.0), threshold * 1.2, 1e-6)
    X = lambda n: int(pad_l + pw * n / max(1, n_frames - 1))  # noqa: E731
    Y = lambda v: int(pad_t + ph * (1 - (v - lo) / (hi - lo)))  # noqa: E731
    if types:
        for n, t in enumerate(types):
            if t == "I":
                cv2.line(img, (X(n), pad_t), (X(n), pad_t + ph), (200, 200, 230), 1)
    cv2.line(img, (pad_l, Y(threshold)), (pad_l + pw, Y(threshold)), (230, 120, 40), 1)
    cv2.line(img, (pad_l, Y(0)), (pad_l + pw, Y(0)), (180, 180, 180), 1)
    pts = np.array([[X(x), Y(y)] for x, y in zip(xs, ys)], np.int32)
    if len(pts) > 1:
        cv2.polylines(img, [pts], False, (40, 90, 160), 1, cv2.LINE_AA)
    by_x = dict(zip(xs, ys))
    for m in marks:
        if m in by_x:
            cv2.circle(img, (X(m), Y(by_x[m])), 4, (210, 40, 40), -1, cv2.LINE_AA)
    cv2.putText(img, title, (pad_l, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
    cv2.putText(img, f"frame 0 .. {n_frames - 1}", (pad_l, h - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 90, 90), 1, cv2.LINE_AA)
    return img.astype(np.float32) / 255.0


@register(
    "video.double_encoding",
    name="Double encoding (VPF)",
    version="1.0.0",
    category="Video authentication",
    description="Reads every macroblock's type from the bitstream (intra, predicted, skipped) and looks for P-frames "
    "that were coded far more expensively than their neighbours: more intra and fewer skipped macroblocks. When "
    "such frames recur at a regular spacing, the video is consistent with having been decoded and encoded again, "
    "and the spacing estimates the first encoding's GOP length. Works on H.264 and MPEG-2/4 Part 2 streams.",
    reference="D. Vázquez-Padín, M. Fontani, T. Bianchi, P. Comesaña, A. Piva and M. Barni, 'Detection of Video "
    "Double Encoding with GOP Size Estimation', IEEE WIFS 2012; W. Wang and H. Farid, 'Exposing Digital Forgeries "
    "in Video by Detecting Double MPEG Compression', ACM MM&Sec 2006.",
    applies_to=("video",),
    reading_guide="Regular footprints are consistent with re-encoding after capture: an export from a player or "
    "editor, a platform transcode, or editing. Re-encoding alone is not manipulation; it tells you the file is not "
    "the recorder's original stream. No footprint does not prove a single encoding: it fails with B-frame-heavy "
    "or constant-quality second encodings, very static scenes, and when the second GOP is a multiple of the first. "
    "Irregular isolated footprints can be scene changes.",
    params=[Param("max_frames", "int", 3000, "Frames to analyse", 50, 20000, 50)],
)
def double_encoding(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    codec = ctx.probe.get("codec")
    if codec not in VPF_CODECS:
        raise ValueError(f"double-encoding analysis reads macroblock types, which FFmpeg reports for {', '.join(VPF_CODECS)}; this stream is {codec}")
    frames = macroblock_stats(ctx.path, ctx.settings, p["max_frames"])
    n = min(len(frames), int(ctx.probe.get("frame_count") or len(frames)))
    frames = frames[len(frames) - n :] if n else frames  # drop frames FFmpeg decoded twice while probing
    res = vpf(frames)
    flags = []
    if res["detected"]:
        flags.append({"code": "venc.double", "level": "warning",
                      "text": f"{len(res['footprint_frames'])} P-frames carry the footprint of former I-frames, about every "
                      f"{res['estimated_first_gop']} frames (current GOP {res['current_gop']}): consistent with the video having "
                      "been encoded twice."})
    elif res["footprint_frames"]:
        flags.append({"level": "info", "text": f"{len(res['footprint_frames'])} isolated P-frame footprint(s) without a regular "
                      "spacing (frames " + ", ".join(map(str, res["footprint_frames"][:8])) + "): scene changes or local edits; look at them."})
    types = [f["type"] for f in frames]
    counts = {t: types.count(t) for t in sorted(set(types))}
    plot = _plot(len(frames), res["feature_frames"], res["feature"], res["footprint_frames"], VPF_Z,
                 "VPF feature per P-frame (red: footprints; blue lines: current I-frames)", types)
    findings = {k: v for k, v in res.items() if k not in ("feature", "feature_frames")} | {
        "frames_analysed": len(frames), "frame_types": counts, "threshold_z": VPF_Z}
    summary = ("Consistent with double encoding" + (f" (first GOP about {res['estimated_first_gop']})." if res["estimated_first_gop"] else ".")
               if res["detected"] else "Too few P-frames to test." if res.get("note") else "No regular double-encoding footprint found.")
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps={"vpf_plot": plot})


# Frame integrity ---------------------------------------------------------------------------------------------

THUMB_W = 96
DUP_RATIO = 0.15
JUMP_Z = 8.0
SEGMENT_MIN = 5


def decode_thumbs(path, settings, width: int, height: int, max_frames: int) -> np.ndarray:
    tw = THUMB_W
    th = max(8, int(round(height * tw / max(width, 1) / 2)) * 2)
    raw = media._run([settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
                      "-frames:v", str(max_frames), "-vf", f"scale={tw}:{th}:flags=area", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    n = len(raw) // (tw * th)
    return np.frombuffer(raw[: n * tw * th], np.uint8).reshape(n, th, tw).astype(np.float32)


def frame_integrity_scan(thumbs: np.ndarray, times: list | None = None) -> dict:
    n = len(thumbs)
    if n < 3:
        return {"frames": n, "duplicates": [], "jumps": [], "repeated_segments": [], "diff": []}
    d = np.mean(np.abs(np.diff(thumbs, axis=0)), axis=(1, 2))  # d[i]: change from frame i to i+1
    typical = float(np.median(d)) + 1e-6
    dups = [int(i + 1) for i, v in enumerate(d) if v < DUP_RATIO * typical and v < 0.6]
    z = _robust_z(d, k=8)
    jumps = [{"between": [int(i), int(i + 1)], "z": round(float(z[i]), 1), "change": round(float(d[i]), 2)}
             for i in range(len(d)) if z[i] >= JUMP_Z and d[i] > 2 * typical]
    # Repeated segments: a run of frames (with motion) that reappears later.
    flat = thumbs.reshape(n, -1)
    flat = flat - flat.mean(axis=1, keepdims=True)
    norms = np.sum(flat * flat, axis=1)
    near = np.zeros((n, n), bool) if n <= 4000 else None
    segments = []
    if near is not None:
        tol = (0.5 * typical) ** 2 * flat.shape[1]  # RMS difference under half the typical frame-to-frame change
        for a in range(0, n, 512):
            blk = norms[a : a + 512, None] + norms[None, :] - 2 * flat[a : a + 512] @ flat.T
            near[a : a + 512] = blk < tol
        for i in range(n):
            near[i, max(0, i - SEGMENT_MIN) : i + SEGMENT_MIN + 1] = False
        seen = set()
        for i, j in zip(*np.nonzero(np.triu(near))):
            if (i, j) in seen:
                continue
            k = 0
            while i + k < n and j + k < n and near[i + k, j + k]:
                seen.add((i + k, j + k))
                k += 1
            if k >= SEGMENT_MIN and float(np.sum(d[i : i + k - 1])) > 0.5 * typical * (k - 1):  # the run has motion, not a static scene
                segments.append({"first": [int(i), int(i + k - 1)], "repeat": [int(j), int(j + k - 1)], "length": int(k)})
    gaps = []
    if times and len(times) == n:
        dt = np.diff([t if t is not None else np.nan for t in times])
        med = float(np.nanmedian(dt)) if np.isfinite(dt).any() else None
        if med and med > 0:
            gaps = [{"between": [int(i), int(i + 1)], "seconds": round(float(v), 4), "expected": round(med, 4)}
                    for i, v in enumerate(dt) if np.isfinite(v) and v > 1.5 * med]
    return {"frames": n, "typical_change": round(typical, 3), "duplicates": dups, "jumps": jumps,
            "repeated_segments": segments[:20], "timestamp_gaps": gaps[:50], "diff": [round(float(v), 3) for v in d],
            "z": [round(float(v), 2) for v in z]}


@register(
    "video.frame_integrity",
    name="Frame duplication and deletion",
    version="1.0.0",
    category="Video authentication",
    description="Measures how much each frame differs from the previous one (on small greyscale copies) and looks "
    "for: repeated frames (almost no change), jumps far larger than the surrounding motion (frames removed, "
    "inserted or a cut), runs of frames that appear twice (a copied or looped segment), and gaps in the "
    "recorded timestamps.",
    reference="W. Wang and H. Farid, 'Exposing Digital Forgeries in Video by Detecting Duplication', ACM MM&Sec "
    "2007; Q. Wang, Z. Li, Z. Zhang and Q. Ma, 'Video Inter-Frame Forgery Identification Based on Consistency of "
    "Correlation Coefficients of Gray Values', JCC 2(4), 2014; SWGDE Best Practices for Video Authentication, 2018.",
    applies_to=("video",),
    reading_guide="Each finding marks frames to look at, not a conclusion. Repeated frames are common in CCTV "
    "(recorders repeat frames when the camera stalls or the frame rate changes). A jump can be a deletion, an "
    "insertion, a cut in the recording, a camera switch, a light switching on or a dropped frame; compare with the "
    "timestamps and look at the frames either side. A repeated segment of moving content is rare in authentic "
    "footage and worth explaining. A static scene hides all of these.",
    params=[Param("max_frames", "int", 9000, "Frames to analyse", 10, 100000, 10)],
)
def frame_integrity(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    thumbs = decode_thumbs(ctx.path, ctx.settings, ctx.probe["width"], ctx.probe["height"], p["max_frames"])
    times = None
    try:
        table = media.frame_table(ctx.path, ctx.settings)[: len(thumbs)]
        times = [f["time"] for f in table]
    except media.MediaError:
        pass
    res = frame_integrity_scan(thumbs, times)
    flags = []
    if res["repeated_segments"]:
        s = res["repeated_segments"][0]
        flags.append({"code": "vint.repeated_segment", "level": "warning",
                      "text": f"{len(res['repeated_segments'])} run(s) of moving frames appear twice (e.g. frames {s['first'][0]}-"
                      f"{s['first'][1]} again at {s['repeat'][0]}-{s['repeat'][1]}): consistent with a copied or looped segment."})
    if res["jumps"]:
        flags.append({"code": "vint.jump", "level": "warning",
                      "text": f"{len(res['jumps'])} abrupt change(s) far above the surrounding motion (between frames "
                      + ", ".join(f"{j['between'][0]}-{j['between'][1]}" for j in res["jumps"][:6])
                      + "): consistent with removed or inserted frames, a cut or a camera switch."})
    if res["duplicates"]:
        flags.append({"code": "vint.duplicates", "level": "info",
                      "text": f"{len(res['duplicates'])} frame(s) repeat the previous one almost exactly (e.g. "
                      + ", ".join(map(str, res["duplicates"][:8])) + "); common when a recorder repeats frames."})
    if res.get("timestamp_gaps"):
        flags.append({"code": "vint.gaps", "level": "warning",
                      "text": f"{len(res['timestamp_gaps'])} gap(s) in frame timestamps longer than 1.5 frame intervals."})
    plot = _plot(res["frames"], list(range(len(res["z"]))), [min(v, 40.0) for v in res["z"]],
                 [j["between"][0] for j in res["jumps"]], JUMP_Z, "Frame-to-frame change, robust z (red: jumps)")
    findings = {k: v for k, v in res.items() if k not in ("diff", "z")} | {"thresholds": {
        "duplicate_ratio": DUP_RATIO, "jump_z": JUMP_Z, "segment_min_frames": SEGMENT_MIN}}
    n_warn = sum(1 for f in flags if f["level"] == "warning")
    summary = (f"{n_warn} kind(s) of temporal inconsistency to examine." if n_warn else
               "No jumps, repeated segments or timestamp gaps found.") + (f" {len(res['duplicates'])} repeated frame(s)." if res["duplicates"] else "")
    return AnalysisResult(summary=summary, findings=json.loads(json.dumps(findings)), flags=flags, maps={"change_plot": plot})
