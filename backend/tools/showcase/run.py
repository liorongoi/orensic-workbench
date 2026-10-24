"""Run every filter, analysis and workflow through the real application and record the results.

    python -m tools.showcase.run --media DIR --work DIR

Writes WORK/results.json and the images it refers to. Nothing here bypasses the application:
filters run as saved chains (rendered, re-run from the original to check the result is
byte-identical, then exported); analyses run through Workbench.run_analysis; the workflows call
the same services the API and the command line use. Failures are recorded, not hidden.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from forensic import __version__, compute
from forensic.analysis import ANALYSES
from forensic.audit import Actor
from forensic.config import load_settings
from forensic.filters.base import REGISTRY
from forensic.workbench import Workbench

from . import inputs as inp

A = Actor("Showcase Examiner")
JUSTIFY = "Validation report demonstration of the generative step; illustrative only, not evidence."

# Filter id -> input, parameters (beyond the defaults) and frames for multi-frame filters.
PLAN: dict[str, dict] = {
    "ai.super_resolution": {"input": "small", "params": {"model": "fsrcnn", "scale": "2"}},
    "ai.realesrgan": {"input": "small"},
    "redact.faces": {"input": "people", "params": {"min_score": 0.5}},
    "deblur.wiener": {"input": "motion", "params": {"psf": "motion", "length": 15, "angle": 30, "nsr": 0.02}},
    "deblur.richardson_lucy": {"input": "motion", "params": {"psf": "motion", "length": 15, "angle": 30, "iterations": 30}},
    "deblur.tv": {"input": "defocus", "params": {"psf": "defocus", "radius": 5}},
    "deblur.blind": {"input": "gauss_blur", "params": {"kernel_size": 15}},
    "deblur.variable": {"input": "motion", "params": {"streaks": [[100, 120], [113, 112.5], [400, 200], [413, 192.5]]}},
    "denoise.median": {"input": "saltpepper", "params": {"size": 3}},
    "denoise.gaussian": {"input": "noisy"},
    "denoise.bilateral": {"input": "noisy"},
    "denoise.nlm": {"input": "noisy", "params": {"strength": 18, "color_strength": 18}},
    "denoise.fft_notch": {"input": "periodic"},
    "denoise.wiener_adaptive": {"input": "noisy"},
    "denoise.deblock": {"input": "blocky"},
    "sharpen.unsharp_mask": {"input": "gauss_blur", "params": {"radius": 2, "amount": 1.5}},
    "sharpen.laplacian": {"input": "gauss_blur"},
    "exposure.levels": {"input": "dark", "params": {"in_white": 0.48, "gamma": 1.0}},
    "exposure.brightness_contrast": {"input": "flat", "params": {"contrast": 2.85, "brightness": -0.14}},
    "exposure.curves": {"input": "dark", "params": {"points": [[0, 0], [0.12, 0.3], [0.3, 0.63], [0.48, 1.0], [1, 1]]}},
    "exposure.clahe": {"input": "flat"},
    "exposure.histogram_equalization": {"input": "flat"},
    "exposure.range_expand": {"input": "flat"},
    "exposure.low_light": {"input": "dark"},
    "exposure.stops": {"input": "dark", "params": {"stops": 2.32}},
    "exposure.contrast_stretch": {"input": "flat"},
    "exposure.shadows_highlights": {"input": "dark"},
    "exposure.homomorphic": {"input": "flat"},
    "color.channel_gains": {"input": "cast", "params": {"mode": "grey_world"}},
    "color.grayscale": {"input": "fruits"},
    "color.invert": {"input": "building"},
    "color.white_balance_area": {"input": "cast", "params": {"roi": [[150, 60], [230, 140]]}},
    "color.saturation": {"input": "fruits", "params": {"factor": 0.3}},
    "color.channel_mixer": {"input": "fruits", "params": {"r_r": 0, "r_g": 1, "g_r": 1, "g_g": 0}},
    "color.space_channel": {"input": "fruits", "params": {"space": "HSV", "channel": 1}},
    "color.false_colour": {"input": "dark"},
    "color.threshold": {"input": "text"},
    "edges.detect": {"input": "building"},
    "morphology.op": {"input": "text"},
    "fft.spectrum": {"input": "periodic"},
    "fft.band_filter": {"input": "noisy"},
    "geometry.fisheye": {"input": "fisheye", "params": {"focal": 0.35, "balance": 0.0}},
    "geometry.unwrap_360": {"input": "equirect", "params": {"yaw": 0, "pitch": 15, "fov": 100, "width": 480}},
    "geometry.crop": {"input": "building", "params": {"x": 120, "y": 60, "width": 240, "height": 180}},
    "geometry.rotate": {"input": "building", "params": {"angle": 12}},
    "geometry.flip": {"input": "building"},
    "geometry.resize": {"input": "small", "params": {"scale_x": 2, "scale_y": 2}},
    "geometry.aspect_ratio": {"input": "building", "params": {"ratio_w": 4, "ratio_h": 3}},
    "geometry.perspective": {"input": "plate_car", "params": {"points": [[185, 254], [278, 254], [278, 293], [185, 293]], "width": 280, "height": 116}},
    "geometry.lens_undistort": {"input": "barrel", "params": {"k1": -0.2}},
    "geometry.deinterlace": {"input": "interlaced"},
    "stack.background_subtract": {"input": "moving", "frames": [0, 23], "show": 12, "params": {"output": "overlay"}},
    "stack.frame_difference": {"input": "moving", "frames": [0, 23], "show": 12},
    "stack.optical_flow": {"input": "moving", "frames": [0, 23], "show": 12},
    "stack.stabilise": {"input": "shaky", "frames": [0, 9], "show": 5},
    "stack.integrate": {"input": "shaky", "frames": [0, 9]},
    "stack.pick_frame": {"input": "shaky", "frames": [0, 9], "params": {"index": 3}},
    "stack.super_resolution": {"input": "shaky", "frames": [0, 9]},
    "stack.denoise_temporal": {"input": "shaky", "frames": [0, 9], "show": 5},
    "stack.turbulence": {"input": "turbulence", "frames": [0, 9]},
}
# Chains that need a step before the filter (the filter works on what the step produces).
PRE: dict[str, list[dict]] = {
    "stack.integrate": [{"filter": "stack.stabilise"}],
    "stack.denoise_temporal": [{"filter": "stack.stabilise"}],
}

ANALYSIS_PLAN: dict[str, dict] = {
    "tamper.ela": {"input": "spliced"},
    "tamper.jpeg_ghost": {"input": "spliced"},
    "tamper.copy_move": {"input": "copymove"},
    "tamper.double_jpeg": {"input": "spliced"},
    "tamper.nonaligned_grid": {"input": "spliced"},
    "tamper.jpeg_dimples": {"input": "authentic"},
    "tamper.noise_map": {"input": "spliced"},
    "tamper.fusion": {"input": "spliced"},
    "tamper.luminance_gradient": {"input": "spliced"},
    "file.metadata": {"input": "spliced"},
    "file.qt_database": {"input": "spliced"},
    "synthetic.provenance": {"input": "authentic"},
    "synthetic.camera_traces": {"input": "authentic"},
    "synthetic.generated": {"input": "authentic"},
    "synthetic.learned": {"input": "authentic"},
    "face.consistency": {"input": "people"},
    "video.double_encoding": {"input": "street"},
    "video.frame_integrity": {"input": "street"},
    "camera.prnu": {"input": "prnu_test", "needs_reference": True},
    "tamper.prnu_map": {"input": "prnu_test", "needs_reference": True},
    "geometry.shadows": {"skip": "Needs the examiner to mark object/shadow point pairs on the image; there is no automatic input."},
    "geometry.reflections": {"skip": "Needs the examiner to mark object/reflection point pairs on the image."},
    "geometry.perspective": {"skip": "Needs the examiner to mark parallel lines on the image."},
}


def u8(img: np.ndarray) -> np.ndarray:
    return np.clip(np.round(img * 255), 0, 255).astype(np.uint8) if img.dtype != np.uint8 else img


def save_rgb(img: np.ndarray, path: Path) -> str:
    Image.fromarray(u8(img)).save(path)
    return str(path)


def align_to(ref: np.ndarray, img: np.ndarray) -> np.ndarray:
    """Shift `img` (resized to ref) by the translation that best aligns it to `ref` (phase correlation)."""
    if img.shape != ref.shape:
        img = cv2.resize(img, (ref.shape[1], ref.shape[0]), interpolation=cv2.INTER_AREA)
    g1 = cv2.cvtColor(ref, cv2.COLOR_RGB2GRAY).astype(np.float32)
    g2 = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(g1, g2)
    M = np.float32([[1, 0, -dx], [0, 1, -dy]])
    return cv2.warpAffine(img, M, (ref.shape[1], ref.shape[0]), borderMode=cv2.BORDER_REFLECT)


def psnr_ssim(a: np.ndarray, b: np.ndarray) -> dict:
    from forensic.videoops import similarity

    a = a.astype(np.float32) / (255.0 if a.dtype == np.uint8 else 1)
    b = b.astype(np.float32) / (255.0 if b.dtype == np.uint8 else 1)
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    m = similarity(a, b)
    return {"psnr_db": m["psnr_db"], "ssim": m["ssim"]}


class Runner:
    def __init__(self, media: Path, work: Path):
        self.media, self.work = media, work
        if work.exists():
            shutil.rmtree(work)
        (work / "img").mkdir(parents=True)
        self.wb = Workbench(load_settings(work / "data"))
        self.case = self.wb.create_case("Lucida showcase", "Showcase Examiner", A, case_number="SHOW-2026-1",
                                        legal_authority="Internal validation (no real case)")
        self.items = inp.make_all(media, work / "inputs")
        self.ev: dict[str, dict] = {}
        self.out: dict = {"inputs": {}, "filters": [], "analyses": [], "workflows": {}}
        for k, it in self.items.items():
            self.ev[k] = self.wb.ingest_path(self.case["id"], Path(it["path"]), A)
            self.out["inputs"][k] = {**it, "evidence_id": self.ev[k]["id"], "sha256": self.ev[k]["sha256"],
                                     "media_type": self.ev[k]["media_type"], "probe": self.ev[k]["probe"]}

    def img(self, name: str) -> Path:
        return self.work / "img" / name

    # Filters -----------------------------------------------------------------------------------------

    def filters(self):
        for fid, f in REGISTRY.items():
            plan = PLAN.get(fid, {"input": "building"})
            rec = {"id": fid, "name": f.name, "version": f.version, "category": f.category, "description": f.description,
                   "reference": f.reference, "input": plan["input"], "ai": f.ai, "generative": f.generative}
            try:
                ev = self.ev[plan["input"]]
                params = f.validate(plan.get("params") or {})
                rec["params"] = params
                rec["defaults"] = {p.name: p.default for p in f.params}
                node = {"filter": fid, "params": params}
                if f.generative:
                    node["justification"] = JUSTIFY
                frames = plan.get("frames")
                g = {"source": {"evidence_id": ev["id"], **({"frames": {"start": frames[0], "end": frames[1]}} if frames else {})},
                     "nodes": [*PRE.get(fid, []), node]}
                ch = self.wb.create_chain(self.case["id"], ev["id"], f.name, A, g)
                t0 = time.perf_counter()
                r = self.wb.render_chain(ch["id"], A)
                rec["seconds"] = round(time.perf_counter() - t0, 3)
                v = self.wb.verify_chain(ch["id"], A)
                rec["reproduced"] = v["reproduced"]
                rec["output_hash"] = v["expected_hash"]
                rec["graph_hash"] = r["graph_hash"]
                rec["models"] = r.get("models") or []
                show = plan.get("show", 0)
                png, info = self.wb.export_chain(ch["id"], A, frame=show if f.output_kind == "stack" else 0)
                rec["watermarked"] = info["watermarked"]
                after = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
                before = u8(self.wb.source_frame(ev["id"], (frames[0] + show) if frames else 0))
                rec["before"] = save_rgb(before, self.img(f"f_{fid}_before.png"))
                rec["after"] = save_rgb(after, self.img(f"f_{fid}_after.png"))
                rec["pre"] = [s["filter"] for s in PRE.get(fid, [])]
                truth = self.items[plan["input"]].get("truth")
                if truth:
                    t = np.asarray(Image.open(truth).convert("RGB"))
                    from forensic.graph import frame_of

                    raw = self.wb.engine.run(self.wb.get_chain(ch["id"])["graph"]).data  # pixels before any watermark
                    after = u8(frame_of(raw, show if f.output_kind == "stack" else 0))
                    if frames:  # burst frames are shifted against the truth: align by phase correlation first
                        before, after = align_to(t, before), align_to(t, after)
                    if fid.startswith(("denoise", "deblur", "sharpen", "exposure", "color.channel_gains", "color.white_balance",
                                       "stack.integrate", "stack.super_resolution", "stack.turbulence", "stack.denoise_temporal")) \
                            or fid in ("ai.super_resolution", "ai.realesrgan", "geometry.resize"):
                        rec["truth_metrics"] = {"before": psnr_ssim(t, before), "after": psnr_ssim(t, after)}
            except Exception as e:  # noqa: BLE001 - recorded in the report
                rec["error"] = f"{type(e).__name__}: {e}"
                rec["trace"] = traceback.format_exc(limit=4)
            self.out["filters"].append(rec)
            print(f"filter {fid:34} {'ERROR ' + rec['error'][:80] if 'error' in rec else rec.get('truth_metrics', '')}", flush=True)

    # Analyses ----------------------------------------------------------------------------------------

    def analyses(self):
        # PRNU: a synthetic "camera" whose sensor adds a fixed multiplicative noise pattern.
        rng = np.random.default_rng(7)
        K = rng.normal(0, 0.03, (360, 520, 1)).astype(np.float32)
        srcs = []
        base = [cv2.imread(str(self.media / n)) for n in ("building.jpg", "home.jpg", "fruits.jpg", "board.jpg", "left01.jpg",
                                                          "messi5.jpg", "street.png", "dog416.png")]
        for i, b in enumerate(base):
            b = cv2.resize(b, (520, 360)).astype(np.float32)
            b = cv2.GaussianBlur(b, (0, 0), 3) * 0.5 + 64 + i * 4  # smooth, mid-grey scenes: the pattern dominates the residual
            img = np.clip(b * (1 + K) + rng.normal(0, 2, b.shape), 0, 255).astype(np.uint8)
            p = self.work / "inputs" / f"prnu_{i}.png"
            cv2.imwrite(str(p), img)
            srcs.append(p)
        refs = [self.wb.ingest_path(self.case["id"], p, A) for p in srcs[:7]]
        test = self.wb.ingest_path(self.case["id"], srcs[7], A)
        other = cv2.resize(cv2.imread(str(self.media / "dog416.png")), (520, 360))
        other = cv2.GaussianBlur(other.astype(np.float32), (0, 0), 3) * 0.5 + 92
        other = np.clip(other * (1 + rng.normal(0, 0.03, (360, 520, 1))) + rng.normal(0, 2, other.shape), 0, 255).astype(np.uint8)
        po = self.work / "inputs" / "prnu_other_camera.png"
        cv2.imwrite(str(po), other)
        other_ev = self.wb.ingest_path(self.case["id"], po, A)
        self.ev["prnu_test"] = test
        try:
            ref = self.wb.create_camera_reference(self.case["id"], "Synthetic camera A", [r["id"] for r in refs], A, camera="Synthetic A")
            self.out["prnu"] = {"reference": {k: ref.get(k) for k in ("id", "name", "images")}, "reference_id": ref["id"]}
        except Exception as e:  # noqa: BLE001
            ref = None
            self.out["prnu"] = {"error": str(e)}
        for aid, a in ANALYSES.items():
            plan = ANALYSIS_PLAN.get(aid, {"input": "authentic"})
            rec = {"id": aid, "name": a.name, "version": a.version, "category": a.category, "description": a.description,
                   "reference": a.reference, "ai": a.ai}
            if plan.get("skip"):
                rec["skipped"] = plan["skip"]
                self.out["analyses"].append(rec)
                continue
            try:
                ev = self.ev[plan["input"]]
                rec["input"] = plan["input"]
                params = {}
                if plan.get("needs_reference"):
                    if not ref:
                        raise ValueError("camera reference could not be built")
                    params["reference_id"] = ref["id"]
                t0 = time.perf_counter()
                res = self.wb.run_analysis(ev["id"], aid, params, A)
                rec["seconds"] = round(time.perf_counter() - t0, 3)
                rec["params"] = res.get("params")
                payload = res.get("result") or res
                rec["summary"] = payload.get("summary")
                rec["flags"] = payload.get("flags", [])
                rec["findings"] = payload.get("findings", {})
                maps = payload.get("maps") or {}
                rec["maps"] = {}
                for name, digest in list(maps.items())[:2]:
                    p = self.wb.store.derived_path(digest, "png")
                    if p.exists():
                        rec["maps"][name] = str(p)
                rec["before"] = save_rgb(u8(self.wb.source_frame(ev["id"], 0)), self.img(f"a_{aid}_input.png"))
                if aid == "camera.prnu":
                    r2 = self.wb.run_analysis(other_ev["id"], aid, params, A)
                    p2 = r2.get("result") or r2
                    rec["control"] = {"summary": p2.get("summary"), "findings": p2.get("findings", {})}
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"
            self.out["analyses"].append(rec)
            print(f"analysis {aid:28} {'ERROR ' + rec['error'][:90] if 'error' in rec else (rec.get('summary') or '')[:90]}", flush=True)

    # Workflows --------------------------------------------------------------------------------------

    def wf(self, name: str, fn):
        print(f"workflow {name}", flush=True)
        try:
            self.out["workflows"][name] = fn()
        except Exception as e:  # noqa: BLE001
            self.out["workflows"][name] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=5)}
            print("   ERROR", e, flush=True)

    def w_vertical(self):
        wb, cid = self.wb, None
        c = wb.create_case("Vertical slice", "Showcase Examiner", A, case_number="SHOW-VS")
        cid = c["id"]
        ev = wb.ingest_path(cid, Path(self.items["dark"]["path"]), A)
        meta = wb.run_analysis(ev["id"], "file.metadata", {}, A)
        g = {"source": {"evidence_id": ev["id"]}, "nodes": [{"filter": "exposure.stops", "params": {"stops": 2.0}},
                                                             {"filter": "exposure.clahe", "params": {}},
                                                             {"filter": "sharpen.unsharp_mask", "params": {"amount": 0.6}}]}
        ch = wb.create_chain(cid, ev["id"], "Brighten, local contrast, sharpen", A, g)
        r = wb.render_chain(ch["id"], A)
        v = wb.verify_chain(ch["id"], A)
        png, info = wb.export_chain(ch["id"], A)
        p_after = self.img("w_vs_after.png")
        p_after.write_bytes(png)
        from forensic.report import build_bundle, render_pdf

        bundle = build_bundle(wb, cid, A)
        pdf = render_pdf(wb, bundle)
        (self.work / "vertical_slice_report.pdf").write_bytes(pdf)
        pages = pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")
        audit = wb.audit.verify()
        ev2 = wb.get_evidence(ev["id"])
        return {"evidence": {"filename": ev["filename"], "sha256": ev["sha256"], "sha512": ev2["sha512"], "md5": ev["md5"],
                             "size": ev["size"], "probe": ev["probe"]},
                "metadata_summary": (meta.get("result") or meta).get("summary"),
                "chain": {"steps": [f"{n['filter']}@{n['version']} {json.dumps(n['params'])}" for n in wb.get_chain(ch["id"])["graph"]["nodes"]],
                          "graph_hash": r["graph_hash"], "output_hash": v["expected_hash"], "reproduced": v["reproduced"]},
                "export": info, "report_pages": pages, "report_bytes": len(pdf), "audit": audit,
                "custody": [e["event"] for e in wb.casework.custody(cid, ev["id"])],
                "before": save_rgb(u8(wb.source_frame(ev["id"])), self.img("w_vs_before.png")), "after": str(p_after),
                "audit_entries": [e["action"] for e in wb.audit.entries(cid)]}

    def w_tamper(self):
        wb = self.wb
        c = wb.create_case("Tamper check", "Showcase Examiner", A)
        # A file of its own: originals are stored once per content, so damaging a shared file would affect other cases.
        own = self.work / "inputs" / "tamper_subject.png"
        cv2.imwrite(str(own), cv2.flip(cv2.imread(self.items["building"]["path"]), 1))
        ev = wb.ingest_path(c["id"], own, A)
        ok = wb.verify_evidence(ev["id"], A)
        p = wb.evidence_path(ev["id"])
        os.chmod(p, 0o600)
        with open(p, "r+b") as f:
            f.seek(5000)
            f.write(b"\x00")
        bad = wb.verify_evidence(ev["id"], A)
        logged = [e for e in wb.audit.entries(c["id"]) if e["action"] == "evidence.integrity_failure"]
        try:
            wb.casework.export_package(c["id"], A)
            blocked = False
        except ValueError as e:
            blocked = str(e)
        return {"before": {"ok": ok["ok"], "sha256": ok["sha256"]}, "after": {"ok": bad["ok"], "sha256": bad["sha256"]},
                "logged": len(logged), "export_blocked": blocked, "custody": [e["event"] for e in wb.casework.custody(c["id"], ev["id"])]}

    def w_video(self):
        wb, vo = self.wb, self.wb.videoops
        ev = self.ev["street"]
        clips = []
        for mode in ("copy", "lossless", "h264"):
            c = vo.extract_clip(ev["id"], 7, 21, A, mode=mode)
            conv = c["conversion"]
            clips.append({"mode": mode, "frames": c["probe"]["frame_count"], "codec": c["probe"].get("codec"),
                          "requested": conv.get("requested"), "actual": conv.get("actual"), "note": conv.get("note") or conv.get("keyframe_note"),
                          "frame_check": conv.get("frame_check"), "sha256": c["sha256"]})
        zipped, man = vo.export_frames(ev["id"], A, start=0, end=29, step=5)
        prof = vo.temporal_profile(self.ev["moving"]["id"])
        sheet = vo.contact_sheet(ev["id"], A, count=12)
        sheet_p = self.img("w_contact_sheet.png")
        shutil.copy(wb.store.derived_path(sheet["png"], "png"), sheet_p)
        timing = vo.av_timing(ev["id"])
        # compare: original vs a JPEG Q20 version
        b = cv2.imread(self.items["building"]["path"])
        q20 = self.work / "inputs" / "building_q20.jpg"
        cv2.imwrite(str(q20), b, [cv2.IMWRITE_JPEG_QUALITY, 20])
        e2 = wb.ingest_path(self.case["id"], q20, A)
        cmp_ = vo.compare({"evidence_id": self.ev["building"]["id"]}, {"evidence_id": e2["id"]}, A)
        diff_p = self.img("w_compare_diff.png")
        shutil.copy(wb.store.derived_path(cmp_["difference_map"], "png"), diff_p)
        # chain rendered as video
        g = {"source": {"evidence_id": self.ev["moving"]["id"], "frames": {"start": 0, "end": 23}},
             "nodes": [{"filter": "stack.background_subtract", "params": {"output": "mask"}}]}
        ch = wb.create_chain(self.case["id"], self.ev["moving"]["id"], "Foreground", A, g)
        wb.render_chain(ch["id"], A)
        rv = vo.render_chain_video(ch["id"], A)
        seq = vo.sequence_to_video(self.case["id"], [self.ev["building"]["id"], self.ev["dark"]["id"], self.ev["flat"]["id"]], 1.0, A)
        return {"clips": clips, "frames_zip": {"count": man["count"], "sha256": man["zip_sha256"]},
                "temporal": {"scene_changes": prof["scene_changes"][:10], "repeated": prof["repeated_frames"][:10], "method": prof["method"],
                             "scores": prof["scores"]},
                "contact_sheet": str(sheet_p), "av_timing": timing,
                "compare": {k: cmp_[k] for k in ("mse", "psnr_db", "ssim", "histogram_correlation", "max_abs_diff_8bit")}, "compare_diff": str(diff_p),
                "render_video": {k: rv["conversion"].get(k) for k in ("frames_out", "source_frames", "frames_removed", "frames_generated_by_algorithm", "algorithm_note", "lossless_encoding")},
                "sequence": {"frames": seq["probe"]["frame_count"], "record": {k: seq["conversion"].get(k) for k in ("method", "frame_rate", "frames")}}}

    def w_sync(self):
        wb, s = self.wb, self.wb.sync
        # Reference: 4 s at 25 fps with a white flash at 1.60 s and a tone burst at 1.60 s.
        # Second camera: the same scene starting 0.48 s later (cut), so its local times are 0.48 s behind.
        d = self.work / "inputs"
        ref = d / "cam_ref.mkv"
        cam2 = d / "cam_2.mkv"
        src = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi",
               "-i", "anoisesrc=color=white:amplitude=0.02:seed=3", "-f", "lavfi", "-i", "sine=frequency=900:sample_rate=48000",
               "-filter_complex", "[2:a]volume='if(between(t,1.6,1.7),0.9,0)':eval=frame[t];[1:a][t]amix=inputs=2:normalize=0[a];"
               "[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,1.6,1.639)'[v]",
               "-map", "[v]", "-map", "[a]", "-t", "4", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-ar", "48000"]
        subprocess.run(src + [str(ref)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.48", "-i", str(ref), "-c:v", "ffv1", "-c:a", "pcm_s16le", str(cam2)], check=True)
        c = wb.create_case("Multi-camera", "Showcase Examiner", A)
        e1 = wb.ingest_path(c["id"], ref, A)
        e2 = wb.ingest_path(c["id"], cam2, A)
        cam_a = s.create_camera(c["id"], "Camera A (shop)", A, location="Shop entrance")
        cam_b = s.create_camera(c["id"], "Camera B (street)", A, location="Opposite pavement")
        s.assign_camera(e1["id"], cam_a["id"], A)
        s.assign_camera(e2["id"], cam_b["id"], A)
        s.set_reference(e1["id"], A, "2026-03-01T10:15:00+02:00")
        truth = 0.48
        rows = []
        r = s.sync_event(e2["id"], 28, e1["id"], 40, A, label="white flash")
        rows.append(("event (flash: frame 28 ↔ reference frame 40)", r))
        r = s.sync_correlate(e2["id"], e1["id"], A, signal="audio", max_lag_s=2)
        rows.append(("audio cross-correlation", r))
        r = s.sync_correlate(e2["id"], e1["id"], A, signal="visual", max_lag_s=2)
        rows.append(("brightness cross-correlation", r))
        r = s.sync_clock(e2["id"], "2026-03-01T10:15:00.500+02:00", 1.0, A, source="burnt-in clock read at frame 0")
        rows.append(("on-screen clock (reading 10:15:00.5, ±1 s)", r))
        r = s.sync_two_events(e2["id"], (3, 78), e1["id"], (15, 90), A, label="two matched frames")
        rows.append(("two events (frames 3, 78 ↔ 15, 90)", r))
        T, _ = s.global_time(e1["id"], 62)  # 2.48 s: after the flash, so the frames are visible
        grid = s.synchronized_frames(c["id"], T, A)
        grid_p = self.img("w_sync_grid.png")
        shutil.copy(wb.store.derived_path(grid["png"], "png"), grid_p)
        out = []
        ref_off = s.current(e1["id"])["offset_s"]
        for label, r in rows:
            out.append({"method": label, "offset_vs_reference_s": round(r["offset_s"] - ref_off, 4), "truth_s": truth,
                        "error_s": round(r["offset_s"] - ref_off - truth, 4), "uncertainty_s": r["uncertainty_s"],
                        "drift_ppm": r["drift_ppm"], "strength": (r.get("details") or {}).get("strength"),
                        "peak_ratio": (r.get("details") or {}).get("peak_ratio")})
        return {"rows": out, "grid": str(grid_p), "grid_time": grid["case_time_label"], "history": len(s.history(e2["id"])),
                "timeline": [{k: i.get(k) for k in ("filename", "camera", "start_label", "end_label")} for i in s.timeline(c["id"])["items"]]}

    def w_detect(self):
        wb = self.wb
        ev = self.ev["street"]
        run = wb.intel.run(ev["id"], A, plates=True, frame_step=1)
        tracks = wb.intel.tracks(self.case["id"], run_id=run["id"])
        shots = []
        for n in (0, 15, 29):
            p = self.img(f"w_detect_{n}.png")
            p.write_bytes(wb.intel.overlay(ev["id"], n))
            shots.append(str(p))
        csv_bytes, _ = wb.intel.export_tracks(self.case["id"], A, "csv", ev["id"])
        return {"run": {k: run[k] for k in ("params", "models", "stats", "output_hash")}, "overlays": shots,
                "tracks": [{k: t[k] for k in ("number", "cls", "start_frame", "end_frame", "mean_score")} | {"boxes": len(t["boxes"]),
                           "colour": (t["attributes"].get("colour") or {}).get("colour")} for t in tracks],
                "profile": compute.hardware_profile(), "csv_head": csv_bytes.decode()[:400]}

    def w_plates(self):
        wb = self.wb
        from forensic.intel import lpr

        out = {"ocr": {"engine": wb.intel.ocr.id, "version": wb.intel.ocr.version, "available": wb.intel.ocr.available()}}
        cases = []
        for key, country in (("plate_car", "IL"), ("car", "GB"), ("plate_motion", "ANY")):
            ev = self.ev[key]
            run = wb.intel.run(ev["id"], A, plates=True, country=country)
            plates = wb.intel.plates(self.case["id"], evidence_id=ev["id"])
            items = []
            for p in plates[:2]:
                rp = self.img(f"w_plate_{key}_{p['id'][:6]}.png")
                shutil.copy(wb.store.derived_path(p["rectified_sha256"], "png"), rp)
                items.append({"id": p["id"], "best_text": p["best_text"], "confidence": p["best_confidence"], "format": p["format_match"],
                              "status": p["status"], "candidates": [{k: c.get(k) for k in ("text", "confidence", "format", "variant")} for c in p["candidates"][:6]],
                              "rectified": str(rp)})
            cases.append({"input": key, "country": country, "vehicles": run["stats"]["by_class"], "plates": items,
                          "overlay": self._overlay(ev["id"], f"w_plate_{key}.png")})
        # Review: the examiner confirms the synthetic plate and rejects junk.
        reviews = []
        for c in cases:
            for p in c["plates"]:
                if c["input"] == "plate_car" and p["best_text"]:
                    r = wb.intel.review_plate(p["id"], "confirm", A, note="matches the plate as read by the examiner")
                else:
                    r = wb.intel.review_plate(p["id"], "reject", A, note="unreadable at this resolution")
                reviews.append({"input": c["input"], "machine": p["best_text"], "status": r["status"], "reviewed_text": r["reviewed_text"]})
        hits = wb.intel.search(A, case_ids=[self.case["id"]], plate="12*67")
        out.update(cases=cases, reviews=reviews, search={"query": "12*67", "total": hits["total"],
                                                         "results": [{k: h.get(k) for k in ("reviewed_text", "best_text", "status", "matched_on")} for h in hits["results"]]},
                   formats={k: v["description"] if isinstance(v, dict) and "description" in v else str(v)[:120] for k, v in lpr.FORMATS.items()})
        return out

    def _overlay(self, evid, name):
        p = self.img(name)
        p.write_bytes(self.wb.intel.overlay(evid, 0))
        return str(p)

    def w_redaction(self):
        wb = self.wb
        ev = self.ev["street"]
        run = wb.intel.runs(self.case["id"], ev["id"])[-1]
        persons = [t for t in wb.intel.tracks(self.case["id"], run_id=run["id"]) if t["cls"] == "person"]
        regions = wb.redaction.track_regions(ev["id"], [t["id"] for t in persons], method="blur")
        regions.append({"label": "shop sign", "method": "solid", "keyframes": [{"frame": 0, "box": [10, 380, 70, 50]},
                                                                                {"frame": 29, "box": [0, 380, 40, 50]}]})
        doc = wb.redaction.save(ev["id"], "redaction", "Disclosure copy", {"regions": regions, "audio": "mute",
                                                                           "auto": {"faces": True, "plates": True, "method": "pixelate"}}, A)
        first = wb.redaction.render(doc["id"], A, codec="h264")
        ver1 = first["conversion"]["verification"]
        first_shot = save_rgb(u8(wb.source_frame(first["id"], 0)), self.img("w_red_pass1_0.png"))
        # Second pass, as an examiner would after reviewing the first copy: cover the plate zone of every
        # tracked vehicle as well as what the automatic plate finder caught.
        vehicles = [t for t in wb.intel.tracks(self.case["id"], run_id=run["id"]) if t["cls"] in ("car", "truck", "bus", "motorcycle")]
        regions2 = regions + wb.redaction.track_regions(ev["id"], [t["id"] for t in vehicles], method="pixelate", part="plate_zone")
        doc2 = wb.redaction.save(ev["id"], "redaction", "Disclosure copy, second pass", {"regions": regions2, "audio": "mute",
                                 "auto": {"faces": True, "plates": True, "method": "pixelate"}}, A)
        out = wb.redaction.render(doc2["id"], A, codec="h264")
        ver = out["conversion"]["verification"]
        shots = []
        for n in (0, 20):
            shots.append((save_rgb(u8(wb.source_frame(ev["id"], n)), self.img(f"w_red_before_{n}.png")),
                          save_rgb(u8(wb.source_frame(out["id"], n)), self.img(f"w_red_after_{n}.png"))))
        ann = wb.redaction.save(self.ev["plate_car"]["id"], "annotation", "Exhibit", {
            "shapes": [{"type": "rect", "points": [[180, 248], [284, 298]], "color": "#ff3030", "width": 3},
                       {"type": "arrow", "points": [[60, 380], [176, 290]], "color": "#ff3030", "width": 3},
                       {"type": "text", "points": [[20, 400]], "text": "Plate as read: 12-345-67", "color": "#ffffff"}],
            "legend": "Arrow and box mark the registration plate."}, A)
        ex = wb.redaction.render_exhibit(ann["id"], A, exhibit_id="SHOW/1")
        ex_p = self.img("w_exhibit.png")
        shutil.copy(wb.store.derived_path(ex["png"], "png"), ex_p)
        return {"regions": [{"label": r.get("label"), "method": r.get("method"), "keyframes": len(r["keyframes"])} for r in regions2],
                "first_pass": {"verification": {k: v for k, v in ver1.items() if k != "frames"},
                               "flagged_examples": dict(list(ver1["frames"].items())[:3]), "shot": first_shot,
                               "vehicles_added": len(vehicles)},
                "verification": {k: v for k, v in ver.items() if k != "frames"}, "flagged_examples": dict(list(ver["frames"].items())[:3]),
                "record": {k: out["conversion"].get(k) for k in ("codec", "frames", "audio", "frames_removed", "frames_duplicated", "document_hash")},
                "shots": shots, "exhibit": str(ex_p), "original_still_verifies": wb.verify_evidence(ev["id"], A)["ok"]}

    def w_case(self):
        wb, cw = self.wb, self.wb.casework
        c = wb.create_case("Case management", "Showcase Examiner", A, case_number="SHOW-CM", legal_authority="Validation")
        sub = wb.create_case("Sub-case: camera B", "Showcase Examiner", A, case_number="SHOW-CM/B", parent_id=c["id"])
        e1 = wb.ingest_path(c["id"], Path(self.items["building"]["path"]), A)
        e2 = wb.ingest_path(c["id"], Path(self.items["blocky"]["path"]), A)
        e3 = wb.ingest_path(c["id"], Path(self.items["street"]["path"]), A)
        cw.set_tags(e3["id"], ["cctv", "high street"], A)
        cw.add_bookmark(e3["id"], 12, "Pedestrian enters", A, end_frame=20, note="left of frame")
        cw.record_custody(e1["id"], "transferred", A, from_party="Lab", to_party="Court clerk", location="Court 3")
        dups = cw.duplicates(c["id"], actor=A)
        found = cw.search("pedestrian", A)
        pkg, info = cw.export_package(c["id"], A, evidence_ids=[e1["id"], e3["id"]], include_report=True, purpose="Disclosure", recipient="Defence")
        check = cw.verify_package(pkg)
        bad = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(pkg)) as zin, zipfile.ZipFile(bad, "w") as zout:
            for n in zin.namelist():
                d = zin.read(n)
                zout.writestr(n, d[:-1] + b"X" if n.startswith("evidence/") else d)
        bad_check = cw.verify_package(bad.getvalue())
        pkg2, info2 = cw.export_package(c["id"], A, evidence_ids=[e1["id"], e3["id"]], include_report=False, purpose="Disclosure", recipient="Defence")
        pkg3, info3 = cw.export_package(c["id"], A, evidence_ids=[e1["id"], e3["id"]], include_report=False, purpose="Disclosure", recipient="Defence")
        cw.set_retention(c["id"], "2000-01-01", A)
        cw.set_legal_hold(c["id"], True, "Appeal pending", A)
        try:
            cw.dispose_case(c["id"], A, reason="test", confirm="SHOW-CM", role="admin")
            hold_blocks = False
        except ValueError as e:
            hold_blocks = str(e)
        cw.set_legal_hold(c["id"], False, "", A)
        try:
            cw.dispose_case(c["id"], A, reason="test", confirm="SHOW-CM", role="examiner")
            role_blocks = False
        except PermissionError as e:
            role_blocks = str(e)
        return {"subcases": [s["case_number"] for s in cw.subcases(c["id"])], "sub": sub["case_number"],
                "custody": [{k: e[k] for k in ("event", "actor", "from_party", "to_party")} for e in cw.custody(c["id"])][:12],
                "duplicates": {"exact": len(dups["exact"]), "near": [{"a": n["a"]["filename"], "b": n["b"]["filename"], "distance": n["distance"]} for n in dups["near"]]},
                "search": {"query": "pedestrian", "hits": [(h["filename"], h["matched"]) for h in found["results"]]},
                "export": {"package_sha256": info["package_sha256"], "files": [{k: f[k] for k in ("path", "kind", "size", "sha256")} for f in info["manifest"]["files"]],
                           "verify": check, "tampered_verify": bad_check,
                           "members_identical": [f["sha256"] for f in info2["manifest"]["files"]] == [f["sha256"] for f in info3["manifest"]["files"]],
                           "package_hashes_differ_only_by_manifest_time": info2["package_sha256"] != info3["package_sha256"]},
                "legal_hold_blocks_disposal": hold_blocks, "non_admin_blocked": role_blocks}

    def w_batch(self):
        wb = self.wb
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tests.test_dvr_formats import N, _access_units, _encode, build_dhav, build_hxvs, build_xm

        d = self.work / "batch_in"
        (d / "NVR" / "ch01").mkdir(parents=True)
        es = _encode(self.work / "es.h264", "libx264", "h264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p")
        aus = _access_units(es, "h264")
        (d / "NVR" / "ch01" / "20260301_101500.dav").write_bytes(build_dhav(aus, [i * 40.0 for i in range(N)]))
        (d / "NVR" / "ch01" / "copy.dav").write_bytes((d / "NVR" / "ch01" / "20260301_101500.dav").read_bytes())
        (d / "NVR" / "cam2_xm.h264").write_bytes(build_xm(aus))
        (d / "NVR" / "sd_card.264").write_bytes(build_hxvs(aus, [i * 40.0 for i in range(N)]))
        shutil.copy(self.items["street"]["path"], d / "phone_clip.mp4")
        (d / "Player.exe").write_bytes(b"MZ" + os.urandom(4000))
        (d / "notes.txt").write_text("export log")
        (d / "damaged.bin").write_bytes(os.urandom(6000))
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in d.rglob("*") if p.is_file()}
        outd = self.work / "batch_out"
        b = wb.batch.create("DVR export", A, input_dir=str(d), output_dir=str(outd), output="copy")
        wb.batch.start(b["id"], A, wait=True)
        b = wb.batch.get(b["id"])
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in d.rglob("*") if p.is_file()}
        mj = (outd / "manifest.json").read_bytes()
        return {"profile": b["profile"], "concurrency": b["concurrency"], "counts": b["counts"],
                "items": [{"input": str(Path(i["input_path"]).relative_to(d)), "detected": (i["detected"] or {}).get("label"),
                           "status": i["status"], "method": i["mode"], "outputs": [(o["file"], o["frames"]) for o in i["outputs"] or []],
                           "input_frames": (i["checks"] or {}).get("input_frames"), "frame_check": (i["checks"] or {}).get("frame_count_ok"),
                           "error_class": i["error_class"], "error": (i["error"] or "")[:140]} for i in b["items"]],
                "inputs_unchanged": before == after, "manifest_sha256": hashlib.sha256(mj).hexdigest(),
                "manifest_sidecar_ok": (outd / "manifest.json.sha256").read_text().split()[0] == hashlib.sha256(mj).hexdigest(),
                "csv_rows": len((outd / "manifest.csv").read_text().strip().splitlines()) - 1}

    def w_cli(self):
        from forensic import cli

        data = str(self.work / "data")
        os.environ["LUCIDA_BACKUP_PASSWORD"] = "showcase backup password"
        out = self.work / "backup.lucida"
        rc_b = cli.main(["--data-dir", data, "--examiner", "Showcase Examiner", "backup", "--output", str(out), "--encrypt"])
        rc_again = cli.main(["--data-dir", data, "backup", "--output", str(out), "--encrypt"])
        rc_r = cli.main(["--examiner", "Showcase Examiner", "restore", str(out), "--target", str(self.work / "restored")])
        os.environ["LUCIDA_BACKUP_PASSWORD"] = "wrong password!!!"
        rc_wrong = cli.main(["restore", str(out), "--target", str(self.work / "restored2")])
        rc_verify = cli.main(["--data-dir", str(self.work / "restored"), "verify"])
        return {"backup_exit": rc_b, "overwrite_refused_exit": rc_again, "restore_exit": rc_r, "wrong_password_exit": rc_wrong,
                "verify_restored_exit": rc_verify, "backup_bytes": out.stat().st_size,
                "backup_sha256": (Path(str(out) + ".sha256")).read_text().split()[0]}

    def w_profiles(self):
        return {"this_machine": compute.hardware_profile(), "profiles": compute.PROFILES,
                "cpu_threads": compute.cpu_threads(), "ram_gb": round(compute.physical_memory() / 2 ** 30, 1)}

    def run(self):
        self.filters()
        self.analyses()
        for name, fn in [("vertical_slice", self.w_vertical), ("cli", self.w_cli), ("tamper", self.w_tamper), ("case_management", self.w_case),
                         ("video", self.w_video), ("sync", self.w_sync), ("detection", self.w_detect), ("plates", self.w_plates),
                         ("redaction", self.w_redaction), ("batch", self.w_batch), ("profiles", self.w_profiles)]:
            self.wf(name, fn)
        self.out["environment"] = {"lucida": __version__, "python": sys.version.split()[0], "opencv": cv2.__version__,
                                   "numpy": np.__version__, "ffmpeg": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.split("\n")[0],
                                   "tesseract": self.wb.intel.ocr.version, "machine": compute.hardware_profile()["reason"]}
        self.out["audit_ok"] = self.wb.audit.verify()
        (self.work / "results.json").write_text(json.dumps(self.out, indent=1, default=str))
        return self.out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media", required=True)
    ap.add_argument("--work", required=True)
    a = ap.parse_args()
    Runner(Path(a.media), Path(a.work)).run()


if __name__ == "__main__":
    main()
