"""Still-image traffic detection: scene rules, lamps and background lights, regions, time, review,
training from corrections, re-run policy, exports, report and text-driven charts."""

import io
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from forensic import api
from forensic.audit import Actor
from forensic.config import load_settings
from forensic.traffic import charts, detectors, ensemble, learn, scene, timeinfo
from forensic.traffic import background as bgmod
from forensic.workbench import Workbench

DATA = Path(__file__).parent / "fixtures" / "traffic"
EX = Actor("Traffic Examiner")
LAMP = (60, 30)  # a street lamp, always lit


def night(seed: int, cars: list[tuple[int, int, str]], size=(360, 640), lamp=True, windows=False) -> np.ndarray:
    """A dark road: optional street lamp, optional pair of lit windows (scenery that looks like a lamp
    pair), and vehicles as lamp pairs (x, y, 'head'|'tail')."""
    H, W = size
    rng = np.random.default_rng(seed)
    img = np.zeros((H, W, 3), np.float32)
    img[:] = np.linspace(8, 30, H)[:, None, None]
    img[int(H * 0.55):, :] += 10  # road
    if lamp:
        cv2.circle(img, LAMP, 7, (255, 255, 255), -1)
        cv2.circle(img, LAMP, 14, (90, 90, 90), 2)
    if windows:
        cv2.rectangle(img, (520, 76), (527, 83), (255, 255, 255), -1)
        cv2.rectangle(img, (560, 76), (567, 83), (255, 255, 255), -1)
    for x, y, kind in cars:
        col = (255, 255, 255) if kind == "head" else (40, 40, 255)
        r = 5
        cv2.circle(img, (x, y), r, col, -1)
        cv2.circle(img, (x + 50, y), r, col, -1)
    img += rng.normal(0, 2.0, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def jpg(bgr) -> bytes:
    ok, b = cv2.imencode(".png", bgr)
    return b.tobytes()


# Scene rules -------------------------------------------------------------------------------------


def test_scene_lighting_rules():
    day = np.full((200, 300, 3), (120, 160, 190), np.uint8)
    day[100:] = (90, 100, 110)
    assert scene.analyse(day)["lighting"] == "day"
    assert scene.analyse(night(0, [(200, 250, "head")]))["lighting"] == "night"
    ir = cv2.cvtColor(cv2.cvtColor(night(1, []), cv2.COLOR_BGR2GRAY) + 40, cv2.COLOR_GRAY2BGR)
    assert scene.analyse(ir)["lighting"] == "night_ir"
    o = scene.analyse(day, "night")
    assert o["lighting"] == "night" and o["automatic_lighting"] == "day" and o["overridden"]
    with pytest.raises(ValueError):
        scene.analyse(day, "dusk")


def test_preprocess_never_touches_input():
    img = night(2, [(300, 250, "head")])
    before = img.copy()
    plan = scene.preprocess_plan(scene.analyse(img))
    assert [s["op"] for s in plan] == ["clahe"]
    out = scene.apply_plan(img, plan)
    assert np.array_equal(img, before) and not np.array_equal(out, img)
    haze = scene.dehaze(np.full((64, 64, 3), 200, np.uint8))
    assert haze.shape == (64, 64, 3)


# Lamps, background lights and regions --------------------------------------------------------------


def test_lamp_pairs_and_background_lights():
    img = night(3, [(300, 250, "head"), (450, 300, "tail")])
    found, info = detectors.lights_member(img)
    classes = sorted(c["cls"] for c in found)
    # Without a learned background the street lamp is an unpaired "vehicle_lamp".
    assert classes == ["vehicle_headlights", "vehicle_lamp", "vehicle_taillights"]
    bg, lights, stats = bgmod.learn([night(s, [(100 + 40 * s, 230, "head")]) for s in range(10, 15)])
    assert lights[LAMP[1], LAMP[0]] > 0.9 and stats["images"] == 5
    found, info = detectors.lights_member(img, background_lights=lights)
    assert sorted(c["cls"] for c in found) == ["vehicle_headlights", "vehicle_taillights"]
    assert info["background_lights_removed"] == 1
    head = next(c for c in found if c["cls"] == "vehicle_headlights")
    x, y, w, h = head["box"]
    assert x < 300 < 350 < x + w and y < 250 < y + h


def test_change_member_and_regions():
    bgs = [night(s, [], lamp=True) for s in range(20, 25)]
    bg, lights, _ = bgmod.learn(bgs)
    img = bgs[0].copy()
    cv2.rectangle(img, (400, 220), (470, 260), (180, 120, 60), -1)
    found, info = detectors.change_member(img, bg)
    assert info["used"] and any(detectors.iou(c["box"], [400, 220, 70, 40]) > 0.5 for c in found)
    regions = ensemble.validate_regions([{"kind": "exclude", "points": [[380, 200], [500, 200], [500, 300], [380, 300]]}])
    mask = ensemble.region_mask(img.shape[:2], regions)
    found2, _ = detectors.change_member(img, bg, allowed=mask)
    assert not found2
    with pytest.raises(ValueError):
        ensemble.validate_regions([{"kind": "include", "points": [[0, 0], [1, 1]]}])
    inc = ensemble.region_mask((100, 100), [{"kind": "include", "points": [[0, 0], [50, 0], [50, 50], [0, 50]]}])
    assert ensemble.inside(inc, [10, 10, 10, 10]) and not ensemble.inside(inc, [70, 70, 10, 10])


def test_fusion_noisy_or_and_threshold():
    ctx = ensemble.context(np.zeros((100, 200, 3), np.uint8), True)
    cands = [{"member": "yolox", "cls": "car", "score": 0.5, "box": [10, 10, 40, 30], "attrs": {}},
             {"member": "lights", "cls": "vehicle_headlights", "score": 0.6, "box": [12, 12, 38, 26],
              "attrs": {"lamps": [[15, 30, 5, 5], [40, 30, 5, 5]]}},
             {"member": "change", "cls": "vehicle", "score": 0.3, "box": [120, 50, 30, 20], "attrs": {}}]
    objs = ensemble.fuse(cands, {}, None, ctx)
    car = next(o for o in objs if o["cls"] == "car")
    assert car["members"] == {"lights": 0.6, "yolox": 0.5}
    assert car["fused_score"] == pytest.approx(1 - (1 - 0.5) * (1 - 0.7 * 0.6), abs=1e-4)
    assert car["kept"] and car["box"] == [10, 10, 40, 30]
    lone = next(o for o in objs if o["cls"] == "vehicle")
    assert lone["fused_score"] == pytest.approx(0.45 * 0.3, abs=1e-4) and not lone["kept"]


# Time --------------------------------------------------------------------------------------------------


def test_capture_time_sources():
    assert timeinfo.from_name("cam3_20240131_235959.jpg")[0].isoformat() == "2024-01-31T23:59:59+00:00"
    assert timeinfo.from_name("2024-01-31 23-59-59 north.png")[0].hour == 23
    assert timeinfo.from_name("IMG_0001.jpg") is None
    from PIL import Image

    im = Image.new("RGB", (8, 8))
    ex = im.getexif()
    sub = ex.get_ifd(0x8769)
    sub[0x9003] = "2024:05:01 10:00:00"
    sub[0x9011] = "+03:00"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex)
    utc, src = timeinfo.capture_time(buf.getvalue(), "x.jpg")
    assert utc == "2024-05-01T07:00:00+00:00" and "offset" in src
    assert timeinfo.local(utc, 2.5) == "2024-05-01T09:30:00+02:30"
    assert timeinfo.parse_user_time("2024-05-01 12:00:00", 3, True) == "2024-05-01T09:00:00+00:00"


# Learning ----------------------------------------------------------------------------------------------


def test_training_improves_separable_corrections():
    rng = np.random.default_rng(0)
    samples = []
    for i in range(60):
        lab = int(i % 3 != 0)
        f = list(rng.normal(0, 0.05, len(ensemble.FEATURES)))
        f[1] = 0.6 + (0.3 if lab else -0.3) + rng.normal(0, 0.05)  # lamp score separates
        fused = 0.5  # the fused score does not
        samples.append({"image": f"im{i % 6}", "features": f, "fused": fused, "score_current": fused, "label": lab})
    res = learn.train(samples, {"threshold": 0.4, "unrecoverable": 0})
    assert res["decision"] == "adopted" and res["rescorer"]
    assert res["cross_validated"]["f1"] > res["baseline"]["f1"]
    # Same data, same model: deterministic.
    assert learn.train(samples, {"threshold": 0.4, "unrecoverable": 0})["rescorer"] == res["rescorer"]
    labels, missing = learn.label_objects([{"box": [0, 0, 10, 10]}, {"box": [50, 50, 10, 10]}],
                                          [{"box": [1, 1, 10, 10]}, {"box": [200, 200, 5, 5]}])
    assert labels == [1, 0] and missing == 1


def test_chart_grammar():
    s = charts.parse("false alarms by hour and lighting as stacked for camera North gate from 2024-05-01", ["North gate"])
    assert s["measure"] == "false_alarms" and s["dims"] == ["hour", "lighting"] and s["type"] == "stacked"
    assert s["camera"] == "North gate" and s["from"] == "2024-05-01"
    s = charts.parse("images by status as pie night", [])
    assert s["dims"] == ["status"] and s["type"] == "pie" and s["lighting"] == "night"
    assert charts.parse("detections please by camera", [])["ignored"] == ["please"]


# Learned detector on a real picture ---------------------------------------------------------------------


def test_real_street_vehicles_found_people_vetoed(tmp_path):
    wb = Workbench(load_settings(tmp_path / "data"))
    try:
        case = wb.create_case("Street", "E", EX)
        with open(DATA / "street_day.jpg", "rb") as f:
            ev = wb.ingest(case["id"], f, "street_20240501_120000.jpg", EX)
        r = wb.traffic.detect(ev["id"], EX)
        img = wb.traffic.image(ev["id"])
        assert img["lighting"] == "day" and r["detections"] >= 1
        assert any(detectors.iou(d["box"], [733, 51, 34, 44]) > 0.5 for d in img["detections"])
        assert all(d["cls"] in detectors.VEHICLE_CLASSES for d in img["detections"])
        # Reproducible: the same image, settings and profile give the same output hash.
        assert wb.traffic.detect(ev["id"], EX)["output_hash"] == r["output_hash"]
        run = wb.traffic.get_run(r["run_id"])
        assert run["members"]["yolox"]["people"] >= 3 and run["models"][0]["sha256"]
    finally:
        wb.db.close()


# Whole workflow through the API ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield c
    api.jobs().stop()
    api._wb.db.close()
    api._wb = None


def _up(c, case_id, name, bgr):
    r = c.post(f"/api/cases/{case_id}/evidence", files={"file": (name, jpg(bgr))})
    r.raise_for_status()
    return r.json()


def _wait(c, job, timeout=120):
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_traffic_workflow_api(client):
    c = client
    case = c.post("/api/cases", json={"name": "Night road", "examiner": "E"}).json()
    cid = case["id"]
    evs = [_up(c, cid, f"north_2024050{1 + i // 3}_2{i % 3}0000.png",
               night(100 + i, [(150 + 60 * i, 240, "head"), (420, 290 + i * 3, "tail")], windows=True)) for i in range(6)]
    cams = c.get(f"/api/cases/{cid}/traffic/cameras").json()
    imgs = c.get(f"/api/cases/{cid}/traffic/images").json()
    assert len(imgs) == 6 and all(i["status"] == "pending" for i in imgs)
    cams = c.get(f"/api/cases/{cid}/traffic/cameras").json()
    cam = cams[0]["camera_id"]
    # UTC offset, a mask over the top strip, train after 3 corrections, lamps only (no learned detector needed here)
    r = c.put(f"/api/traffic/cameras/{cam}", json={"utc_offset_hours": 3, "settings": {"auto_train_after": 3,
              "members": {"yolox": False}}})
    assert r.status_code == 200 and r.json()["utc_offset_hours"] == 3
    imgs = c.get(f"/api/cases/{cid}/traffic/images").json()
    assert imgs[0]["capture_utc"] == "2024-05-01T20:00:00+00:00" and imgs[0]["local_time"] == "2024-05-01T23:00:00+03:00"

    job = c.post(f"/api/cases/{cid}/traffic/detect", json={}).json()
    assert _wait(c, job)["status"] == "done"
    imgs = c.get(f"/api/cases/{cid}/traffic/images").json()
    assert all(i["status"] == "detected" and i["lighting"] == "night" for i in imgs)
    first = c.get(f"/api/traffic/images/{evs[0]['id']}").json()
    # headlights + tail-lights + the pair of lit windows (scenery not learned yet). The single street
    # lamp scores below the default threshold.
    assert first["detection_count"] == 3
    run = c.get(f"/api/traffic/runs/{first['run_id']}").json()
    assert any(o["cls"] == "vehicle_lamp" and not o["kept"] for o in run["objects"])
    lamp = min((d for d in first["detections"] if d["cls"] == "vehicle_headlights"), key=lambda d: d["box"][1])
    assert lamp["box"][1] < 80

    # Review: the lamp is a false alarm (and becomes a learned scenery light); add a missed car; approve.
    fa = c.post(f"/api/traffic/detections/{lamp['id']}/false-alarm", json={"note": "street lamp"}).json()
    assert fa["state"] == "false_alarm" and fa["learned_static_light"]
    add = c.post(f"/api/traffic/images/{evs[0]['id']}/detections", json={"box": [560, 200, 60, 40], "cls": "car"}).json()
    assert add["origin"] == "manual"
    assert c.post(f"/api/traffic/images/{evs[0]['id']}/detections", json={"box": [5000, 0, 10, 10], "cls": "car"}).status_code == 400
    head = max((d for d in first["detections"] if d["cls"] == "vehicle_headlights"), key=lambda d: d["box"][1])
    ed = c.patch(f"/api/traffic/detections/{head['id']}", json={"cls": "car"}).json()
    assert ed["cls"] == "car" and ed["edited"]
    ap = c.post(f"/api/traffic/images/{evs[0]['id']}/approve", json={}).json()
    assert ap["status"] == "approved" and ap["detection_count"] == 3
    # A reviewed image is protected from re-runs.
    assert c.post(f"/api/traffic/images/{evs[0]['id']}/detect", json={}).status_code == 403
    assert c.post(f"/api/traffic/images/{evs[0]['id']}/detect", json={"force": True}).status_code == 400

    # The third correction (the class edit) reached auto_train_after: training was queued on its own.
    assert fa["training_job"] is None and add["training_job"] is None
    train_job = ed["training_job"]
    assert train_job and train_job["kind"] == "traffic.train"
    assert _wait(c, train_job)["status"] == "done"
    notes = c.get(f"/api/cases/{cid}/traffic/notifications").json()
    kinds = [n["kind"] for n in notes]
    assert "training_started" in kinds and "training_done" in kinds
    prof = c.get(f"/api/traffic/cameras/{cam}/profiles").json()
    assert prof and prof[0]["training"]["images"] == [evs[0]["id"]]
    # Unedited images were re-run with the new profile (if adopted); the approved one was left alone.
    after = c.get(f"/api/traffic/images/{evs[0]['id']}").json()
    assert after["status"] == "approved" and after["run_id"] == first["run_id"]
    if prof[0]["status"] == "adopted":
        assert "rerun_done" in kinds
        other = c.get(f"/api/traffic/images/{evs[1]['id']}").json()
        assert other["run"]["profile_id"] == prof[0]["id"]
    # The deleted windows are now a scenery light: not detected on unedited images re-run afterwards.
    if prof[0]["status"] != "adopted":
        c.post(f"/api/cases/{cid}/traffic/detect", json={"only": "unedited", "evidence_ids": [evs[1]["id"]]})
    other = c.get(f"/api/traffic/images/{evs[1]['id']}").json()
    assert other["detection_count"] == 2 and all(d["box"][1] > 150 for d in other["detections"])
    assert c.post(f"/api/cases/{cid}/traffic/notifications/read", json={}).json()["marked"] >= 2

    # Background learning from the unedited night images
    bg = c.post(f"/api/traffic/cameras/{cam}/background", json={}).json()
    assert bg["learned"] and bg["learned"][0]["lighting"] == "night"

    # Exports
    x = c.get(f"/api/cases/{cid}/traffic/export.xlsx")
    wbk = load_workbook(io.BytesIO(x.content))
    assert wbk.sheetnames == ["Images", "Detections", "Corrections", "Training", "Method"]
    rows = list(wbk["Images"].values)
    assert rows[0][:4] == ("image_name", "camera", "local_time", "utc_offset_hours") and len(rows) == 7
    kinds = {r[2] for r in list(wbk["Corrections"].values)[1:]}
    assert {"false_alarm", "missed", "class_edit"} <= kinds
    assert c.get(f"/api/cases/{cid}/traffic/export.csv").text.splitlines()[0].startswith("﻿image_name")
    ds = c.get(f"/api/cases/{cid}/traffic/dataset.zip")
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(ds.content))
    assert "classes.txt" in z.namelist() and json.loads(z.read("manifest.json"))["images"][0]["boxes"] == 3

    # Charts and reports
    ch = c.get(f"/api/cases/{cid}/traffic/chart", params={"q": "detections by date and status as stacked"}).json()
    # Local dates: 20:00-22:00 UTC + 3 h crosses midnight.
    assert ch["x"] == ["2024-05-01", "2024-05-02", "2024-05-03"] and {s["name"] for s in ch["series"]} >= {"approved"}
    h = c.get(f"/api/cases/{cid}/traffic/report.html", params={"chart": "images by status as pie"}).text
    assert "Still-image traffic report" in h and "Images by status" in h and "<svg" in h
    pdf = c.get(f"/api/cases/{cid}/traffic/report.pdf")
    assert pdf.content[:5] == b"%PDF-"
    # Every step is in the audit log.
    acts = {e["action"] for e in c.get(f"/api/cases/{cid}/audit").json()["entries"]}
    assert {"traffic.config", "traffic.detect", "traffic.detection.false_alarm", "traffic.detection.add",
            "traffic.detection.edit", "traffic.image.approve", "traffic.train", "traffic.export", "traffic.report"} <= acts
