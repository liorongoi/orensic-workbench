"""Phase 0 gate: originals are immutable, the audit log is tamper-evident, and
re-running a chain from the original is byte-identical."""

import os
import stat

import numpy as np
import pytest

from forensic import media
from forensic.audit import Actor
from forensic.filters import REGISTRY, FilterError
from forensic.graph import normalise
from forensic.store import hash_file

from .conftest import EXAMINER, ingest


def test_ingest_hashes_and_locks_original(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    sha, md5, size = hash_file(jpeg_path)
    assert (ev["sha256"], ev["md5"], ev["size"]) == (sha, md5, size)
    stored = wb.evidence_path(ev["id"])
    assert not stat.S_IMODE(os.stat(stored).st_mode) & 0o222
    check = wb.verify_evidence(ev["id"], EXAMINER)
    assert check["ok"] and check["read_only"]


def test_verify_detects_changed_original(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    stored = wb.evidence_path(ev["id"])
    os.chmod(stored, 0o644)
    with open(stored, "r+b") as f:
        f.seek(200)
        f.write(b"\x00")
    check = wb.verify_evidence(ev["id"], EXAMINER)
    assert not check["ok"] and not check["read_only"]


def test_audit_chain_detects_edits(wb, case, jpeg_path):
    ingest(wb, case, jpeg_path)
    assert wb.audit.verify()["ok"]
    wb.db.execute("UPDATE audit SET actor = 'someone else' WHERE seq = 2")
    res = wb.audit.verify()
    assert not res["ok"] and res["broken_at"] == 2


def test_audit_chain_detects_deleted_entry(wb, case, jpeg_path):
    ingest(wb, case, jpeg_path)
    wb.note(case["id"], "checked", EXAMINER)
    wb.db.execute("DELETE FROM audit WHERE seq = 2")
    assert not wb.audit.verify()["ok"]


def _every_image_filter_chain():
    nodes = []
    for f in REGISTRY.values():
        if f.input_kind != "image":
            continue
        params = {}
        if f.id == "geometry.perspective":
            params = {"points": [[20, 20], [280, 30], [290, 200], [10, 210]]}
        if f.id == "geometry.crop":
            params = {"x": 4, "y": 4}
        if f.id == "deblur.variable":
            params = {"streaks": [[10, 10], [16, 12], [200, 150], [206, 146]], "tiles": 3}
        nodes.append({"filter": f.id, "params": params})
    return nodes


def test_rerun_is_byte_identical_for_every_filter(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    for i, node in enumerate(_every_image_filter_chain()):
        ch = wb.create_chain(case["id"], ev["id"], node["filter"], EXAMINER, {"nodes": [node]})
        rendered = wb.render_chain(ch["id"], EXAMINER)
        check = wb.verify_chain(ch["id"], EXAMINER)
        assert check["reproduced"], node["filter"]
        assert check["rerun_hash"] == rendered["output_hash"]


def test_png_export_bytes_are_reproducible(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    ch = wb.create_chain(case["id"], ev["id"], "c", EXAMINER, {"nodes": [{"filter": "exposure.clahe"}, {"filter": "denoise.nlm"}]})
    a = wb.render_chain(ch["id"], EXAMINER)["outputs"][0]["png"]
    wb.engine.cache = type(wb.engine.cache)(0)
    b = wb.render_chain(ch["id"], EXAMINER)["outputs"][0]["png"]
    assert a == b


def test_video_stabilise_and_integrate(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    assert ev["media_type"] == "video" and ev["probe"]["frame_count"] == 25
    graph = {
        "source": {"frames": {"start": 3, "end": 10}},
        "nodes": [
            {"filter": "denoise.median"},
            {"filter": "stack.stabilise", "params": {"motion": "translation"}},
            {"filter": "stack.integrate", "params": {"method": "median"}},
        ],
    }
    ch = wb.create_chain(case["id"], ev["id"], "video", EXAMINER, graph)
    out = wb.render_chain(ch["id"], EXAMINER)
    assert out["shape"] == [120, 160, 3]
    assert wb.verify_chain(ch["id"], EXAMINER)["reproduced"]


def test_frame_accurate_decode(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    stack = media.decode_frames(wb.evidence_path(ev["id"]), wb.settings, 0, 24, 160, 120)
    single = wb.source_frame(ev["id"], 17)
    assert np.array_equal(stack[17], single)
    table = wb.frame_table(ev["id"])
    assert len(table) == 25 and table[0]["type"] == "I"


def test_stack_filter_on_image_is_rejected(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    ch = wb.create_chain(case["id"], ev["id"], "c", EXAMINER, {"nodes": [{"filter": "stack.integrate"}]})
    with pytest.raises(FilterError):
        wb.render_chain(ch["id"], EXAMINER)


def test_param_validation():
    with pytest.raises(FilterError):
        normalise({"source": {"evidence_id": "x"}, "nodes": [{"filter": "exposure.levels", "params": {"gamma": 99}}]})
    with pytest.raises(FilterError):
        normalise({"source": {"evidence_id": "x"}, "nodes": [{"filter": "exposure.levels", "params": {"bogus": 1}}]})
    with pytest.raises(FilterError):
        normalise({"source": {"evidence_id": "x"}, "nodes": [{"filter": "exposure.levels", "version": "0.9.0"}]})
    with pytest.raises(FilterError):
        normalise({"source": {"evidence_id": "x"}, "nodes": [{"filter": "does.not.exist"}]})


def test_chain_pinned_to_original_hash(wb, case, jpeg_path, spliced_path):
    a = ingest(wb, case, jpeg_path)
    b = ingest(wb, case, spliced_path)
    ch = wb.create_chain(case["id"], a["id"], "c", EXAMINER)
    g = ch["graph"]
    g["source"]["sha256"] = b["sha256"]
    with pytest.raises(FilterError):
        wb.engine.run(g)


def test_agent_steps_are_proposals_until_accepted(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    bot = Actor("planner", "agent", "model-x")
    ch = wb.create_chain(case["id"], ev["id"], "p", bot, {"nodes": [{"filter": "exposure.clahe"}]})
    node = ch["graph"]["nodes"][0]
    assert node["proposed_by"] == "agent:planner (model-x)" and "accepted_by" not in node
    # An agent echoing an acceptance back does not count.
    g = ch["graph"]
    g["nodes"][0]["accepted_by"] = "human:Test Examiner"
    g["nodes"].append({"filter": "denoise.median"})
    ch = wb.update_chain(ch["id"], g, bot)
    assert all("accepted_by" not in n for n in ch["graph"]["nodes"])
    with pytest.raises(ValueError):
        wb.accept_proposals(ch["id"], bot)
    ch = wb.accept_proposals(ch["id"], EXAMINER)
    assert all(n["accepted_by"] == "human:Test Examiner" for n in ch["graph"]["nodes"])
    actions = [e["action"] for e in wb.audit_entries(case["id"])]
    assert "chain.accept_proposals" in actions
    agent_entries = [e for e in wb.audit_entries(case["id"]) if e["actor_kind"] == "agent"]
    assert agent_entries and agent_entries[0]["params"]["agent_model"] == "model-x"
