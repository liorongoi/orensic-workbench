"""Tests for the platform modules: case management, custody and export, video operations, extra
filters, multi-camera sync, detection/tracking/plates and redaction."""

import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from forensic import compute
from forensic.audit import Actor
from forensic.intel import lpr
from forensic.intel.tracker import Tracker, iou
from forensic.redaction import _detail, interpolate, obscure

from .conftest import EXAMINER, ingest

MEDIA = Path(os.environ.get("FORENSIC_TEST_MEDIA", "/tmp/claude-0/dl"))


@pytest.fixture(scope="session")
def av_path(tmp_path_factory):
    """2 s video with a 1 kHz tone burst at 0.8 s and a white flash at frame 20."""
    d = tmp_path_factory.mktemp("media")
    p = d / "av.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-f", "lavfi",
         "-i", "sine=frequency=1000:sample_rate=16000", "-t", "2",
         "-af", "volume='if(between(t,0.8,0.9),1,0.02)':eval=frame",
         "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='eq(n,20)'",
         "-c:v", "ffv1", "-c:a", "pcm_s16le", str(p)],
        check=True,
    )
    return p


@pytest.fixture(scope="session")
def plate_path(tmp_path_factory):
    """A synthetic car-like scene with a plate reading 1234567 (known ground truth)."""
    img = np.full((360, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (120, 120), (520, 330), (40, 40, 160), -1)
    cv2.rectangle(img, (240, 250), (400, 295), (20, 200, 240), -1)
    cv2.rectangle(img, (240, 250), (400, 295), (0, 0, 0), 2)
    cv2.putText(img, "1234567", (252, 285), cv2.FONT_HERSHEY_SIMPLEX, 1.05, (0, 0, 0), 3, cv2.LINE_AA)
    p = tmp_path_factory.mktemp("media") / "plate.png"
    cv2.imwrite(str(p), img)
    return p


# Case management -----------------------------------------------------------------------------------


def test_case_number_unique_and_subcases(wb):
    a = wb.create_case("Main", "Examiner", EXAMINER, case_number="2026-001", legal_authority="Warrant 17")
    assert a["case_number"] == "2026-001" and a["legal_authority"] == "Warrant 17"
    with pytest.raises(ValueError):
        wb.create_case("Dup", "Examiner", EXAMINER, case_number="2026-001")
    sub = wb.create_case("Sub", "Examiner", EXAMINER, case_number="2026-001/A", parent_id=a["id"])
    assert [s["id"] for s in wb.casework.subcases(a["id"])] == [sub["id"]]


def test_ingest_records_custody_sha512_and_source(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    import hashlib

    data = jpeg_path.read_bytes()
    assert ev["sha512"] == hashlib.sha512(data).hexdigest()
    assert ev["source"]["kind"] == "local path"
    events = wb.casework.custody(case["id"], ev["id"])
    assert events[0]["event"] == "acquired"
    wb.verify_evidence(ev["id"], EXAMINER)
    assert [e["event"] for e in wb.casework.custody(case["id"], ev["id"])][-1] == "verified"
    with pytest.raises(ValueError):
        wb.casework.record_custody(ev["id"], "acquired", EXAMINER)
    t = wb.casework.record_custody(ev["id"], "transferred", EXAMINER, from_party="Lab", to_party="Court clerk")
    assert t["to_party"] == "Court clerk"


def test_tamper_detected_and_logged(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    p = wb.evidence_path(ev["id"])
    os.chmod(p, 0o600)
    with open(p, "r+b") as f:
        f.seek(100)
        f.write(b"\x00\x01\x02")
    r = wb.verify_evidence(ev["id"], EXAMINER)
    assert not r["ok"]
    assert wb.get_evidence(ev["id"])["integrity"]["ok"] is False
    assert any(e["action"] == "evidence.integrity_failure" for e in wb.audit.entries(case["id"]))
    assert not wb.casework.verify_case(case["id"], EXAMINER)["ok"]
    with pytest.raises(ValueError):
        wb.casework.export_package(case["id"], EXAMINER)


def test_tags_bookmarks_search(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    assert wb.casework.set_tags(ev["id"], ["CCTV", "suspect vehicle", "cctv"], EXAMINER) == ["cctv", "suspect vehicle"]
    bm = wb.casework.add_bookmark(ev["id"], 5, "Car enters", EXAMINER, end_frame=12, note="from the left")
    assert bm["frame"] == 5 and bm["end_frame"] == 12 and bm["time"] is not None
    with pytest.raises(ValueError):
        wb.casework.add_bookmark(ev["id"], 999, "too late", EXAMINER)
    hits = wb.casework.search("car enters", EXAMINER)
    assert any(h["id"] == ev["id"] for h in hits["results"])
    assert wb.casework.search("x", EXAMINER, tag="suspect vehicle")["results"]
    assert any(e["action"] == "search.evidence" for e in wb.audit.entries(None))


def test_duplicates_exact_and_near(wb, case, jpeg_path, tmp_path):
    a = ingest(wb, case, jpeg_path)
    other = tmp_path / "resaved.jpg"
    Image.open(jpeg_path).save(other, "JPEG", quality=60)
    b = ingest(wb, case, other)
    d = wb.casework.duplicates(case["id"])
    pair = {a["id"], b["id"]}
    assert any({n["a"]["id"], n["b"]["id"]} == pair for n in d["near"])


def test_export_package_is_deterministic_and_verifiable(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    pkg, info = wb.casework.export_package(case["id"], EXAMINER, purpose="Disclosure", recipient="Defence")
    check = wb.casework.verify_package(pkg)
    assert check["ok"] and check["files"] == 1
    with zipfile.ZipFile(io.BytesIO(pkg)) as z:
        assert z.read(f"evidence/{ev['id']}_scene.jpg") == jpeg_path.read_bytes()
    assert wb.casework.export_bytes(info["export_id"]) == pkg
    assert wb.casework.custody(case["id"], ev["id"])[-1]["event"] == "exported"
    # Tamper with one member: the recipient's check catches it.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(pkg)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for n in zin.namelist():
            d = zin.read(n)
            zout.writestr(n, d[:-1] + b"X" if n.startswith("evidence/") else d)
    assert not wb.casework.verify_package(buf.getvalue())["ok"]


def test_legal_hold_retention_disposal(wb, jpeg_path):
    c = wb.create_case("Old", "Examiner", EXAMINER, case_number="OLD-1")
    ev = ingest(wb, c, jpeg_path)
    with pytest.raises(PermissionError):
        wb.casework.dispose_case(c["id"], EXAMINER, reason="r", confirm="OLD-1", role="examiner")
    with pytest.raises(ValueError):
        wb.casework.dispose_case(c["id"], EXAMINER, reason="r", confirm="OLD-1", role="admin")  # no retention date
    wb.casework.set_retention(c["id"], "2000-01-01", EXAMINER)
    wb.casework.set_legal_hold(c["id"], True, "Appeal pending", EXAMINER)
    with pytest.raises(ValueError):
        wb.casework.dispose_case(c["id"], EXAMINER, reason="r", confirm="OLD-1", role="admin")
    wb.casework.set_legal_hold(c["id"], False, "", EXAMINER)
    with pytest.raises(ValueError):
        wb.casework.dispose_case(c["id"], EXAMINER, reason="r", confirm="wrong", role="admin")
    done = wb.casework.dispose_case(c["id"], EXAMINER, reason="Retention expired", confirm="OLD-1", role="admin")
    assert done["disposed"]["originals_deleted"] == [ev["sha256"]]
    assert not wb.evidence_path(ev["id"]).exists()
    with pytest.raises(ValueError):
        ingest(wb, c, jpeg_path)
    assert wb.audit.verify()["ok"]


def test_fork_and_presets(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    ch = wb.create_chain(case["id"], ev["id"], "Enhance", EXAMINER, {"nodes": [
        {"filter": "exposure.stops", "params": {"stops": 1.0}}, {"filter": "sharpen.laplacian"}]})
    first = ch["graph"]["nodes"][0]["id"]
    br = wb.casework.fork_chain(ch["id"], EXAMINER, upto=first)
    assert len(br["graph"]["nodes"]) == 1 and br["parent_chain_id"] == ch["id"]
    assert len(wb.get_chain(ch["id"])["graph"]["nodes"]) == 2
    pr = wb.casework.save_preset(ch["id"], "Brighten and sharpen", EXAMINER)
    applied = wb.casework.apply_preset(pr["id"], case["id"], ev["id"], EXAMINER)
    assert applied["output_hash"] == wb.get_chain(ch["id"])["output_hash"]


# Video operations ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["copy", "lossless", "h264"])
def test_extract_clip(wb, case, video_path, mode):
    ev = ingest(wb, case, video_path)
    clip = wb.videoops.extract_clip(ev["id"], 5, 14, EXAMINER, mode=mode)
    assert clip["derived_from"] == ev["id"]
    rec = clip["conversion"]
    assert rec["requested"] == {"start": 5, "end": 14}
    if mode == "lossless":
        assert clip["probe"]["frame_count"] == 10
        # Lossless: every frame identical to the source frame.
        for i in (0, 9):
            a = wb.source_frame(clip["id"], i)
            b = wb.source_frame(ev["id"], 5 + i)
            assert np.abs(a - b).max() < 1e-6
    if mode == "copy":
        assert rec["actual"]["start"] <= 5  # stream copy can only cut on a keyframe; it says so
    with pytest.raises(ValueError):
        wb.videoops.extract_clip(ev["id"], 20, 5, EXAMINER, mode=mode)


def test_temporal_profile_and_contact_sheet(wb, case, av_path):
    ev = ingest(wb, case, av_path)
    prof = wb.videoops.temporal_profile(ev["id"])
    assert 20 in [c["frame"] for c in prof["scene_changes"]]
    sheet = wb.videoops.contact_sheet(ev["id"], EXAMINER, count=6)
    assert len(sheet["frames"]) == 6 and len(sheet["png"]) == 64
    zipped, manifest = wb.videoops.export_frames(ev["id"], EXAMINER, frames=[0, 20])
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        assert len([n for n in z.namelist() if n.endswith(".png")]) == 2


def test_compare_and_quality(wb, case, jpeg_path, tmp_path):
    a = ingest(wb, case, jpeg_path)
    same = wb.videoops.compare({"evidence_id": a["id"]}, {"evidence_id": a["id"]}, EXAMINER)
    assert same["ssim"] == pytest.approx(1.0) and same["mse"] == 0
    noisy = tmp_path / "noisy.png"
    img = np.asarray(Image.open(jpeg_path)).astype(np.int16)
    img = np.clip(img + np.random.default_rng(0).integers(-20, 21, img.shape), 0, 255).astype(np.uint8)
    Image.fromarray(img).save(noisy)
    b = ingest(wb, case, noisy)
    diff = wb.videoops.compare({"evidence_id": a["id"]}, {"evidence_id": b["id"]}, EXAMINER)
    assert 0 < diff["ssim"] < 0.95 and 20 < diff["psnr_db"] < 40


def test_sequence_concat_and_chain_render(wb, case, video_path, jpeg_path):
    ev = ingest(wb, case, video_path)
    c1 = wb.videoops.extract_clip(ev["id"], 0, 9, EXAMINER, mode="lossless")
    c2 = wb.videoops.extract_clip(ev["id"], 10, 19, EXAMINER, mode="lossless")
    joined = wb.videoops.concatenate(case["id"], [c1["id"], c2["id"]], EXAMINER, mode="lossless")
    assert joined["probe"]["frame_count"] == 20
    im = ingest(wb, case, jpeg_path)
    seq = wb.videoops.sequence_to_video(case["id"], [im["id"], im["id"]], 2.0, EXAMINER)
    assert seq["probe"]["frame_count"] == 2
    ch = wb.create_chain(case["id"], c1["id"], "Bright", EXAMINER, {"source": {"evidence_id": c1["id"], "frames": {"start": 0, "end": 9}}, "nodes": [{"filter": "exposure.stops", "params": {"stops": 0.5}}]})
    out = wb.videoops.render_chain_video(ch["id"], EXAMINER)
    assert out["probe"]["frame_count"] == 10
    assert out["conversion"]["frames_removed"] == 0 and out["conversion"]["frames_out"] == 10


# Extra filters -----------------------------------------------------------------------------------------


def test_extra_filters_behave(wb):
    from types import SimpleNamespace

    from forensic.filters.base import get_filter

    rng = np.random.default_rng(1)
    img = np.clip(rng.normal(0.3, 0.05, (64, 64, 3)), 0, 1).astype(np.float32)
    def run(name, **p):
        f = get_filter(name)
        return f.fn(img, f.validate(p))

    assert np.allclose(run("exposure.stops", stops=0.0), img, atol=1e-5)
    assert run("exposure.stops", stops=1.0).mean() > img.mean() + 0.05
    stretched = run("exposure.contrast_stretch")
    assert stretched.max() - stretched.min() > img.max() - img.min()
    gray = run("color.saturation", factor=0.0)
    assert np.allclose(gray[..., 0], gray[..., 1], atol=1e-5)
    thr = run("color.threshold")
    assert set(np.unique(thr)) <= {0.0, 1.0}
    spec = run("fft.spectrum")
    assert spec.shape == img.shape


# Multi-camera synchronisation -----------------------------------------------------------------------


def test_sync_methods(wb, case, av_path, video_path):
    ref = ingest(wb, case, av_path)
    other = ingest(wb, case, av_path)
    cam = wb.sync.create_camera(case["id"], "Shop front", EXAMINER, location="High St")
    wb.sync.assign_camera(ref["id"], cam["id"], EXAMINER)
    wb.sync.set_reference(ref["id"], EXAMINER, "2026-03-01T10:00:00+00:00")
    T, unc = wb.sync.global_time(ref["id"], 25)
    assert unc == 0 and T == pytest.approx(wb.sync.global_time(ref["id"], 0)[0] + 1.0)
    with pytest.raises(ValueError):
        wb.sync.sync_manual(other["id"], 1.0, EXAMINER)  # basis required
    with pytest.raises(ValueError):
        wb.sync.set_reference(ref["id"], EXAMINER, "2026-03-01T10:00:00")  # timezone required
    ev = wb.sync.sync_event(other["id"], 20, ref["id"], 20, EXAMINER, label="flash")
    assert ev["offset_s"] == pytest.approx(wb.sync.current(ref["id"])["offset_s"], abs=1e-6)
    assert ev["uncertainty_s"] == pytest.approx(0.04, abs=1e-3)
    two = wb.sync.sync_two_events(other["id"], (5, 45), ref["id"], (5, 45), EXAMINER)
    assert two["drift_ppm"] == pytest.approx(0, abs=1)
    audio = wb.sync.sync_correlate(other["id"], ref["id"], EXAMINER, signal="audio", max_lag_s=1)
    assert abs(audio["details"]["lag_s"]) < 0.01
    vis = wb.sync.sync_correlate(other["id"], ref["id"], EXAMINER, signal="visual", max_lag_s=1)
    assert abs(vis["details"]["lag_s"]) < 0.02
    assert len(wb.sync.history(other["id"])) == 4
    tl = wb.sync.timeline(case["id"])
    assert {i["evidence_id"] for i in tl["items"]} >= {ref["id"], other["id"]}
    T20, _ = wb.sync.global_time(ref["id"], 20)
    assert wb.sync.frame_at(other["id"], T20)["frame"] == 20
    grid = wb.sync.synchronized_frames(case["id"], T20, EXAMINER)
    assert len(grid["png"]) == 64 and len(grid["frames"]) == 2


# Detection, tracking and plates ----------------------------------------------------------------------


def test_tracker_follows_and_separates():
    t = Tracker(min_hits=2)
    for f in range(10):
        t.update(f, f / 25, [{"cls": "car", "score": 0.9, "box": [10 + 5 * f, 10, 40, 30]},
                             {"cls": "person", "score": 0.8, "box": [200, 100 - 3 * f, 20, 50]}])
    tracks = t.close()
    assert len(tracks) == 2
    assert all(len(tr.history) == 10 for tr in tracks)
    assert iou([0, 0, 10, 10], [5, 0, 10, 10]) == pytest.approx(1 / 3)


def test_plate_formats_and_alternatives():
    assert lpr.matching_format("1234567", "IL")
    assert lpr.matching_format("12345678", "IL")
    assert not lpr.matching_format("12A4567", "IL")
    alts = lpr.format_alternatives("12B4567", "IL")
    assert "1284567" in alts


def test_plate_reading_known_truth_and_review(wb, case, plate_path):
    if not lpr.TesseractOcr().available():
        pytest.skip("tesseract not installed")
    ev = ingest(wb, case, plate_path)
    bgr = cv2.imread(str(plate_path))
    cands = lpr.localise(bgr)
    assert cands, "plate not found"
    p = wb.intel.add_plate(ev["id"], 0, [[240, 250], [400, 250], [400, 295], [240, 295]], EXAMINER, country="IL")
    assert p["status"] == "unreviewed"  # never auto-confirmed
    assert p["best_text"] == "1234567", p["candidates"]
    with pytest.raises(ValueError):
        wb.intel.review_plate(p["id"], "correct", EXAMINER, text="")
    r = wb.intel.review_plate(p["id"], "correct", EXAMINER, text="1234561", note="last digit read as 7 by OCR")
    assert r["status"] == "corrected" and r["reviewed_text"] == "1234561" and r["best_text"] == "1234567"
    hits = wb.intel.search(EXAMINER, case_ids=[case["id"]], plate="123*")
    assert hits["results"]
    assert wb.intel.review_queue(case["id"]) == []


@pytest.mark.skipif(not (MEDIA / "street.png").exists(), reason="street.png sample not available")
def test_detector_and_tracking_on_real_image(wb, case, tmp_path):
    src = cv2.imread(str(MEDIA / "street.png"))
    h, w = src.shape[:2]
    # A short pan across the photo gives a video with real objects that move.
    vid = tmp_path / "pan.mp4"
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s",
                             f"{w - 40}x{h - 20}", "-r", "10", "-i", "-", "-c:v", "ffv1", str(vid.with_suffix(".mkv"))],
                            stdin=subprocess.PIPE)
    for f in range(8):
        proc.stdin.write(np.ascontiguousarray(src[10:h - 10, 5 * f:5 * f + w - 40]).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    ev = ingest(wb, case, vid.with_suffix(".mkv"))
    run = wb.intel.run(ev["id"], EXAMINER, frame_step=1, model="detect.yolox_tiny")
    assert run["params"]["profile"] == compute.hardware_profile()["name"]
    dets = wb.intel.detections(run["id"], 0)
    assert any(d["cls"] == "car" and d["score"] > 0.6 for d in dets)
    tracks = wb.intel.tracks(case["id"], run_id=run["id"])
    assert any(t["cls"] == "car" and len(t["boxes"]) >= 6 for t in tracks)
    png = wb.intel.overlay(ev["id"], 0)
    assert png[:4] == b"\x89PNG"
    data, mime = wb.intel.export_tracks(case["id"], EXAMINER, "csv")
    assert data.startswith(b"track") and mime == "text/csv"


# Redaction ---------------------------------------------------------------------------------------------


def test_interpolate_and_obscure():
    kf = [{"frame": 0, "box": [0, 0, 10, 10]}, {"frame": 10, "box": [10, 0, 10, 10]}]
    assert interpolate(kf, 5) == [5, 0, 10, 10]
    assert interpolate(kf, 11) is None and interpolate(kf, 11, hold=True) == [10, 0, 10, 10]
    img = (np.random.default_rng(0).random((100, 100, 3)) * 255).astype(np.uint8)
    for method in ("blur", "pixelate", "solid"):
        out = img.copy()
        obscure(out, [20, 20, 50, 50], method)
        g0 = cv2.cvtColor(img[20:70, 20:70], cv2.COLOR_RGB2GRAY)
        g1 = cv2.cvtColor(out[20:70, 20:70], cv2.COLOR_RGB2GRAY)
        assert _detail(g1) < 0.2 * _detail(g0)
        assert np.array_equal(out[:10], img[:10])  # outside the region untouched


def test_redaction_render_and_verify(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    doc = wb.redaction.save(ev["id"], "redaction", "Hide logo", {
        "regions": [{"keyframes": [{"frame": 0, "box": [10, 10, 40, 30]}, {"frame": 24, "box": [100, 60, 40, 30]}],
                     "method": "solid"}],
        "audio": "remove"}, EXAMINER)
    out = wb.redaction.render(doc["id"], EXAMINER, codec="ffv1")
    v = out["conversion"]["verification"]
    assert v["frames_in"] == v["frames_out"] == 25
    assert v["ok"], v
    assert v["audio"]["ok"]
    assert out["derived_from"] == ev["id"]
    # The original is untouched.
    assert wb.verify_evidence(ev["id"], EXAMINER)["ok"]
    # Solid region really is black in the output at an interpolated position.
    f12 = wb.source_frame(out["id"], 12)
    x, y = int(10 + 90 * 12 / 24) + 5, int(10 + 50 * 12 / 24) + 5
    assert f12[y:y + 15, x:x + 25].max() < 0.05
    with pytest.raises(ValueError):
        wb.redaction.save(ev["id"], "redaction", "bad", {"regions": [{"method": "smudge", "keyframes": []}]}, EXAMINER)


def test_annotation_exhibit(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    doc = wb.redaction.save(ev["id"], "annotation", "Exhibit", {
        "shapes": [{"type": "arrow", "points": [[10, 10], [80, 60]], "color": "#ff0000"},
                   {"type": "text", "points": [[90, 70]], "text": "Vehicle"}],
        "legend": "Arrow marks the vehicle"}, EXAMINER)
    ex = wb.redaction.render_exhibit(doc["id"], EXAMINER, exhibit_id="LW/1")
    assert ex["exhibit_id"] == "LW/1" and ex["source_sha256"] == ev["sha256"]
    img = Image.open(wb.store.derived_path(ex["png"], "png"))
    assert img.height > 240  # caption bar added below the image


def test_hardware_profile_override(monkeypatch):
    monkeypatch.setenv("FORENSIC_PROFILE", "light")
    p = compute.hardware_profile()
    assert p["name"] == "light" and p["detector"] == "detect.yolox_nano" and p["overridden"]
    monkeypatch.setenv("FORENSIC_PROFILE", "bogus")
    assert compute.hardware_profile()["name"] in compute.PROFILES


@pytest.mark.skipif(not (MEDIA / "synth_plate_car.jpg").exists(), reason="synthetic plate car sample not available")
def test_plate_read_on_still_image_is_not_skipped(wb, case):
    """A still image has no tracks; its vehicles must still be searched for plates."""
    if not lpr.TesseractOcr().available():
        pytest.skip("tesseract not installed")
    ev = ingest(wb, case, MEDIA / "synth_plate_car.jpg")
    run = wb.intel.run(ev["id"], EXAMINER, plates=True, country="IL")
    assert run["stats"]["tracks"] == 0 and run["stats"]["plates"] >= 1
    p = wb.intel.review_queue(case["id"])[0]
    assert p["best_text"] == "1234567" and p["status"] == "unreviewed"
