import subprocess

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
        assert (info["cols"], info["rows"]) == (10, 8)  # 160x120 -> 10x8 macroblocks
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
