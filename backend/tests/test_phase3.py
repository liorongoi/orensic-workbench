"""Phase 3: AI-assisted filters and the generative policy, synthetic-media and face checks, video
authentication, photogrammetry, the reviewer agent and the bundled models."""

import datetime
import io
import json
import subprocess

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from forensic import models, policy
from forensic.agents import review_case
from forensic.audit import Actor
from forensic.filters import get_filter
from forensic.measure import measure
from forensic.report import build_bundle, render_html, render_pdf

from .conftest import EXAMINER, _scene, ingest

AGENT = Actor("test-agent", "agent")
JUSTIFICATION = "Shown to the court only to illustrate the vehicle's general shape, not to read the plate."


# Models ---------------------------------------------------------------------------------


def test_bundled_models_match_pinned_hashes():
    ms = models.manifest()
    assert {"sr.fsrcnn_x2", "sr.espcn_x4", "face.yunet"} <= {m["id"] for m in ms}
    for m in ms:
        assert models.verify(m)["ok"], m["id"]
        assert m["licence"] and m["source"] and m["training_data"]


def test_super_resolution_is_deterministic_and_generative():
    f = get_filter("ai.super_resolution")
    assert f.generative and f.ai
    img = _scene(seed=3).astype(np.float32)[:64, :80] / 255
    a = f.fn(img, f.validate({"model": "fsrcnn", "scale": "2"}))
    b = f.fn(img, f.validate({"model": "fsrcnn", "scale": "2"}))
    assert a.shape == (128, 160, 3) and np.array_equal(a, b)
    assert f.model_ids(f.validate({"model": "espcn", "scale": "3"})) == ["sr.espcn_x3"]


def test_lab_model_install_requires_a_card(tmp_path, monkeypatch):
    monkeypatch.setenv("FORENSIC_MODELS_DIR", str(tmp_path / "lab"))
    src = models.get("sr.fsrcnn_x2")
    onnx = tmp_path / "det.onnx"
    onnx.write_bytes(open(src["path"], "rb").read())
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"id": "lab.det", "kind": "detector", "task": "x"}))
    with pytest.raises(models.ModelError):
        models.install(onnx, card)


# Generative policy ------------------------------------------------------------------------


def test_generative_step_policy(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    ch = wb.create_chain(case["id"], ev["id"], "sr", EXAMINER)
    # An agent proposes the generative step and tries to justify it: the justification is dropped.
    g = ch["graph"]
    g["nodes"].append({"id": "n1", "filter": "ai.super_resolution", "params": {"model": "fsrcnn", "scale": "2"},
                       "justification": JUSTIFICATION})
    ch = wb.update_chain(ch["id"], g, AGENT)
    assert not ch["graph"]["nodes"][0].get("justification")
    with pytest.raises(ValueError, match="justif"):
        wb.accept_proposals(ch["id"], EXAMINER)
    # The examiner writes the justification, then accepts.
    g = ch["graph"]
    g["nodes"][0]["justification"] = JUSTIFICATION
    wb.update_chain(ch["id"], g, EXAMINER)
    wb.accept_proposals(ch["id"], EXAMINER)
    r = wb.render_chain(ch["id"], EXAMINER)
    assert r["policy"]["illustrative_only"] and r["policy"]["evidential_export"]
    assert r["models"][0]["model"] == "sr.fsrcnn_x2"
    png, info = wb.export_chain(ch["id"], EXAMINER)
    assert info["watermarked"] and info["justifications"][0]["justification"] == JUSTIFICATION
    # Measurement on a generative chain is refused.
    with pytest.raises(ValueError, match="generative"):
        wb.measure(ev["id"], "distance", {}, EXAMINER, chain_id=ch["id"])
    actions = [e["action"] for e in wb.audit_entries(case["id"])]
    assert "chain.generative_justification" in actions and "chain.export" in actions


def test_unjustified_generative_chain_cannot_be_exported(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    ch = wb.create_chain(case["id"], ev["id"], "sr", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "ai.super_resolution", "params": {"scale": "2"}}]})
    r = wb.render_chain(ch["id"], EXAMINER)
    assert r["policy"]["unjustified_steps"] == ["n1"] and not r["policy"]["evidential_export"]
    with pytest.raises(PermissionError):
        wb.export_chain(ch["id"], EXAMINER)


def test_watermark_marks_both_edges():
    img = np.full((200, 400, 3), 0.5, np.float32)
    out = policy.watermark(img)
    assert out[2, 200, 0] > out[2, 200, 1] + 0.1  # red band at the top
    assert out[-3, 200, 0] > out[-3, 200, 1] + 0.1  # and at the bottom
    assert np.allclose(out[100, 200], 0.5, atol=1 / 255)


def test_redact_faces_runs_with_manual_regions():
    f = get_filter("redact.faces")
    img = _scene(seed=4).astype(np.float32) / 255
    out = f.fn(img, f.validate({"method": "solid", "extra": [[10, 10], [50, 40]]}))
    assert np.all(out[10:40, 10:50] == 0) and np.array_equal(out[100:, 100:], img[100:, 100:])


# Synthetic media ------------------------------------------------------------------------


def _signed_c2pa_jpeg(tmp_path):
    c2pa = pytest.importorskip("c2pa")
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test")])
    ku = dict(content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False,
              encipher_only=False, decipher_only=False)
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .add_extension(x509.KeyUsage(digital_signature=False, key_cert_sign=True, crl_sign=True, **ku), True)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Generator"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test")]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(2)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False, **ku), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), False)
            .sign(ca_key, hashes.SHA256()))
    chain = leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)
    key = leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    signer = c2pa.Signer.from_info(c2pa.C2paSignerInfo(b"es256", chain, key, None))
    src = io.BytesIO()
    Image.fromarray(_scene(seed=5)).save(src, "JPEG", quality=90)
    src.seek(0)
    manifest = {"claim_generator_info": [{"name": "Test Image Generator", "version": "1.0"}], "title": "gen.jpg",
                "assertions": [{"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created",
                    "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}]}}]}
    dst = io.BytesIO()
    c2pa.Builder(json.dumps(manifest)).sign(signer, "image/jpeg", src, dst)
    good = tmp_path / "gen.jpg"
    good.write_bytes(dst.getvalue())
    # Tamper with the pixels after signing: flip bytes near the end of the entropy-coded data.
    data = bytearray(dst.getvalue())
    for i in range(len(data) - 200, len(data) - 180):
        data[i] ^= 0x01
    bad = tmp_path / "gen_edited.jpg"
    bad.write_bytes(bytes(data))
    return good, bad


def test_provenance_reads_c2pa_ai_declaration(wb, case, tmp_path):
    good, bad = _signed_c2pa_jpeg(tmp_path)
    r = wb.run_analysis(ingest(wb, case, good)["id"], "synthetic.provenance", {}, EXAMINER)["result"]
    codes = {f.get("code") for f in r["flags"]}
    assert "prov.ai_declared" in codes and "prov.c2pa_invalid" not in codes
    assert r["summary"].startswith("Consistent with AI generation")
    r = wb.run_analysis(ingest(wb, case, bad)["id"], "synthetic.provenance", {}, EXAMINER)["result"]
    assert "prov.c2pa_invalid" in {f.get("code") for f in r["flags"]}


def test_provenance_reads_generator_png_text(wb, case, tmp_path):
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text("parameters", "a photo of a cat, Steps: 30, Sampler: Euler a, Model: sd_xl_base_1.0")
    p = tmp_path / "gen.png"
    Image.fromarray(_scene(seed=6)).save(p, pnginfo=meta)
    r = wb.run_analysis(ingest(wb, case, p)["id"], "synthetic.provenance", {}, EXAMINER)["result"]
    assert "prov.ai_declared" in {f.get("code") for f in r["flags"]}
    assert r["findings"]["metadata_text"]["png:parameters"].startswith("a photo of a cat")


def _bayer_capture(seed=7, h=256, w=320):
    """A camera-like image: a scene sampled through an RGGB mosaic and demosaiced."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    scene = np.stack([0.5 + 0.3 * np.sin(xx / 7.0 + yy / 11.0), 0.5 + 0.3 * np.cos(xx / 5.0), 0.4 + 0.2 * np.sin(yy / 6.0)], 2)
    scene += rng.normal(0, 0.05, scene.shape)
    scene = np.clip(scene * 255, 0, 255).astype(np.uint8)
    raw = np.zeros((h, w), np.uint8)
    raw[0::2, 0::2] = scene[0::2, 0::2, 0]
    raw[0::2, 1::2] = scene[0::2, 1::2, 1]
    raw[1::2, 0::2] = scene[1::2, 0::2, 1]
    raw[1::2, 1::2] = scene[1::2, 1::2, 2]
    return cv2.cvtColor(cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR), cv2.COLOR_BGR2RGB), scene


def test_camera_traces_find_cfa_and_resampling(wb, case, tmp_path):
    cap, scene = _bayer_capture()
    p = tmp_path / "capture.png"
    Image.fromarray(cap).save(p)
    r = wb.run_analysis(ingest(wb, case, p)["id"], "synthetic.camera_traces", {}, EXAMINER)["result"]
    assert "trace.cfa" in {f.get("code") for f in r["flags"]}
    q = tmp_path / "rendered.png"
    Image.fromarray(scene).save(q)  # never went through a colour filter array
    r = wb.run_analysis(ingest(wb, case, q)["id"], "synthetic.camera_traces", {}, EXAMINER)["result"]
    assert "trace.cfa" not in {f.get("code") for f in r["flags"]}
    up = cv2.resize(scene, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_LINEAR)
    s = tmp_path / "resampled.png"
    Image.fromarray(up).save(s)
    r = wb.run_analysis(ingest(wb, case, s)["id"], "synthetic.camera_traces", {}, EXAMINER)["result"]
    assert "trace.resampled" in {f.get("code") for f in r["flags"]}


def test_learned_detector_slot_is_unavailable_without_a_model(wb):
    a = next(a for a in wb.analyses() if a["id"] == "synthetic.learned")
    assert a["ai"] and a["unavailable"]


def test_face_consistency_flags_a_pasted_region(wb, case, tmp_path):
    rng = np.random.default_rng(8)
    img = _scene(h=360, w=480, seed=8).astype(np.float64)
    img += rng.normal(0, 6, img.shape)
    region = (slice(120, 220), slice(180, 270))
    smooth = cv2.GaussianBlur(np.clip(img, 0, 255).astype(np.uint8), (0, 0), 2.5).astype(np.float64)
    img[region] = smooth[region]  # a patch with different noise and detail, as a pasted face would have
    p = tmp_path / "pasted.png"
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(p)
    ev = ingest(wb, case, p)
    r = wb.run_analysis(ev["id"], "face.consistency", {"detect": False, "regions": [[180, 120], [270, 220]]}, EXAMINER)["result"]
    assert "face.inconsistent" in {f.get("code") for f in r["flags"]}
    r = wb.run_analysis(ev["id"], "face.consistency", {"detect": False, "regions": [[300, 240], [390, 340]]}, EXAMINER)["result"]
    assert "face.inconsistent" not in {f.get("code") for f in r["flags"]}


# Video authentication ---------------------------------------------------------------------


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("video")
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", 8, "-c:v", "libx264", "-g", 50, "-pix_fmt", "yuv420p", d / "orig.mp4")
    _ff("-i", d / "orig.mp4", "-vf", "select='not(between(n,80,99))',setpts=N/25/TB", "-c:v", "libx264", "-pix_fmt", "yuv420p", d / "deleted.mp4")
    _ff("-i", d / "orig.mp4", "-filter_complex",
        "[0:v]trim=start_frame=0:end_frame=120,setpts=PTS-STARTPTS[a];[0:v]trim=start_frame=40:end_frame=60,setpts=PTS-STARTPTS[b];"
        "[0:v]trim=start_frame=120:end_frame=200,setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1[o]",
        "-map", "[o]", "-c:v", "libx264", "-pix_fmt", "yuv420p", d / "looped.mp4")
    # Double encoding: first GOP 15 at lower quality, then re-encoded with a long GOP (a typical export).
    src = "mandelbrot=size=320x240:rate=25,noise=alls=6:allf=t"
    _ff("-f", "lavfi", "-i", src, "-t", 10, "-c:v", "libx264", "-g", 15, "-bf", 0, "-crf", 26, "-pix_fmt", "yuv420p", d / "first.mp4")
    _ff("-i", d / "first.mp4", "-c:v", "libx264", "-g", 250, "-bf", 0, "-crf", 22, d / "double.mp4")
    _ff("-f", "lavfi", "-i", src, "-t", 10, "-c:v", "libx264", "-g", 250, "-bf", 0, "-crf", 22, "-pix_fmt", "yuv420p", d / "single.mp4")
    return d


def test_frame_integrity(wb, case, videos):
    def codes(name):
        r = wb.run_analysis(ingest(wb, case, videos / name)["id"], "video.frame_integrity", {}, EXAMINER)["result"]
        return {f.get("code") for f in r["flags"]}, r["findings"]

    c, _ = codes("orig.mp4")
    assert not c & {"vint.jump", "vint.repeated_segment"}
    c, f = codes("deleted.mp4")
    assert "vint.jump" in c and f["jumps"][0]["between"] == [79, 80]
    c, f = codes("looped.mp4")
    assert "vint.repeated_segment" in c and f["repeated_segments"][0]["first"][0] == 40


def test_double_encoding(wb, case, videos):
    r = wb.run_analysis(ingest(wb, case, videos / "double.mp4")["id"], "video.double_encoding", {}, EXAMINER)["result"]
    assert "venc.double" in {f.get("code") for f in r["flags"]} and r["findings"]["estimated_first_gop"] == 15
    r = wb.run_analysis(ingest(wb, case, videos / "single.mp4")["id"], "video.double_encoding", {}, EXAMINER)["result"]
    assert "venc.double" not in {f.get("code") for f in r["flags"]}


def test_triage_battery_includes_video_checks(wb, case, videos):
    t = wb.smart_report(ingest(wb, case, videos / "looped.mp4")["id"], EXAMINER)
    assert t["level"] == "red" and t["result"]["rules_version"] == "1.1.0"
    assert {a["name"] for a in t["result"]["analyses"]} >= {"video.frame_integrity", "video.double_encoding", "synthetic.provenance"}


# Photogrammetry ---------------------------------------------------------------------------


def _camera():
    W, H, f = 1280, 720, 1100.0
    C = np.array([2.0, -8.0, 3.5])
    z = np.array([0, 0, 1.0]) - C
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    R = np.vstack([x, np.cross(z, x), z])
    K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1]])

    def proj(P):
        q = K @ (R @ np.asarray(P, float) - R @ C)
        return (q[:2] / q[2]).tolist()

    return W, H, f, C, proj


def test_camera_height_resection():
    W, H, f, C, proj = _camera()
    ctrl = [[-2, -1, 0], [2, -1, 0], [-2, 3, 0], [2, 3, 0], [-2, 3, 2.5], [2, 3, 2.5], [0, 1, 0], [-1, 2, 1.2]]
    person = np.array([0.5, 0.5, 0.0])
    r = measure("camera_height", {"control_image_points": [proj(p) for p in ctrl], "control_world_points": ctrl,
                                  "image_size": [W, H], "target_base": proj(person), "target_top": proj(person + [0, 0, 1.78])})
    assert abs(r["value"] - 1.78) < 0.01
    lo, hi = r["interval_95"]
    assert lo < 1.78 < hi and hi - lo < 0.15
    assert abs(r["camera"]["focal_length_px"] - f) < 5 and np.allclose(r["camera"]["position_m"], C, atol=0.02)
    with pytest.raises(ValueError, match="six"):
        measure("camera_height", {"control_image_points": [proj(p) for p in ctrl[:4]], "control_world_points": ctrl[:4],
                                  "image_size": [W, H], "target_base": [0, 0], "target_top": [0, 0]})


# Reviewer ----------------------------------------------------------------------------------


def test_reviewer_reruns_case_and_flags_generative_steps(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    wb.smart_report(ev["id"], EXAMINER)
    ok = wb.create_chain(case["id"], ev["id"], "classical", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "exposure.levels", "params": {}}]})
    wb.render_chain(ok["id"], EXAMINER)
    gen = wb.create_chain(case["id"], ev["id"], "sr", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "ai.super_resolution", "params": {"scale": "2"}}]})
    wb.render_chain(gen["id"], EXAMINER)
    wb.measure(ev["id"], "distance", {"plane_image_points": [[0, 0], [200, 0], [200, 150], [0, 150]],
                                      "plane_world_points": [[0, 0], [2, 0], [2, 1.5], [0, 1.5]],
                                      "point_a": [10, 10], "point_b": [100, 100]}, EXAMINER)
    r = review_case(wb, case["id"])
    items = r["result"]["items"]
    assert r["level"] == "fail"  # the unjustified generative step
    by = {(i["check"], i["level"]) for i in items}
    assert ("evidence.hash", "pass") in by and ("audit.chain", "pass") in by
    assert ("chain.generative", "fail") in by and ("model.hash", "pass") in by
    assert ("measurement.rerun", "pass") in by and ("analysis.rerun", "fail") not in by
    assert sum(1 for i in items if i["check"] == "chain.rerun" and i["level"] == "pass") == 2
    assert wb.latest_review(case["id"])["id"] == r["id"]
    assert wb.audit_entries(case["id"])[-1]["action"] == "review.run"

    # The report carries the review, the banner and the model hash.
    html = render_html(wb, build_bundle(wb, case["id"], EXAMINER))
    assert "Independent review" in html and "NOT JUSTIFIED" in html and models.get("sr.fsrcnn_x2")["sha256"] in html
    assert render_pdf(wb, build_bundle(wb, case["id"], EXAMINER, verify=False))[:4] == b"%PDF"


def test_reviewer_detects_a_changed_recorded_result(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    res = wb.run_analysis(ev["id"], "file.metadata", {}, EXAMINER)
    wb.db.execute("UPDATE analyses SET output_hash = ? WHERE id = ?", ("0" * 64, res["id"]))
    r = review_case(wb, case["id"])
    assert any(i["check"] == "analysis.rerun" and i["level"] == "fail" for i in r["result"]["items"])


# API ---------------------------------------------------------------------------------------


def test_phase3_api(tmp_path, jpeg_path):
    from forensic import api
    from forensic.config import load_settings
    from forensic.workbench import Workbench

    api._wb = Workbench(load_settings(tmp_path / "data"))
    c = TestClient(api.app)
    h = {"X-Examiner": "Test Examiner"}
    assert any(m["id"] == "face.yunet" for m in c.get("/api/models").json())
    case = c.post("/api/cases", json={"name": "c", "examiner": "Test Examiner"}, headers=h).json()
    ev = c.post(f"/api/cases/{case['id']}/evidence", files={"file": ("a.jpg", jpeg_path.read_bytes())}, headers=h).json()
    ch = c.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "name": "sr", "graph": {"source": {}, "nodes": [
        {"id": "n1", "filter": "ai.super_resolution", "params": {"scale": "2"}}]}}, headers=h).json()
    c.post(f"/api/chains/{ch['id']}/render", headers=h).raise_for_status()
    assert c.get(f"/api/chains/{ch['id']}/export.png", headers=h).status_code == 403
    r = c.post(f"/api/cases/{case['id']}/review", json={}, headers=h).json()
    assert r["level"] == "fail"
    assert c.get(f"/api/cases/{case['id']}").json()["review"]["id"] == r["id"]
    api._wb.db.close()
    api._wb = None


def test_lab_detector_installs_and_runs(wb, case, jpeg_path, tmp_path, monkeypatch):
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    monkeypatch.setenv("FORENSIC_MODELS_DIR", str(tmp_path / "lab"))
    # A stand-in "detector": mean brightness minus 0.2, through a sigmoid in the card.
    g = helper.make_graph(
        [helper.make_node("ReduceMean", ["x"], ["m"], keepdims=0), helper.make_node("Sub", ["m", "c"], ["y"])],
        "toy", [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [])],
        [helper.make_tensor("c", TensorProto.FLOAT, [], [0.2])])
    path = tmp_path / "toy.onnx"
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], ir_version=8), path)
    card = {"id": "lab.toy", "kind": "detector", "task": "test", "architecture": "mean", "reference": "none",
            "source": "test", "licence": "test", "training_data": "none", "trained_on": "2026-01-01", "version": "0.1",
            "input": {"size": 64, "mode": "tiles", "scale": 1.0}, "output": {"activation": "sigmoid"}, "threshold": 0.5}
    (tmp_path / "card.json").write_text(json.dumps(card))
    models.install(path, tmp_path / "card.json")
    a = next(a for a in wb.analyses() if a["id"] == "synthetic.learned")
    assert a["unavailable"] is None
    r = wb.run_analysis(ingest(wb, case, jpeg_path)["id"], "synthetic.learned", {}, EXAMINER)["result"]
    assert r["findings"]["model"]["id"] == "lab.toy" and r["findings"]["crops"] > 1 and "tile_scores" in r["maps"]
    assert r["findings"]["trained_on"] == "2026-01-01"
