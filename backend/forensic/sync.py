"""Multi-camera synchronisation and incident timelines.

Each recording keeps its own original timestamps. Synchronisation never edits media: it records
how a recording's local time maps onto one case timeline,

    T = offset + t_local · (1 + drift_ppm · 10⁻⁶)

where t_local is the recording's own frame time measured from its first frame, offset is in
seconds (an absolute Unix time when the timeline is anchored to a wall clock, or seconds on an
arbitrary case timeline), and drift is the recording clock's rate error. Every record carries the
method, the evidence for it and an uncertainty. Camera clocks are never assumed to be right.
"""

from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import media
from .audit import Actor
from .db import canonical_json, new_id, now_iso

if TYPE_CHECKING:  # pragma: no cover
    from .workbench import Workbench

METHODS = {
    "reference": "Reference recording: its own time defines the case timeline (offset 0, no drift).",
    "manual": "Offset and drift typed by the examiner, with their stated basis.",
    "clock": "Wall-clock time of the first frame read from the recording (on-screen clock, recorder log or "
             "metadata), with the examiner's estimate of that clock's error.",
    "event": "The same event identified in this recording and in an already-synchronised one.",
    "two_events": "Two events matched in both recordings: gives offset and clock drift.",
    "audio": "Cross-correlation of the audio envelopes of two recordings.",
    "visual": "Cross-correlation of the mean-brightness signals of two recordings (flashes, lights switching).",
}
SPEED_OF_SOUND = 343.0


def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    if t > 1e8:  # an absolute Unix time
        return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")
    return f"T+{t:.3f}s"


class MultiCamera:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.db = wb.db

    # Cameras -------------------------------------------------------------------------------------

    def create_camera(self, case_id: str, label: str, actor: Actor, *, location: str = "", latitude: float | None = None,
                      longitude: float | None = None, notes: str = "") -> dict:
        self.wb.get_case(case_id)
        if not label.strip():
            raise ValueError("give the camera a label")
        cid = new_id()
        self.db.execute("INSERT INTO cameras (id, case_id, label, location, latitude, longitude, notes, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (cid, case_id, label.strip()[:200], location.strip()[:500], latitude, longitude,
                         notes.strip()[:2000], now_iso()))
        self.wb.audit.append(actor, "camera.create", case_id=case_id, target=cid,
                             params={"label": label.strip(), "location": location.strip(), "latitude": latitude,
                                     "longitude": longitude})
        return self.db.one("SELECT * FROM cameras WHERE id = ?", (cid,))

    def cameras(self, case_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM cameras WHERE case_id = ? ORDER BY label", (case_id,))

    def assign_camera(self, evidence_id: str, camera_id: str | None, actor: Actor) -> dict:
        ev = self.wb.get_evidence(evidence_id)
        if camera_id:
            cam = self.db.one("SELECT * FROM cameras WHERE id = ?", (camera_id,))
            if not cam or cam["case_id"] != ev["case_id"]:
                raise ValueError("that camera is not in this case")
        self.db.execute("UPDATE evidence SET camera_id = ? WHERE id = ?", (camera_id, evidence_id))
        self.wb.audit.append(actor, "camera.assign", case_id=ev["case_id"], target=evidence_id,
                             params={"camera_id": camera_id})
        return self.wb.get_evidence(evidence_id)

    # Local times -------------------------------------------------------------------------------------

    def local_times(self, evidence_id: str) -> list[float]:
        """Each frame's own time, from the first frame (seconds)."""
        ev = self.wb.get_evidence(evidence_id)
        if ev["media_type"] != "video":
            return [0.0]
        table = self.wb.frame_table(evidence_id)
        t0 = next((f["time"] for f in table if f["time"] is not None), 0.0)
        rate = _rate(ev["probe"])
        out, last = [], None
        for f in table:
            t = f["time"] - t0 if f["time"] is not None else (last + 1 / rate if last is not None else 0.0)
            out.append(t)
            last = t
        return out

    def current(self, evidence_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM sync_records WHERE evidence_id = ? AND current = 1", (evidence_id,))
        if row:
            row["details"] = json.loads(row.pop("details_json"))
        return row

    def history(self, evidence_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM sync_records WHERE evidence_id = ? ORDER BY created_at, rowid", (evidence_id,))
        for r in rows:
            r["details"] = json.loads(r.pop("details_json"))
        return rows

    def global_time(self, evidence_id: str, frame: int) -> tuple[float, float | None]:
        s = self.current(evidence_id)
        if not s:
            raise ValueError(f"{self.wb.get_evidence(evidence_id)['filename']} is not synchronised yet")
        lt = self.local_times(evidence_id)
        if not 0 <= frame < len(lt):
            raise ValueError(f"frame {frame} is outside 0-{len(lt) - 1}")
        return s["offset_s"] + lt[frame] * (1 + s["drift_ppm"] * 1e-6), s["uncertainty_s"]

    def frame_at(self, evidence_id: str, T: float) -> dict | None:
        """The frame of `evidence_id` shown at case time T (nearest; None if T is outside the recording)."""
        s = self.current(evidence_id)
        if not s:
            return None
        lt = np.array(self.local_times(evidence_id))
        local = (T - s["offset_s"]) / (1 + s["drift_ppm"] * 1e-6)
        dur = 1 / _rate(self.wb.get_evidence(evidence_id)["probe"]) if len(lt) > 1 else 0
        if local < lt[0] - dur / 2 or local > lt[-1] + max(dur, 1e-9) / 2:
            return None
        i = int(np.argmin(np.abs(lt - local)))
        return {"frame": i, "local_time": float(lt[i]), "error_s": float(lt[i] - local),
                "uncertainty_s": s["uncertainty_s"]}

    # Recording a synchronisation -----------------------------------------------------------------------

    def _record(self, evidence_id: str, offset: float, drift: float, uncertainty: float | None, method: str,
                details: dict, actor: Actor, reference: str | None = None) -> dict:
        ev = self.wb.get_evidence(evidence_id)
        if method not in METHODS:
            raise ValueError(f"unknown synchronisation method {method}")
        if not math.isfinite(offset) or not math.isfinite(drift) or abs(drift) > 1e5:
            raise ValueError("offset and drift must be finite; drift is limited to ±100000 ppm")
        with self.db.transaction():
            self.db.execute("UPDATE sync_records SET current = 0 WHERE evidence_id = ?", (evidence_id,))
            sid = new_id()
            self.db.execute("INSERT INTO sync_records (id, case_id, evidence_id, reference_evidence_id, offset_s, drift_ppm,"
                            " uncertainty_s, method, details_json, current, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                            (sid, ev["case_id"], evidence_id, reference, float(offset), float(drift),
                             None if uncertainty is None else float(uncertainty), method, canonical_json(details),
                             actor.label, now_iso()))
        self.wb.audit.append(actor, "sync.record", case_id=ev["case_id"], target=evidence_id,
                             params={"method": method, "offset_s": offset, "drift_ppm": drift, "uncertainty_s": uncertainty,
                                     "reference": reference, "details": details})
        return self.current(evidence_id)

    def set_reference(self, evidence_id: str, actor: Actor, start_iso: str | None = None) -> dict:
        offset = _parse_iso(start_iso) if start_iso else 0.0
        return self._record(evidence_id, offset, 0.0, 0.0, "reference",
                            {"start_iso": start_iso, "note": METHODS["reference"]}, actor)

    def sync_manual(self, evidence_id: str, offset: float, actor: Actor, *, drift_ppm: float = 0.0,
                    uncertainty_s: float | None = None, basis: str = "") -> dict:
        if not basis.strip():
            raise ValueError("state the basis for a manual offset (what it was read from)")
        return self._record(evidence_id, offset, drift_ppm, uncertainty_s, "manual", {"basis": basis.strip()[:1000]}, actor)

    def sync_clock(self, evidence_id: str, start_iso: str, clock_error_s: float, actor: Actor, *, source: str = "",
                   frame: int = 0) -> dict:
        """The wall-clock time shown at `frame` (e.g. an on-screen clock) anchors the recording."""
        t = _parse_iso(start_iso)
        lt = self.local_times(evidence_id)
        if not 0 <= frame < len(lt):
            raise ValueError("frame outside the recording")
        return self._record(evidence_id, t - lt[frame], 0.0, abs(clock_error_s), "clock",
                            {"clock_time": start_iso, "at_frame": frame, "source": source.strip()[:500],
                             "assumption": "the recording's clock reading is accurate to within the stated error"}, actor)

    def sync_event(self, evidence_id: str, frame: int, reference_id: str, reference_frame: int, actor: Actor,
                   *, label: str = "") -> dict:
        T, ref_unc = self.global_time(reference_id, reference_frame)
        lt = self.local_times(evidence_id)
        if not 0 <= frame < len(lt):
            raise ValueError("frame outside the recording")
        cur = self.current(evidence_id)
        drift = cur["drift_ppm"] if cur and cur["method"] == "two_events" else 0.0
        offset = T - lt[frame] * (1 + drift * 1e-6)
        # Each recording only shows the event to within one frame interval.
        unc = 0.5 / _rate(self.wb.get_evidence(evidence_id)["probe"]) + 0.5 / _rate(self.wb.get_evidence(reference_id)["probe"])
        unc = math.hypot(unc, ref_unc or 0.0)
        return self._record(evidence_id, offset, drift, unc, "event",
                            {"frame": frame, "reference_frame": reference_frame, "event": label.strip()[:300],
                             "case_time": T}, actor, reference=reference_id)

    def sync_two_events(self, evidence_id: str, frames: tuple[int, int], reference_id: str, reference_frames: tuple[int, int],
                        actor: Actor, *, label: str = "") -> dict:
        (f1, f2), (r1, r2) = frames, reference_frames
        T1, u1 = self.global_time(reference_id, r1)
        T2, _ = self.global_time(reference_id, r2)
        lt = self.local_times(evidence_id)
        if not (0 <= f1 < len(lt) and 0 <= f2 < len(lt)) or f1 == f2:
            raise ValueError("choose two different frames inside the recording")
        scale = (T2 - T1) / (lt[f2] - lt[f1])
        drift = (scale - 1) * 1e6
        offset = T1 - lt[f1] * scale
        unc = math.hypot(1 / _rate(self.wb.get_evidence(evidence_id)["probe"]), u1 or 0.0)
        return self._record(evidence_id, offset, drift, unc, "two_events",
                            {"frames": [f1, f2], "reference_frames": [r1, r2], "event": label.strip()[:300],
                             "elapsed_local_s": lt[f2] - lt[f1], "elapsed_reference_s": T2 - T1}, actor, reference=reference_id)

    # Signal correlation ------------------------------------------------------------------------------------

    def _audio_envelope(self, evidence_id: str, rate: int = 1000) -> np.ndarray:
        path = self.wb.evidence_path(evidence_id)
        cmd = [self.wb.settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1",
               "-ar", "16000", "-f", "s16le", "-"]
        p = subprocess.run(cmd, capture_output=True, check=False)
        if p.returncode != 0 or not p.stdout:
            raise ValueError(f"{self.wb.get_evidence(evidence_id)['filename']} has no readable audio")
        x = np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768
        env = np.abs(x)
        k = 16000 // rate
        env = env[: len(env) // k * k].reshape(-1, k).mean(axis=1)
        # Containers can start the audio later (or earlier) than the first video frame; decoding ignores
        # that, so shift the envelope to put sample 0 at the first frame's time, like the frame clock.
        lead = round(self._audio_lead_s(path) * rate)
        if lead > 0:
            env = np.concatenate([np.zeros(lead, env.dtype), env])
        elif lead < 0:
            env = env[-lead:]
        w = max(3, rate // 2)  # remove level changes slower than about half a second
        return env - np.convolve(env, np.ones(w) / w, mode="same")

    def _audio_lead_s(self, path) -> float:
        """Audio stream start minus video stream start, in seconds (0 when either is unknown)."""
        cmd = [self.wb.settings.ffprobe, "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json", str(path)]
        try:
            info = json.loads(subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout or b"{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0.0
        start = {}
        for st in info.get("streams", []):
            t = st.get("start_time")
            if st.get("codec_type") in ("audio", "video") and st["codec_type"] not in start and t not in (None, "N/A"):
                start[st["codec_type"]] = float(t)
        return start["audio"] - start["video"] if len(start) == 2 else 0.0

    def _brightness(self, evidence_id: str, rate: int = 100) -> np.ndarray:
        path = self.wb.evidence_path(evidence_id)
        g = self.wb.videoops._luma_stream(path, width=64)
        mean = g.reshape(len(g), -1).mean(axis=1)
        lt = np.array(self.local_times(evidence_id))[: len(mean)]
        grid = np.arange(0, lt[-1], 1 / rate)
        sig = np.interp(grid, lt, mean)
        return np.diff(sig, prepend=sig[0])

    @staticmethod
    def _xcorr(a: np.ndarray, b: np.ndarray, rate: float, max_lag_s: float) -> dict:
        """Lag (seconds) that best aligns b to a: a(t) ≈ b(t - lag)."""
        if a.std() == 0 or b.std() == 0:
            raise ValueError("one of the signals is constant; there is nothing to correlate")
        a = (a - a.mean()) / a.std()
        b = (b - b.mean()) / b.std()
        n = 1 << int(math.ceil(math.log2(len(a) + len(b))))
        c = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
        lags = np.concatenate([np.arange(0, n // 2), np.arange(-n // 2, 0)])
        m = int(max_lag_s * rate)
        keep = np.abs(lags) <= m
        c, lags = c[keep], lags[keep]
        c = c / min(len(a), len(b))
        i = int(np.argmax(c))
        peak = float(c[i])
        excl = np.abs(lags - lags[i]) > max(2, int(0.05 * rate))
        second = float(c[excl].max()) if excl.any() else 0.0
        # Sub-sample refinement by parabola through the peak.
        frac = 0.0
        if 0 < i < len(c) - 1:
            den = c[i - 1] - 2 * c[i] + c[i + 1]
            frac = 0.5 * (c[i - 1] - c[i + 1]) / den if den != 0 else 0.0
        return {"lag_s": (lags[i] + frac) / rate, "peak": round(peak, 4), "second_peak": round(second, 4),
                "peak_ratio": round(peak / second, 3) if second > 0 else None, "resolution_s": 1 / rate}

    def sync_correlate(self, evidence_id: str, reference_id: str, actor: Actor, *, signal: str = "audio",
                       max_lag_s: float = 30.0, distance_m: float | None = None) -> dict:
        """Find the offset between two recordings from their audio or brightness signals.

        The result is a candidate: if the peak is not clearly above the next-best match (ratio
        < 1.5) it is recorded as weak and must be confirmed by an event. Microphones at different
        distances from a sound hear it at different times (343 m/s); give `distance_m`, the
        difference in distance, to include that in the uncertainty."""
        ref = self.current(reference_id)
        if not ref:
            raise ValueError("synchronise the reference recording first")
        rate = 1000 if signal == "audio" else 100
        if signal == "audio":
            a, b = self._audio_envelope(reference_id, rate), self._audio_envelope(evidence_id, rate)
        elif signal == "visual":
            a, b = self._brightness(reference_id, rate), self._brightness(evidence_id, rate)
        else:
            raise ValueError("signal must be audio or visual")
        r = self._xcorr(a, b, rate, max_lag_s)
        # local time t_b in evidence corresponds to reference local time t_b + lag.
        offset = ref["offset_s"] + r["lag_s"] * (1 + ref["drift_ppm"] * 1e-6)
        unc = math.hypot(r["resolution_s"], ref["uncertainty_s"] or 0.0)
        if signal == "audio" and distance_m:
            unc = math.hypot(unc, abs(distance_m) / SPEED_OF_SOUND)
        strength = "strong" if (r["peak_ratio"] or 0) >= 1.5 else "weak"
        details = {**r, "signal": signal, "max_lag_s": max_lag_s, "strength": strength,
                   "distance_difference_m": distance_m,
                   "assumption": "both recordings captured the same sounds/light changes; drift over the overlap is negligible"}
        rec = self._record(evidence_id, offset, ref["drift_ppm"], unc, "audio" if signal == "audio" else "visual", details,
                           actor, reference=reference_id)
        return rec

    # Timeline and synchronised frames -----------------------------------------------------------------------

    def timeline(self, case_id: str) -> dict:
        items, events = [], []
        cams = {c["id"]: c for c in self.cameras(case_id)}
        for ev in self.wb.list_evidence(case_id):
            s = self.current(ev["id"])
            lt = self.local_times(ev["id"]) if ev["media_type"] in ("video", "image") else [0.0]
            entry = {"evidence_id": ev["id"], "filename": ev["filename"], "media_type": ev["media_type"],
                     "camera": cams.get(ev.get("camera_id") or "", {}).get("label"), "sync": s}
            if s:
                k = 1 + s["drift_ppm"] * 1e-6
                entry["start"] = s["offset_s"] + lt[0] * k
                entry["end"] = s["offset_s"] + lt[-1] * k
                entry["start_label"], entry["end_label"] = _iso(entry["start"]), _iso(entry["end"])
            items.append(entry)
            for b in self.wb.casework.bookmarks(case_id, ev["id"]):
                e = {"evidence_id": ev["id"], "filename": ev["filename"], "camera": entry["camera"], "frame": b["frame"],
                     "label": b["label"], "note": b["note"], "kind": b["kind"], "local_time": b["time"]}
                if s and b["frame"] < len(lt):
                    e["case_time"] = s["offset_s"] + lt[b["frame"]] * (1 + s["drift_ppm"] * 1e-6)
                    e["case_time_label"] = _iso(e["case_time"])
                    e["uncertainty_s"] = s["uncertainty_s"]
                events.append(e)
        events.sort(key=lambda e: (e.get("case_time") is None, e.get("case_time") or 0))
        return {"items": items, "events": events, "methods": METHODS,
                "note": "Case times come from the synchronisation records; each carries its own uncertainty. "
                        "Unsynchronised items are listed without case times."}

    def synchronized_frames(self, case_id: str, T: float, actor: Actor, evidence_ids: list[str] | None = None,
                            tile_width: int = 480) -> dict:
        ids = evidence_ids or [e["id"] for e in self.wb.list_evidence(case_id) if e["media_type"] == "video"]
        cams = {c["id"]: c for c in self.cameras(case_id)}
        tiles, rows = [], []
        for eid in ids:
            ev = self.wb.get_evidence(eid)
            hit = self.frame_at(eid, T)
            label = cams.get(ev.get("camera_id") or "", {}).get("label") or ev["filename"]
            if hit is None:
                rows.append({"evidence_id": eid, "label": label, "frame": None})
                img = np.full((int(tile_width * 9 / 16), tile_width, 3), 30, np.uint8)
                cv2.putText(img, "no frame at this time", (12, img.shape[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (200, 200, 200), 1, cv2.LINE_AA)
            else:
                rows.append({"evidence_id": eid, "label": label, **hit})
                fr = media.to_uint8(self.wb.source_frame(eid, hit["frame"]))
                h = int(round(fr.shape[0] * tile_width / fr.shape[1]))
                img = cv2.resize(fr, (tile_width, h), interpolation=cv2.INTER_AREA)
            bar = np.full((40, tile_width, 3), 15, np.uint8)
            txt = f"{label[:40]}" + (f"  #{hit['frame']}  t={hit['local_time']:.3f}s" if hit else "")
            cv2.putText(bar, txt, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 240, 240), 1, cv2.LINE_AA)
            if hit:
                u = hit["uncertainty_s"]
                cv2.putText(bar, f"case {_iso(T)}  frame offset {hit['error_s'] * 1000:+.0f} ms"
                            + (f"  ±{u * 1000:.0f} ms" if u is not None else ""), (6, 33),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 200, 255), 1, cv2.LINE_AA)
            tiles.append(np.vstack([img, bar]))
        if not tiles:
            raise ValueError("no recordings to show")
        cols = 2 if len(tiles) > 1 else 1
        hmax = max(t.shape[0] for t in tiles)
        tiles = [np.vstack([t, np.zeros((hmax - t.shape[0], tile_width, 3), np.uint8)]) for t in tiles]
        while len(tiles) % cols:
            tiles.append(np.zeros_like(tiles[0]))
        grid = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
        png = media.encode_png(grid.astype(np.float32) / 255)
        digest, _ = self.wb.store.put_derived(png, "png")
        self.wb.audit.append(actor, "sync.frames", case_id=case_id, params={"case_time": T, "frames": rows},
                             output_hash=digest)
        return {"png": digest, "case_time": T, "case_time_label": _iso(T), "frames": rows}


def _rate(probe: dict) -> float:
    from fractions import Fraction

    try:
        r = float(Fraction(probe.get("frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        r = 0.0
    return r if r > 0 else 25.0


def _parse_iso(s: str) -> float:
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError("give the time as ISO 8601, e.g. 2026-03-14T21:05:33.200+01:00") from e
    if d.tzinfo is None:
        raise ValueError("give the time zone (e.g. +01:00 or Z); camera clocks are often in local time")
    return d.timestamp()
