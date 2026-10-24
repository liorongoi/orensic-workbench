"""Batch conversion: detection, conversion profiles, checks, duplicates, failures, control and manifests."""

import csv
import hashlib
import json
import os
import subprocess

import pytest

from .conftest import EXAMINER
from .test_dvr_formats import N, build_dhav, h264  # noqa: F401  (fixture)


@pytest.fixture()
def export_folder(tmp_path, h264):  # noqa: F811
    es, aus = h264
    d = tmp_path / "export"
    (d / "ch1").mkdir(parents=True)
    (d / "ch1" / "ch01.dav").write_bytes(build_dhav(aus, [i * 40.0 for i in range(N)]))
    (d / "ch1" / "copy_of_ch01.dav").write_bytes((d / "ch1" / "ch01.dav").read_bytes())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "1",
                    "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(d / "cam2.mp4")], check=True)
    (d / "player.exe").write_bytes(b"MZ" + os.urandom(2000))
    (d / "random.bin").write_bytes(os.urandom(5000))
    (d / "readme.txt").write_text("export notes")
    return d


def _hashes(folder):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}


def test_scan_detects_by_content(wb, export_folder):
    files = {f["relative"]: f for f in wb.batch.scan(export_folder)}
    assert files["readme.txt"]["skip"]
    assert files["ch1/ch01.dav"]["detected"]["family"] == "dahua_dav"
    assert files["cam2.mp4"]["detected"]["family"] == "isobmff"
    assert files["player.exe"]["detected"]["closed_format"]


def test_batch_converts_checks_and_writes_manifest(wb, export_folder, tmp_path):
    before = _hashes(export_folder)
    out = tmp_path / "out"
    b = wb.batch.create("Shop DVR", EXAMINER, input_dir=str(export_folder), output_dir=str(out), concurrency=2)
    assert b["total"] == 5 and b["status"] == "ready"
    b = wb.batch.start(b["id"], EXAMINER, wait=True)
    b = wb.batch.get(b["id"])
    assert b["status"] == "done"
    by = {os.path.basename(i["input_path"]): i for i in b["items"]}
    dav = by["ch01.dav"] if by["ch01.dav"]["status"] == "done" else by["copy_of_ch01.dav"]
    other = by["copy_of_ch01.dav"] if dav is by["ch01.dav"] else by["ch01.dav"]
    assert dav["mode"] == "parse" and dav["checks"]["frame_count_ok"] and dav["checks"]["input_frames"] == N
    assert dav["outputs"][0]["frames"] == N and dav["outputs"][0]["file"].endswith(".ts")
    assert other["status"] == "duplicate"
    assert by["cam2.mp4"]["status"] == "done" and by["cam2.mp4"]["mode"] == "none"
    assert by["player.exe"]["status"] == "failed" and by["player.exe"]["error_class"] == "unsupported_format"
    assert by["random.bin"]["status"] == "failed" and by["random.bin"]["error_class"] == "unsupported_format"
    # Inputs untouched; outputs match their recorded hashes; manifest is self-consistent.
    assert _hashes(export_folder) == before
    for it in b["items"]:
        for o in it["outputs"] or []:
            assert hashlib.sha256((out / o["file"]).read_bytes()).hexdigest() == o["sha256"]
    mj = (out / "manifest.json").read_bytes()
    assert (out / "manifest.json.sha256").read_text().split()[0] == hashlib.sha256(mj).hexdigest() == b["manifest_sha256"]
    m = json.loads(mj)
    assert m["counts"]["done"] == 2 and m["counts"]["failed"] == 2 and m["counts"]["duplicate"] == 1
    rows = list(csv.DictReader((out / "manifest.csv").open()))
    assert len(rows) == 5
    assert any(e["action"] == "batch.finish" for e in wb.audit.entries(None))


def test_batch_transcode_segments_and_case_ingest(wb, case, export_folder, tmp_path):
    out = tmp_path / "out"
    b = wb.batch.create("Transcode", EXAMINER, files=[str(export_folder / "cam2.mp4")], output_dir=str(out),
                        output="ffv1", segment_seconds=0.4, case_id=case["id"])
    b = wb.batch.start(b["id"], EXAMINER, wait=True)
    it = wb.batch.get(b["id"])["items"][0]
    assert it["status"] == "done", it["error"]
    assert len(it["outputs"]) >= 2 and sum(o["frames"] for o in it["outputs"]) == 25
    ev = wb.list_evidence(case["id"])
    originals = [e for e in ev if not e["derived_from"]]
    assert len(originals) == 1 and len(ev) == 1 + len(it["outputs"])


def test_no_overwrite_and_output_inside_input(wb, export_folder, tmp_path):
    with pytest.raises(ValueError):
        wb.batch.create("bad", EXAMINER, input_dir=str(export_folder), output_dir=str(export_folder / "out"))
    out = tmp_path / "out"
    out.mkdir()
    (out / "0001_cam2.mp4").write_bytes(b"something else")
    b = wb.batch.create("x", EXAMINER, files=[str(export_folder / "cam2.mp4")], output_dir=str(out))
    wb.batch.start(b["id"], EXAMINER, wait=True)
    it = wb.batch.get(b["id"])["items"][0]
    assert it["status"] == "failed" and it["error_class"] == "output_exists"
    assert (out / "0001_cam2.mp4").read_bytes() == b"something else"
    # Retry after the examiner moves the file away.
    os.rename(out / "0001_cam2.mp4", tmp_path / "moved")
    os.remove(out / "manifest.json")
    wb.batch.retry(b["id"], EXAMINER)
    wb.batch.start(b["id"], EXAMINER, wait=True)
    it = wb.batch.get(b["id"])["items"][0]
    assert it["status"] == "done" and it["attempts"] == 2 and "queued again" in it["log"]


def test_cancel_before_start(wb, export_folder, tmp_path):
    b = wb.batch.create("x", EXAMINER, input_dir=str(export_folder), output_dir=str(tmp_path / "o"))
    b = wb.batch.cancel(b["id"], EXAMINER)
    assert b["status"] == "cancelled" and b["counts"]["cancelled"] == 5
    with pytest.raises(ValueError):
        wb.batch.start(b["id"], EXAMINER)
