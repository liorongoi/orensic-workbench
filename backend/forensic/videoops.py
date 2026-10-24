"""Video operations: clip extraction, frame export, scene and duplicate-frame detection, contact
sheets, image sequence to video, concatenation, processed-video export, similarity and quality
metrics, and audio/video timing information.

Every operation either produces a linked derived evidence item (with a record of exactly what
was done, including whether frames were dropped, duplicated, reordered or generated) or a
content-addressed derived file. Timestamps, cadence and audio are never changed silently: when an
operation must change them, the record says how.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import zipfile
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import media
from .audit import Actor
from .db import canonical_json, now_iso
from .formats import _redact
from .graph import Engine, frame_of
from .media import MediaError

if TYPE_CHECKING:  # pragma: no cover
    from .workbench import Workbench

CLIP_MODES = {
    "copy": "Stream copy: the compressed frames are copied bit for bit. The clip must start on a keyframe, "
            "so the start moves back to the keyframe at or before the requested frame.",
    "lossless": "Frame-exact, lossless: the requested frames are decoded and re-encoded with FFV1 "
                "(mathematically lossless intra-frame coding) in Matroska; pixels equal the decoded original.",
    "h264": "Frame-exact, lossy: re-encoded with H.264 (x264, CRF 12, yuv420p) for playback compatibility. "
            "Pixels are close to, but not identical with, the original.",
}
VIDEO_CODECS = {
    "ffv1": (["-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "bgr0"], "mkv", True),
    "h264": (["-c:v", "libx264", "-crf", "12", "-preset", "medium", "-pix_fmt", "yuv420p"], "mp4", False),
}


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, check=False)
    if p.returncode != 0:
        raise MediaError(p.stderr.decode(errors="replace")[-1500:])
    return p.stderr.decode(errors="replace")


def _rate(probe: dict) -> Fraction:
    try:
        r = Fraction(probe.get("frame_rate") or "0")
    except (ValueError, ZeroDivisionError):
        r = Fraction(0)
    return r if r > 0 else Fraction(25)


def has_audio(probe: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe.get("streams", []))


class VideoOps:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.settings = wb.settings

    def _video(self, evidence_id: str) -> tuple[dict, dict, Path]:
        row = self.wb._evidence_row(evidence_id)
        if row["media_type"] != "video":
            raise ValueError("this operation needs a video evidence item")
        return row, json.loads(row["probe_json"]), Path(row["stored_path"])

    # Clips ---------------------------------------------------------------------------------------

    def extract_clip(self, evidence_id: str, start: int, end: int, actor: Actor, mode: str = "lossless",
                     audio: bool = True) -> dict:
        if mode not in CLIP_MODES:
            raise ValueError(f"mode must be one of {', '.join(CLIP_MODES)}")
        row, probe, path = self._video(evidence_id)
        last = probe["frame_count"] - 1
        if not 0 <= start <= end <= last:
            raise ValueError(f"frames must satisfy 0 <= start <= end <= {last}")
        table = self.wb.frame_table(evidence_id)
        times = [f["time"] for f in table]
        t0 = times[start] if start < len(times) and times[start] is not None else start / float(_rate(probe))
        t_end = times[end] if end < len(times) and times[end] is not None else end / float(_rate(probe))
        frame_dur = 1 / float(_rate(probe))
        record: dict = {"method": f"clip:{mode}", "description": CLIP_MODES[mode], "requested": {"start": start, "end": end},
                        "source_sha256": row["sha256"], "ffmpeg": media.ffmpeg_version(self.settings.ffmpeg)}
        keep_audio = audio and has_audio(probe)
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            if mode == "copy":
                key = max([f["n"] for f in table if f["key"] and f["n"] <= start] or [0])
                count = end - key + 1
                tk = times[key] if times[key] is not None else 0.0
                dest = Path(tmp) / "clip.mkv"
                cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{tk:.6f}", "-i", str(path),
                       "-map", "0:v:0", *(["-map", "0:a?"] if keep_audio else []), "-c", "copy",
                       "-frames:v", str(count), "-avoid_negative_ts", "make_zero", str(dest)]
                _run(cmd)
                record["actual"] = {"start": key, "end": end}
                record["start_moved_to_keyframe"] = key != start
            else:
                vargs, ext, lossless = VIDEO_CODECS["ffv1" if mode == "lossless" else "h264"]
                dest = Path(tmp) / f"clip.{ext}"
                vf = f"select=between(n\\,{start}\\,{end}),setpts=PTS-STARTPTS"
                cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(path), "-map", "0:v:0",
                       "-vf", vf, "-fps_mode", "passthrough", *vargs]
                if keep_audio:
                    cmd += ["-map", "0:a:0", "-af", f"atrim=start={t0:.6f}:end={t_end + frame_dur:.6f},asetpts=PTS-STARTPTS",
                            "-c:a", "flac" if lossless else "aac"]
                cmd += [str(dest)]
                _run(cmd)
                record["actual"] = {"start": start, "end": end}
            out_probe = media.probe(dest, self.settings)
            expected = record["actual"]["end"] - record["actual"]["start"] + 1
            record["frame_check"] = {"expected": expected, "found": out_probe["frame_count"],
                                     "ok": out_probe["frame_count"] == expected}
            record["timing"] = {"source_start_time": t0 if mode != "copy" else times[record["actual"]["start"]],
                                "note": "Clip time 0 corresponds to the source time given here; frame cadence unchanged."}
            record["frames_removed_inside_range"] = 0
            record["frames_duplicated"] = 0
            record["frames_interpolated"] = 0
            record["audio"] = ("kept" if keep_audio else "source has no audio" if not has_audio(probe) else "removed on request")
            record["command"] = _redact(cmd, path, dest)
            with open(dest, "rb") as f:
                stored = self.wb.store.ingest(f, dest.name)
        name = f"{Path(row['filename']).stem} frames {record['actual']['start']}-{end} ({mode}){dest.suffix}"
        record["output_sha256"] = stored["sha256"]
        return self.wb._insert_evidence(row["case_id"], name, stored, out_probe, actor, derived_from=evidence_id,
                                        conversion=record)

    # Frames ---------------------------------------------------------------------------------------

    def frame_list(self, probe: dict, frames: list[int] | None, start: int | None, end: int | None,
                   step: int | None) -> list[int]:
        last = probe["frame_count"] - 1
        if frames:
            out = sorted({int(f) for f in frames})
        else:
            s, e, st = int(start or 0), int(last if end is None else end), max(1, int(step or 1))
            out = list(range(s, e + 1, st))
        if not out or out[0] < 0 or out[-1] > last:
            raise ValueError(f"frames must be within 0-{last}")
        if len(out) > 2000:
            raise ValueError("at most 2000 frames per export")
        return out

    def export_frames(self, evidence_id: str, actor: Actor, *, frames: list[int] | None = None, start: int | None = None,
                      end: int | None = None, step: int | None = None, chain_id: str | None = None) -> tuple[bytes, dict]:
        """Lossless PNG frames in a ZIP with a manifest (frame number, source time, PNG hash)."""
        row, probe, path = self._video(evidence_id)
        sel = self.frame_list(probe, frames, start, end, step)
        times = {f["n"]: f["time"] for f in self.wb.frame_table(evidence_id)}
        entries, files = [], []
        graph = self.wb.get_chain(chain_id)["graph"] if chain_id else None
        for n in sel:
            if graph:
                g = dict(graph, source={**graph["source"], "frames": {"start": n, "end": n}})
                img = frame_of(self.wb.engine.run(g).data)
            else:
                img = self.wb.source_frame(evidence_id, n)
            png = media.encode_png(img)
            name = f"frame_{n:06d}.png"
            files.append((name, png))
            entries.append({"file": name, "frame": n, "time": times.get(n), "sha256": hashlib.sha256(png).hexdigest()})
        manifest = {"evidence_id": evidence_id, "source_sha256": row["sha256"], "filename": row["filename"],
                    "processed_by_chain": chain_id, "frames": entries, "created_at": now_iso(), "created_by": actor.name,
                    "format": "PNG, 8-bit RGB, lossless"}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in files:
                z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
            z.writestr(zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0)),
                       json.dumps(manifest, indent=2).encode())
        data = buf.getvalue()
        digest, _ = self.wb.store.put_derived(data, "zip")
        self.wb.audit.append(actor, "video.export_frames", case_id=row["case_id"], target=evidence_id,
                             params={"frames": sel[:200], "count": len(sel), "chain_id": chain_id}, output_hash=digest)
        return data, {"zip_sha256": digest, "count": len(sel), "manifest": manifest}

    # Scene changes and duplicated frames -------------------------------------------------------------

    def _luma_stream(self, path: Path, width: int = 160):
        """Decode every frame as small greyscale images (aspect kept, even height)."""
        probe = media.probe(path, self.settings)
        h = max(2, int(round(probe["height"] * width / probe["width"] / 2)) * 2)
        cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
               "-vf", f"scale={width}:{h}:flags=area", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        out = subprocess.run(cmd, capture_output=True, check=False)
        if out.returncode != 0:
            raise MediaError(out.stderr.decode(errors="replace")[-1000:])
        frames = np.frombuffer(out.stdout, np.uint8)
        return frames.reshape(-1, h, width)

    def temporal_profile(self, evidence_id: str, threshold: float = 0.35, duplicate_tolerance: float = 0.5,
                         actor: Actor | None = None) -> dict:
        """Per-frame change scores. Scene changes: the histogram distance (half the L1 distance of
        64-bin grey histograms, 0-1) between consecutive frames exceeds `threshold`. Repeated
        frames: mean absolute difference below `duplicate_tolerance` grey levels."""
        row, probe, path = self._video(evidence_id)
        g = self._luma_stream(path)
        times = {f["n"]: f["time"] for f in self.wb.frame_table(evidence_id)}
        hist = np.stack([np.histogram(f, bins=64, range=(0, 256))[0] for f in g]).astype(np.float64)
        hist /= hist.sum(axis=1, keepdims=True)
        scores = [0.0] + [float(0.5 * np.abs(hist[i] - hist[i - 1]).sum()) for i in range(1, len(g))]
        mad = [0.0] + [float(np.mean(np.abs(g[i].astype(np.int16) - g[i - 1]))) for i in range(1, len(g))]
        cuts = [{"frame": i, "time": times.get(i), "score": round(s, 4)} for i, s in enumerate(scores) if i and s > threshold]
        dups = [{"frame": i, "time": times.get(i), "mean_abs_diff": round(m, 3)} for i, m in enumerate(mad)
                if i and m < duplicate_tolerance]
        res = {"frames": len(g), "threshold": threshold, "duplicate_tolerance": duplicate_tolerance,
               "scene_changes": cuts, "repeated_frames": dups,
               "scores": [round(s, 4) for s in scores], "mean_abs_diff": [round(m, 3) for m in mad],
               "method": "64-bin grey histogram distance and mean absolute difference on 160-px-wide luma",
               "limitations": "Fast pans, flashes and lighting changes also score high; static scenes produce "
                              "repeated frames legitimately. Review each flagged frame."}
        if actor:
            self.wb.audit.append(actor, "video.temporal_profile", case_id=row["case_id"], target=evidence_id,
                                 params={"threshold": threshold, "scene_changes": len(cuts), "repeated": len(dups)})
        return res

    # Contact sheet ------------------------------------------------------------------------------------

    def contact_sheet(self, evidence_id: str, actor: Actor, *, frames: list[int] | None = None, count: int = 12,
                      columns: int = 4, thumb_width: int = 320) -> dict:
        row, probe, path = self._video(evidence_id)
        if not frames:
            n = probe["frame_count"]
            frames = sorted({int(round(i * (n - 1) / max(1, count - 1))) for i in range(count)})
        frames = self.frame_list(probe, frames, None, None, None)
        times = {f["n"]: f["time"] for f in self.wb.frame_table(evidence_id)}
        th = int(round(probe["height"] * thumb_width / probe["width"]))
        rows_n = (len(frames) + columns - 1) // columns
        label_h = 22
        sheet = np.full((rows_n * (th + label_h) + 8, columns * thumb_width + (columns + 1) * 4, 3), 24, np.uint8)
        for i, n in enumerate(frames):
            img = media.to_uint8(self.wb.source_frame(evidence_id, n))
            img = cv2.resize(img, (thumb_width, th), interpolation=cv2.INTER_AREA)
            r, c = divmod(i, columns)
            y, x = r * (th + label_h) + 4, c * (thumb_width + 4) + 4
            sheet[y:y + th, x:x + thumb_width] = img
            t = times.get(n)
            label = f"#{n}" + (f"  t={t:.3f}s" if t is not None else "")
            cv2.putText(sheet, label, (x + 2, y + th + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (230, 230, 230), 1, cv2.LINE_AA)
        png = media.encode_png(sheet.astype(np.float32) / 255.0)
        digest, _ = self.wb.store.put_derived(png, "png")
        self.wb.audit.append(actor, "video.contact_sheet", case_id=row["case_id"], target=evidence_id,
                             params={"frames": frames}, output_hash=digest)
        return {"png": digest, "frames": frames}

    # Image sequence -> video, concatenation ----------------------------------------------------------------

    def sequence_to_video(self, case_id: str, evidence_ids: list[str], frame_rate: float, actor: Actor,
                          codec: str = "ffv1", name: str = "sequence") -> dict:
        """Still images in the given order become one frame each at a stated constant rate. No frame is
        interpolated or dropped; images of another size are refused rather than resized."""
        if codec not in VIDEO_CODECS:
            raise ValueError(f"codec must be one of {', '.join(VIDEO_CODECS)}")
        if not 0 < frame_rate <= 240:
            raise ValueError("frame rate must be between 0 and 240")
        if len(evidence_ids) < 2:
            raise ValueError("choose at least two images")
        rows = [self.wb._evidence_row(e) for e in evidence_ids]
        if any(r["case_id"] != case_id or r["media_type"] != "image" for r in rows):
            raise ValueError("all items must be still images in this case")
        imgs = [media.decode_image(Path(r["stored_path"])) for r in rows]
        shape = imgs[0].shape
        if any(i.shape != shape for i in imgs):
            raise ValueError("all images must have the same dimensions (resize them in a chain first)")
        vargs, ext, _ = VIDEO_CODECS[codec]
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            dest = Path(tmp) / f"sequence.{ext}"
            cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{shape[1]}x{shape[0]}", "-r", f"{frame_rate:g}", "-i", "pipe:0", *vargs,
                   *(["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"] if codec == "h264" else []), str(dest)]
            raw = b"".join(media.to_uint8(i).tobytes() for i in imgs)
            p = subprocess.run(cmd, input=raw, capture_output=True, check=False)
            if p.returncode != 0:
                raise MediaError(p.stderr.decode(errors="replace")[-1000:])
            out_probe = media.probe(dest, self.settings)
            with open(dest, "rb") as f:
                stored = self.wb.store.ingest(f, dest.name)
        record = {"method": f"sequence:{codec}", "description": "Image sequence to video, one image per frame",
                  "inputs": [{"evidence_id": r["id"], "sha256": r["sha256"], "filename": r["filename"]} for r in rows],
                  "frame_rate": frame_rate, "frames_in": len(rows), "frames_out": out_probe["frame_count"],
                  "frames_interpolated": 0, "frames_dropped": 0, "lossless": codec == "ffv1",
                  "timestamps": f"synthetic: frame n at n/{frame_rate:g} s", "command": _redact(cmd, Path("pipe:0"), dest),
                  "output_sha256": stored["sha256"]}
        return self.wb._insert_evidence(case_id, f"{name}.{ext}", stored, out_probe, actor, derived_from=rows[0]["id"],
                                        conversion=record)

    @staticmethod
    def compatibility(probes: list[dict]) -> dict:
        keys = ("codec", "width", "height", "pix_fmt", "frame_rate")
        diffs = {k: sorted({str(p.get(k)) for p in probes}) for k in keys}
        problems = [f"{k} differs: {', '.join(v)}" for k, v in diffs.items() if len(v) > 1]
        return {"stream_copy_compatible": not problems, "problems": problems}

    def concatenate(self, case_id: str, evidence_ids: list[str], actor: Actor, mode: str = "copy") -> dict:
        """Join recordings end to end. Stream copy is allowed only when codec, size, pixel format and
        rate all match; otherwise `mode='lossless'` re-encodes each to FFV1 first (documented)."""
        if len(evidence_ids) < 2:
            raise ValueError("choose at least two videos")
        rows = [self.wb._evidence_row(e) for e in evidence_ids]
        if any(r["case_id"] != case_id or r["media_type"] != "video" for r in rows):
            raise ValueError("all items must be videos in this case")
        probes = [json.loads(r["probe_json"]) for r in rows]
        comp = self.compatibility(probes)
        if mode == "copy" and not comp["stream_copy_compatible"]:
            raise ValueError("these recordings cannot be stream-copied together: " + "; ".join(comp["problems"]))
        if mode == "lossless" and len({(p["width"], p["height"]) for p in probes}) > 1:
            raise ValueError("recordings of different sizes cannot be joined without resizing; resize them first")
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            lst = Path(tmp) / "list.txt"
            lst.write_text("".join(f"file '{r['stored_path']}'\n" for r in rows))
            ext = "mkv"
            dest = Path(tmp) / f"joined.{ext}"
            if mode == "copy":
                cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                       "-map", "0:v:0", "-c", "copy", str(dest)]
            else:
                inputs = []
                for r in rows:
                    inputs += ["-i", r["stored_path"]]
                fc = "".join(f"[{i}:v:0]" for i in range(len(rows))) + f"concat=n={len(rows)}:v=1:a=0[v]"
                cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", *inputs, "-filter_complex", fc,
                       "-map", "[v]", "-fps_mode", "passthrough", *VIDEO_CODECS["ffv1"][0], str(dest)]
            _run(cmd)
            out_probe = media.probe(dest, self.settings)
            with open(dest, "rb") as f:
                stored = self.wb.store.ingest(f, dest.name)
        expected = sum(p["frame_count"] for p in probes)
        record = {"method": f"concat:{mode}", "description": "Recordings joined end to end in the order given",
                  "inputs": [{"evidence_id": r["id"], "sha256": r["sha256"], "frames": p["frame_count"]}
                             for r, p in zip(rows, probes)],
                  "compatibility": comp, "frame_check": {"expected": expected, "found": out_probe["frame_count"],
                                                         "ok": expected == out_probe["frame_count"]},
                  "timestamps": "each later recording follows the previous one; gaps between recordings are not represented",
                  "audio": "not included", "command": " ".join(c for c in cmd if not c.startswith("/")),
                  "output_sha256": stored["sha256"]}
        return self.wb._insert_evidence(case_id, f"joined ({len(rows)} items).{ext}", stored, out_probe, actor,
                                        derived_from=rows[0]["id"], conversion=record)

    # Processed video ------------------------------------------------------------------------------------

    def render_chain_video(self, chain_id: str, actor: Actor, codec: str = "ffv1") -> dict:
        """Encode a chain's processed frame range as a video, at the source frame rate.

        The record states how many frames went in and came out and whether any were generated
        by an algorithm (multi-frame filters such as integration or super resolution produce
        frames that are combinations of several source frames)."""
        if codec not in VIDEO_CODECS:
            raise ValueError(f"codec must be one of {', '.join(VIDEO_CODECS)}")
        ch = self.wb.get_chain(chain_id)
        row, probe, path = self._video(ch["evidence_id"])
        fr = ch["graph"]["source"].get("frames") or {"start": 0, "end": 0}
        n_in = min(fr["end"], probe["frame_count"] - 1) - fr["start"] + 1
        res = Engine(self.wb._load_source).run(ch["graph"])
        stack = res.data if res.is_stack else res.data[None]
        from .filters import get_filter

        stack_steps = [n["filter"] for n in ch["graph"]["nodes"] if n["enabled"] and get_filter(n["filter"]).input_kind == "stack"]
        generative = [n["filter"] for n in ch["graph"]["nodes"] if n["enabled"] and get_filter(n["filter"]).generative]
        rate = _rate(probe)
        vargs, ext, lossless = VIDEO_CODECS[codec]
        h, w = stack.shape[1:3]
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            dest = Path(tmp) / f"processed.{ext}"
            cmd = [self.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{w}x{h}", "-r", str(rate), "-i", "pipe:0", *vargs,
                   *(["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"] if codec == "h264" else []), str(dest)]
            raw = b"".join(media.to_uint8(f).tobytes() for f in stack)
            p = subprocess.run(cmd, input=raw, capture_output=True, check=False)
            if p.returncode != 0:
                raise MediaError(p.stderr.decode(errors="replace")[-1000:])
            out_probe = media.probe(dest, self.settings)
            with open(dest, "rb") as f:
                stored = self.wb.store.ingest(f, dest.name)
        n_out = stack.shape[0]
        record = {"method": f"processed:{codec}", "description": "Chain output encoded as video",
                  "chain_id": chain_id, "graph_hash": res.graph_hash, "output_pixel_hash": res.output_hash,
                  "source_frames": {"start": fr["start"], "end": fr["start"] + n_in - 1, "count": n_in},
                  "frames_out": n_out, "frame_rate": str(rate),
                  "frames_removed": max(0, n_in - n_out) if not stack_steps else None,
                  "frames_duplicated": 0, "frames_reordered": 0, "frames_interpolated": 0,
                  "frames_generated_by_algorithm": bool(stack_steps or generative),
                  "algorithm_note": ("multi-frame steps " + ", ".join(stack_steps) + " combine several source frames per output frame")
                  if stack_steps else "each output frame is the processed version of exactly one source frame",
                  "generative_steps": generative, "lossless_encoding": lossless,
                  "timestamps": f"constant rate {rate}; source time of output frame 0 = source frame {fr['start']}",
                  "audio": "not included", "command": _redact(cmd, Path("pipe:0"), dest), "output_sha256": stored["sha256"]}
        return self.wb._insert_evidence(row["case_id"], f"{ch['name']} (processed).{ext}", stored, out_probe, actor,
                                        derived_from=row["id"], conversion=record)

    # Similarity and quality ------------------------------------------------------------------------------

    def _image(self, ref: dict) -> np.ndarray:
        if ref.get("chain_id"):
            ch = self.wb.get_chain(ref["chain_id"])
            g = ch["graph"]
            if ref.get("frame") is not None and g["source"].get("frames") is not None:
                g = dict(g, source={**g["source"], "frames": {"start": int(ref["frame"]), "end": int(ref["frame"])}})
            return frame_of(self.wb.engine.run(g).data)
        return self.wb.source_frame(ref["evidence_id"], int(ref.get("frame") or 0))

    def compare(self, a: dict, b: dict, actor: Actor, align: bool = False) -> dict:
        """Similarity between two images or frames: MSE, PSNR, SSIM (Wang et al. 2004, Gaussian
        window sigma 1.5 on luma), histogram correlation, and an absolute-difference map. With
        `align`, b is first registered to a by ECC (affine) and the transform is reported."""
        ia, ib = self._image(a), self._image(b)
        note = None
        warp = None
        if ia.shape != ib.shape:
            ib = cv2.resize(ib, (ia.shape[1], ia.shape[0]), interpolation=cv2.INTER_AREA)
            note = "b was resized to a's dimensions (INTER_AREA) before comparison"
        if align:
            ga = cv2.cvtColor(ia, cv2.COLOR_RGB2GRAY)
            gb = cv2.cvtColor(ib, cv2.COLOR_RGB2GRAY)
            warp = np.eye(2, 3, dtype=np.float32)
            try:
                _, warp = cv2.findTransformECC(ga, gb, warp, cv2.MOTION_AFFINE,
                                               (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), None, 5)
                ib = cv2.warpAffine(ib, warp, (ia.shape[1], ia.shape[0]), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
            except cv2.error:
                note = (note + "; " if note else "") + "alignment did not converge; compared unaligned"
                warp = None
        m = similarity(ia, ib)
        diff = np.abs(ia - ib)
        dm = np.clip(diff.max(axis=2) * 4.0, 0, 1)
        heat = cv2.applyColorMap((dm * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)[..., ::-1].astype(np.float32) / 255
        digest, _ = self.wb.store.put_derived(media.encode_png(heat), "png")
        out = {**m, "difference_map": digest, "difference_gain": 4.0, "note": note,
               "alignment": warp.tolist() if warp is not None else None}
        case_id = self.wb.get_evidence(a.get("evidence_id") or self.wb.get_chain(a["chain_id"])["evidence_id"])["case_id"]
        self.wb.audit.append(actor, "image.compare", case_id=case_id, params={"a": a, "b": b, "align": align, **m},
                             output_hash=digest)
        return out

    def quality(self, ref: dict) -> dict:
        return quality_metrics(self._image(ref))

    # Audio / video timing ------------------------------------------------------------------------------------

    def av_timing(self, evidence_id: str) -> dict:
        """Start times and durations of every stream from the container, and the audio-video start offset.

        This reports what the container declares; it cannot prove lip-sync. Compare a visible
        sound-producing event against the audio to check synchronisation."""
        row, probe, _ = self._video(evidence_id)
        streams = []
        for s in probe.get("streams", []):
            streams.append({"index": s.get("index"), "type": s.get("codec_type"), "codec": s.get("codec_name"),
                            "start_time": _f(s.get("start_time")), "duration": _f(s.get("duration")),
                            "time_base": s.get("time_base"), "sample_rate": s.get("sample_rate"),
                            "channels": s.get("channels"), "channel_layout": s.get("channel_layout")})
        v = next((s for s in streams if s["type"] == "video"), None)
        a = next((s for s in streams if s["type"] == "audio"), None)
        offset = (a["start_time"] - v["start_time"]) if a and v and a["start_time"] is not None and v["start_time"] is not None else None
        return {"streams": streams, "audio_minus_video_start_s": offset,
                "note": "Declared start-time offset only. A positive value means audio starts later than video."}


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    c1, c2 = (0.01) ** 2, (0.03) ** 2
    k = (11, 11)
    mu_a, mu_b = cv2.GaussianBlur(a, k, 1.5), cv2.GaussianBlur(b, k, 1.5)
    saa = cv2.GaussianBlur(a * a, k, 1.5) - mu_a ** 2
    sbb = cv2.GaussianBlur(b * b, k, 1.5) - mu_b ** 2
    sab = cv2.GaussianBlur(a * b, k, 1.5) - mu_a * mu_b
    s = ((2 * mu_a * mu_b + c1) * (2 * sab + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (saa + sbb + c2))
    return float(s.mean())


def similarity(a: np.ndarray, b: np.ndarray) -> dict:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    mse = float(np.mean((a - b) ** 2))
    psnr = float("inf") if mse == 0 else float(10 * np.log10(1.0 / mse))
    la = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    lb = 0.299 * b[..., 0] + 0.587 * b[..., 1] + 0.114 * b[..., 2]
    ha = cv2.calcHist([(la * 255).astype(np.uint8)], [0], None, [64], [0, 256])
    hb = cv2.calcHist([(lb * 255).astype(np.uint8)], [0], None, [64], [0, 256])
    return {"mse": round(mse, 8), "psnr_db": round(psnr, 3) if np.isfinite(psnr) else None, "identical": mse == 0,
            "ssim": round(_ssim(la, lb), 5), "histogram_correlation": round(float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL)), 5),
            "max_abs_diff_8bit": int(round(float(np.abs(a - b).max()) * 255))}


def quality_metrics(img: np.ndarray) -> dict:
    """No-reference measures used to compare enhancement candidates and judge source quality."""
    g = (0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F, ksize=3)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    # Immerkær (1996) fast noise variance estimate.
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    conv = cv2.filter2D(g, cv2.CV_32F, k)
    h, w = g.shape
    sigma = float(np.sqrt(np.pi / 2) / (6 * (w - 2) * (h - 2)) * np.abs(conv[1:-1, 1:-1]).sum())
    # Blockiness: mean jump across 8-pixel boundaries relative to elsewhere.
    dh = np.abs(np.diff(g, axis=1))
    on = dh[:, 7::8].mean() if dh.shape[1] > 8 else 0.0
    off = np.delete(dh, np.s_[7::8], axis=1).mean() if dh.shape[1] > 8 else 1.0
    return {
        "sharpness_laplacian_var": round(float(lap.var()) * 1e4, 3),
        "tenengrad": round(float(np.mean(gx ** 2 + gy ** 2)) * 1e3, 4),
        "noise_sigma_8bit": round(sigma * 255, 3),
        "rms_contrast": round(float(g.std()), 5),
        "mean_brightness": round(float(g.mean()), 5),
        "clipped_dark_pct": round(float((g <= 1 / 255).mean() * 100), 3),
        "clipped_bright_pct": round(float((g >= 254 / 255).mean() * 100), 3),
        "blockiness_ratio": round(float(on / off) if off > 0 else 0.0, 4),
        "notes": "Laplacian variance and Tenengrad rise with sharpness and also with noise; "
                 "blockiness > 1.1 suggests visible 8x8 JPEG/MPEG blocking.",
    }
