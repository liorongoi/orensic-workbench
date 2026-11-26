import subprocess
import sys

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from forensic import api, codecview
from forensic.config import load_settings
from forensic.filters import FilterError
from forensic.workbench import Workbench

from .conftest import EXAMINER, sign_in

H = {"X-Examiner": "Test Examiner"}


def _ingest(wb, case, path):
    with open(path, "rb") as f:
        return wb.ingest(case["id"], f, path.name, EXAMINER)


def test_block_grid_matches_frame_types(wb, case, video_path):
    ev = _ingest(wb, case, video_path)
    table = wb.frame_table(ev["id"])
    for n in range(len(table)):
        info = wb.codec_info(ev["id"], n)
        # Display order: the decoder's picture type agrees with the frame strip's.
        assert info["type"] == table[n]["type"]
        # 160x120 -> 10x8 macroblocks; on a torn grid, show the decoder log that tore it.
        assert (info["cols"], info["rows"]) == (10, 8), "\n\n".join(codecview.TORN_LOGS)
        assert sum(info["counts"].values()) == 80 and info["counts"]["other"] == 0
        assert len(info["kinds"]) == 8 and len(info["qp_grid"][0]) == 10
        if info["type"] == "I":
            assert info["share"]["intra"] == 1.0
        if info["type"] == "P":
            assert info["counts"]["bwd"] == info["counts"]["bi"] == 0
        assert 0 <= info["qp"]["min"] <= info["qp"]["mean"] <= info["qp"]["max"] <= 51
    assert any(wb.codec_info(ev["id"], n)["counts"]["bwd"] + wb.codec_info(ev["id"], n)["counts"]["bi"]
               for n in range(len(table)) if table[n]["type"] == "B")


def test_render_layers(wb, case, video_path):
    ev = _ingest(wb, case, video_path)
    plain = wb.source_frame(ev["id"], 0)
    none = wb.codec_view(ev["id"], 0, layer="none")
    assert none.shape == (120, 160, 3) and none.dtype == np.uint8
    assert np.array_equal(none, np.clip(np.rint(plain * 255), 0, 255).astype(np.uint8))
    # An I-frame is all intra: every pixel is pulled towards red.
    typed = wb.codec_view(ev["id"], 0, layer="type").astype(int)
    assert (typed[..., 0] - typed[..., 2]).mean() > (none[..., 0].astype(int) - none[..., 2]).mean() + 40
    assert not np.array_equal(wb.codec_view(ev["id"], 0, layer="qp"), typed)
    grid = wb.codec_view(ev["id"], 0, layer="none", grid=True)
    assert (grid[:, 16] < none[:, 16]).any() and np.array_equal(grid[1:16, 1:16], none[1:16, 1:16])
    with pytest.raises(FilterError):
        wb.codec_view(ev["id"], 0, layer="bogus")
    with pytest.raises(FilterError):
        wb.codec_info(ev["id"], 999)


def test_motion_vectors(wb, case, tmp_path):
    # A panning picture gives every predicted frame real motion.
    p = tmp_path / "pan.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-vf",
         "crop=240:176:x='min(t*80,80)':y=20", "-t", "1", "-c:v", "libx264", "-g", "12", "-bf", "2",
         "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )
    ev = _ingest(wb, case, p)
    table = wb.frame_table(ev["id"])
    i = next(n for n, f in enumerate(table) if f["type"] == "I")
    pn = next(n for n, f in enumerate(table) if f["type"] == "P")
    bn = next(n for n, f in enumerate(table) if f["type"] == "B")
    fw, bw = codecview.motion_vectors(p, wb.settings, i, 240, 176)
    assert fw.max() == 0 and bw.max() == 0
    fw, bw = codecview.motion_vectors(p, wb.settings, pn, 240, 176)
    assert fw.max() > 0.5 and bw.max() == 0
    fw, bw = codecview.motion_vectors(p, wb.settings, bn, 240, 176)
    assert fw.max() + bw.max() > 0.5
    a = wb.codec_view(ev["id"], pn, layer="none", mv=True)
    b = wb.codec_view(ev["id"], pn, layer="none")
    assert (a != b).any()


def test_unsupported_codec(wb, case, tmp_path):
    p = tmp_path / "clip.avi"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=25", "-t", "0.2",
         "-c:v", "mjpeg", str(p)],
        check=True,
    )
    ev = _ingest(wb, case, p)
    with pytest.raises(FilterError, match="not available"):
        wb.codec_info(ev["id"], 0)


def test_codec_endpoints(tmp_path, video_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    try:
        with TestClient(api.app) as c:
            sign_in(c)
            case = c.post("/api/cases", json={"name": "Codec", "examiner": "Test Examiner"}, headers=H).json()
            vid = c.post(f"/api/cases/{case['id']}/evidence", files={"file": ("clip.mp4", video_path.read_bytes())}, headers=H).json()
            info = c.get(f"/api/evidence/{vid['id']}/codec/0").json()
            assert info["type"] == "I" and [k["id"] for k in info["legend"]][:2] == ["intra", "fwd"]
            r = c.get(f"/api/evidence/{vid['id']}/codec/3.png?layer=qp&mv=1&grid=1")
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            assert img.shape == (120, 160, 3)
            assert c.get(f"/api/evidence/{vid['id']}/codec/0.png?layer=x").status_code == 400
    finally:
        api._wb.db.close()
        api._wb = None


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in FFmpeg is a shell script")
def test_block_grid_survives_rows_broken_by_other_threads(tmp_path):
    """FFmpeg's filter and muxer threads log while the decoder prints its grid; a row cut in two by their
    messages, or repeated rows, must still give the full grid."""
    d = "[h264 @ 0x55aa]"
    row = "30d  30>  30X+ 30d  "
    log = "\n".join([
        f"{d} New frame, type: P",
        f"{d} {row}",
        f"{d} 30d  30>  [out#0/null @ 0x77bb] sq: send 0 ts 0.08",
        f"{d} 30X+ 30d  ",
        "[graph 0 input from stream 0:0 @ 0x66cc] Setting 'pix_fmt' to value '0'",
        f"{d} {row}",
        f"{d} nal_unit_type: 1(Coded slice of a non-IDR picture), nal_ref_idc: 2",
        f"{d} New frame, type: I",
        f"{d} 9i   9I   9i   9I   ",
    ]) + "\n"
    fake = tmp_path / "ffmpeg"
    (tmp_path / "log.txt").write_text(log)
    fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'log.txt'}' >&2\n")
    fake.chmod(0o755)
    settings = load_settings(tmp_path / "data")
    object.__setattr__(settings, "ffmpeg", str(fake))
    out = codecview.blocks(tmp_path / "x.mp4", settings, 0, 2, 64, 48)
    assert out[0]["type"] == "P" and out[0]["qp"].shape == (3, 4)
    assert (out[0]["qp"] == 30).all()
    assert out[1]["qp"].shape == (1, 4)



@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in FFmpeg is a shell script")
def test_block_grid_reads_the_ffmpeg_8_layout(tmp_path):
    """FFmpeg 8 prints a header of column positions and starts each row with its pixel row number."""
    d = "[h264 @ 0x55aa]"
    log = "\n".join([
        f"{d} New frame, type: B",
        f"{d}    0              64",
        f"{d}   0 34X+=34d  34< |34d  ",
        f"[out#0/null @ 0x77bb] sq: send 0 ts 0.08",
        f"{d}  16  9i   9I   9i   9I  ",
        f"{d}  32 30d  30d  30d  30d  ",
    ]) + "\n"
    (tmp_path / "log.txt").write_text(log)
    fake = tmp_path / "ffmpeg"
    fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'log.txt'}' >&2\n")
    fake.chmod(0o755)
    settings = load_settings(tmp_path / "data")
    object.__setattr__(settings, "ffmpeg", str(fake))
    out = codecview.blocks(tmp_path / "x.mp4", settings, 0, 1, 64, 48)
    assert out[0]["qp"].tolist() == [[34, 34, 34, 34], [9, 9, 9, 9], [30, 30, 30, 30]]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in FFmpeg is a shell script")
def test_block_grid_survives_demuxer_messages_inside_a_row(tmp_path):
    """At the end of the file FFmpeg 6's demuxer thread prints with no prefix, even between a cell's QP and type."""
    d = "[h264 @ 0x55aa]"
    log = "\n".join([
        f"{d} New frame, type: B",
        f"{d} 34d  34>  34d  EOF while reading input",
        "[in#0/mov,mp4,m4a,3gp,3g2,mj2 @ 0x66cc] Terminating demuxer thread",
        f"{d} 34<  ",
        f"{d} 30d  30EOF while reading input",
        f"{d} Terminating demuxer thread",
        f"{d} d  30d  30X- ",
        f"{d} 32d  32d  32d  32d  ",
    ]) + "\n"
    (tmp_path / "log.txt").write_text(log)
    fake = tmp_path / "ffmpeg"
    fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'log.txt'}' >&2\n")
    fake.chmod(0o755)
    settings = load_settings(tmp_path / "data")
    object.__setattr__(settings, "ffmpeg", str(fake))
    out = codecview.blocks(tmp_path / "x.mp4", settings, 0, 1, 64, 48)
    assert out[0]["qp"].tolist() == [[34, 34, 34, 34], [30, 30, 30, 30], [32, 32, 32, 32]]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in FFmpeg is a shell script")
def test_block_grid_decodes_again_when_a_row_is_lost(tmp_path):
    """A frame that comes out a row short (another thread's message broke it) is decoded again."""
    d = "[h264 @ 0x55aa]"
    rows = [f"{d} {q}d  {q}d  {q}d  {q}d  " for q in (34, 30, 32, 28)]
    (tmp_path / "short.txt").write_text("\n".join([f"{d} New frame, type: P", rows[0], "30d  stray 30", *rows[2:]]) + "\n")
    (tmp_path / "whole.txt").write_text("\n".join([f"{d} New frame, type: P", *rows]) + "\n")
    fake = tmp_path / "ffmpeg"
    # The first run prints the broken grid, later runs the whole one.
    fake.write_text(
        f"#!/bin/sh\nif [ -e '{tmp_path / 'ran'}' ]; then cat '{tmp_path / 'whole.txt'}' >&2; "
        f"else touch '{tmp_path / 'ran'}'; cat '{tmp_path / 'short.txt'}' >&2; fi\n"
    )
    fake.chmod(0o755)
    settings = load_settings(tmp_path / "data")
    object.__setattr__(settings, "ffmpeg", str(fake))
    out = codecview.blocks(tmp_path / "x.mp4", settings, 0, 1, 64, 64)
    assert out[0]["qp"].tolist() == [[q] * 4 for q in (34, 30, 32, 28)]
