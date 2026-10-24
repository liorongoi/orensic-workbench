"""Video intelligence: object detection, tracking, licence-plate recognition and a searchable index.

This subsystem produces investigative leads. Every detection, track and plate reading is stored
with the model, its weights hash, the settings and the frame it came from, starts in an
unreviewed state, and can be confirmed, corrected or rejected by an examiner. Searches and
exports are audited because they concern people and vehicles.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import cv2
import numpy as np

from .. import compute, media
from ..audit import Actor
from ..db import canonical_json, new_id, now_iso
from . import lpr
from .detectors import COCO, DEFAULT_CLASSES, VEHICLES, YoloxDetector
from .tracker import Tracker

if TYPE_CHECKING:  # pragma: no cover
    from ..workbench import Workbench

PLATE_STATUSES = ("unreviewed", "confirmed", "corrected", "rejected")
TRACK_STATUSES = ("machine", "confirmed", "rejected")


def _palette(n: int) -> tuple[int, int, int]:
    rng = np.random.default_rng(n * 7919)
    c = rng.integers(60, 255, 3)
    return int(c[0]), int(c[1]), int(c[2])


class Intelligence:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.db = wb.db
        self._detectors: dict[str, YoloxDetector] = {}
        self.ocr = lpr.TesseractOcr()

    def detector(self, model_id: str) -> YoloxDetector:
        if model_id not in self._detectors:
            self._detectors[model_id] = YoloxDetector(model_id)
        return self._detectors[model_id]

    def capabilities(self) -> dict:
        prof = compute.hardware_profile()
        return {"classes": list(COCO), "default_classes": list(DEFAULT_CLASSES), "vehicle_classes": list(VEHICLES),
                "detectors": [m for m in ("detect.yolox_nano", "detect.yolox_tiny", "detect.yolox_s")],
                "profile": prof, "ocr": {"engine": self.ocr.id, "version": self.ocr.version, "available": self.ocr.available()},
                "plate_formats": {k: v["label"] for k, v in lpr.FORMATS.items()}}

    # Frames ----------------------------------------------------------------------------------------

    def frames(self, evidence_id: str, start: int, end: int, step: int) -> Iterator[tuple[int, np.ndarray]]:
        """Decoded BGR frames start, start+step, ... end, streamed (one frame in memory at a time)."""
        row = self.wb._evidence_row(evidence_id)
        if row["media_type"] == "image":
            yield 0, cv2.cvtColor(media.to_uint8(self.wb.source_frame(evidence_id, 0)), cv2.COLOR_RGB2BGR)
            return
        if row["media_type"] != "video":
            raise ValueError("convert this recorder export to a standard container first")
        probe = json.loads(row["probe_json"])
        w, h = probe["width"], probe["height"]
        cmd = [self.wb.settings.ffmpeg, "-v", "error", "-nostdin", "-i", row["stored_path"], "-map", "0:v:0",
               "-vf", f"select=between(n\\,{start}\\,{end})*not(mod(n-{start}\\,{step}))", "-fps_mode", "passthrough",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        size = w * h * 3
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            n = start
            while n <= end:
                buf = p.stdout.read(size)
                if len(buf) < size:
                    break
                yield n, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                n += step
        finally:
            # Also runs when the caller stops early: never leave FFmpeg blocked on a full pipe.
            p.kill()
            p.stdout.close()
            p.wait()

    def frame_bgr(self, evidence_id: str, n: int) -> np.ndarray:
        """One frame through the workbench's frame cache."""
        return cv2.cvtColor(media.to_uint8(self.wb.source_frame(evidence_id, n)), cv2.COLOR_RGB2BGR)

    # Detection and tracking ------------------------------------------------------------------------

    def run(self, evidence_id: str, actor: Actor, *, classes: list[str] | None = None, min_score: float = 0.35,
            frame_step: int | None = None, start: int = 0, end: int | None = None, model: str | None = None,
            track: bool = True, plates: bool = False, country: str = "ANY", plate_frames_per_track: int = 3) -> dict:
        ev = self.wb.get_evidence(evidence_id)
        prof = compute.hardware_profile()
        model = model or prof["detector"]
        step = int(frame_step or (prof["frame_step"] if ev["media_type"] == "video" else 1))
        if step < 1:
            raise ValueError("frame step must be at least 1")
        if not 0.05 <= min_score <= 0.99:
            raise ValueError("score threshold must be between 0.05 and 0.99")
        cls = tuple(c for c in (classes or DEFAULT_CLASSES))
        bad = [c for c in cls if c not in COCO]
        if bad:
            raise ValueError(f"unknown class(es): {', '.join(bad)}")
        if country not in lpr.FORMATS:
            raise ValueError(f"plate format must be one of {', '.join(lpr.FORMATS)}")
        last = max(0, int(ev["probe"].get("frame_count") or 1) - 1)
        end = last if end is None else min(int(end), last)
        if not 0 <= start <= end:
            raise ValueError(f"frames must be within 0-{last}")
        if (end - start) // step + 1 > prof["max_frames"]:
            raise ValueError(f"{(end - start) // step + 1} frames is more than this computer's profile allows in one run "
                             f"({prof['max_frames']}); raise the frame step or split the range")
        det = self.detector(model)
        times = {f["n"]: f["time"] for f in self.wb.frame_table(evidence_id)} if ev["media_type"] == "video" else {0: 0.0}
        tracker = Tracker(max_missed=max(2, 8 // step))
        per_frame: list[tuple[int, list[dict], list]] = []
        best_vehicle_frames: dict[int, list[tuple[float, int]]] = {}
        processed = 0
        for n, bgr in self.frames(evidence_id, start, end, step):
            processed += 1
            dets = det.detect(bgr, cls, min_score)
            assigned = tracker.update(n, times.get(n), dets) if track else [(None, d) for d in dets]
            per_frame.append((n, dets, assigned))
            if plates:
                for tn, d in assigned:
                    if d["cls"] in VEHICLES and d["cls"] != "bicycle":
                        best_vehicle_frames.setdefault(tn if tn is not None else -len(best_vehicle_frames) - 1, []).append(
                            (d["box"][2] * d["box"][3], n))
        tracks = tracker.close() if track else []
        kept = {t.number for t in tracks}
        run_id = new_id()
        track_ids = {t.number: f"{run_id}-t{t.number}" for t in tracks}
        rows = []
        for n, dets, assigned in per_frame:
            for tn, d in assigned:
                rows.append((run_id, ev["case_id"], evidence_id, n, times.get(n), d["cls"], d["score"], *d["box"],
                             track_ids.get(tn) if tn in kept else None))
        out_hash = hashlib.sha256(canonical_json([list(r[3:]) for r in rows]).encode()).hexdigest()
        params = {"classes": list(cls), "min_score": min_score, "frame_step": step, "start": start, "end": end,
                  "model": model, "track": track, "plates": plates, "country": country,
                  "tracker": Tracker().params() if track else None, "profile": prof["name"]}
        models_used = [det.describe()]
        if plates:
            models_used.append({"plate_localiser": f"lpr {lpr.VERSION} (character grouping, morphology, bright regions)",
                                "ocr": self.ocr.id, "ocr_version": self.ocr.version})
        stats = {"frames_processed": processed, "detections": len(rows), "tracks": len(tracks),
                 "by_class": {c: sum(1 for r in rows if r[5] == c) for c in sorted({r[5] for r in rows})}}
        with self.db.transaction():
            self.db.execute("INSERT INTO detection_runs (id, case_id, evidence_id, kind, params_json, models_json, stats_json,"
                            " output_hash, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (run_id, ev["case_id"], evidence_id, "detection", canonical_json(params),
                             canonical_json(models_used), canonical_json(stats), out_hash, actor.label, now_iso()))
            self.db._conn.executemany(
                "INSERT INTO detections (run_id, case_id, evidence_id, frame, time, cls, score, x, y, w, h, track_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            for t in tracks:
                hist = [{"frame": f, "time": tm, "box": b, "score": s} for f, tm, b, s in t.history]
                self.db.execute(
                    "INSERT INTO tracks (id, run_id, case_id, evidence_id, number, cls, start_frame, end_frame, start_time,"
                    " end_time, mean_score, boxes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (track_ids[t.number], run_id, ev["case_id"], evidence_id, t.number, t.cls, hist[0]["frame"],
                     hist[-1]["frame"], hist[0]["time"], hist[-1]["time"], round(float(np.mean([h["score"] for h in hist])), 4),
                     canonical_json(hist)))
        if plates:
            stats["plates"] = self._read_plates(run_id, ev, best_vehicle_frames, track_ids, kept, country,
                                                plate_frames_per_track, times, prof)
            self.db.execute("UPDATE detection_runs SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
        if track:
            self._appearance(run_id)
        self.wb.audit.append(actor, "intel.detect", case_id=ev["case_id"], target=evidence_id,
                             params={"run_id": run_id, **params, "models": models_used, "stats": stats},
                             output_hash=out_hash)
        return self.get_run(run_id)

    def _read_plates(self, run_id, ev, best, track_ids, kept, country, per_track, times, prof) -> int:
        count = 0
        for tn, sizes in best.items():
            if tn > 0 and tn not in kept:
                continue
            frames = [n for _, n in sorted(sizes, key=lambda x: (-x[0], x[1]))[:per_track]]
            for n in sorted(frames):
                bgr = self.frame_bgr(ev["id"], n)
                row = self.db.one("SELECT x, y, w, h FROM detections WHERE run_id = ? AND frame = ? AND track_id IS ?"
                                  " AND cls IN ('car','truck','bus','motorcycle') ORDER BY w*h DESC LIMIT 1",
                                  (run_id, n, track_ids.get(tn)))
                if not row:
                    continue
                count += self._plate_in_box(run_id, ev, n, times.get(n), bgr, (row["x"], row["y"], row["w"], row["h"]),
                                            country, track_ids.get(tn), prof)
        return count

    def _plate_in_box(self, run_id, ev, n, t, bgr, box, country, track_id, prof) -> int:
        x, y, w, h = box
        H, W = bgr.shape[:2]
        x0, y0 = int(max(0, x - 0.05 * w)), int(max(0, y + 0.3 * h))
        x1, y1 = int(min(W, x + 1.05 * w)), int(min(H, y + 1.05 * h))
        crop = bgr[y0:y1, x0:x1]
        if crop.size == 0:
            return 0
        found = 0
        for c in lpr.localise(crop, max_candidates=2, upscale_to=prof["plate_upscale"]):
            if c.score < 0.35:
                continue
            corners = (np.array(c.corners) + [x0, y0]).round(2).tolist()
            found += self.store_plate(run_id, ev, n, t, bgr, corners, c.score, country, track_id,
                                      vehicle=lpr.vehicle_colour(bgr[int(y):int(y + h), int(x):int(x + w)]) | {"box": list(box)},
                                      proposer=c.proposer)
        return found

    def store_plate(self, run_id, ev, n, t, bgr, corners, score, country, track_id, vehicle=None, proposer="manual") -> int:
        rect = lpr.rectify(bgr, corners)
        readings = lpr.read_plate(rect, self.ocr, country) if self.ocr.available() else []
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        pad = 0.25 * (max(xs) - min(xs))
        cx0, cy0 = int(max(0, min(xs) - pad)), int(max(0, min(ys) - pad))
        cx1, cy1 = int(min(bgr.shape[1], max(xs) + pad)), int(min(bgr.shape[0], max(ys) + pad))
        crop = bgr[cy0:cy1, cx0:cx1]
        crop_sha, _ = self.wb.store.put_derived(media.encode_png(crop[..., ::-1].astype(np.float32) / 255), "png")
        rect_sha, _ = self.wb.store.put_derived(media.encode_png(rect[..., ::-1].astype(np.float32) / 255), "png")
        best = readings[0] if readings else None
        self.db.execute(
            "INSERT INTO plates (id, run_id, case_id, evidence_id, frame, time, corners_json, score, crop_sha256,"
            " rectified_sha256, candidates_json, best_text, best_confidence, format_match, vehicle_json, track_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), run_id, ev["case_id"], ev["id"], n, t, canonical_json(corners), round(float(score), 4), crop_sha,
             rect_sha, canonical_json([r.__dict__ for r in readings] + ([] if self.ocr.available() else
                                                                      [{"note": "no OCR engine installed: read manually"}])),
             best.text if best else "", best.confidence if best else 0.0, best.format if best else None,
             canonical_json((vehicle or {}) | {"localiser": proposer}), track_id))
        return 1

    def add_plate(self, evidence_id: str, frame: int, corners: list[list[float]], actor: Actor, country: str = "ANY") -> dict:
        """The examiner marks a plate's four corners by hand; it is rectified and read like a detected one."""
        ev = self.wb.get_evidence(evidence_id)
        if len(corners) != 4:
            raise ValueError("give the plate's four corners")
        bgr = self.frame_bgr(evidence_id, frame)
        run = self.db.one("SELECT id FROM detection_runs WHERE evidence_id = ? AND kind = 'manual'", (evidence_id,))
        if not run:
            rid = new_id()
            self.db.execute("INSERT INTO detection_runs (id, case_id, evidence_id, kind, params_json, models_json, stats_json,"
                            " output_hash, actor, created_at) VALUES (?,?,?,'manual','{}','[]','{}','',?,?)",
                            (rid, ev["case_id"], evidence_id, actor.label, now_iso()))
        else:
            rid = run["id"]
        times = {f["n"]: f["time"] for f in self.wb.frame_table(evidence_id)} if ev["media_type"] == "video" else {}
        ordered = lpr._order(np.array(corners, np.float32)).tolist()
        self.store_plate(rid, ev, frame, times.get(frame), bgr, ordered, 1.0, country, None, proposer="manual")
        pid = self.db.one("SELECT id FROM plates WHERE run_id = ? ORDER BY rowid DESC LIMIT 1", (rid,))["id"]
        self.wb.audit.append(actor, "intel.plate_manual", case_id=ev["case_id"], target=evidence_id,
                             params={"plate_id": pid, "frame": frame, "corners": ordered})
        return self.get_plate(pid)

    # Appearance (for cross-camera candidates) ------------------------------------------------------

    def _appearance(self, run_id: str) -> None:
        """Colour histogram (HSV, 8x4x4) of each track's largest box: a weak cue for cross-camera candidates."""
        for t in self.db.all("SELECT * FROM tracks WHERE run_id = ?", (run_id,)):
            hist = json.loads(t["boxes_json"])
            big = max(hist, key=lambda b: b["box"][2] * b["box"][3])
            bgr = self.frame_bgr(t["evidence_id"], big["frame"])
            x, y, w, h = [int(round(v)) for v in big["box"]]
            crop = bgr[max(0, y):y + h, max(0, x):x + w]
            if crop.size == 0:
                continue
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            hh = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
            hh = (hh / max(hh.sum(), 1)).round(5).tolist()
            attrs = {"appearance_hist": hh, "appearance_frame": big["frame"]}
            if t["cls"] in VEHICLES:
                attrs["colour"] = lpr.vehicle_colour(crop)
            self.db.execute("UPDATE tracks SET attributes_json = ? WHERE id = ?", (canonical_json(attrs), t["id"]))

    def cross_camera_candidates(self, case_id: str, actor: Actor, min_similarity: float = 0.6) -> dict:
        tracks = [self._public_track(t) for t in self.db.all(
            "SELECT * FROM tracks WHERE case_id = ? AND status != 'rejected' AND merged_into IS NULL", (case_id,))]
        out = []
        for i, a in enumerate(tracks):
            for b in tracks[i + 1:]:
                if a["evidence_id"] == b["evidence_id"] or a["cls"] != b["cls"]:
                    continue
                ha, hb = a["attributes"].get("appearance_hist"), b["attributes"].get("appearance_hist")
                if not ha or not hb:
                    continue
                sim = float(np.minimum(ha, hb).sum())  # histogram intersection
                if sim < min_similarity:
                    continue
                cand = {"a": _brief(a), "b": _brief(b), "similarity": round(sim, 3), "label": "candidate, unverified"}
                ta = self._case_time(a["evidence_id"], a["end_frame"])
                tb = self._case_time(b["evidence_id"], b["start_frame"])
                if ta is not None and tb is not None:
                    cand["gap_s"] = round(tb - ta, 3)
                out.append(cand)
        out.sort(key=lambda c: -c["similarity"])
        self.wb.audit.append(actor, "intel.cross_camera", case_id=case_id, params={"pairs": len(out), "min_similarity": min_similarity})
        return {"candidates": out, "method": "HSV colour-histogram intersection of each track's largest box, same class, "
                "different recordings", "limitations": "Colour similarity is weak evidence: lighting, camera colour "
                "response and similar clothing or vehicles produce false matches. These are leads, not identifications."}

    def _case_time(self, evidence_id: str, frame: int) -> float | None:
        try:
            return self.wb.sync.global_time(evidence_id, frame)[0]
        except ValueError:
            return None

    # Reading results -----------------------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict:
        r = self.db.one("SELECT * FROM detection_runs WHERE id = ?", (run_id,))
        if not r:
            from ..workbench import NotFound

            raise NotFound(f"detection run {run_id} not found")
        for k in ("params", "models", "stats"):
            r[k] = json.loads(r.pop(f"{k}_json"))
        return r

    def runs(self, case_id: str, evidence_id: str | None = None) -> list[dict]:
        q = "SELECT id FROM detection_runs WHERE case_id = ?" + (" AND evidence_id = ?" if evidence_id else "") + " ORDER BY created_at"
        return [self.get_run(r["id"]) for r in self.db.all(q, (case_id, evidence_id) if evidence_id else (case_id,))]

    def detections(self, run_id: str, frame: int | None = None) -> list[dict]:
        if frame is None:
            return self.db.all("SELECT * FROM detections WHERE run_id = ? ORDER BY frame, id", (run_id,))
        return self.db.all("SELECT * FROM detections WHERE run_id = ? AND frame = ? ORDER BY id", (run_id, frame))

    def nearest_detections(self, evidence_id: str, frame: int) -> list[dict]:
        """Detections of the latest run on the frame nearest `frame` that was processed."""
        run = self.db.one("SELECT id FROM detection_runs WHERE evidence_id = ? AND kind = 'detection' ORDER BY created_at DESC LIMIT 1",
                          (evidence_id,))
        if not run:
            return []
        near = self.db.one("SELECT frame FROM detections WHERE run_id = ? ORDER BY ABS(frame - ?) LIMIT 1", (run["id"], frame))
        return self.detections(run["id"], near["frame"]) if near else []

    @staticmethod
    def _public_track(t: dict) -> dict:
        t = dict(t)
        t["boxes"] = json.loads(t.pop("boxes_json"))
        t["attributes"] = json.loads(t.pop("attributes_json") or "{}")
        return t

    def tracks(self, case_id: str, evidence_id: str | None = None, run_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM tracks WHERE case_id = ?", [case_id]
        if evidence_id:
            q += " AND evidence_id = ?"
            a.append(evidence_id)
        if run_id:
            q += " AND run_id = ?"
            a.append(run_id)
        return [self._public_track(t) for t in self.db.all(q + " ORDER BY evidence_id, start_frame, number", tuple(a))]

    def get_track(self, track_id: str) -> dict:
        t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        if not t:
            from ..workbench import NotFound

            raise NotFound(f"track {track_id} not found")
        return self._public_track(t)

    # Track corrections -------------------------------------------------------------------------------

    def update_track(self, track_id: str, actor: Actor, *, label: str | None = None, status: str | None = None) -> dict:
        t = self.get_track(track_id)
        changes = {}
        if label is not None and label.strip() != t["label"]:
            changes["label"] = label.strip()[:200]
        if status is not None and status != t["status"]:
            if status not in TRACK_STATUSES:
                raise ValueError(f"status must be one of {', '.join(TRACK_STATUSES)}")
            changes["status"] = status
        if changes:
            self.db.execute(f"UPDATE tracks SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*changes.values(), track_id))
            self.wb.audit.append(actor, "intel.track_update", case_id=t["case_id"], target=track_id, params=changes)
        return self.get_track(track_id)

    def merge_tracks(self, keep_id: str, other_id: str, actor: Actor) -> dict:
        """The examiner decides two tracks are the same object: other's boxes join keep. Other is kept,
        marked merged, so the machine result stays visible."""
        a, b = self.get_track(keep_id), self.get_track(other_id)
        if a["evidence_id"] != b["evidence_id"] or a["cls"] != b["cls"]:
            raise ValueError("only tracks of the same class in the same recording can be merged")
        if b["merged_into"]:
            raise ValueError("that track was already merged")
        boxes = sorted(a["boxes"] + b["boxes"], key=lambda x: x["frame"])
        self.db.execute("UPDATE tracks SET boxes_json = ?, start_frame = ?, end_frame = ?, start_time = ?, end_time = ?,"
                        " status = 'confirmed' WHERE id = ?",
                        (canonical_json(boxes), boxes[0]["frame"], boxes[-1]["frame"], boxes[0]["time"], boxes[-1]["time"], keep_id))
        self.db.execute("UPDATE tracks SET merged_into = ? WHERE id = ?", (keep_id, other_id))
        self.db.execute("UPDATE detections SET track_id = ? WHERE track_id = ?", (keep_id, other_id))
        self.wb.audit.append(actor, "intel.track_merge", case_id=a["case_id"], target=keep_id, params={"merged": other_id})
        return self.get_track(keep_id)

    def split_track(self, track_id: str, at_frame: int, actor: Actor) -> dict:
        t = self.get_track(track_id)
        first = [b for b in t["boxes"] if b["frame"] < at_frame]
        second = [b for b in t["boxes"] if b["frame"] >= at_frame]
        if not first or not second:
            raise ValueError("the split frame must fall inside the track")
        nid = f"{track_id}-s{at_frame}"
        num = (self.db.one("SELECT MAX(number) AS m FROM tracks WHERE run_id = ?", (t["run_id"],))["m"] or 0) + 1
        self.db.execute("UPDATE tracks SET boxes_json = ?, end_frame = ?, end_time = ? WHERE id = ?",
                        (canonical_json(first), first[-1]["frame"], first[-1]["time"], track_id))
        self.db.execute("INSERT INTO tracks (id, run_id, case_id, evidence_id, number, cls, start_frame, end_frame, start_time,"
                        " end_time, mean_score, label, status, boxes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (nid, t["run_id"], t["case_id"], t["evidence_id"], num, t["cls"], second[0]["frame"], second[-1]["frame"],
                         second[0]["time"], second[-1]["time"], round(float(np.mean([b["score"] for b in second])), 4), "",
                         "confirmed", canonical_json(second)))
        self.db.execute("UPDATE detections SET track_id = ? WHERE track_id = ? AND frame >= ?", (nid, track_id, at_frame))
        self.wb.audit.append(actor, "intel.track_split", case_id=t["case_id"], target=track_id,
                             params={"at_frame": at_frame, "new_track": nid})
        return {"first": self.get_track(track_id), "second": self.get_track(nid)}

    def add_track_box(self, track_id: str, frame: int, box: list[float], actor: Actor) -> dict:
        """Manual annotation: the examiner adds or replaces the box on one frame."""
        t = self.get_track(track_id)
        if len(box) != 4 or box[2] <= 0 or box[3] <= 0:
            raise ValueError("box is [x, y, width, height]")
        times = {f["n"]: f["time"] for f in self.wb.frame_table(t["evidence_id"])}
        boxes = [b for b in t["boxes"] if b["frame"] != frame] + [
            {"frame": frame, "time": times.get(frame), "box": [round(float(v), 1) for v in box], "score": 1.0, "manual": True}]
        boxes.sort(key=lambda b: b["frame"])
        self.db.execute("UPDATE tracks SET boxes_json = ?, start_frame = ?, end_frame = ?, start_time = ?, end_time = ? WHERE id = ?",
                        (canonical_json(boxes), boxes[0]["frame"], boxes[-1]["frame"], boxes[0]["time"], boxes[-1]["time"], track_id))
        self.wb.audit.append(actor, "intel.track_box", case_id=t["case_id"], target=track_id, params={"frame": frame, "box": box})
        return self.get_track(track_id)

    def export_tracks(self, case_id: str, actor: Actor, fmt: str = "json", evidence_id: str | None = None) -> tuple[bytes, str]:
        tracks = self.tracks(case_id, evidence_id)
        if fmt == "json":
            data = json.dumps({"case_id": case_id, "exported_at": now_iso(), "exported_by": actor.name,
                               "note": "Track numbers are machine associations, not identities.",
                               "tracks": [{k: v for k, v in t.items() if k != "attributes"} for t in tracks]},
                              indent=2).encode()
            mime = "application/json"
        elif fmt == "csv":
            buf = io.StringIO()
            wr = csv.writer(buf)
            wr.writerow(["track_id", "evidence_id", "class", "status", "label", "frame", "time_s", "x", "y", "w", "h", "score", "manual"])
            for t in tracks:
                for b in t["boxes"]:
                    wr.writerow([t["id"], t["evidence_id"], t["cls"], t["status"], t["label"], b["frame"], b["time"], *b["box"],
                                 b["score"], bool(b.get("manual"))])
            data, mime = buf.getvalue().encode(), "text/csv"
        else:
            raise ValueError("format must be json or csv")
        self.wb.audit.append(actor, "intel.export_tracks", case_id=case_id, params={"format": fmt, "tracks": len(tracks)},
                             output_hash=hashlib.sha256(data).hexdigest())
        return data, mime

    # Plates ---------------------------------------------------------------------------------------------

    @staticmethod
    def _public_plate(p: dict) -> dict:
        p = dict(p)
        p["corners"] = json.loads(p.pop("corners_json"))
        p["candidates"] = json.loads(p.pop("candidates_json"))
        p["vehicle"] = json.loads(p.pop("vehicle_json") or "{}")
        p["display_text"] = p["reviewed_text"] if p["status"] in ("confirmed", "corrected") else None
        return p

    def get_plate(self, plate_id: str) -> dict:
        p = self.db.one("SELECT * FROM plates WHERE id = ?", (plate_id,))
        if not p:
            from ..workbench import NotFound

            raise NotFound(f"plate {plate_id} not found")
        return self._public_plate(p)

    def plates(self, case_id: str, status: str | None = None, evidence_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM plates WHERE case_id = ?", [case_id]
        if status:
            q += " AND status = ?"
            a.append(status)
        if evidence_id:
            q += " AND evidence_id = ?"
            a.append(evidence_id)
        return [self._public_plate(p) for p in self.db.all(q + " ORDER BY evidence_id, frame", tuple(a))]

    def review_queue(self, case_id: str) -> list[dict]:
        """Unreviewed readings, least confident first."""
        return sorted(self.plates(case_id, "unreviewed"), key=lambda p: (p["best_confidence"], p["frame"]))

    def review_plate(self, plate_id: str, action: str, actor: Actor, text: str = "", note: str = "") -> dict:
        p = self.get_plate(plate_id)
        if actor.kind != "human":
            raise PermissionError("only an examiner can review plate readings")
        if action not in ("confirm", "correct", "reject", "reopen"):
            raise ValueError("action must be confirm, correct, reject or reopen")
        if action == "confirm":
            text = lpr.normalise_text(text or p["best_text"])
            if not text:
                raise ValueError("there is no reading to confirm; use correct with the text you read")
            status = "confirmed"
        elif action == "correct":
            text = lpr.normalise_text(text)
            if not text:
                raise ValueError("type the plate as you read it")
            status = "corrected"
        elif action == "reject":
            text, status = None, "rejected"
        else:
            text, status = None, "unreviewed"
        self.db.execute("UPDATE plates SET status = ?, reviewed_text = ?, reviewer = ?, reviewed_at = ? WHERE id = ?",
                        (status, text, actor.label if status != "unreviewed" else None,
                         now_iso() if status != "unreviewed" else None, plate_id))
        self.wb.audit.append(actor, "intel.plate_review", case_id=p["case_id"], target=plate_id,
                             params={"action": action, "text": text, "machine_best": p["best_text"],
                                     "machine_confidence": p["best_confidence"], "note": note[:500],
                                     "alternatives_kept": len(p["candidates"])})
        return self.get_plate(plate_id)

    # Search --------------------------------------------------------------------------------------------

    def search(self, actor: Actor, *, case_ids: list[str], plate: str | None = None, cls: str | None = None,
               evidence_ids: list[str] | None = None, min_score: float = 0.0, reviewed_only: bool = False,
               time_from: float | None = None, time_to: float | None = None, limit: int = 200, offset: int = 0) -> dict:
        """Search the index. Plates match the reviewed text, the machine's best reading or any
        alternative reading; `*` and `?` are wildcards. Times are case-timeline seconds (synchronised
        recordings only). Every search is audited."""
        if not case_ids:
            return {"total": 0, "results": []}
        results = []
        scope = f"case_id IN ({','.join('?' * len(case_ids))})"
        if plate:
            pat = lpr.normalise_text(plate.replace("*", "\x00").replace("?", "\x01")).replace("\x00", "*").replace("\x01", "?")
            pat = plate.upper().replace(" ", "").replace("-", "") if not pat else pat
            for p in self.db.all(f"SELECT * FROM plates WHERE {scope}", tuple(case_ids)):
                pp = self._public_plate(p)
                if reviewed_only and pp["status"] not in ("confirmed", "corrected"):
                    continue
                if pp["status"] == "rejected":
                    continue
                texts = [("reviewed", pp["reviewed_text"])] if pp["reviewed_text"] else []
                texts += [("best", pp["best_text"])] + [("alternative", c.get("text")) for c in pp["candidates"]]
                hit = next((k for k, t in texts if t and fnmatch.fnmatchcase(t, pat)), None)
                if hit:
                    results.append({"type": "plate", "matched_on": hit, **_brief_plate(pp)})
        else:
            q = f"SELECT * FROM detections WHERE {scope} AND score >= ?"
            a: list = [*case_ids, min_score]
            if cls:
                q += " AND cls = ?"
                a.append(cls)
            if evidence_ids:
                q += f" AND evidence_id IN ({','.join('?' * len(evidence_ids))})"
                a += evidence_ids
            rows = self.db.all(q + " ORDER BY evidence_id, frame LIMIT 20000", tuple(a))
            seen_tracks = set()
            for r in rows:
                if r["track_id"]:
                    if r["track_id"] in seen_tracks:
                        continue
                    seen_tracks.add(r["track_id"])
                    t = self.get_track(r["track_id"])
                    if t["status"] == "rejected" or t["merged_into"]:
                        continue
                    if reviewed_only and t["status"] != "confirmed":
                        continue
                    results.append({"type": "track", **_brief(t)})
                elif not reviewed_only:
                    results.append({"type": "detection", "evidence_id": r["evidence_id"], "frame": r["frame"],
                                    "time": r["time"], "cls": r["cls"], "score": r["score"], "box": [r["x"], r["y"], r["w"], r["h"]]})
        for r in results:
            ct = self._case_time(r["evidence_id"], r.get("frame", r.get("start_frame", 0)))
            r["case_time"] = ct
        if time_from is not None or time_to is not None:
            results = [r for r in results if r["case_time"] is not None
                       and (time_from is None or r["case_time"] >= time_from) and (time_to is None or r["case_time"] <= time_to)]
        total = len(results)
        page = results[offset: offset + min(limit, 1000)]
        self.wb.audit.append(actor, "intel.search", params={"cases": case_ids, "plate": plate, "class": cls,
                                                             "reviewed_only": reviewed_only, "results": total})
        return {"total": total, "offset": offset, "results": page}

    def occurrences(self, case_id: str, text: str, actor: Actor) -> dict:
        """Every sighting of a plate (reviewed text or machine reading) in time order, with duplicates
        (several readings of the same vehicle track) grouped."""
        res = self.search(actor, case_ids=[case_id], plate=text)["results"]
        by_track: dict[str, list] = {}
        for r in res:
            by_track.setdefault(r.get("track_id") or r["id"], []).append(r)
        sightings = []
        for key, rs in by_track.items():
            rs.sort(key=lambda r: r["frame"])
            sightings.append({"key": key, "evidence_id": rs[0]["evidence_id"], "first_frame": rs[0]["frame"],
                              "last_frame": rs[-1]["frame"], "case_time": rs[0]["case_time"], "readings": len(rs),
                              "statuses": sorted({r["status"] for r in rs}), "plates": [r["id"] for r in rs]})
        sightings.sort(key=lambda s: (s["case_time"] is None, s["case_time"] or 0, s["evidence_id"], s["first_frame"]))
        return {"query": text, "sightings": sightings}

    # Overlay --------------------------------------------------------------------------------------------

    def overlay(self, evidence_id: str, frame: int) -> bytes:
        """The frame with detections, track numbers and plates drawn on (a review aid, not stored)."""
        bgr = self.frame_bgr(evidence_id, frame).copy()
        for d in self.nearest_detections(evidence_id, frame):
            x, y, w, h = [int(round(v)) for v in (d["x"], d["y"], d["w"], d["h"])]
            n = int(d["track_id"].split("-t")[-1].split("-")[0]) if d["track_id"] else 0
            col = _palette(n) if n else (200, 200, 200)
            cv2.rectangle(bgr, (x, y), (x + w, y + h), col, 2)
            label = f"{d['cls']} {d['score']:.2f}" + (f" #{n}" if n else "")
            cv2.putText(bgr, label, (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
        for p in self.plates(self.wb.get_evidence(evidence_id)["case_id"], evidence_id=evidence_id):
            if p["frame"] == frame:
                pts = np.array(p["corners"], np.int32)
                cv2.polylines(bgr, [pts], True, (0, 255, 255), 2)
                txt = p["display_text"] or (f"? {p['best_text']}" if p["best_text"] else "?")
                cv2.putText(bgr, txt, (int(pts[:, 0].min()), int(pts[:, 1].max()) + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (0, 255, 255), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".png", bgr)
        return buf.tobytes()


def _brief(t: dict) -> dict:
    return {"track_id": t["id"], "evidence_id": t["evidence_id"], "number": t["number"], "cls": t["cls"],
            "start_frame": t["start_frame"], "end_frame": t["end_frame"], "start_time": t["start_time"],
            "end_time": t["end_time"], "status": t["status"], "label": t["label"], "mean_score": t["mean_score"],
            "colour": (t.get("attributes") or {}).get("colour", {}).get("colour")}


def _brief_plate(p: dict) -> dict:
    return {"id": p["id"], "evidence_id": p["evidence_id"], "frame": p["frame"], "time": p["time"],
            "best_text": p["best_text"], "best_confidence": p["best_confidence"], "status": p["status"],
            "reviewed_text": p["reviewed_text"], "track_id": p["track_id"], "crop_sha256": p["crop_sha256"],
            "rectified_sha256": p["rectified_sha256"], "format_match": p["format_match"]}
