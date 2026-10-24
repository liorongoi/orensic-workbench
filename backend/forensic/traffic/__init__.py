"""Still-image traffic detection: vehicles in day and night images from fixed and mobile cameras.

Workflow (one case, one or more cameras):

1. Images are ingested as evidence (write-once, hashed). Each still image in the case becomes a row
   of the traffic image table with its name, capture time in UTC (EXIF or file name, or entered by
   the examiner), local time (UTC + the camera's offset in hours), lighting class, number of
   detections and review status.
2. Per camera the examiner sets the UTC offset and draws polygons: scan areas (detect only here)
   and masks (never detect here). An image can carry its own polygons instead of the camera's.
3. "Learn background" estimates, per camera and lighting class, the empty-scene picture and the
   map of scenery lights (background.py).
4. Detection runs the ensemble (detectors.py + ensemble.py) under the camera's current profile and
   stores every fused object (kept or not), the members' settings and model hashes, the scene
   analysis and an output hash, so a run can be repeated and compared.
5. Review: the examiner moves or resizes boxes, changes classes, deletes false alarms and adds missed
   vehicles, then approves the image. Every correction is kept in the feedback table with the image
   crop, for training and for later detector development. A deleted lamp false alarm also marks
   that place as a scenery light for the camera.
6. Training (learn.py) runs automatically after `auto_train_after` new corrections on a camera, or on
   request. The examiner is told when training starts and when it finishes, with the measured
   effect. A new profile re-runs detection only on images that were detected but never edited or
   approved; reviewed images keep exactly what the examiner left.

Statuses: pending (no run yet), detected (machine result, untouched), edited, approved.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import TYPE_CHECKING

import cv2
import numpy as np

from ..audit import Actor
from ..db import canonical_json, new_id, now_iso
from ..store import hash_bytes
from . import background as bgmod
from . import detectors as det
from . import ensemble, learn, scene, timeinfo

if TYPE_CHECKING:
    from ..workbench import Workbench

VERSION = "1.0.0"
DEFAULT_MODEL = "detect.yolox_s"
CLASSES = (*det.VEHICLE_CLASSES, *det.LIGHT_CLASSES, "vehicle")
STATUSES = ("pending", "detected", "edited", "approved")
DEFAULT_SETTINGS = {
    "model": DEFAULT_MODEL,
    "preprocess": "auto",
    "tile": "auto",
    "members": {"yolox": True, "lights": True, "change": True},
    "auto_train_after": 20,
}


class Traffic:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.db = wb.db

    # Cameras and configuration -------------------------------------------------------------------

    def default_camera(self, case_id: str, actor: Actor) -> dict:
        cams = self.wb.sync.cameras(case_id)
        if cams:
            return cams[0]
        return self.wb.sync.create_camera(case_id, "Camera 1", actor, notes="created for traffic detection")

    def config(self, camera_id: str) -> dict:
        cam = self.db.one("SELECT * FROM cameras WHERE id = ?", (camera_id,))
        if not cam:
            from ..workbench import NotFound

            raise NotFound(f"camera {camera_id} not found")
        row = self.db.one("SELECT * FROM traffic_config WHERE camera_id = ?", (camera_id,))
        settings = {**DEFAULT_SETTINGS, **(json.loads(row["settings_json"]) if row else {})}
        settings["members"] = {**DEFAULT_SETTINGS["members"], **settings.get("members", {})}
        return {
            "camera_id": camera_id, "case_id": cam["case_id"], "label": cam["label"], "location": cam["location"],
            "utc_offset_hours": row["utc_offset_hours"] if row else 0.0,
            "regions": json.loads(row["regions_json"]) if row else [],
            "static_lights": json.loads(row["static_lights_json"]) if row else [],
            "settings": settings,
            "profile": self._public_profile(self.current_profile(camera_id)),
            "backgrounds": [self._public_bg(b) for b in self.db.all(
                "SELECT * FROM traffic_backgrounds WHERE camera_id = ? AND current = 1 ORDER BY lighting", (camera_id,))],
        }

    def cameras(self, case_id: str) -> list[dict]:
        self.wb.get_case(case_id)
        out = []
        for c in self.wb.sync.cameras(case_id):
            cfg = self.config(c["id"])
            counts = {r["status"]: r["n"] for r in self.db.all(
                "SELECT status, COUNT(*) AS n FROM traffic_images WHERE camera_id = ? GROUP BY status", (c["id"],))}
            cfg["counts"] = counts
            cfg["pending_feedback"] = self._feedback_since_profile(c["id"])
            out.append(cfg)
        return out

    def create_camera(self, case_id: str, label: str, actor: Actor, utc_offset_hours: float = 0.0, location: str = "") -> dict:
        cam = self.wb.sync.create_camera(case_id, label, actor, location=location)
        self.set_config(cam["id"], actor, utc_offset_hours=utc_offset_hours)
        return self.config(cam["id"])

    def set_config(self, camera_id: str, actor: Actor, *, utc_offset_hours: float | None = None,
                   regions: list[dict] | None = None, settings: dict | None = None) -> dict:
        cur = self.config(camera_id)
        off = cur["utc_offset_hours"] if utc_offset_hours is None else float(utc_offset_hours)
        if not -14 <= off <= 14:
            raise ValueError("the UTC offset must be between -14 and +14 hours")
        regs = cur["regions"] if regions is None else ensemble.validate_regions(regions)
        st = dict(cur["settings"])
        if settings:
            for k, v in settings.items():
                if k not in DEFAULT_SETTINGS:
                    raise ValueError(f"unknown setting {k}")
                if k == "members":
                    st["members"] = {**st["members"], **{m: bool(x) for m, x in v.items() if m in det.MEMBERS}}
                elif k == "preprocess" and v not in ("auto", "none", "clahe", "dehaze"):
                    raise ValueError("preprocess must be auto, none, clahe or dehaze")
                elif k == "auto_train_after":
                    st[k] = max(1, int(v))
                elif k == "model":
                    if v not in ("detect.yolox_nano", "detect.yolox_tiny", "detect.yolox_s"):
                        raise ValueError("unknown detector model")
                    st[k] = v
                else:
                    st[k] = v
        self.db.execute(
            "INSERT INTO traffic_config (camera_id, case_id, utc_offset_hours, regions_json, settings_json, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET utc_offset_hours = excluded.utc_offset_hours,"
            " regions_json = excluded.regions_json, settings_json = excluded.settings_json, updated_at = excluded.updated_at",
            (camera_id, cur["case_id"], off, canonical_json(regs), canonical_json(st), now_iso()))
        self.wb.audit.append(actor, "traffic.config", case_id=cur["case_id"], target=camera_id,
                             params={"utc_offset_hours": off, "regions": regs, "settings": st})
        return self.config(camera_id)

    # Images ------------------------------------------------------------------------------------------

    def sync_images(self, case_id: str, actor: Actor) -> int:
        """Add a traffic row for every still image of the case that has none (capture time read once)."""
        rows = self.db.all(
            "SELECT e.id, e.filename, e.camera_id FROM evidence e LEFT JOIN traffic_images t ON t.evidence_id = e.id"
            " WHERE e.case_id = ? AND e.media_type = 'image' AND e.removed_json IS NULL AND t.evidence_id IS NULL",
            (case_id,))
        if not rows:
            return 0
        cam_default = None
        for r in rows:
            cam = r["camera_id"]
            if not cam:
                cam_default = cam_default or self.default_camera(case_id, actor)["id"]
                cam = cam_default
                self.db.execute("UPDATE evidence SET camera_id = ? WHERE id = ?", (cam, r["id"]))
            data = self.wb.evidence_path(r["id"]).read_bytes()
            utc, source = timeinfo.capture_time(data, r["filename"])
            self.db.execute(
                "INSERT OR IGNORE INTO traffic_images (evidence_id, case_id, camera_id, image_name, capture_utc, time_source,"
                " updated_at) VALUES (?,?,?,?,?,?,?)", (r["id"], case_id, cam, r["filename"], utc, source, now_iso()))
        return len(rows)

    def images(self, case_id: str, actor: Actor, *, camera_id: str | None = None, status: str | None = None) -> list[dict]:
        self.wb.get_case(case_id)
        self.sync_images(case_id, actor)
        sql = ("SELECT t.*, e.sha256, e.camera_id AS ev_camera FROM traffic_images t JOIN evidence e ON e.id = t.evidence_id"
               " WHERE t.case_id = ? AND e.removed_json IS NULL")
        args: list = [case_id]
        if camera_id:
            sql += " AND t.camera_id = ?"
            args.append(camera_id)
        if status:
            sql += " AND t.status = ?"
            args.append(status)
        sql += " ORDER BY COALESCE(t.capture_utc, ''), t.image_name"
        offsets = {r["camera_id"]: r["utc_offset_hours"] for r in self.db.all(
            "SELECT camera_id, utc_offset_hours FROM traffic_config WHERE case_id = ?", (case_id,))}
        return [self._public_image(r, offsets.get(r["camera_id"], 0.0)) for r in self.db.all(sql, tuple(args))]

    def image(self, evidence_id: str) -> dict:
        r = self._image_row(evidence_id)
        cfg = self.config(r["camera_id"])
        out = self._public_image(r, cfg["utc_offset_hours"])
        out["detections"] = self.detections(evidence_id)
        out["run"] = self.get_run(r["run_id"]) if r["run_id"] else None
        out["camera"] = {k: cfg[k] for k in ("camera_id", "label", "utc_offset_hours", "regions")}
        out["regions_effective"] = out["regions"] if out["regions"] is not None else cfg["regions"]
        ev = self.wb.get_evidence(evidence_id)
        out["width"], out["height"] = ev["probe"].get("width"), ev["probe"].get("height")
        return out

    def _image_row(self, evidence_id: str) -> dict:
        r = self.db.one("SELECT * FROM traffic_images WHERE evidence_id = ?", (evidence_id,))
        if not r:
            ev = self.wb.get_evidence(evidence_id)
            if ev["media_type"] != "image":
                raise ValueError("traffic detection works on still images; use Intelligence for video")
            self.sync_images(ev["case_id"], Actor("system", "human"))
            r = self.db.one("SELECT * FROM traffic_images WHERE evidence_id = ?", (evidence_id,))
        return r

    @staticmethod
    def _public_image(r: dict, offset: float) -> dict:
        return {
            "evidence_id": r["evidence_id"], "case_id": r["case_id"], "camera_id": r["camera_id"],
            "image_name": r["image_name"], "sha256": r.get("sha256"), "capture_utc": r["capture_utc"],
            "local_time": timeinfo.local(r["capture_utc"], offset), "utc_offset_hours": offset,
            "time_source": r["time_source"], "lighting": r["lighting_override"] or r["lighting"],
            "lighting_auto": r["lighting"], "lighting_overridden": bool(r["lighting_override"]),
            "scene": json.loads(r["scene_json"]), "detection_count": r["detection_count"], "status": r["status"],
            "run_id": r["run_id"], "approved_by": r["approved_by"], "approved_at": r["approved_at"],
            "regions": json.loads(r["regions_json"]) if r["regions_json"] else None, "updated_at": r["updated_at"],
        }

    def set_image(self, evidence_id: str, actor: Actor, *, camera_id: str | None = None, time: str | None = None,
                  time_is_local: bool = True, lighting: str | None = None, regions: list[dict] | None = None,
                  clear_regions: bool = False) -> dict:
        r = self._image_row(evidence_id)
        changes: dict = {}
        if camera_id and camera_id != r["camera_id"]:
            self.wb.sync.assign_camera(evidence_id, camera_id, actor)
            self.db.execute("UPDATE traffic_images SET camera_id = ? WHERE evidence_id = ?", (camera_id, evidence_id))
            changes["camera_id"] = camera_id
            r["camera_id"] = camera_id
        if time:
            off = self.config(r["camera_id"])["utc_offset_hours"]
            utc = timeinfo.parse_user_time(time, off, time_is_local)
            self.db.execute("UPDATE traffic_images SET capture_utc = ?, time_source = 'entered by examiner' WHERE evidence_id = ?",
                            (utc, evidence_id))
            changes["capture_utc"] = utc
        if lighting is not None:
            if lighting and lighting not in scene.LIGHTING:
                raise ValueError(f"lighting must be one of {', '.join(scene.LIGHTING)}")
            self.db.execute("UPDATE traffic_images SET lighting_override = ? WHERE evidence_id = ?", (lighting or None, evidence_id))
            changes["lighting_override"] = lighting or None
        if regions is not None or clear_regions:
            regs = None if clear_regions else ensemble.validate_regions(regions)
            self.db.execute("UPDATE traffic_images SET regions_json = ? WHERE evidence_id = ?",
                            (canonical_json(regs) if regs is not None else None, evidence_id))
            changes["regions"] = regs
        self.db.execute("UPDATE traffic_images SET updated_at = ? WHERE evidence_id = ?", (now_iso(), evidence_id))
        self.wb.audit.append(actor, "traffic.image.update", case_id=r["case_id"], target=evidence_id, params=changes)
        return self.image(evidence_id)

    # Background --------------------------------------------------------------------------------------

    def learn_background(self, camera_id: str, actor: Actor, lighting: str | None = None) -> dict:
        cfg = self.config(camera_id)
        imgs = self.db.all("SELECT * FROM traffic_images WHERE camera_id = ? ORDER BY COALESCE(capture_utc,''), image_name",
                           (camera_id,))
        groups: dict[tuple, list[tuple[str, np.ndarray]]] = {}
        for r in imgs:
            bgr = self._bgr(r["evidence_id"])
            light = r["lighting_override"] or r["lighting"] or self._scene_for(r["evidence_id"], bgr)["lighting"]
            if lighting and light != lighting:
                continue
            groups.setdefault((light, bgr.shape), []).append((r["evidence_id"], bgr))
        made = []
        for (light, shape), items in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            if len(items) < bgmod.MIN_IMAGES:
                continue
            bg, lights, stats = bgmod.learn([b for _, b in items])
            ok, png = cv2.imencode(".png", bg)
            img_sha, _ = self.wb.store.put_derived(png.tobytes(), "png")
            lights_sha, _ = self.wb.store.put_derived(bgmod.lights_to_png(lights), "png")
            bid = new_id()
            self.db.execute("UPDATE traffic_backgrounds SET current = 0 WHERE camera_id = ? AND lighting = ? AND width = ? AND height = ?",
                            (camera_id, light, shape[1], shape[0]))
            self.db.execute(
                "INSERT INTO traffic_backgrounds (id, case_id, camera_id, lighting, width, height, evidence_json, image_sha256,"
                " lights_sha256, stats_json, current, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (bid, cfg["case_id"], camera_id, light, shape[1], shape[0], canonical_json([e for e, _ in items[:bgmod.MAX_IMAGES]]),
                 img_sha, lights_sha, canonical_json(stats), actor.name, now_iso()))
            self.wb.audit.append(actor, "traffic.background.learn", case_id=cfg["case_id"], target=camera_id,
                                 params={"lighting": light, "images": [e for e, _ in items[:bgmod.MAX_IMAGES]], "stats": stats,
                                         "version": bgmod.VERSION}, output_hash=img_sha)
            made.append({"id": bid, "lighting": light, "size": [shape[1], shape[0]], **stats})
        skipped = [{"lighting": k[0], "size": [k[1][1], k[1][0]], "images": len(v)} for k, v in groups.items()
                   if len(v) < bgmod.MIN_IMAGES]
        return {"camera_id": camera_id, "learned": made, "skipped": skipped,
                "note": f"a background needs at least {bgmod.MIN_IMAGES} images of the same size and lighting"}

    def _background(self, camera_id: str, lighting: str, shape) -> tuple[dict | None, np.ndarray | None, np.ndarray | None]:
        row = self.db.one("SELECT * FROM traffic_backgrounds WHERE camera_id = ? AND lighting = ? AND width = ? AND height = ?"
                          " AND current = 1 ORDER BY created_at DESC LIMIT 1", (camera_id, lighting, shape[1], shape[0]))
        if not row:
            return None, None, None
        bg = cv2.imread(str(self.wb.store.derived_path(row["image_sha256"], "png")), cv2.IMREAD_COLOR)
        lights = bgmod.lights_from_png(self.wb.store.derived_path(row["lights_sha256"], "png").read_bytes())
        return row, bg, lights

    @staticmethod
    def _public_bg(b: dict) -> dict:
        return {"id": b["id"], "lighting": b["lighting"], "size": [b["width"], b["height"]],
                "images": len(json.loads(b["evidence_json"])), "image_sha256": b["image_sha256"],
                "lights_sha256": b["lights_sha256"], "stats": json.loads(b["stats_json"]), "created_at": b["created_at"]}

    # Detection ---------------------------------------------------------------------------------------

    def _bgr(self, evidence_id: str) -> np.ndarray:
        from .. import media

        return cv2.cvtColor(media.to_uint8(self.wb.source_frame(evidence_id, 0)), cv2.COLOR_RGB2BGR)

    def _scene_for(self, evidence_id: str, bgr: np.ndarray) -> dict:
        r = self.db.one("SELECT lighting_override FROM traffic_images WHERE evidence_id = ?", (evidence_id,))
        sc = scene.analyse(bgr, r["lighting_override"] if r else None)
        self.db.execute("UPDATE traffic_images SET lighting = ?, scene_json = ? WHERE evidence_id = ?",
                        (sc.get("automatic_lighting", sc["lighting"]), canonical_json(sc), evidence_id))
        return sc

    def detect(self, evidence_id: str, actor: Actor, *, force: bool = False, reason: str = "") -> dict:
        r = self._image_row(evidence_id)
        if r["status"] in ("edited", "approved") and not force:
            raise PermissionError(f"{r['image_name']} was {r['status']} by an examiner; re-running would replace the "
                                  "reviewed result. Re-run with force and a reason to do it anyway.")
        if force and r["status"] in ("edited", "approved") and not reason.strip():
            raise ValueError("give a reason for replacing a reviewed result")
        cfg = self.config(r["camera_id"])
        st = cfg["settings"]
        prof_row = self.current_profile(r["camera_id"])
        profile = json.loads(prof_row["profile_json"]) if prof_row else dict(ensemble.DEFAULT_PROFILE)
        bgr = self._bgr(evidence_id)
        sc = self._scene_for(evidence_id, bgr)
        night = sc["lighting"] != "day"
        regions = json.loads(r["regions_json"]) if r["regions_json"] else cfg["regions"]
        mask = ensemble.region_mask(bgr.shape[:2], regions)
        steps = scene.preprocess_plan(sc, st["preprocess"])
        work = scene.apply_plan(bgr, steps)
        cands: list[dict] = []
        members_info: dict = {}
        models_used: list[dict] = []
        people: list = []
        if st["members"].get("yolox"):
            d = self.wb.intel.detector(st["model"])
            c, info = det.yolox_member(d, work, tile=st["tile"], min_score=profile["member_min_score"])
            cands += c
            people = info.pop("people")
            info["people"] = len(people)
            members_info["yolox"] = {**info, "candidates": len(c), **det.MEMBER_INFO["yolox"]}
            models_used.append(d.describe())
        bg_row, bg_img, bg_lights = self._background(r["camera_id"], sc["lighting"], bgr.shape)
        if st["members"].get("lights") and night:
            lights_map = bgmod.add_static_lights(bg_lights, bgr.shape[:2], cfg["static_lights"])
            c, info = det.lights_member(bgr, background_lights=lights_map, allowed=mask, min_score=profile["member_min_score"])
            c, info["people_vetoed"] = det.veto_people(c, people)
            cands += c
            members_info["lights"] = {**info, "candidates": len(c), "background": bg_row["id"] if bg_row else None,
                                      "static_lights": len(cfg["static_lights"]), **det.MEMBER_INFO["lights"]}
        if st["members"].get("change"):
            c, info = det.change_member(bgr, bg_img, allowed=mask, min_score=profile["member_min_score"])
            c, vetoed = det.veto_people(c, people)
            info["people_vetoed"] = vetoed
            cands += c
            members_info["change"] = {**info, "candidates": len(c), "background": bg_row["id"] if bg_row else None,
                                      **det.MEMBER_INFO["change"]}
        objs = ensemble.fuse(cands, profile, mask, ensemble.context(bgr, night))
        run_id = new_id()
        params = {"settings": st, "regions": regions, "preprocess": steps, "profile_id": prof_row["id"] if prof_row else None,
                  "profile_sha256": prof_row["sha256"] if prof_row else hash_bytes(canonical_json(ensemble.DEFAULT_PROFILE).encode()),
                  "ensemble_version": ensemble.VERSION, "traffic_version": VERSION, "force_reason": reason.strip() or None}
        pub = [{k: o[k] for k in ("cls", "box", "score", "fused_score", "members", "in_region", "kept")} for o in objs]
        out_hash = hash_bytes(canonical_json({"evidence": self.wb.get_evidence(evidence_id)["sha256"], "params": params,
                                              "objects": pub}).encode())
        ts = now_iso()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO traffic_runs (id, case_id, evidence_id, camera_id, profile_id, params_json, models_json, scene_json,"
                " members_json, output_hash, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, r["case_id"], evidence_id, r["camera_id"], params["profile_id"], canonical_json(params),
                 canonical_json(models_used), canonical_json(sc), canonical_json(members_info), out_hash, actor.name, ts))
            for i, o in enumerate(objs):
                self.db.execute(
                    "INSERT INTO traffic_objects (run_id, idx, cls, x, y, w, h, fused_score, score, members_json, features_json,"
                    " in_region, kept) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, i, o["cls"], *o["box"], o["fused_score"], o["score"],
                     canonical_json({"scores": o["members"], "lamps": o["lamps"]}), canonical_json(o["features"]),
                     int(o["in_region"]), int(o["kept"])))
            self.db.execute("UPDATE traffic_detections SET state = 'superseded', updated_at = ? WHERE evidence_id = ? AND state = 'active'",
                            (ts, evidence_id))
            kept = 0
            for i, o in enumerate(objs):
                if not o["kept"]:
                    continue
                kept += 1
                self.db.execute(
                    "INSERT INTO traffic_detections (case_id, evidence_id, run_id, object_idx, cls, score, x, y, w, h, origin,"
                    " members_json, actor, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'auto', ?,?,?,?)",
                    (r["case_id"], evidence_id, run_id, i, o["cls"], o["score"], *o["box"],
                     canonical_json({"scores": o["members"], "lamps": o["lamps"]}), actor.name, ts, ts))
            self.db.execute("UPDATE traffic_images SET run_id = ?, detection_count = ?, status = 'detected', approved_by = NULL,"
                            " approved_at = NULL, updated_at = ? WHERE evidence_id = ?", (run_id, kept, ts, evidence_id))
        self.wb.audit.append(actor, "traffic.detect", case_id=r["case_id"], target=evidence_id,
                             params={"run_id": run_id, "detections": kept, "objects": len(objs), "lighting": sc["lighting"],
                                     "profile_id": params["profile_id"], "models": [m["model"] for m in models_used],
                                     "force_reason": params["force_reason"]}, output_hash=out_hash)
        return {"run_id": run_id, "evidence_id": evidence_id, "detections": kept, "objects": len(objs),
                "lighting": sc["lighting"], "output_hash": out_hash}

    def detect_many(self, case_id: str, actor: Actor, *, evidence_ids: list[str] | None = None, camera_id: str | None = None,
                    only: str = "pending", notify: bool = False) -> dict:
        """Run detection on a batch in order. `only`: pending (never run), unedited (pending + detected) or all
        unreviewed. Edited and approved images are never re-run here."""
        self.sync_images(case_id, actor)
        statuses = {"pending": ("pending",), "unedited": ("pending", "detected")}.get(only)
        if statuses is None:
            raise ValueError("only must be pending or unedited")
        rows = self.db.all("SELECT evidence_id, status, camera_id FROM traffic_images WHERE case_id = ? ORDER BY image_name", (case_id,))
        todo = [r["evidence_id"] for r in rows if r["status"] in statuses and (not camera_id or r["camera_id"] == camera_id)
                and (not evidence_ids or r["evidence_id"] in evidence_ids)]
        done, failed = [], []
        for eid in todo:
            try:
                done.append(self.detect(eid, actor))
            except Exception as e:  # noqa: BLE001 - one bad image must not stop the batch; reported per item
                failed.append({"evidence_id": eid, "error": str(e)})
        out = {"processed": len(done), "failed": failed, "detections": sum(d["detections"] for d in done)}
        if notify and todo:
            self.notify(case_id, "batch_done", f"Detection finished on {len(done)} images: {out['detections']} vehicles"
                        + (f"; {len(failed)} images failed ({failed[0]['error']})" if failed else "") + ".", out)
        return out

    def get_run(self, run_id: str) -> dict:
        r = self.db.one("SELECT * FROM traffic_runs WHERE id = ?", (run_id,))
        if not r:
            from ..workbench import NotFound

            raise NotFound(f"run {run_id} not found")
        objs = self.db.all("SELECT * FROM traffic_objects WHERE run_id = ? ORDER BY idx", (run_id,))
        return {"id": r["id"], "evidence_id": r["evidence_id"], "camera_id": r["camera_id"], "profile_id": r["profile_id"],
                "params": json.loads(r["params_json"]), "models": json.loads(r["models_json"]),
                "scene": json.loads(r["scene_json"]), "members": json.loads(r["members_json"]),
                "output_hash": r["output_hash"], "actor": r["actor"], "created_at": r["created_at"],
                "objects": [{"idx": o["idx"], "cls": o["cls"], "box": [o["x"], o["y"], o["w"], o["h"]],
                             "fused_score": o["fused_score"], "score": o["score"],
                             "members": json.loads(o["members_json"])["scores"], "in_region": bool(o["in_region"]),
                             "kept": bool(o["kept"])} for o in objs]}

    # Review ----------------------------------------------------------------------------------------------

    def detections(self, evidence_id: str, include_removed: bool = False) -> list[dict]:
        sql = "SELECT * FROM traffic_detections WHERE evidence_id = ?"
        sql += " AND state != 'superseded'" if include_removed else " AND state = 'active'"
        return [self._public_det(d) for d in self.db.all(sql + " ORDER BY id", (evidence_id,))]

    @staticmethod
    def _public_det(d: dict) -> dict:
        m = json.loads(d["members_json"])
        return {"id": d["id"], "evidence_id": d["evidence_id"], "cls": d["cls"], "score": d["score"],
                "box": [d["x"], d["y"], d["w"], d["h"]], "origin": d["origin"], "state": d["state"],
                "members": m.get("scores", {}), "edited": d["original_json"] is not None, "note": d["note"],
                "actor": d["actor"], "updated_at": d["updated_at"]}

    def _det_row(self, det_id: int) -> dict:
        d = self.db.one("SELECT * FROM traffic_detections WHERE id = ?", (det_id,))
        if not d or d["state"] == "superseded":
            from ..workbench import NotFound

            raise NotFound(f"detection {det_id} not found")
        return d

    def _feedback(self, img: dict, kind: str, actor: Actor, *, det_id: int | None, cls: str | None, box, before=None) -> None:
        crop_sha = None
        if box is not None:
            bgr = self._bgr(img["evidence_id"])
            x, y, w, h = (int(round(v)) for v in box)
            pad = int(0.15 * max(w, h))
            crop = bgr[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
            if crop.size:
                ok, png = cv2.imencode(".png", crop)
                crop_sha, _ = self.wb.store.put_derived(png.tobytes(), "png")
        self.db.execute(
            "INSERT INTO traffic_feedback (case_id, camera_id, evidence_id, detection_id, kind, cls, box_json, before_json,"
            " crop_sha256, lighting, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (img["case_id"], img["camera_id"], img["evidence_id"], det_id, kind, cls,
             canonical_json(box) if box is not None else None, canonical_json(before) if before is not None else None,
             crop_sha, img["lighting_override"] or img["lighting"], actor.name, now_iso()))

    def _mark_edited(self, evidence_id: str) -> None:
        n = self.db.one("SELECT COUNT(*) AS n FROM traffic_detections WHERE evidence_id = ? AND state = 'active'", (evidence_id,))["n"]
        self.db.execute("UPDATE traffic_images SET detection_count = ?, status = 'edited', approved_by = NULL, approved_at = NULL,"
                        " updated_at = ? WHERE evidence_id = ?", (n, now_iso(), evidence_id))

    def add_detection(self, evidence_id: str, box: list[float], cls: str, actor: Actor, note: str = "") -> dict:
        img = self._image_row(evidence_id)
        box = self._check_box(evidence_id, box)
        if cls not in CLASSES:
            raise ValueError(f"class must be one of {', '.join(CLASSES)}")
        ts = now_iso()
        cur = self.db.execute(
            "INSERT INTO traffic_detections (case_id, evidence_id, run_id, cls, score, x, y, w, h, origin, note, actor,"
            " created_at, updated_at) VALUES (?,?,?,?,NULL,?,?,?,?,'manual',?,?,?,?)",
            (img["case_id"], evidence_id, img["run_id"], cls, *box, note[:500], actor.name, ts, ts))
        did = cur.lastrowid
        self._feedback(img, "missed", actor, det_id=did, cls=cls, box=box)
        self._mark_edited(evidence_id)
        self.wb.audit.append(actor, "traffic.detection.add", case_id=img["case_id"], target=evidence_id,
                             params={"detection_id": did, "cls": cls, "box": box, "note": note[:500]})
        return self._public_det(self._det_row(did))

    def update_detection(self, det_id: int, actor: Actor, *, box: list[float] | None = None, cls: str | None = None) -> dict:
        d = self._det_row(det_id)
        if d["state"] != "active":
            raise ValueError("only an active detection can be edited; restore it first")
        img = self._image_row(d["evidence_id"])
        before = {"cls": d["cls"], "box": [d["x"], d["y"], d["w"], d["h"]]}
        new_box = self._check_box(d["evidence_id"], box) if box is not None else before["box"]
        new_cls = cls or d["cls"]
        if new_cls not in CLASSES:
            raise ValueError(f"class must be one of {', '.join(CLASSES)}")
        original = d["original_json"] or canonical_json(before)
        self.db.execute("UPDATE traffic_detections SET cls = ?, x = ?, y = ?, w = ?, h = ?, original_json = ?, updated_at = ?"
                        " WHERE id = ?", (new_cls, *new_box, original, now_iso(), det_id))
        kind = "class_edit" if box is None else "box_edit"
        self._feedback(img, kind, actor, det_id=det_id, cls=new_cls, box=new_box, before=before)
        self._mark_edited(d["evidence_id"])
        self.wb.audit.append(actor, "traffic.detection.edit", case_id=d["case_id"], target=d["evidence_id"],
                             params={"detection_id": det_id, "before": before, "after": {"cls": new_cls, "box": new_box}})
        return self._public_det(self._det_row(det_id))

    def delete_detection(self, det_id: int, actor: Actor, note: str = "") -> dict:
        """Mark a detection as a false alarm. The row stays (state false_alarm) for training and audit."""
        d = self._det_row(det_id)
        if d["state"] != "active":
            raise ValueError("this detection is already removed")
        img = self._image_row(d["evidence_id"])
        self.db.execute("UPDATE traffic_detections SET state = 'false_alarm', note = ?, updated_at = ? WHERE id = ?",
                        (note[:500], now_iso(), det_id))
        box = [d["x"], d["y"], d["w"], d["h"]]
        self._feedback(img, "false_alarm", actor, det_id=det_id, cls=d["cls"], box=box)
        members = json.loads(d["members_json"])
        learned_light = False
        # A lamp-only false alarm at night: the lamps are scenery. Remember them for this camera.
        if d["origin"] == "auto" and set(members.get("scores", {})) <= {"lights"} and members.get("lamps"):
            row = self.db.one("SELECT static_lights_json FROM traffic_config WHERE camera_id = ?", (img["camera_id"],))
            lights = json.loads(row["static_lights_json"]) if row else []
            lights += [[round(v, 1) for v in lb] for lb in members["lamps"]]
            if not row:
                self.set_config(img["camera_id"], actor)
            self.db.execute("UPDATE traffic_config SET static_lights_json = ? WHERE camera_id = ?",
                            (canonical_json(lights[-500:]), img["camera_id"]))
            learned_light = True
        self._mark_edited(d["evidence_id"])
        self.wb.audit.append(actor, "traffic.detection.false_alarm", case_id=d["case_id"], target=d["evidence_id"],
                             params={"detection_id": det_id, "cls": d["cls"], "box": box, "note": note[:500],
                                     "learned_static_light": learned_light})
        return self._public_det(self._det_row(det_id)) | {"learned_static_light": learned_light}

    def restore_detection(self, det_id: int, actor: Actor) -> dict:
        d = self._det_row(det_id)
        if d["state"] != "false_alarm":
            raise ValueError("only a detection marked as a false alarm can be restored")
        self.db.execute("UPDATE traffic_detections SET state = 'active', updated_at = ? WHERE id = ?", (now_iso(), det_id))
        img = self._image_row(d["evidence_id"])
        self._feedback(img, "restored", actor, det_id=det_id, cls=d["cls"], box=None)
        self._mark_edited(d["evidence_id"])
        self.wb.audit.append(actor, "traffic.detection.restore", case_id=d["case_id"], target=d["evidence_id"],
                             params={"detection_id": det_id})
        return self._public_det(self._det_row(det_id))

    def approve(self, evidence_id: str, actor: Actor, approved: bool = True) -> dict:
        img = self._image_row(evidence_id)
        if img["status"] == "pending":
            raise ValueError("run detection (or add detections by hand) before approving")
        if approved:
            if img["status"] == "detected":
                self._feedback(img, "confirmed", actor, det_id=None, cls=None, box=None)
            self.db.execute("UPDATE traffic_images SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ?"
                            " WHERE evidence_id = ?", (actor.name, now_iso(), now_iso(), evidence_id))
        else:
            self.db.execute("UPDATE traffic_images SET status = 'edited', approved_by = NULL, approved_at = NULL, updated_at = ?"
                            " WHERE evidence_id = ?", (now_iso(), evidence_id))
        self.wb.audit.append(actor, "traffic.image.approve" if approved else "traffic.image.reopen", case_id=img["case_id"],
                             target=evidence_id, params={"detections": img["detection_count"]})
        return self.image(evidence_id)

    def _check_box(self, evidence_id: str, box) -> list[float]:
        if not box or len(box) != 4:
            raise ValueError("a box is [x, y, width, height]")
        ev = self.wb.get_evidence(evidence_id)
        W, H = ev["probe"].get("width") or 0, ev["probe"].get("height") or 0
        x, y, w, h = (float(v) for v in box)
        x0, y0 = max(0.0, x), max(0.0, y)
        x1, y1 = min(float(W), x + w), min(float(H), y + h)
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise ValueError("the box must be at least 2 pixels wide and high inside the image")
        return [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]

    # Training ---------------------------------------------------------------------------------------------

    def current_profile(self, camera_id: str) -> dict | None:
        return self.db.one("SELECT * FROM traffic_profiles WHERE camera_id = ? AND status = 'adopted' ORDER BY version DESC LIMIT 1",
                           (camera_id,))

    @staticmethod
    def _public_profile(p: dict | None) -> dict | None:
        if not p:
            return None
        return {"id": p["id"], "camera_id": p["camera_id"], "version": p["version"], "status": p["status"], "sha256": p["sha256"],
                "profile": json.loads(p["profile_json"]), "metrics": json.loads(p["metrics_json"]),
                "training": json.loads(p["training_json"]), "actor": p["actor"], "created_at": p["created_at"]}

    def profiles(self, camera_id: str) -> list[dict]:
        return [self._public_profile(p) for p in self.db.all(
            "SELECT * FROM traffic_profiles WHERE camera_id = ? ORDER BY version DESC", (camera_id,))]

    def _feedback_since_profile(self, camera_id: str) -> int:
        last = self.db.one("SELECT MAX(created_at) AS t FROM traffic_profiles WHERE camera_id = ?", (camera_id,))["t"]
        return self.db.one("SELECT COUNT(*) AS n FROM traffic_feedback WHERE camera_id = ? AND created_at > ?",
                           (camera_id, last or ""))["n"]

    def wants_training(self, camera_id: str) -> bool:
        return self._feedback_since_profile(camera_id) >= self.config(camera_id)["settings"]["auto_train_after"]

    def training_set(self, camera_id: str, profile: dict) -> tuple[list[dict], int, list[str]]:
        imgs = self.db.all("SELECT * FROM traffic_images WHERE camera_id = ? AND status IN ('edited','approved') AND run_id IS NOT NULL",
                           (camera_id,))
        samples, unrec = [], 0
        for im in imgs:
            objs = self.db.all("SELECT * FROM traffic_objects WHERE run_id = ? AND in_region = 1 ORDER BY idx", (im["run_id"],))
            finals = [{"box": [d["x"], d["y"], d["w"], d["h"]]} for d in self.db.all(
                "SELECT * FROM traffic_detections WHERE evidence_id = ? AND state = 'active'", (im["evidence_id"],))]
            boxes = [{"box": [o["x"], o["y"], o["w"], o["h"]]} for o in objs]
            labels, missing = learn.label_objects(boxes, finals)
            unrec += missing
            for o, lab in zip(objs, labels):
                f = json.loads(o["features_json"])
                cur = ensemble.rescore(profile["rescorer"], f) if profile.get("rescorer") else o["fused_score"]
                samples.append({"image": im["evidence_id"], "features": f, "fused": o["fused_score"], "score_current": cur,
                                "label": lab})
        return samples, unrec, [im["evidence_id"] for im in imgs]

    def train(self, camera_id: str, actor: Actor, *, rerun: bool = True) -> dict:
        cfg = self.config(camera_id)
        case_id = cfg["case_id"]
        cur_row = self.current_profile(camera_id)
        cur = json.loads(cur_row["profile_json"]) if cur_row else dict(ensemble.DEFAULT_PROFILE)
        self.notify(case_id, "training_started", f"Training started for {cfg['label']} from "
                    f"{self._feedback_since_profile(camera_id)} new corrections.", {"camera_id": camera_id})
        samples, unrec, image_ids = self.training_set(camera_id, cur)
        res = learn.train(samples, {"threshold": cur["threshold"], "unrecoverable": unrec})
        version = (self.db.one("SELECT MAX(version) AS v FROM traffic_profiles WHERE camera_id = ?", (camera_id,))["v"] or 0) + 1
        if res["decision"] == "no_data":
            self.notify(case_id, "training_done", f"Training for {cfg['label']} found nothing to learn from yet: review and "
                        "correct some detected images first.", {"camera_id": camera_id, "decision": "no_data"})
            return {"decision": "no_data", "camera_id": camera_id}
        new = {**cur, "threshold": res["threshold"], "rescorer": res["rescorer"]}
        label_hash = hash_bytes(canonical_json([(s["image"], s["label"]) for s in samples]).encode())
        training = {"images": image_ids, "samples": res["samples"], "positives": res["positives"],
                    "negatives": res["negatives"], "unrecoverable_misses": unrec, "labels_sha256": label_hash,
                    "method": res["method"], "learn_version": learn.VERSION}
        metrics = {"baseline": res["baseline"], "cross_validated": res["cross_validated"], "decision": res["decision"],
                   "reason": res.get("reason")}
        pid = new_id()
        sha = hash_bytes(canonical_json(new).encode())
        self.db.execute(
            "INSERT INTO traffic_profiles (id, case_id, camera_id, version, parent_id, profile_json, metrics_json, training_json,"
            " sha256, status, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, case_id, camera_id, version, cur_row["id"] if cur_row else None, canonical_json(new),
             canonical_json(metrics), canonical_json(training), sha, res["decision"], actor.name, now_iso()))
        self.wb.audit.append(actor, "traffic.train", case_id=case_id, target=camera_id,
                             params={"profile_id": pid, "version": version, "decision": res["decision"], "metrics": metrics,
                                     "training": {k: v for k, v in training.items() if k != "images"}}, output_hash=sha)
        b, c = res["baseline"], res["cross_validated"]
        if res["decision"] == "adopted":
            msg = (f"Training finished for {cfg['label']}: profile v{version} adopted. On your {len(image_ids)} reviewed images "
                   f"F1 {b['f1']:.2f} → {c['f1']:.2f} (precision {c['precision']:.2f}, recall {c['recall']:.2f}, threshold "
                   f"{c['threshold']:.2f}).")
            if unrec:
                msg += f" {unrec} added vehicles had no candidate at all; they are kept for detector fine-tuning."
        else:
            msg = f"Training finished for {cfg['label']}: the current profile stays, because {res['reason']}."
        rerun_result = None
        if rerun and res["decision"] == "adopted":
            todo = [r["evidence_id"] for r in self.db.all(
                "SELECT evidence_id FROM traffic_images WHERE camera_id = ? AND status = 'detected'", (camera_id,))]
            msg += f" Re-running detection on {len(todo)} unedited images; edited and approved images are left as they are."
            self.notify(case_id, "training_done", msg, {"camera_id": camera_id, "profile_id": pid, "decision": res["decision"]})
            rerun_result = self.detect_many(case_id, actor, evidence_ids=todo, only="unedited") if todo else {"processed": 0}
            self.notify(case_id, "rerun_done", f"Re-run with profile v{version} finished on {rerun_result['processed']} images "
                        f"of {cfg['label']}.", {"camera_id": camera_id, "profile_id": pid, **rerun_result})
        else:
            self.notify(case_id, "training_done", msg, {"camera_id": camera_id, "profile_id": pid, "decision": res["decision"]})
        return {"profile": self._public_profile(self.db.one("SELECT * FROM traffic_profiles WHERE id = ?", (pid,))),
                "rerun": rerun_result}

    # Notifications -------------------------------------------------------------------------------------------

    def notify(self, case_id: str, kind: str, message: str, details: dict | None = None) -> None:
        self.db.execute("INSERT INTO traffic_notifications (case_id, kind, message, details_json, created_at) VALUES (?,?,?,?,?)",
                        (case_id, kind, message, canonical_json(details or {}), now_iso()))

    def notifications(self, case_id: str, unread_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM traffic_notifications WHERE case_id = ?" + (" AND read_at IS NULL" if unread_only else "")
        return [{**{k: r[k] for k in ("id", "kind", "message", "created_at", "read_at")}, "details": json.loads(r["details_json"])}
                for r in self.db.all(sql + " ORDER BY id DESC LIMIT 100", (case_id,))]

    def read_notifications(self, case_id: str, ids: list[int] | None = None) -> int:
        if ids:
            q = ",".join("?" * len(ids))
            cur = self.db.execute(f"UPDATE traffic_notifications SET read_at = ? WHERE case_id = ? AND id IN ({q}) AND read_at IS NULL",
                                  (now_iso(), case_id, *ids))
        else:
            cur = self.db.execute("UPDATE traffic_notifications SET read_at = ? WHERE case_id = ? AND read_at IS NULL",
                                  (now_iso(), case_id))
        return cur.rowcount

    # Pictures, exports -----------------------------------------------------------------------------------------

    def overlay(self, evidence_id: str, max_side: int = 0) -> bytes:
        """The image with its current detections and regions drawn (a derived picture, never evidence)."""
        img = self.image(evidence_id)
        bgr = self._bgr(evidence_id).copy()
        for r in img["regions_effective"] or []:
            pts = np.array(r["points"], np.int32)
            col = (0, 200, 0) if r["kind"] == "include" else (0, 0, 220)
            cv2.polylines(bgr, [pts], True, col, max(1, bgr.shape[1] // 600))
        lw = max(1, bgr.shape[1] // 500)
        for d in img["detections"]:
            x, y, w, h = (int(round(v)) for v in d["box"])
            col = (0, 220, 255) if d["origin"] == "manual" else (255, 160, 0)
            cv2.rectangle(bgr, (x, y), (x + w, y + h), col, lw)
            cv2.putText(bgr, f"{d['id']} {d['cls']}", (x, max(10, y - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.4 * lw, col, lw)
        if max_side and max(bgr.shape[:2]) > max_side:
            s = max_side / max(bgr.shape[:2])
            bgr = cv2.resize(bgr, (int(bgr.shape[1] * s), int(bgr.shape[0] * s)), interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", bgr)
        return png.tobytes()

    def table(self, case_id: str, actor: Actor) -> dict:
        images = self.images(case_id, actor)
        cams = {c["id"]: c for c in self.wb.sync.cameras(case_id)}
        dets = []
        for im in images:
            for d in self.detections(im["evidence_id"], include_removed=True):
                dets.append({"image_name": im["image_name"], "evidence_id": im["evidence_id"], **d})
        feedback = self.db.all("SELECT * FROM traffic_feedback WHERE case_id = ? ORDER BY id", (case_id,))
        profiles = self.db.all("SELECT * FROM traffic_profiles WHERE case_id = ? ORDER BY camera_id, version", (case_id,))
        return {"images": images, "cameras": cams, "detections": dets, "feedback": feedback,
                "profiles": [self._public_profile(p) for p in profiles]}

    def export(self, case_id: str, actor: Actor, fmt: str = "xlsx") -> tuple[bytes, str, str]:
        t = self.table(case_id, actor)
        cam_label = {k: v["label"] for k, v in t["cameras"].items()}
        cols = ["image_name", "camera", "local_time", "utc_offset_hours", "capture_utc", "time_source", "lighting",
                "detection_count", "status", "approved_by", "approved_at", "sha256", "evidence_id"]
        rows = [[im["image_name"], cam_label.get(im["camera_id"], ""), im["local_time"] or "", im["utc_offset_hours"],
                 im["capture_utc"] or "", im["time_source"], im["lighting"] or "", im["detection_count"], im["status"],
                 im["approved_by"] or "", im["approved_at"] or "", im["sha256"], im["evidence_id"]] for im in t["images"]]
        dcols = ["image_name", "detection_id", "class", "state", "origin", "score", "x", "y", "width", "height", "members", "edited",
                 "note", "actor"]
        drows = [[d["image_name"], d["id"], d["cls"], d["state"], d["origin"], d["score"], *d["box"],
                  ", ".join(f"{k}={v:.2f}" for k, v in d["members"].items()), "yes" if d["edited"] else "", d["note"], d["actor"]]
                 for d in t["detections"]]
        self.wb.audit.append(actor, "traffic.export", case_id=case_id, target=case_id, params={"format": fmt, "images": len(rows)})
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            w.writerows(rows)
            return buf.getvalue().encode("utf-8-sig"), "text/csv", "traffic-images.csv"
        if fmt == "json":
            return json.dumps({"images": t["images"], "detections": t["detections"], "profiles": t["profiles"]},
                              indent=1, default=str).encode(), "application/json", "traffic.json"
        if fmt != "xlsx":
            raise ValueError("format must be xlsx, csv or json")
        from openpyxl import Workbook
        from openpyxl.styles import Font

        wbk = Workbook()
        ws = wbk.active
        ws.title = "Images"
        for name, header, data in (("Images", cols, rows), ("Detections", dcols, drows)):
            sh = ws if name == "Images" else wbk.create_sheet(name)
            sh.append(header)
            for r in data:
                sh.append(r)
            for c in sh[1]:
                c.font = Font(bold=True)
            sh.freeze_panes = "A2"
            sh.auto_filter.ref = sh.dimensions
        fb = wbk.create_sheet("Corrections")
        fb.append(["id", "image", "kind", "class", "box", "before", "lighting", "crop_sha256", "actor", "time"])
        names = {im["evidence_id"]: im["image_name"] for im in t["images"]}
        for f in t["feedback"]:
            fb.append([f["id"], names.get(f["evidence_id"], f["evidence_id"]), f["kind"], f["cls"], f["box_json"], f["before_json"],
                       f["lighting"], f["crop_sha256"], f["actor"], f["created_at"]])
        pr = wbk.create_sheet("Training")
        pr.append(["camera", "version", "status", "method", "reviewed images", "positives", "negatives", "unrecoverable misses",
                   "F1 before", "F1 cross-validated", "precision", "recall", "threshold", "profile sha256", "time"])
        for p in t["profiles"]:
            m, tr = p["metrics"], p["training"]
            pr.append([cam_label.get(p["camera_id"], ""), p["version"], p["status"], tr["method"],
                       len(tr["images"]), tr["positives"], tr["negatives"], tr["unrecoverable_misses"], m["baseline"]["f1"],
                       m["cross_validated"]["f1"], m["cross_validated"]["precision"], m["cross_validated"]["recall"],
                       m["cross_validated"]["threshold"], p["sha256"], p["created_at"]])
        note = wbk.create_sheet("Method")
        for line in METHOD_NOTES:
            note.append([line])
        out = io.BytesIO()
        wbk.save(out)
        return out.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "traffic-detections.xlsx"

    def dataset(self, case_id: str, actor: Actor) -> bytes:
        """Reviewed images with their final boxes in YOLO text format, for detector fine-tuning elsewhere.
        Only reviewed (edited or approved) images; class list in classes.txt; a manifest with hashes."""
        imgs = self.db.all("SELECT t.*, e.sha256 FROM traffic_images t JOIN evidence e ON e.id = t.evidence_id"
                           " WHERE t.case_id = ? AND t.status IN ('edited','approved')", (case_id,))
        buf = io.BytesIO()
        manifest = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("classes.txt", "\n".join(CLASSES) + "\n")
            for im in imgs:
                ev = self.wb.get_evidence(im["evidence_id"])
                W, H = ev["probe"]["width"], ev["probe"]["height"]
                ext = ev["filename"].rsplit(".", 1)[-1].lower() if "." in ev["filename"] else "img"
                stem = f"{im['evidence_id']}"
                z.write(self.wb.evidence_path(im["evidence_id"]), f"images/{stem}.{ext}")
                lines = []
                for d in self.detections(im["evidence_id"]):
                    x, y, w, h = d["box"]
                    lines.append(f"{CLASSES.index(d['cls'])} {(x + w / 2) / W:.6f} {(y + h / 2) / H:.6f} {w / W:.6f} {h / H:.6f}")
                z.writestr(f"labels/{stem}.txt", "\n".join(lines) + ("\n" if lines else ""))
                manifest.append({"evidence_id": im["evidence_id"], "image_name": im["image_name"], "sha256": im["sha256"],
                                 "status": im["status"], "lighting": im["lighting_override"] or im["lighting"], "boxes": len(lines)})
            z.writestr("manifest.json", json.dumps({"case_id": case_id, "created_at": now_iso(), "images": manifest}, indent=1))
        data = buf.getvalue()
        self.wb.audit.append(actor, "traffic.dataset.export", case_id=case_id, target=case_id,
                             params={"images": len(manifest)}, output_hash=hash_bytes(data))
        return data

    def summary(self, case_id: str) -> dict:
        """Counts for reports: per camera and lighting."""
        rows = self.db.all("SELECT camera_id, COALESCE(lighting_override, lighting) AS lighting, status, COUNT(*) AS images,"
                           " SUM(detection_count) AS detections FROM traffic_images WHERE case_id = ?"
                           " GROUP BY camera_id, COALESCE(lighting_override, lighting), status", (case_id,))
        return {"rows": rows}


METHOD_NOTES = [
    "Lucida still-image traffic detection " + VERSION,
    "Detections are machine proposals unless the image status is 'approved' (reviewed by the examiner named).",
    "Ensemble members: learned detector YOLOX (COCO, Apache-2.0) with tiling for distant vehicles; vehicle lamp pairs at "
    "night with learned background lights removed; change against the camera's learned background.",
    "Local time = capture time in UTC + the camera's UTC offset entered by the examiner. Without an EXIF offset tag the "
    "camera clock is taken as UTC; the source of each time is in the time_source column.",
    "Training metrics are measured on the examiner's own reviewed images (internal consistency), not a validation for "
    "other cameras or conditions.",
    "False alarms and added (missed) vehicles are kept with image crops in the Corrections sheet.",
]
