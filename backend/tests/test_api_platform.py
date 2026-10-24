"""HTTP API for the platform services, and the optional lab sign-in mode."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from forensic import api
from forensic.config import load_settings
from forensic.workbench import Workbench


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield c
    api.jobs().stop()
    api._wb.db.close()
    api._wb = None


def _upload(c, case_id, path):
    with open(path, "rb") as f:
        r = c.post(f"/api/cases/{case_id}/evidence", files={"file": (path.name, f)})
    r.raise_for_status()
    return r.json()


def test_platform_endpoints(client, video_path, jpeg_path):
    c = client
    assert c.get("/api/platform/profile").json()["auth"] == "none"
    case = c.post("/api/cases", json={"name": "API", "examiner": "E"}).json()
    vid = _upload(c, case["id"], video_path)
    img = _upload(c, case["id"], jpeg_path)
    # v1 alias
    assert c.get(f"/api/v1/evidence/{vid['id']}").json()["id"] == vid["id"]
    # case details, tags, bookmarks, custody, search
    assert c.post(f"/api/cases/{case['id']}/details", json={"case_number": "API-1"}).json()["case_number"] == "API-1"
    assert c.put(f"/api/evidence/{vid['id']}/tags", json={"tags": ["cctv"]}).json()["tags"] == ["cctv"]
    bm = c.post(f"/api/evidence/{vid['id']}/bookmarks", json={"frame": 3, "label": "start"}).json()
    assert c.get(f"/api/cases/{case['id']}/bookmarks").json()[0]["id"] == bm["id"]
    assert c.post(f"/api/evidence/{vid['id']}/bookmarks", json={"label": "no frame"}).status_code == 400
    assert c.get(f"/api/cases/{case['id']}/custody").json()[0]["event"] == "acquired"
    assert c.get("/api/search", params={"q": "cctv"}).json()["results"][0]["matched"] == ["tag"]
    assert c.get("/api/search", params={"q": "", "tag": "cctv"}).json()["total"] == 1
    # video
    clip = c.post(f"/api/evidence/{vid['id']}/clip", json={"start": 2, "end": 6}).json()
    assert clip["probe"]["frame_count"] == 5
    z = c.post(f"/api/evidence/{vid['id']}/frames.zip", json={"frames": [0, 1]})
    assert z.headers["content-type"] == "application/zip"
    assert c.get(f"/api/evidence/{vid['id']}/temporal").json()["frames"] == 25
    cmp_ = c.post("/api/compare", json={"a": {"evidence_id": img["id"]}, "b": {"evidence_id": img["id"]}}).json()
    assert cmp_["identical"]
    # sync
    assert c.post(f"/api/evidence/{vid['id']}/sync", json={"method": "reference", "start_iso": "2026-01-01T00:00:00Z"}).status_code == 200
    assert c.post(f"/api/evidence/{clip['id']}/sync", json={"method": "event", "frame": 0, "reference_id": vid["id"],
                                                             "reference_frame": 2}).status_code == 200
    tl = c.get(f"/api/cases/{case['id']}/timeline").json()
    assert len(tl["items"]) >= 2
    # redaction preview, save, render (queued)
    doc = {"regions": [{"keyframes": [{"frame": 0, "box": [10, 10, 30, 30]}], "hold": True, "method": "solid"}]}
    png = c.post(f"/api/evidence/{vid['id']}/redaction-preview/3.png", json=doc)
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    d = c.post(f"/api/evidence/{vid['id']}/documents", json={"kind": "redaction", "doc": doc}).json()
    red = c.post(f"/api/documents/{d['id']}/render", json={"codec": "ffv1"}).json()
    assert red["conversion"]["verification"]["ok"]
    # plates: manual add then review
    p = c.post(f"/api/evidence/{img['id']}/plates", json={"frame": 0, "corners": [[80, 60], [200, 60], [200, 120], [80, 120]]}).json()
    assert p["status"] == "unreviewed"
    assert c.post(f"/api/plates/{p['id']}/review", json={"action": "reject"}).json()["status"] == "rejected"
    # export package
    ex = c.post(f"/api/cases/{case['id']}/exports", json={"evidence_ids": [img["id"]], "purpose": "test"}).json()
    pkg = c.get(f"/api/exports/{ex['export_id']}/package.zip").content
    assert zipfile.ZipFile(io.BytesIO(pkg)).read("manifest.json")
    v = c.post("/api/exports/verify", files={"file": ("p.zip", pkg)}).json()
    assert v["ok"]
    # job kinds visible in the queue history
    kinds = {j["kind"] for j in c.get("/api/jobs").json()["recent"]}
    assert {"video.clip", "redaction.render", "case.export"} <= kinds


def test_accounts_mode(client, monkeypatch, jpeg_path):
    c = client
    assert c.post("/api/auth/login", json={}).status_code == 404  # off by default
    monkeypatch.setenv("FORENSIC_AUTH", "accounts")
    assert c.get("/api/cases").status_code == 401
    assert c.get("/api/auth/status").json()["needs_setup"]
    admin = {"username": "chief", "full_name": "Chief Examiner", "password": "a long admin password"}
    assert c.post("/api/auth/setup", json=admin).status_code == 200
    assert c.post("/api/auth/setup", json=admin).status_code == 409
    assert c.post("/api/auth/login", json={"username": "chief", "password": "wrong"}).status_code == 403
    assert c.post("/api/auth/login", json={"username": "chief", "password": admin["password"]}).status_code == 200
    case = c.post("/api/cases", json={"name": "Lab case", "examiner": "x"}).json()
    other = c.post("/api/cases", json={"name": "Other", "examiner": "x"}).json()
    ev = _upload(c, case["id"], jpeg_path)
    for u, role in (("ann", "examiner"), ("vic", "viewer")):
        assert c.post("/api/users", json={"username": u, "full_name": u.title(), "role": role,
                                          "password": "another long password"}).status_code == 200
    assert c.post(f"/api/cases/{case['id']}/members", json={"username": "ann", "role": "examiner"}).status_code == 200
    assert c.post(f"/api/cases/{case['id']}/members", json={"username": "vic", "role": "viewer"}).status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/cases").status_code == 401
    # Examiner: only their case
    c.post("/api/auth/login", json={"username": "ann", "password": "another long password"}).raise_for_status()
    assert [x["id"] for x in c.get("/api/cases").json()] == [case["id"]]
    assert c.get(f"/api/cases/{other['id']}").status_code == 403
    assert c.put(f"/api/evidence/{ev['id']}/tags", json={"tags": ["a"]}).status_code == 200
    assert c.get("/api/users").status_code == 403
    assert c.post("/api/batches", json={"output_dir": "/tmp/x", "input_dir": "/tmp"}).status_code == 403
    # The audit log names the signed-in person.
    entries = api.wb().audit.entries(case["id"])
    assert any(e["actor"].startswith("Ann") for e in entries)
    c.post("/api/auth/logout")
    # Viewer: read only
    c.post("/api/auth/login", json={"username": "vic", "password": "another long password"}).raise_for_status()
    assert c.get(f"/api/evidence/{ev['id']}").status_code == 200
    assert c.put(f"/api/evidence/{ev['id']}/tags", json={"tags": ["b"]}).status_code == 403
    c.post("/api/auth/logout")
    # Lockout after five wrong passwords.
    for _ in range(5):
        c.post("/api/auth/login", json={"username": "vic", "password": "nope"})
    r = c.post("/api/auth/login", json={"username": "vic", "password": "another long password"})
    assert r.status_code == 403 and "too many" in r.json()["detail"]
