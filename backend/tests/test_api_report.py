import json

import pytest
from fastapi.testclient import TestClient

from forensic import api
from forensic.config import load_settings
from forensic.workbench import Workbench

from .conftest import sign_in

H = {"X-Examiner": "Test Examiner"}


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield sign_in(c)
    api._wb.db.close()
    api._wb = None


def test_full_workflow(client, jpeg_path, video_path):
    assert client.post("/api/cases", json={"name": "x", "examiner": ""}, headers=H).status_code == 400
    case = client.post("/api/cases", json={"name": "Burglary 42", "examiner": "Test Examiner"}, headers=H).json()
    ev = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("scene.jpg", jpeg_path.read_bytes())}, headers=H).json()
    vid = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("clip.mp4", video_path.read_bytes())}, headers=H).json()
    assert ev["media_type"] == "image" and vid["media_type"] == "video"
    bad = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("notes.txt", b"hello")}, headers=H)
    assert bad.status_code == 400

    assert any(f["id"] == "exposure.clahe" for f in client.get("/api/filters").json())
    graph = {"nodes": [{"id": "n1", "filter": "exposure.levels", "params": {"gamma": 1.3}}, {"id": "n2", "filter": "denoise.median"}]}
    prev = client.post(f"/api/evidence/{ev['id']}/preview", json={"graph": graph, "upto": "n1"})
    assert prev.status_code == 200 and prev.headers["content-type"] == "image/png"
    assert len(json.loads(prev.headers["x-preview-info"])["steps"]) == 1

    ch = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "graph": graph}, headers=H).json()
    graph["nodes"].append({"id": "n3", "filter": "sharpen.unsharp_mask"})
    ch = client.put(f"/api/chains/{ch['id']}", json={"graph": graph}, headers=H).json()
    assert len(ch["graph"]["nodes"]) == 3
    r = client.post(f"/api/chains/{ch['id']}/render", headers=H).json()
    assert client.get(f"/api/derived/{r['outputs'][0]['png']}.png").status_code == 200
    assert client.post(f"/api/chains/{ch['id']}/verify", headers=H).json()["reproduced"]

    vch = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": vid["id"], "graph": {
        "source": {"frames": {"start": 0, "end": 5}}, "nodes": [{"filter": "stack.integrate"}]}}, headers=H).json()
    client.post(f"/api/chains/{vch['id']}/render", headers=H)

    for a in ("file.metadata", "tamper.ela", "tamper.jpeg_ghost"):
        assert client.post(f"/api/evidence/{ev['id']}/analyses", json={"analysis": a}, headers=H).status_code == 200
    assert client.post(f"/api/evidence/{vid['id']}/analyses", json={"analysis": "tamper.ela"}, headers=H).status_code == 400

    bundle = client.get(f"/api/cases/{case['id']}/report.json", headers=H).json()
    assert all(c["reproducibility"]["reproduced"] for c in bundle["chains"])
    assert all(e["integrity"]["ok"] for e in bundle["evidence"])
    assert bundle["audit"]["verification"]["ok"]
    html = client.get(f"/api/cases/{case['id']}/report.html", params={"examiner": "Test Examiner"}).text
    assert "reproduced bit for bit" in html and "Error Level Analysis" in html
    pdf = client.get(f"/api/cases/{case['id']}/report.pdf", headers=H)
    assert pdf.content[:4] == b"%PDF" and len(pdf.content) > 5000

    audit = client.get(f"/api/cases/{case['id']}/audit").json()
    actions = [e["action"] for e in audit["entries"]]
    for expected in ("case.create", "evidence.ingest", "evidence.rejected", "chain.create", "chain.update",
                     "chain.render", "chain.verify", "analysis.run", "report.generate"):
        assert expected in actions, expected
