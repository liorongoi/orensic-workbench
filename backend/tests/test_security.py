"""Regressions for the security audit: playlist evidence, agent step removal, model card ids,
the reviewer's model hash check and the API host allowlist."""

import io
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from forensic import media, models
from forensic.agents import review_case
from forensic.audit import Actor

from .conftest import EXAMINER, ingest

AGENT = Actor("test-agent", "agent")


def test_playlist_evidence_is_rejected(wb, case, tmp_path):
    outside = tmp_path / "outside.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=32x32:rate=1", "-t", "1",
                    "-pix_fmt", "yuv420p", str(outside)], check=True)
    playlist = (f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n{outside}\n"
                "#EXT-X-ENDLIST\n").encode()
    with pytest.raises(media.MediaError):
        wb.ingest(case["id"], io.BytesIO(playlist), "export.m3u8", EXAMINER)
    assert wb.list_evidence(case["id"]) == []


def _examiner_chain(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    return wb.create_chain(case["id"], ev["id"], "examiner", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "exposure.brightness_contrast", "params": {"brightness": 0.2}},
        {"id": "n2", "filter": "exposure.brightness_contrast", "params": {"contrast": 1.5}}]})


def test_agent_cannot_remove_disable_or_reorder_examiner_steps(wb, case, jpeg_path):
    ch = _examiner_chain(wb, case, jpeg_path)
    nodes = ch["graph"]["nodes"]
    with pytest.raises(ValueError, match="remove"):
        wb.update_chain(ch["id"], ch["graph"] | {"nodes": nodes[1:]}, AGENT)
    with pytest.raises(ValueError, match="disable"):
        wb.update_chain(ch["id"], ch["graph"] | {"nodes": [nodes[0] | {"enabled": False}, nodes[1]]}, AGENT)
    with pytest.raises(ValueError, match="reorder"):
        wb.update_chain(ch["id"], ch["graph"] | {"nodes": [nodes[1], nodes[0]]}, AGENT)
    assert [n["id"] for n in wb.get_chain(ch["id"])["graph"]["nodes"]] == ["n1", "n2"]
    # The examiner can still remove their own step.
    assert len(wb.update_chain(ch["id"], ch["graph"] | {"nodes": nodes[1:]}, EXAMINER)["graph"]["nodes"]) == 1


def test_agent_can_withdraw_its_own_pending_proposal(wb, case, jpeg_path):
    ch = _examiner_chain(wb, case, jpeg_path)
    g = ch["graph"]
    g["nodes"].insert(1, {"id": "n3", "filter": "exposure.levels", "params": {}})
    ch = wb.update_chain(ch["id"], g, AGENT)
    assert ch["graph"]["nodes"][1].get("proposed_by")
    g = ch["graph"] | {"nodes": [n for n in ch["graph"]["nodes"] if n["id"] != "n3"]}
    assert [n["id"] for n in wb.update_chain(ch["id"], g, AGENT)["graph"]["nodes"]] == ["n1", "n2"]


@pytest.mark.parametrize("bad_id", ["../escaped", "a/b", "/abs/x", "..", ""])
def test_model_card_id_must_be_a_plain_name(tmp_path, monkeypatch, bad_id):
    monkeypatch.setenv("FORENSIC_MODELS_DIR", str(tmp_path / "work" / "lab"))
    onnx = tmp_path / "m.onnx"
    onnx.write_bytes(b"dummy")
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"id": bad_id, "kind": "enhancement", "task": "t", "architecture": "a",
                                "reference": "r", "source": "s", "licence": "l", "training_data": "d"}))
    with pytest.raises(models.ModelError):
        models.install(onnx, card)
    assert not list((tmp_path / "work").rglob("*.json"))


def test_reviewer_fails_a_model_whose_weights_changed(wb, case, jpeg_path, monkeypatch):
    ev = ingest(wb, case, jpeg_path)
    wb.create_chain(case["id"], ev["id"], "sr", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "ai.super_resolution", "params": {"model": "fsrcnn", "scale": "2"}}]})
    monkeypatch.setattr(models, "verify", lambda m: {"id": m["id"], "ok": False, "problem": "weights file missing"})
    items = review_case(wb, case["id"])["result"]["items"]
    hashes = [i for i in items if i["check"] == "model.hash"]
    assert hashes and all(i["level"] == "fail" and "weights file missing" in i["text"] for i in hashes)


def test_api_refuses_unknown_host_names(tmp_path, monkeypatch):
    monkeypatch.setenv("FORENSIC_DATA_DIR", str(tmp_path / "data"))
    from forensic import api

    monkeypatch.setattr(api, "_wb", None)
    c = TestClient(api.app, base_url="http://127.0.0.1:8000")
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert c.get("/api/health", headers={"Host": "rebind.attacker.example:8000"}).status_code == 400
