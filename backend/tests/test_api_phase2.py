import asyncio

import pytest
from fastapi.testclient import TestClient

from forensic import api
from forensic.config import load_settings
from forensic.mcp_server import build
from forensic.validation import run as validation
from forensic.workbench import Workbench

from .test_phase2 import _camera

H = {"X-Examiner": "Test Examiner"}


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield c
    api._wb.db.close()
    api._wb = None


def test_phase2_endpoints(client, jpeg_path, video_path):
    cat = client.get("/api/catalogue").json()
    assert {"remux", "carve"} <= set(cat["conversion_methods"]) and "height" in cat["measurements"]
    assert any(a.get("card") for a in client.get("/api/analyses").json())

    case = client.post("/api/cases", json={"name": "Phase 2", "examiner": "Test Examiner"}, headers=H).json()
    ev = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("scene.jpg", jpeg_path.read_bytes())}, headers=H).json()
    vid = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("clip.mp4", video_path.read_bytes())}, headers=H).json()

    sr = client.post(f"/api/evidence/{ev['id']}/smart-report", headers=H).json()
    assert sr["level"] in ("green", "yellow", "red") and sr["result"]["line"]
    tri = client.post(f"/api/cases/{case['id']}/triage", json={}, headers=H).json()
    assert len(tri["items"]) == 2

    blur = client.post(f"/api/evidence/{ev['id']}/estimate-blur", json={}).json()
    assert {"length", "angle"} <= set(blur)

    plan = client.post(f"/api/cases/{case['id']}/plan", json={"evidence_id": vid["id"], "goal": "brighten the dark scene",
                                                            "frames": {"start": 0, "end": 3}}, headers=H).json()
    assert len(plan["proposed_chains"]) == 2

    proj = _camera()
    wp = [[-3, 8], [3, 8], [3, 20], [-3, 20]]
    inputs = {"plane_image_points": [proj([x, y, 0]) for x, y in wp], "plane_world_points": wp,
              "point_a": proj([0, 10, 0]), "point_b": proj([0, 15, 0])}
    m = client.post(f"/api/evidence/{ev['id']}/measurements", json={"kind": "distance", "inputs": inputs}, headers=H).json()
    assert abs(m["result"]["value"] - 5.0) < 1e-6
    assert client.post(f"/api/evidence/{ev['id']}/measurements", json={"kind": "speed", "inputs": inputs}, headers=H).status_code == 400

    bad = client.post(f"/api/cases/{case['id']}/camera-references", json={"name": "one image", "evidence_ids": [ev["id"]]}, headers=H)
    assert bad.status_code == 400
    assert client.post(f"/api/evidence/{ev['id']}/convert", json={"method": "carve"}, headers=H).status_code == 400

    got = client.get(f"/api/cases/{case['id']}").json()
    assert len(got["measurements"]) == 1 and got["triage"]
    html = client.get(f"/api/cases/{case['id']}/report.html", params={"examiner": "Test Examiner"}).text
    assert "Measurements" in html and "Triage" in html


def test_mcp_servers_list_phase2_tools():
    names = lambda kind: {t.name for t in asyncio.run(build(kind).list_tools())}  # noqa: E731
    assert {"measure_height", "measure_distance", "measure_speed", "vanishing_point"} <= names("measure")
    assert {"smart_report", "triage", "prnu_create_reference", "prnu_match"} <= names("authenticate")
    assert {"estimate_motion_blur", "plan_enhancement"} <= names("enhance")
    assert {"identify_container", "convert_evidence"} <= names("evidence")


def test_validation_quick_run_holds_published_floor(tmp_path):
    """Regression guard for the detector cards: a seeded quick run must stay above these floors."""
    res = validation.run(quick=True, out_dir=tmp_path, log=lambda *a, **k: None)["results"]
    assert res["adjpg"]["true_positive_rate"] >= 0.95 and res["adjpg"]["false_positive_rate"] <= 0.02
    assert res["adjpg_local"]["per_block_auc_mean"] >= 0.9
    assert res["nadjpg"]["false_positive_rate"] <= 0.02 and res["nadjpg"]["tpr_by_first_quality"]["Q1=60"] >= 0.9
    assert res["prnu"]["full_frame"]["true_positive_rate"] >= 0.95 and res["prnu"]["full_frame"]["false_positive_rate"] == 0
    assert res["prnu"]["cropped_quarter_frame_with_search"]["false_positive_rate"] == 0
    assert (tmp_path / "tamper.double_jpeg.json").exists()
