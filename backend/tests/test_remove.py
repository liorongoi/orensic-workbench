import pytest
from fastapi.testclient import TestClient

from forensic import api
from forensic.audit import Actor
from forensic.config import load_settings
from forensic.workbench import Workbench

H = {"X-Examiner": "Test Examiner"}


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield c
    api._wb.db.close()
    api._wb = None


def test_remove_and_restore_evidence_keeps_original(client, jpeg_path):
    case = client.post("/api/cases", json={"name": "c", "examiner": "Test Examiner"}, headers=H).json()
    ev = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("scene.jpg", jpeg_path.read_bytes())}, headers=H).json()
    keep = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("keep.jpg", jpeg_path.read_bytes())}, headers=H).json()
    ch = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "name": "c1"}, headers=H).json()
    client.post(f"/api/evidence/{ev['id']}/analyses", json={"analysis": "file.metadata", "params": {}}, headers=H)

    assert client.post(f"/api/evidence/{ev['id']}/remove", json={"reason": "duplicate"}).status_code == 401
    r = client.post(f"/api/evidence/{ev['id']}/remove", json={"reason": "duplicate"}, headers=H).json()
    assert r["removed"]["reason"] == "duplicate" and r["removed"]["by"] == "Test Examiner"

    d = client.get(f"/api/cases/{case['id']}").json()
    assert [e["id"] for e in d["evidence"]] == [keep["id"]]
    assert [e["id"] for e in d["removed_evidence"]] == [ev["id"]]
    assert d["chains"] == [] and d["analyses"] == []
    assert client.get("/api/cases").json()[0]["evidence_count"] == 1
    # The original is untouched and still verifies.
    assert client.post(f"/api/evidence/{ev['id']}/verify", headers=H).json()["ok"]
    bad = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "name": "x"}, headers=H)
    assert bad.status_code == 400

    bundle = client.get(f"/api/cases/{case['id']}/report.json", params={"examiner": "Test Examiner"}).json()
    assert [e["id"] for e in bundle["removed_evidence"]] == [ev["id"]]
    html = client.get(f"/api/cases/{case['id']}/report.html", params={"examiner": "Test Examiner"}).text
    assert "Removed from the case" in html and "duplicate" in html

    client.post(f"/api/evidence/{ev['id']}/restore", headers=H)
    d = client.get(f"/api/cases/{case['id']}").json()
    assert {e["id"] for e in d["evidence"]} == {ev["id"], keep["id"]}
    assert [c["id"] for c in d["chains"]] == [ch["id"]]
    actions = [a["action"] for a in client.get(f"/api/cases/{case['id']}/audit").json()["entries"]]
    assert "evidence.remove" in actions and "evidence.restore" in actions


def test_remove_chain(client, jpeg_path):
    case = client.post("/api/cases", json={"name": "c", "examiner": "Test Examiner"}, headers=H).json()
    ev = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("scene.jpg", jpeg_path.read_bytes())}, headers=H).json()
    a = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "name": "a"}, headers=H).json()
    b = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "name": "b"}, headers=H).json()
    r = client.post(f"/api/chains/{a['id']}/remove", json={}, headers=H).json()
    assert r["removed"]["by"] == "Test Examiner"
    assert [c["id"] for c in client.get(f"/api/cases/{case['id']}").json()["chains"]] == [b["id"]]
    entries = client.get(f"/api/cases/{case['id']}/audit").json()["entries"]
    assert any(x["action"] == "chain.remove" and x["target"] == a["id"] for x in entries)


def test_agents_cannot_remove(wb, case, jpeg_path):
    ev = wb.ingest_path(case["id"], jpeg_path, Actor("Test Examiner"))
    with pytest.raises(ValueError):
        wb.remove_evidence(ev["id"], Actor("planner", "agent"))
