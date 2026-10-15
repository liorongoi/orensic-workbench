"""Real-ESRGAN enlarger, clone (copy-move) detection and the noise level map."""

from types import SimpleNamespace

import cv2
import numpy as np

from forensic import models
from forensic.analysis import ANALYSES
from forensic.filters import REGISTRY
from forensic.graph import pixel_hash
from forensic.validation import load_card


def _texture(h=240, w=320, seed=0):
    """A photo-like textured image: blurred random blobs at several scales plus mild noise."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    for s in (2, 5, 11):
        img += cv2.GaussianBlur(rng.random((h, w, 3)).astype(np.float32), (0, 0), s) * s
    img = (img - img.min()) / (img.max() - img.min())
    img += rng.normal(0, 0.01, img.shape).astype(np.float32)
    return np.clip(img, 0, 1)


def _analyse(aid, img, **p):
    a = ANALYSES[aid]
    return a.fn(SimpleNamespace(image=img), a.validate(p))


def test_realesrgan_enlarges_deterministically_and_is_generative():
    f = REGISTRY["ai.realesrgan"]
    assert f.generative and f.ai
    x = _texture(40, 52)
    p = f.validate({"model": "compact", "scale": "4"})
    a, b = f.fn(x, p), f.fn(x, p)
    assert a.shape == (160, 208, 3) and pixel_hash(a) == pixel_hash(b)
    assert f.fn(x, f.validate({"model": "compact", "scale": "1"})).shape == x.shape
    assert f.model_ids(p) == ["sr.realesrgan_compact_x4"]
    assert all(v["ok"] for v in models.verify_all())


def test_realesrgan_beats_bicubic_on_a_compressed_plate():
    g = np.full((128, 384, 3), 215, np.uint8)
    cv2.putText(g, "12-345-67", (22, 92), cv2.FONT_HERSHEY_DUPLEX, 2.3, (12, 12, 25), 5, cv2.LINE_AA)
    gt = g.astype(np.float32) / 255
    lr = cv2.resize(gt, (96, 32), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", (lr * 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 60])
    lr = cv2.imdecode(buf, cv2.IMREAD_COLOR).astype(np.float32) / 255
    f = REGISTRY["ai.realesrgan"]
    sr = f.fn(lr, f.validate({"model": "full", "scale": "4"}))
    bic = cv2.resize(lr, (384, 128), interpolation=cv2.INTER_CUBIC)
    # Measured: mean error 0.024 against bicubic's 0.040 (FSRCNN 0.035).
    assert np.abs(sr - gt).mean() < 0.75 * np.abs(bic - gt).mean()


def test_copy_move_finds_moved_and_rotated_copies_and_not_the_original():
    img = _texture()
    assert _analyse("tamper.copy_move", img).findings["copies"] == []
    moved = img.copy()
    moved[150:210, 220:290] = img[20:80, 30:100]
    copies = _analyse("tamper.copy_move", moved).findings["copies"]
    assert copies and any(abs(abs(c["shift_px"][0]) - 190) < 3 and abs(abs(c["shift_px"][1]) - 130) < 3 for c in copies)
    patch = img[10:110, 20:120]
    rot = cv2.warpAffine(patch, cv2.getRotationMatrix2D((50, 50), 20, 1.0), (100, 100), flags=cv2.INTER_CUBIC)
    rotated = img.copy()
    rotated[150:210, 220:290] = rot[20:80, 15:85]
    copies = _analyse("tamper.copy_move", rotated, method="keypoints").findings["copies"]
    assert copies and any(abs(abs(c["rotation_deg"]) - 20) < 2 for c in copies)


def test_noise_map_outlines_a_smoothed_area():
    img = _texture(256, 320, seed=2) * 0.8 + 0.1
    img = np.clip(img + np.random.default_rng(5).normal(0, 0.012, img.shape), 0, 1).astype(np.float32)
    res = _analyse("tamper.noise_map", img)
    assert res.maps["noise_map"].shape == img.shape
    edited = img.copy()
    edited[64:192, 96:224] = cv2.GaussianBlur(img, (0, 0), 1.5)[64:192, 96:224]
    regions = _analyse("tamper.noise_map", edited).findings["regions"]
    assert any(r["noise_ratio"] < 0.3 and r["bbox_px"][0] < 160 < r["bbox_px"][0] + r["bbox_px"][2] for r in regions)


def test_new_analyses_have_cards():
    for aid in ("tamper.copy_move", "tamper.noise_map"):
        card = load_card(aid)
        assert card and card["version"] == ANALYSES[aid].version and card["known_failure_modes"]


def test_smart_report_runs_clone_detection_on_any_image(wb, case, tmp_path):
    from PIL import Image

    from .conftest import EXAMINER, ingest

    img = _texture(seed=4)
    moved = img.copy()
    moved[150:210, 220:290] = img[20:80, 30:100]
    p = tmp_path / "cloned.png"
    Image.fromarray((moved * 255).round().astype(np.uint8)).save(p)
    t = wb.smart_report(ingest(wb, case, p)["id"], EXAMINER)
    assert "tamper.copy_move" in {a["name"] for a in t["result"]["analyses"]}
    assert t["level"] == "red" and "clone.match" in {r["code"] for r in t["result"]["reasons"]}
