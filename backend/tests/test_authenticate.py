import io

import numpy as np
from PIL import Image

from forensic.analysis.jpeg import STD_LUMA, estimate_quality, ijg_table, parse_markers

from .conftest import EXAMINER, ingest


def test_marker_walk_and_quality(jpeg_path):
    info = parse_markers(jpeg_path.read_bytes())
    names = [m["name"] for m in info["markers"]]
    assert names[0] == "SOI" and names[-1] == "EOI" and "SOS" in names
    assert info["frame"]["width"] == 320 and info["frame"]["height"] == 240
    assert np.array_equal(np.array(info["dqt"]["0"]), ijg_table(STD_LUMA, 85))
    assert estimate_quality(info["dqt"])["standard_ijg_match"] == 85


def test_marker_walk_survives_garbage():
    for data in (b"", b"\xff\xd8\xff", b"\xff\xd8\xff\xe0\xff\xff" + b"\x00" * 10, b"not a jpeg"):
        info = parse_markers(data)
        assert isinstance(info["warnings"], list)


def test_trailing_data_is_flagged(wb, case, jpeg_path, tmp_path):
    p = tmp_path / "trailing.jpg"
    p.write_bytes(jpeg_path.read_bytes() + b"hidden payload")
    ev = ingest(wb, case, p)
    res = wb.run_analysis(ev["id"], "file.metadata", {}, EXAMINER)["result"]
    assert any("after the end-of-image" in f["text"] for f in res["flags"])


def test_exif_software_and_dates(wb, case, tmp_path):
    img = Image.fromarray(np.full((64, 96, 3), 128, np.uint8))
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.0"
    exif[0x0132] = "2024:01:02 10:00:00"
    exif.get_ifd(0x8769)[0x9003] = "2024:01:01 09:00:00"
    p = tmp_path / "edited.jpg"
    img.save(p, "JPEG", exif=exif.tobytes())
    ev = ingest(wb, case, p)
    res = wb.run_analysis(ev["id"], "file.metadata", {}, EXAMINER)["result"]
    texts = " ".join(f["text"] for f in res["flags"])
    assert "image editor" in texts and "differs from capture time" in texts


def test_ela_produces_maps(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    res = wb.run_analysis(ev["id"], "tamper.ela", {"quality": 90}, EXAMINER)
    assert set(res["result"]["maps"]) == {"ela", "ela_heat"}
    assert wb.store.derived_path(res["result"]["maps"]["ela"], "png").exists()


def test_jpeg_ghost_finds_prior_quality(wb, case, spliced_path):
    ev = ingest(wb, case, spliced_path)
    res = wb.run_analysis(ev["id"], "tamper.jpeg_ghost", {"q_min": 40, "q_max": 90, "q_step": 5}, EXAMINER)["result"]
    assert "ghost_montage" in res["maps"]
    # The pasted region was saved at 60: its local difference at Q60 is much lower than elsewhere.
    from forensic.analysis.compression import recompress
    from forensic.media import decode_image

    img = decode_image(spliced_path)
    d = ((img - recompress(img, 60)) ** 2).mean(axis=2)
    inside = d[96:160, 112:208].mean()
    outside = np.concatenate([d[:60].ravel(), d[200:].ravel()]).mean()
    assert inside < outside * 0.6


def test_video_metadata(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    res = wb.run_analysis(ev["id"], "file.metadata", {}, EXAMINER)["result"]
    assert res["findings"]["frame_count"] == 25
    assert res["findings"]["frame_types"].get("I", 0) >= 1
