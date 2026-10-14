"""Face-swap and face-edit checks (classical): noise and sharpness consistency of each face
with its surroundings, the face's JPEG compression history, and stability over video frames.

Face swaps and face edits paste a face rendered by another process into a host image. The
pasted region typically differs from its surroundings in sensor-noise level and fineness of
detail, and in a re-saved JPEG it often lacks the double-compression history of the rest.
Each face's differences are compared with the same measurement taken at many random places
in the same image, so the image's own variability sets what counts as unusual.
"""

from __future__ import annotations

import json

import cv2
import numpy as np

from .. import media
from ..filters.ai import detect_faces
from ..filters.base import Param
from .base import AnalysisContext, AnalysisResult, register

_NOISE_K = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float64)
NULL_SAMPLES = 200
Z_FLAG = 4.0
MIN_IOD_RELIABLE = 40  # inter-ocular distance in pixels below which results are unreliable
MIN_IOD = 12


def _residual_maps(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    y = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.float64) * 255.0
    noise = np.abs(cv2.filter2D(y, -1, _NOISE_K, borderType=cv2.BORDER_REFLECT)) * (np.sqrt(np.pi / 2) / 6.0)
    gx = cv2.Sobel(y, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(y, cv2.CV_64F, 0, 1, ksize=3)
    grad = np.hypot(gx, gy)
    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(y, (0, 0), 0.8), cv2.CV_64F))
    return noise, grad, lap


def _masks(shape, cx, cy, rx, ry):
    h, w = shape
    x0, x1 = int(max(0, cx - 1.7 * rx)), int(min(w, cx + 1.7 * rx + 1))
    y0, y1 = int(max(0, cy - 1.7 * ry)), int(min(h, cy + 1.7 * ry + 1))
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = ((xx - cx) / max(rx, 1)) ** 2 + ((yy - cy) / max(ry, 1)) ** 2
    return (slice(y0, y1), slice(x0, x1)), d <= 0.8**2, (d >= 1.25**2) & (d <= 1.65**2)


def _stats(noise, grad, lap, sl, inner, ring):
    n, g, l = noise[sl], grad[sl], lap[sl]
    out = []
    for m in (inner, ring):
        if m.sum() < 30:
            return None
        gm = g[m]
        flat = gm <= np.quantile(gm, 0.6)  # noise from the flatter part: texture inflates it
        out.append((float(np.mean(n[m][flat])) + 0.05, float(np.mean(l[m])) + 0.05))
    (ni, si), (nr, sr) = out
    # Detail relative to noise, so a noisy region is not mistaken for a sharp one.
    return {"noise_log_ratio": float(np.log(ni / nr)), "detail_log_ratio": float(np.log((si / ni) / (sr / nr))),
            "noise_inside": round(ni, 3), "noise_around": round(nr, 3)}


def _null(noise, grad, lap, rx, ry, avoid, seed):
    h, w = noise.shape
    rng = np.random.default_rng(seed)
    out = []
    tries = 0
    while len(out) < NULL_SAMPLES and tries < NULL_SAMPLES * 5:
        tries += 1
        cx = rng.uniform(1.7 * rx, w - 1.7 * rx) if w > 3.4 * rx else w / 2
        cy = rng.uniform(1.7 * ry, h - 1.7 * ry) if h > 3.4 * ry else h / 2
        if any(abs(cx - ax) < (rx + arx) and abs(cy - ay) < (ry + ary) for ax, ay, arx, ary in avoid):
            continue
        sl, inner, ring = _masks(noise.shape, cx, cy, rx, ry)
        s = _stats(noise, grad, lap, sl, inner, ring)
        if s:
            out.append((s["noise_log_ratio"], s["detail_log_ratio"]))
    return np.array(out) if out else np.zeros((0, 2))


def _z(v, null):
    if len(null) < 20:
        return None
    med = float(np.median(null))
    mad = float(np.median(np.abs(null - med))) * 1.4826 + 1e-3
    return round((v - med) / mad, 2)


def _face_regions(img, p) -> list[dict]:
    pts = p["regions"]
    if len(pts) % 2:
        raise ValueError("face regions need pairs of corner points")
    faces = []
    for (ax, ay), (bx, by) in zip(pts[0::2], pts[1::2]):
        x0, y0, x1, y1 = min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
        faces.append({"box": [x0, y0, x1 - x0, y1 - y0], "landmarks": None, "score": None, "source": "examiner"})
    if p["detect"]:
        for f in detect_faces(img, p["min_score"]):
            faces.append(f | {"source": "detector"})
    return faces


def check_faces(img: np.ndarray, faces: list[dict], seed: int = 0, jpeg_llr: np.ndarray | None = None) -> list[dict]:
    noise, grad, lap = _residual_maps(img)
    avoid = [(f["box"][0] + f["box"][2] / 2, f["box"][1] + f["box"][3] / 2, f["box"][2] / 2, f["box"][3] / 2) for f in faces]
    out = []
    for i, f in enumerate(faces):
        x, y, w, h = f["box"]
        cx, cy, rx, ry = x + w / 2, y + h / 2, w / 2, h / 2
        lm = f.get("landmarks")
        iod = float(np.hypot(lm[0][0] - lm[1][0], lm[0][1] - lm[1][1])) if lm else 0.4 * w
        rec = {"face": i + 1, "source": f.get("source"), "box": [round(v, 1) for v in f["box"]], "detector_score": f.get("score"),
               "inter_ocular_px": round(iod, 1),
               "reliability": "too small" if iod < MIN_IOD else ("limited" if iod < MIN_IOD_RELIABLE else "usable")}
        if iod < MIN_IOD:
            out.append(rec)
            continue
        sl, inner, ring = _masks(noise.shape, cx, cy, rx, ry)
        s = _stats(noise, grad, lap, sl, inner, ring)
        if s is None:
            out.append(rec | {"note": "face too close to the edge to compare with its surroundings"})
            continue
        null = _null(noise, grad, lap, rx, ry, avoid, seed + i)
        rec.update({k: round(v, 4) if isinstance(v, float) else v for k, v in s.items()})
        rec["noise_z"] = _z(s["noise_log_ratio"], null[:, 0]) if len(null) else None
        rec["detail_z"] = _z(s["detail_log_ratio"], null[:, 1]) if len(null) else None
        rec["null_samples"] = int(len(null))
        if jpeg_llr is not None and jpeg_llr.size:
            bh, bw = jpeg_llr.shape
            by0, by1 = int(max(0, (cy - 0.8 * ry) // 8)), int(min(bh, (cy + 0.8 * ry) // 8 + 1))
            bx0, bx1 = int(max(0, (cx - 0.8 * rx) // 8)), int(min(bw, (cx + 0.8 * rx) // 8 + 1))
            p_single = 1 / (1 + np.exp(np.clip(jpeg_llr, -30, 30)))
            inside = p_single[by0:by1, bx0:bx1]
            if inside.size:
                rec["jpeg_single_compressed_share_face"] = round(float(np.mean(inside > 0.8)), 3)
                rec["jpeg_single_compressed_share_image"] = round(float(np.mean(p_single > 0.8)), 3)
        out.append(rec)
    return out


def _draw(img, results) -> np.ndarray:
    vis = media.to_uint8(img).copy()
    for r in results:
        x, y, w, h = (int(round(v)) for v in r["box"])
        z = max(abs(r.get("noise_z") or 0), abs(r.get("detail_z") or 0))
        col = (60, 200, 90) if z < 2 else (240, 180, 40) if z < Z_FLAG else (230, 50, 50)
        if r["reliability"] == "too small":
            col = (150, 150, 150)
        t = max(1, int(round(max(img.shape[:2]) / 500)))
        cv2.rectangle(vis, (x, y), (x + w, y + h), col, t)
        label = f"#{r['face']} " + (f"z {z:.1f}" if r.get("noise_z") is not None else r["reliability"])
        cv2.putText(vis, label, (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, max(0.4, t * 0.4), col, t, cv2.LINE_AA)
    return vis.astype(np.float32) / 255.0


def _flags(results, prefix="") -> list[dict]:
    flags = []
    for r in results:
        if r["reliability"] == "too small":
            flags.append({"level": "info", "text": f"{prefix}Face {r['face']} is too small to assess ({r['inter_ocular_px']:.0f} px between the eyes)."})
            continue
        zs = [abs(v) for v in (r.get("noise_z"), r.get("detail_z")) if v is not None]
        if zs and max(zs) >= Z_FLAG:
            which = "noise level" if abs(r.get("noise_z") or 0) >= Z_FLAG else "fineness of detail"
            flags.append({"code": "face.inconsistent", "level": "warning",
                          "text": f"{prefix}Face {r['face']}: its {which} differs from its surroundings more than "
                          f"{Z_FLAG:g} times the spread at random places in the image (z {max(zs):.1f})"
                          + ("; the face is small, so treat this as weak." if r["reliability"] == "limited" else ".")})
        sf, si = r.get("jpeg_single_compressed_share_face"), r.get("jpeg_single_compressed_share_image")
        if sf is not None and si is not None and sf >= 0.5 and si <= 0.25:
            flags.append({"code": "face.jpeg_history", "level": "warning",
                          "text": f"{prefix}Face {r['face']}: {sf:.0%} of its blocks lack the double-compression history "
                          f"found in the image as a whole ({si:.0%} of all blocks)."})
    return flags


@register(
    "face.consistency",
    name="Face-swap and face-edit checks",
    version="1.0.0",
    category="Synthetic media",
    description="Finds faces (YuNet detector, or regions you mark) and compares each face with a ring around it: "
    "sensor-noise level, and fineness of detail relative to noise. Each difference is converted to a z-score "
    "against the same measurement at 200 random places in the image, so the image itself sets what is unusual. "
    "In re-saved JPEGs it also checks whether the face shares the image's double-compression history. On video "
    "it samples frames and follows each face over time. Face size is reported because small faces carry little "
    "information.",
    reference="B. Mahdian and S. Saic, 'Using noise inconsistencies for blind image forensics', Image and Vision "
    "Computing 27(10), 2009; Y. Li et al., 'Face X-ray for More General Face Forgery Detection', CVPR 2020 "
    "(blending boundary); T. Bianchi and A. Piva, IEEE TIFS 7(3), 2012 (double JPEG); J. Immerkaer, 'Fast Noise "
    "Variance Estimation', CVIU 64(2), 1996; W. Wu, H. Peng and S. Yu, 'YuNet', Machine Intelligence Research 20, 2023.",
    applies_to=("image", "video"),
    reading_guide="A large z (4 or more) means the face differs from its surroundings more than almost any other "
    "region of the image does from its own: consistent with a face rendered or edited separately, but also with "
    "make-up, motion blur, focus on the face or local denoising by a phone. A small z is not evidence of "
    "authenticity: modern face swaps can match noise. Faces under about 40 pixels between the eyes give weak "
    "results; below 12 they are not assessed. The detector can miss faces; mark them yourself.",
    params=[
        Param("detect", "bool", True, "Detect faces automatically"),
        Param("regions", "points", [], "Face regions (pairs of corners)", help="Mark faces the detector misses"),
        Param("min_score", "float", 0.6, "Detector confidence threshold", 0.3, 0.95, 0.05),
        Param("start", "int", 0, "First frame (video)", 0, None, 1),
        Param("frames", "int", 25, "Frames to sample (video)", 1, 300, 1),
        Param("step", "int", 5, "Frame step (video)", 1, 250, 1),
    ],
)
def face_consistency(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    if ctx.probe["media_type"] == "video":
        return _video(ctx, p)
    img = ctx.image
    faces = _face_regions(img, p)
    llr = None
    if ctx.data[:3] == b"\xff\xd8\xff":
        from .double_jpeg import ADJPG_THRESHOLD, adjpg

        try:
            dj = adjpg(ctx.data)
            if dj["score"] >= ADJPG_THRESHOLD and dj["supporting_frequencies"] >= 2:
                llr = dj["block_llr"]
        except Exception:  # noqa: BLE001 - not every JPEG supports the test (progressive, 4:2:0 edge cases)
            llr = None
    results = check_faces(img, faces, jpeg_llr=llr)
    flags = _flags(results)
    if not faces:
        flags.append({"level": "info", "text": "No face found. The detector misses small, turned or covered faces; mark regions to check them."})
    summary = (f"{len(results)} face(s) checked; " + (f"{sum(1 for f in flags if f.get('code'))} inconsistency flag(s)."
               if any(f.get("code") for f in flags) else "no inconsistency beyond the image's own variability.")) if results else "No face found."
    findings = {"faces": results, "z_threshold": Z_FLAG, "null_samples": NULL_SAMPLES,
                "double_jpeg_history_used": llr is not None}
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps={"faces": _draw(img, results)} if results else {})


def _iou(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    return inter / max(1e-9, aw * ah + bw * bh - inter)


def _video(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    total = int(ctx.probe["frame_count"])
    idx = [n for n in range(p["start"], total, p["step"])][: p["frames"]]
    if not idx:
        raise ValueError("the start frame is beyond the end of the video")
    tracks: list[dict] = []
    first_img, first_results = None, []
    for n in idx:
        img = media.decode_frames(ctx.path, ctx.settings, n, n, ctx.probe["width"], ctx.probe["height"])[0]
        faces = _face_regions(img, p)
        res = check_faces(img, faces, seed=n)
        if first_img is None and res:
            first_img, first_results = img, res
        for r in res:
            best = max(tracks, key=lambda t: _iou(t["last_box"], r["box"]), default=None)
            if best is None or _iou(best["last_box"], r["box"]) < 0.3 or best["frames"][-1] == n:
                best = {"track": len(tracks) + 1, "frames": [], "results": [], "last_box": r["box"]}
                tracks.append(best)
            best["frames"].append(n)
            best["results"].append(r)
            best["last_box"] = r["box"]
    summaries = []
    for t in tracks:
        rs = [r for r in t["results"] if r.get("noise_z") is not None]
        nz = [r["noise_z"] for r in rs]
        dz = [r["detail_z"] for r in rs if r.get("detail_z") is not None]
        lr = [r["noise_log_ratio"] for r in rs]
        summaries.append({
            "face": t["track"], "frames": len(t["frames"]), "first_frame": t["frames"][0], "last_frame": t["frames"][-1],
            "box": t["results"][0]["box"], "inter_ocular_px": round(float(np.median([r["inter_ocular_px"] for r in t["results"]])), 1),
            "reliability": "too small" if not rs else t["results"][0]["reliability"],
            "noise_z": round(float(np.median(nz)), 2) if nz else None,
            "detail_z": round(float(np.median(dz)), 2) if dz else None,
            "noise_log_ratio_std_over_time": round(float(np.std(lr)), 4) if len(lr) > 2 else None,
            "share_of_frames_flagged": round(float(np.mean([abs(z) >= Z_FLAG for z in nz])), 3) if nz else None,
        })
    flags = _flags(summaries, prefix="Track ")
    for s in summaries:
        if s["noise_log_ratio_std_over_time"] and s["noise_log_ratio_std_over_time"] > 0.35 and s["frames"] >= 5:
            flags.append({"code": "face.flicker", "level": "warning",
                          "text": f"Track {s['face']}: the face's noise relative to its surroundings fluctuates strongly between "
                          f"frames (std {s['noise_log_ratio_std_over_time']:.2f} in log ratio), consistent with per-frame face rendering."})
    findings = {"frames_sampled": idx, "tracks": summaries, "z_threshold": Z_FLAG}
    summary = f"{len(tracks)} face track(s) over {len(idx)} sampled frames" + (
        f"; {sum(1 for f in flags if f.get('code'))} inconsistency flag(s)." if any(f.get("code") for f in flags) else ".")
    maps = {"faces_first_frame": _draw(first_img, first_results)} if first_img is not None else {}
    return AnalysisResult(summary=summary, findings=json.loads(json.dumps(findings)), flags=flags, maps=maps)
