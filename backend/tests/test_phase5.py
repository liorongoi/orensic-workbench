"""JPEG dimples, quantisation-table database, luminance gradient, PRNU tampering map and fusion map."""

import io

import cv2
import numpy as np
from PIL import Image

from forensic import prnu, qtables
from forensic.analysis import dimples as dm
from forensic.analysis.camera import _prnu_tamper_result
from forensic.analysis.jpeg import STD_CHROMA, STD_LUMA, ijg_table
from forensic.validation.phase5 import emulated_camera_jpeg
from forensic.validation.run import decode, jpeg, scene

from .conftest import EXAMINER, ingest


def test_dimple_found_in_camera_style_jpeg_not_in_libjpeg():
    x = scene(5)
    assert abs(dm.dimples(emulated_camera_jpeg(x, 90, 0.5))["z"]) >= dm.DIMPLE_THRESHOLD
    assert abs(dm.dimples(jpeg(x, 90))["z"]) < dm.DIMPLE_THRESHOLD


def test_dimple_map_marks_pasted_area():
    cam = decode(emulated_camera_jpeg(scene(7), 90, 1.0)).copy()
    cam[96:224, 128:288] = decode(jpeg(scene(8), 90))[96:224, 128:288]
    res = dm.dimples(jpeg(cam, 95))
    strength, ok = dm.local_strength(res["cells"], res["position"], 8, res["textured"])
    weak = dm.weak_area(strength, ok)
    assert weak[12:28, 16:36].mean() > 0.25 and weak[30:, :].mean() < 0.05


def _with_exif(img: np.ndarray, make: str, model: str, **save) -> bytes:
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = make, model
    b = io.BytesIO()
    Image.fromarray(img).save(b, "JPEG", exif=exif.tobytes(), **save)
    return b.getvalue()


def test_qt_database_matches_and_flags_mismatch(wb, case, tmp_path):
    db = qtables.load()
    cam = next(e for e in db["entries"] if e["kind"] == "camera" and e.get("chroma") and e["make"] and e["model"])
    img = scene(3, 128, 160)
    original = tmp_path / "original.jpg"
    original.write_bytes(_with_exif(img, cam["make"], cam["model"], qtables=[cam["luma"], cam["chroma"]]))
    resaved = tmp_path / "resaved.jpg"
    resaved.write_bytes(_with_exif(img, cam["make"], cam["model"], quality=85))
    known = qtables.known_tables_for(db, cam["make"], cam["model"])
    std85 = qtables.table_key(ijg_table(STD_LUMA, 85).ravel().tolist(), ijg_table(STD_CHROMA, 85).ravel().tolist())
    r1 = wb.run_analysis(ingest(wb, case, original)["id"], "file.qt_database", {}, EXAMINER)["result"]
    assert r1["findings"]["matches_exif_camera"] is True
    assert cam["camera"] in r1["findings"]["matching_cameras"]
    r2 = wb.run_analysis(ingest(wb, case, resaved)["id"], "file.qt_database", {}, EXAMINER)["result"]
    assert "libjpeg (IJG) standard tables, quality 85" in r2["findings"]["matching_software"]
    if std85 not in known:
        assert any(f.get("code") == "qt.model_mismatch" for f in r2["flags"])


def test_qt_lab_database_grows(tmp_path):
    folder = tmp_path / "refs"
    folder.mkdir()
    (folder / "a.jpg").write_bytes(_with_exif(scene(1, 64, 64), "Acme", "Acme Cam 1", quality=77))
    out = tmp_path / "data" / qtables.LAB_FILE
    assert qtables.add_folder(folder, out, "Lab set")["added"] == 1
    db = qtables.load(tmp_path / "data")
    luma, chroma = ijg_table(STD_LUMA, 77).ravel().tolist(), ijg_table(STD_CHROMA, 77).ravel().tolist()
    assert "Acme Cam 1" in qtables.match(db, luma, chroma)["cameras"]


def test_luminance_gradient_map(wb, case, jpeg_path):
    res = wb.run_analysis(ingest(wb, case, jpeg_path)["id"], "tamper.luminance_gradient", {}, EXAMINER)["result"]
    assert set(res["maps"]) == {"luminance_gradient"}


def test_prnu_map_outlines_area_from_other_camera():
    h, w = 384, 512
    cams = [np.random.default_rng(70 + c).normal(0, 0.005, (h, w)) for c in range(2)]

    def shot(c, seed, flat=False):
        rng = np.random.default_rng(seed)
        base = (0.7 + 0.05 * cv2.GaussianBlur(rng.normal(0, 1, (h, w, 3)), (0, 0), 20)) if flat else scene(seed, h, w) / 255.0
        return (np.clip(base * (1 + cams[c][..., None]) + rng.normal(0, 0.006, base.shape), 0, 1) * 255).round().astype(np.uint8)

    K = prnu.estimate_fingerprint([decode(jpeg(shot(0, 100 + i, True), 95)).astype(np.float32) / 255 for i in range(12)])[0]
    a = shot(0, 200)
    t = a.copy()
    t[100:228, 150:300] = shot(1, 300)[100:228, 150:300]
    clean = _prnu_tamper_result(decode(jpeg(a, 90)).astype(np.float32) / 255, K, 128)
    assert clean.findings["whole_image_matches"] and not clean.findings["regions"]
    found = _prnu_tamper_result(decode(jpeg(t, 90)).astype(np.float32) / 255, K, 128).findings["regions"]
    assert found
    x, y, bw, bh = found[0]["bbox_px"]
    assert x < 300 and x + bw > 150 and y < 228 and y + bh > 100


def test_fusion_combines_and_lists_contributions(wb, case, tmp_path):
    bg = decode(jpeg(scene(20), 60)).copy()
    bg[96:224, 128:288] = scene(21)[96:224, 128:288]
    p = tmp_path / "splice.jpg"
    p.write_bytes(jpeg(bg, 90))
    res = wb.run_analysis(ingest(wb, case, p)["id"], "tamper.fusion", {}, EXAMINER)["result"]
    names = {c["analysis"]: c for c in res["findings"]["contributions"]}
    assert set(names) == {"double_jpeg", "nonaligned_grid", "dimples", "noise"}
    assert names["double_jpeg"]["contributes"]
    assert "fusion_belief" in res["maps"]
