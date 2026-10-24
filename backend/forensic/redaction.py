"""Annotation, redaction and exhibits.

Annotations and redactions are documents kept apart from the pixels: the original is never touched
and nothing is burnt in until the examiner renders an explicitly exported derivative. Rendering a
redaction produces a new, linked evidence item and a verification report that checks:

- every frame is present (frame count in = out);
- every redaction region is actually obscured in the *encoded* output on every frame it covers
  (solid masks are near-uniform; blur and pixelation remove most of the fine detail);
- the face detector (and, when asked, the plate finder) run again on the output find nothing
  that a region does not cover (possible misses);
- tracking-based regions have no long stretches without a box (possible tracking loss);
- the audio is what was asked for (kept, muted to silence, or removed).

The verification is a check, not a guarantee: detectors miss things. The report lists what was
checked so a person can review the result before disclosure.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import media
from .audit import Actor
from .db import canonical_json, new_id, now_iso
from .formats import _redact

if TYPE_CHECKING:  # pragma: no cover
    from .workbench import Workbench

VERSION = "1.0.0"
METHODS = ("blur", "pixelate", "solid")
SHAPES = ("rect", "ellipse")
AUDIO = ("keep", "mute", "remove")
ANNOTATION_TYPES = ("text", "arrow", "line", "rect", "ellipse", "polygon", "circle")
MAX_GAP_FRAMES = 12


def interpolate(keyframes: list[dict], frame: int, hold: bool = False) -> list[float] | None:
    """Box at `frame` by linear interpolation between keyframes; None outside the keyframed span
    (unless `hold`, which keeps the first/last box before/after the span)."""
    if not keyframes:
        return None
    ks = sorted(keyframes, key=lambda k: k["frame"])
    if frame < ks[0]["frame"]:
        return list(ks[0]["box"]) if hold else None
    if frame > ks[-1]["frame"]:
        return list(ks[-1]["box"]) if hold else None
    for a, b in zip(ks, ks[1:]):
        if a["frame"] <= frame <= b["frame"]:
            if b["frame"] == a["frame"]:
                return list(a["box"])
            t = (frame - a["frame"]) / (b["frame"] - a["frame"])
            return [a["box"][i] + t * (b["box"][i] - a["box"][i]) for i in range(4)]
    return list(ks[-1]["box"])


def _clip_box(box, w, h, margin=0.0):
    x, y, bw, bh = box
    x0, y0 = int(np.floor(x - margin * bw)), int(np.floor(y - margin * bh))
    x1, y1 = int(np.ceil(x + bw * (1 + margin))), int(np.ceil(y + bh * (1 + margin)))
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


def obscure(img: np.ndarray, box, method: str, shape: str = "rect", margin: float = 0.0) -> None:
    """Obscure one region of an 8-bit BGR/RGB frame in place."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = _clip_box(box, w, h, margin)
    if x1 <= x0 or y1 <= y0:
        return
    region = img[y0:y1, x0:x1]
    if method == "solid":
        new = np.zeros_like(region)
    elif method == "blur":
        k = max(3, (max(x1 - x0, y1 - y0) // 2) | 1)
        new = cv2.GaussianBlur(region, (k, k), 0, borderType=cv2.BORDER_REPLICATE)
        new = cv2.GaussianBlur(new, (k, k), 0, borderType=cv2.BORDER_REPLICATE)
    else:
        cell = max(6, max(x1 - x0, y1 - y0) // 6)
        small = cv2.resize(region, (max(1, (x1 - x0) // cell), max(1, (y1 - y0) // cell)), interpolation=cv2.INTER_AREA)
        new = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    if shape == "ellipse":
        mask = np.zeros(region.shape[:2], np.uint8)
        cv2.ellipse(mask, ((x1 - x0) / 2, (y1 - y0) / 2), (x1 - x0, y1 - y0), 0, 0, 360, 255, -1)
        region[mask > 0] = new[mask > 0]
    else:
        region[:] = new


def plate_zone(box: list[float]) -> list[float]:
    """The part of a vehicle box [x, y, w, h] where its registration plate normally is."""
    x, y, w, h = box
    return [round(x + 0.15 * w, 1), round(y + 0.55 * h, 1), round(0.7 * w, 1), round(0.45 * h, 1)]


def fine_detail_retained(src_gray: np.ndarray, out_gray: np.ndarray, sigma: float = 1.5) -> float:
    """Correlation (0-1) between the fine detail (high-pass: image minus a Gaussian blur of `sigma`)
    of the source region and of the output region. About 1 when the region is unchanged (even after
    lossy encoding); near 0 when blur, pixelation or a solid mask removed the original detail: the
    new edges that pixelation makes are not correlated with the original texture."""
    s = src_gray.astype(np.float32)
    o = out_gray.astype(np.float32)
    s = s - cv2.GaussianBlur(s, (0, 0), sigma)
    o = o - cv2.GaussianBlur(o, (0, 0), sigma)
    den = float(np.sqrt((s * s).sum() * (o * o).sum()))
    return max(0.0, float((s * o).sum()) / den) if den > 1e-6 else 0.0


FINE_DETAIL_LIMIT = 0.3
CHECK_PLATE_SCORE = 0.6


def _detail(gray: np.ndarray) -> float:
    if gray.size < 16:
        return 0.0
    return float(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F).var())


class Redaction:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.db = wb.db

    # Documents ------------------------------------------------------------------------------------

    def validate(self, kind: str, doc: dict, ev: dict) -> dict:
        last = max(0, int(ev["probe"].get("frame_count") or 1) - 1)
        if kind == "redaction":
            regions = []
            for i, r in enumerate(doc.get("regions") or []):
                method = r.get("method", "pixelate")
                shape = r.get("shape", "rect")
                if method not in METHODS or shape not in SHAPES:
                    raise ValueError(f"region {i + 1}: method must be one of {METHODS}, shape one of {SHAPES}")
                kfs = []
                for k in r.get("keyframes") or []:
                    f = int(k["frame"])
                    b = [float(v) for v in k["box"]]
                    if not 0 <= f <= last or len(b) != 4 or b[2] <= 0 or b[3] <= 0:
                        raise ValueError(f"region {i + 1}: keyframe boxes are [x, y, w, h] on frames 0-{last}")
                    kfs.append({"frame": f, "box": b})
                track = r.get("track_id")
                if not kfs and not track:
                    raise ValueError(f"region {i + 1} needs at least one keyframe or a track")
                regions.append({"id": str(r.get("id") or f"r{i + 1}"), "label": str(r.get("label", ""))[:120],
                                "method": method, "shape": shape, "keyframes": sorted(kfs, key=lambda k: k["frame"]),
                                "track_id": track, "hold": bool(r.get("hold", False)),
                                "margin": float(r.get("margin", 0.1))})
            auto = doc.get("auto") or {}
            out = {"regions": regions, "audio": doc.get("audio", "keep"),
                   "auto": {"faces": bool(auto.get("faces", False)), "plates": bool(auto.get("plates", False)),
                            "min_score": float(auto.get("min_score", 0.6)), "method": auto.get("method", "pixelate"),
                            "margin": float(auto.get("margin", 0.25))}}
            if out["audio"] not in AUDIO:
                raise ValueError(f"audio must be one of {AUDIO}")
            if out["auto"]["method"] not in METHODS:
                raise ValueError(f"auto method must be one of {METHODS}")
            return out
        if kind == "annotation":
            shapes = []
            for i, s in enumerate(doc.get("shapes") or []):
                t = s.get("type")
                if t not in ANNOTATION_TYPES:
                    raise ValueError(f"shape {i + 1}: type must be one of {ANNOTATION_TYPES}")
                pts = [[float(x), float(y)] for x, y in s.get("points") or []]
                if not pts:
                    raise ValueError(f"shape {i + 1} needs points")
                fr = s.get("frames") or [0, last]
                shapes.append({"type": t, "points": pts, "text": str(s.get("text", ""))[:300],
                               "color": s.get("color", "#ffcc00"), "width": int(s.get("width", 2)),
                               "frames": [int(fr[0]), int(fr[1])]})
            return {"shapes": shapes, "show_frame_label": bool(doc.get("show_frame_label", True)),
                    "show_evidence_label": bool(doc.get("show_evidence_label", True)),
                    "legend": str(doc.get("legend", ""))[:500]}
        raise ValueError("kind must be redaction or annotation")

    def save(self, evidence_id: str, kind: str, name: str, doc: dict, actor: Actor, doc_id: str | None = None) -> dict:
        ev = self.wb.get_evidence(evidence_id)
        clean = self.validate(kind, doc, ev)
        h = __import__("hashlib").sha256(canonical_json(clean).encode()).hexdigest()
        ts = now_iso()
        if doc_id:
            old = self.get(doc_id)
            if old["evidence_id"] != evidence_id or old["kind"] != kind:
                raise ValueError("that document belongs to another item")
            self.db.execute("UPDATE annotations SET name = ?, doc_json = ?, doc_hash = ?, updated_at = ? WHERE id = ?",
                            (name.strip() or old["name"], canonical_json(clean), h, ts, doc_id))
        else:
            doc_id = new_id()
            self.db.execute("INSERT INTO annotations (id, case_id, evidence_id, kind, name, doc_json, doc_hash, actor, created_at,"
                            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (doc_id, ev["case_id"], evidence_id, kind, name.strip() or kind, canonical_json(clean), h,
                             actor.label, ts, ts))
        self.wb.audit.append(actor, f"{kind}.save", case_id=ev["case_id"], target=doc_id,
                             params={"evidence_id": evidence_id, "doc_hash": h, "doc": clean})
        return self.get(doc_id)

    def get(self, doc_id: str) -> dict:
        r = self.db.one("SELECT * FROM annotations WHERE id = ?", (doc_id,))
        if not r:
            from .workbench import NotFound

            raise NotFound(f"document {doc_id} not found")
        r["doc"] = json.loads(r.pop("doc_json"))
        return r

    def list(self, case_id: str, evidence_id: str | None = None) -> list[dict]:
        q = "SELECT id FROM annotations WHERE case_id = ?" + (" AND evidence_id = ?" if evidence_id else "") + " ORDER BY created_at"
        return [self.get(r["id"]) for r in self.db.all(q, (case_id, evidence_id) if evidence_id else (case_id,))]

    def track_regions(self, evidence_id: str, track_ids: list[str], method: str = "pixelate", part: str = "whole") -> list[dict]:
        """Redaction regions that follow tracked objects (one keyframe per tracked box).

        part="plate_zone" covers only the band of a vehicle box where a registration plate normally sits
        (middle 70 % of the width, lower 45 % of the height). It is a fixed geometric approximation, not a
        plate detection: the examiner must check the redacted copy, for example when a car is seen side-on.
        """
        if part not in ("whole", "plate_zone"):
            raise ValueError("part must be whole or plate_zone")
        out = []
        for tid in track_ids:
            t = self.wb.intel.get_track(tid)
            if t["evidence_id"] != evidence_id:
                raise ValueError("that track is in another recording")
            boxes = [{"frame": b["frame"], "box": b["box"] if part == "whole" else plate_zone(b["box"])} for b in t["boxes"]]
            label = f"{t['cls']} #{t['number']}" + (" plate zone" if part == "plate_zone" else "")
            out.append({"id": f"track-{t['number']}" + ("-plate" if part == "plate_zone" else ""), "label": label,
                        "method": method, "shape": "rect", "track_id": tid, "keyframes": boxes})
        return out

    # Rendering --------------------------------------------------------------------------------------

    def _regions_at(self, doc: dict, frame: int) -> list[tuple[dict, list[float]]]:
        out = []
        for r in doc["regions"]:
            b = interpolate(r["keyframes"], frame, r["hold"])
            if b is not None:
                out.append((r, b))
        return out

    def _auto_boxes(self, doc: dict, rgb: np.ndarray, plate_score: float = 0.45) -> list[tuple[str, list[float]]]:
        boxes = []
        if doc["auto"]["faces"]:
            from .filters.ai import detect_faces

            for f in detect_faces(rgb.astype(np.float32) / 255, doc["auto"]["min_score"]):
                boxes.append(("face", f["box"]))
        if doc["auto"]["plates"]:
            from .intel import lpr

            for c in lpr.localise(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), max_candidates=4):
                if c.score >= plate_score:
                    xs = [p[0] for p in c.corners]
                    ys = [p[1] for p in c.corners]
                    boxes.append(("plate", [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]))
        return boxes

    def apply(self, doc: dict, rgb: np.ndarray, frame: int) -> tuple[np.ndarray, list[dict]]:
        out = rgb.copy()
        applied = []
        for r, b in self._regions_at(doc, frame):
            obscure(out, b, r["method"], r["shape"], r["margin"])
            applied.append({"region": r["id"], "box": [round(v, 1) for v in b], "method": r["method"], "margin": r["margin"]})
        for kind, b in self._auto_boxes(doc, rgb):
            obscure(out, b, doc["auto"]["method"], "rect", doc["auto"]["margin"])
            applied.append({"region": f"auto-{kind}", "box": [round(v, 1) for v in b], "method": doc["auto"]["method"],
                            "margin": doc["auto"]["margin"]})
        return out, applied

    def render(self, doc_id: str, actor: Actor, codec: str = "h264") -> dict:
        d = self.get(doc_id)
        if d["kind"] != "redaction":
            raise ValueError("use render_exhibit for annotations")
        doc = d["doc"]
        ev = self.wb.get_evidence(d["evidence_id"])
        row = self.wb._evidence_row(ev["id"])
        if ev["media_type"] == "image":
            rgb = media.to_uint8(self.wb.source_frame(ev["id"], 0))
            out, applied = self.apply(doc, rgb, 0)
            png = media.encode_png(out.astype(np.float32) / 255)
            with tempfile.TemporaryDirectory(dir=self.wb.settings.data_dir) as tmp:
                p = Path(tmp) / "redacted.png"
                p.write_bytes(png)
                with open(p, "rb") as f:
                    stored = self.wb.store.ingest(f, p.name)
                info = media.probe(p, self.wb.settings)
            frames_log = {0: applied}
            record = {"method": "redaction", "description": "Redacted disclosure copy", "document_id": doc_id,
                      "document_hash": d["doc_hash"], "version": VERSION, "frames": 1}
        else:
            if codec not in ("h264", "ffv1"):
                raise ValueError("codec must be h264 or ffv1")
            probe = ev["probe"]
            w, h = probe["width"], probe["height"]
            try:
                rate = Fraction(probe.get("frame_rate") or "25")
            except (ValueError, ZeroDivisionError):
                rate = Fraction(25)
            if rate <= 0:
                rate = Fraction(25)
            has_audio = any(s.get("codec_type") == "audio" for s in probe.get("streams", []))
            ext = "mp4" if codec == "h264" else "mkv"
            vargs = (["-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p"] if codec == "h264"
                     else ["-c:v", "ffv1", "-level", "3", "-g", "1"])
            frames_log = {}
            with tempfile.TemporaryDirectory(dir=self.wb.settings.data_dir) as tmp:
                dest = Path(tmp) / f"redacted.{ext}"
                cmd = [self.wb.settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                       "-s", f"{w}x{h}", "-r", str(rate), "-i", "pipe:0"]
                n_frames = int(probe["frame_count"])
                if has_audio and doc["audio"] != "remove":
                    # The audio is cut at the video's duration; every video frame is kept (no -shortest,
                    # which would drop the last frames when the audio track is a little shorter).
                    dur = float(n_frames / rate)
                    cmd += ["-i", row["stored_path"], "-map", "0:v:0", "-map", "1:a:0",
                            "-af", f"atrim=end={dur:.6f}" + (",volume=0" if doc["audio"] == "mute" else "")]
                    cmd += ["-c:a", "aac" if codec == "h264" else "flac"]
                else:
                    cmd += ["-map", "0:v:0"]
                cmd += [*vargs, *(["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"] if codec == "h264" else []),
                        "-frames:v", str(n_frames), str(dest)]
                enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for n, bgr in self.wb.intel.frames(ev["id"], 0, probe["frame_count"] - 1, 1):
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        out, applied = self.apply(doc, rgb, n)
                        frames_log[n] = applied
                        enc.stdin.write(out.tobytes())
                    enc.stdin.close()
                finally:
                    err = enc.stderr.read().decode(errors="replace")
                    code = enc.wait()
                if code != 0:
                    raise media.MediaError(err[-1500:])
                info = media.probe(dest, self.wb.settings)
                verification = self.verify_output(doc, ev, dest, frames_log, codec)
                with open(dest, "rb") as f:
                    stored = self.wb.store.ingest(f, dest.name)
            record = {"method": "redaction", "description": "Redacted disclosure copy", "document_id": doc_id,
                      "document_hash": d["doc_hash"], "version": VERSION, "codec": codec, "frame_rate": str(rate),
                      "frames": len(frames_log), "audio": doc["audio"] if has_audio else "source has no audio",
                      "frames_removed": 0, "frames_duplicated": 0, "frames_interpolated": 0,
                      "command": _redact(cmd, Path(row["stored_path"]), dest), "verification": verification}
        if ev["media_type"] == "image":
            record["verification"] = self.verify_image(doc, ev, stored["path"], frames_log)
        record["regions_per_frame"] = {str(k): v for k, v in list(frames_log.items())[:5000]}
        record["output_sha256"] = stored["sha256"]
        new = self.wb._insert_evidence(ev["case_id"], f"{Path(ev['filename']).stem} (redacted).{Path(stored['path']).suffix.lstrip('.')}",
                                       stored, info, actor, derived_from=ev["id"], conversion=record)
        self.wb.audit.append(actor, "redaction.render", case_id=ev["case_id"], target=new["id"],
                             params={"document_id": doc_id, "document_hash": d["doc_hash"],
                                     "verification": {k: v for k, v in record["verification"].items() if k != "frames"}},
                             output_hash=stored["sha256"])
        return new

    # Verification -------------------------------------------------------------------------------------

    def _check_frame(self, src: np.ndarray, out: np.ndarray, applied: list[dict], doc: dict) -> list[dict]:
        problems = []
        h, w = src.shape[:2]
        gs = cv2.cvtColor(src, cv2.COLOR_RGB2GRAY)
        go = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
        for a in applied:
            x0, y0, x1, y1 = _clip_box(a["box"], w, h, a.get("margin", 0.0))
            # Look at the inner part only: compression blurs edges into the border.
            ix, iy = (x1 - x0) // 6, (y1 - y0) // 6
            rs, ro = gs[y0 + iy:y1 - iy, x0 + ix:x1 - ix], go[y0 + iy:y1 - iy, x0 + ix:x1 - ix]
            if rs.size < 16:
                continue
            if _detail(rs) < 4.0:
                continue  # nothing to hide here (flat area): no measurable detail
            kept = fine_detail_retained(rs, ro)
            if a["method"] == "solid":
                ok = float(ro.std()) < 6 and float(ro.mean()) < 24
            else:
                ok = kept <= FINE_DETAIL_LIMIT
            if not ok:
                problems.append({"region": a["region"], "box": a["box"], "fine_detail_retained": round(kept, 3),
                                 "problem": "region not obscured enough"})
        # Detectors on the output: anything not covered by an applied region is a possible miss.
        covered = [_clip_box(a["box"], w, h, a.get("margin", 0.0)) for a in applied]
        check_doc = {"auto": {"faces": True, "plates": doc["auto"]["plates"], "min_score": 0.7}}
        for kind, b in self._auto_boxes(check_doc, out, plate_score=CHECK_PLATE_SCORE):
            bx0, by0, bx1, by1 = _clip_box(b, w, h)
            area = max(1, (bx1 - bx0) * (by1 - by0))
            cov = max((max(0, min(bx1, c[2]) - max(bx0, c[0])) * max(0, min(by1, c[3]) - max(by0, c[1]))) / area
                      for c in covered) if covered else 0.0
            if cov < 0.8:
                problems.append({"region": None, "box": [round(v, 1) for v in b], "problem": f"possible unredacted {kind}"})
        return problems

    def verify_output(self, doc: dict, ev: dict, dest: Path, frames_log: dict, codec: str) -> dict:
        probe = media.probe(dest, self.wb.settings)
        w, h = ev["probe"]["width"], ev["probe"]["height"]
        cmd = [self.wb.settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(dest), "-map", "0:v:0", "-vf", f"crop={w}:{h}:0:0",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        outp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frame_problems = {}
        try:
            for n, bgr in self.wb.intel.frames(ev["id"], 0, ev["probe"]["frame_count"] - 1, 1):
                buf = outp.stdout.read(w * h * 3)
                if len(buf) < w * h * 3:
                    frame_problems[n] = [{"problem": "frame missing from output"}]
                    break
                out = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                p = self._check_frame(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), out, frames_log.get(n, []), doc)
                if p:
                    frame_problems[n] = p
        finally:
            outp.kill()
            outp.stdout.close()
            outp.wait()
        gaps = []
        for r in doc["regions"]:
            ks = [k["frame"] for k in r["keyframes"]]
            if r.get("track_id"):
                for a, b in zip(ks, ks[1:]):
                    if b - a > MAX_GAP_FRAMES:
                        gaps.append({"region": r["id"], "from": a, "to": b, "problem": "no tracked box for "
                                     f"{b - a} frames: the box was interpolated (possible tracking loss)"})
        audio = self._audio_check(dest, doc["audio"], ev)
        n_in, n_out = ev["probe"]["frame_count"], probe["frame_count"]
        ok = not frame_problems and n_in == n_out and audio["ok"]
        return {"ok": ok, "frames_in": n_in, "frames_out": n_out, "frame_count_ok": n_in == n_out,
                "frames_with_problems": len(frame_problems), "frames": {str(k): v for k, v in list(frame_problems.items())[:500]},
                "tracking_gaps": gaps, "audio": audio, "checked_with": "YuNet faces (score ≥ 0.7)" + (
                    ", plate finder" if doc["auto"]["plates"] else ""),
                "note": "A clean check means the detectors found nothing uncovered, not that nothing sensitive remains. "
                        "Review the redacted copy before release."}

    def verify_image(self, doc: dict, ev: dict, path: Path, frames_log: dict) -> dict:
        src = media.to_uint8(self.wb.source_frame(ev["id"], 0))
        out = media.to_uint8(media.decode_image(Path(path)))
        p = self._check_frame(src, out, frames_log.get(0, []), doc)
        return {"ok": not p, "frames_in": 1, "frames_out": 1, "frame_count_ok": True, "frames_with_problems": int(bool(p)),
                "frames": {"0": p} if p else {}, "tracking_gaps": [], "audio": {"ok": True, "detail": "image"},
                "checked_with": "YuNet faces (score ≥ 0.7)" + (", plate finder" if doc["auto"]["plates"] else ""),
                "note": "A clean check means the detectors found nothing uncovered, not that nothing sensitive remains."}

    def _audio_check(self, dest: Path, mode: str, ev: dict) -> dict:
        src_audio = any(s.get("codec_type") == "audio" for s in ev["probe"].get("streams", []))
        info = media.probe(dest, self.wb.settings)
        out_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        if not src_audio:
            return {"ok": not out_audio, "detail": "source has no audio"}
        if mode == "remove":
            return {"ok": not out_audio, "detail": "audio removed" if not out_audio else "AUDIO STILL PRESENT"}
        if mode == "keep":
            return {"ok": out_audio, "detail": "source audio kept unchanged: listen for names, voices and other "
                    "identifying sound before release"}
        p = subprocess.run([self.wb.settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(dest), "-map", "0:a:0", "-ac", "1",
                            "-f", "s16le", "-"], capture_output=True)
        x = np.frombuffer(p.stdout, np.int16)
        peak = int(np.abs(x).max()) if x.size else 0
        return {"ok": out_audio and peak == 0, "detail": f"muted track, peak sample {peak}"}

    # Exhibits --------------------------------------------------------------------------------------------

    def draw_annotations(self, doc: dict, rgb: np.ndarray, frame: int, ev: dict, time: float | None = None) -> np.ndarray:
        out = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        sc = max(1.0, out.shape[1] / 960)
        for s in doc["shapes"]:
            if not s["frames"][0] <= frame <= s["frames"][1]:
                continue
            c = s["color"].lstrip("#")
            col = (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16)) if len(c) == 6 else (0, 204, 255)
            pts = np.array(s["points"], np.float32)
            th = max(1, int(round(s["width"] * sc)))
            p = [tuple(int(round(v)) for v in q) for q in pts]
            if s["type"] == "text":
                cv2.putText(out, s["text"], p[0], cv2.FONT_HERSHEY_SIMPLEX, 0.7 * sc, col, th, cv2.LINE_AA)
            elif s["type"] == "arrow" and len(p) >= 2:
                cv2.arrowedLine(out, p[0], p[1], col, th, cv2.LINE_AA, tipLength=0.15)
            elif s["type"] == "line" and len(p) >= 2:
                cv2.polylines(out, [np.array(p)], False, col, th, cv2.LINE_AA)
            elif s["type"] == "rect" and len(p) >= 2:
                cv2.rectangle(out, p[0], p[1], col, th, cv2.LINE_AA)
            elif s["type"] in ("ellipse", "circle") and len(p) >= 2:
                cx, cy = (p[0][0] + p[1][0]) // 2, (p[0][1] + p[1][1]) // 2
                ax, ay = abs(p[1][0] - p[0][0]) // 2, abs(p[1][1] - p[0][1]) // 2
                if s["type"] == "circle":
                    ax = ay = max(ax, ay)
                cv2.ellipse(out, (cx, cy), (ax, ay), 0, 0, 360, col, th, cv2.LINE_AA)
            elif s["type"] == "polygon" and len(p) >= 3:
                cv2.polylines(out, [np.array(p)], True, col, th, cv2.LINE_AA)
            if s["text"] and s["type"] != "text":
                cv2.putText(out, s["text"], (p[0][0], max(14, p[0][1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55 * sc, col,
                            max(1, th - 1), cv2.LINE_AA)
        lines = []
        if doc["show_evidence_label"]:
            lines.append(f"{ev['filename']}  SHA-256 {ev['sha256'][:16]}…")
        if doc["show_frame_label"]:
            lines.append(f"frame {frame}" + (f"   t = {time:.3f} s" if time is not None else ""))
        if doc["legend"]:
            lines.append(doc["legend"])
        if lines:
            bar_h = int(22 * sc * len(lines) + 8)
            bar = np.zeros((bar_h, out.shape[1], 3), np.uint8)
            for i, ln in enumerate(lines):
                cv2.putText(bar, ln, (8, int(20 * sc * (i + 1))), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * sc, (235, 235, 235),
                            1, cv2.LINE_AA)
            out = np.vstack([out, bar])
        return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)

    def render_exhibit(self, doc_id: str, actor: Actor, frame: int = 0, exhibit_id: str = "") -> dict:
        """Burn the annotations into one frame as a PNG exhibit (a derivative; the source is unchanged)."""
        d = self.get(doc_id)
        if d["kind"] != "annotation":
            raise ValueError("that is not an annotation document")
        ev = self.wb.get_evidence(d["evidence_id"])
        times = {f["n"]: f["time"] for f in self.wb.frame_table(ev["id"])}
        rgb = media.to_uint8(self.wb.source_frame(ev["id"], frame))
        out = self.draw_annotations(d["doc"], rgb, frame, ev, times.get(frame))
        if exhibit_id:
            cv2.putText(out, f"Exhibit {exhibit_id[:40]}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        png = media.encode_png(out.astype(np.float32) / 255)
        digest, _ = self.wb.store.put_derived(png, "png")
        self.wb.audit.append(actor, "exhibit.render", case_id=ev["case_id"], target=doc_id,
                             params={"evidence_id": ev["id"], "frame": frame, "document_hash": d["doc_hash"],
                                     "exhibit_id": exhibit_id, "source_sha256": ev["sha256"]}, output_hash=digest)
        return {"png": digest, "frame": frame, "exhibit_id": exhibit_id, "source_sha256": ev["sha256"],
                "document_hash": d["doc_hash"]}
