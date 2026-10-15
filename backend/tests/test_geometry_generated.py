"""Shadow, reflection and perspective consistency, and the bundled generated-image detector."""

import math

import numpy as np
import pytest
from PIL import Image

from forensic import models
from forensic.analysis import ANALYSES
from forensic.analysis import generated, geometry
from forensic.validation import load_card

from .conftest import EXAMINER, _scene, ingest

LIGHT = np.array([900.0, -300.0])


def _shadow_pairs(light, objects, length=80.0, behind=False):
    pts = []
    for o in objects:
        o = np.array(o, float)
        d = (light - o) / np.hypot(*(light - o))
        s = o + d * length if behind else o - d * length
        pts += [list(s), list(o)]
    return pts


OBJECTS = [(100, 400), (250, 300), (400, 420), (550, 250), (300, 150)]


@pytest.fixture()
def image_path(tmp_path):
    p = tmp_path / "scene.png"
    Image.fromarray(_scene(h=480, w=640, seed=2)).save(p)
    return p


def _run(wb, case, path, aid, params):
    return wb.run_analysis(ingest(wb, case, path)["id"], aid, params, EXAMINER)["result"]


def test_consistent_shadows_locate_the_light(wb, case, image_path):
    r = _run(wb, case, image_path, "geometry.shadows", {"pairs": _shadow_pairs(LIGHT, OBJECTS)})
    f = r["findings"]
    assert f["consistent"] and f["light_side"] == "in front of the camera"
    assert np.hypot(*(np.array(f["light_projection_xy"]) - LIGHT)) < 60
    assert "constraints" in r["maps"]
    assert not any(fl["level"] == "warning" for fl in r["flags"])


def test_light_behind_the_camera_is_consistent(wb, case, image_path):
    light = np.array([320.0, -900.0])
    r = _run(wb, case, image_path, "geometry.shadows", {"pairs": _shadow_pairs(light, OBJECTS, behind=True)})
    assert r["findings"]["consistent"] and r["findings"]["light_side"] == "behind the camera"


def test_a_shadow_from_another_light_is_found_and_named(wb, case, image_path):
    pts = _shadow_pairs(LIGHT, OBJECTS) + _shadow_pairs(np.array([-600.0, -200.0]), [(450, 350)])
    r = _run(wb, case, image_path, "geometry.shadows", {"pairs": pts})
    f = r["findings"]
    assert not f["consistent"] and 6 in f["smallest_conflicting_set"] and len(f["smallest_conflicting_set"]) <= 3
    assert f["conflict_persists_up_to_deg"] > 10
    assert "geom.shadows.inconsistent" in {fl.get("code") for fl in r["flags"]}


def test_reflections_share_one_vanishing_point(wb, case, image_path):
    vp = np.array([320.0, 2000.0])
    pts = []
    for o in OBJECTS:
        o = np.array(o, float)
        pts += [list(o), list(o + (vp - o) / np.hypot(*(vp - o)) * 60)]
    assert _run(wb, case, image_path, "geometry.reflections", {"pairs": pts})["findings"]["consistent"]
    pts += [[500, 100], [560, 130]]
    assert not _run(wb, case, image_path, "geometry.reflections", {"pairs": pts})["findings"]["consistent"]


def test_perspective_lines_in_either_order(wb, case, image_path):
    vp = np.array([1500.0, 200.0])
    pts = []
    for i, o in enumerate(OBJECTS):
        o = np.array(o, float)
        b = o + (vp - o) / np.hypot(*(vp - o)) * 120
        pts += [list(b), list(o)] if i % 2 else [list(o), list(b)]
    r = _run(wb, case, image_path, "geometry.perspective", {"pairs": pts})
    assert r["findings"]["consistent"] and np.hypot(*(np.array(r["findings"]["vanishing_point_xy"]) - vp)) < 100
    pts += [[300, 100], [400, 180]]
    r = _run(wb, case, image_path, "geometry.perspective", {"pairs": pts})
    assert not r["findings"]["consistent"] and 6 in r["findings"]["smallest_conflicting_set"]


def test_short_constraints_get_wider_wedges():
    ws = geometry.wedges([[0, 0], [10, 0], [0, 0], [200, 0]], 2.0, click_px=2.0)
    assert ws[0]["tol"] > ws[1]["tol"] > math.radians(2.0)


def test_geometry_rejects_incomplete_input(wb, case, image_path):
    with pytest.raises(ValueError):
        _run(wb, case, image_path, "geometry.shadows", {"pairs": [[1, 1], [50, 50], [100, 100]]})
    with pytest.raises(ValueError):
        _run(wb, case, image_path, "geometry.shadows", {"pairs": [[1, 1], [50, 50]]})


def test_geometric_and_generated_detectors_have_cards():
    for aid in ("geometry.shadows", "geometry.reflections", "geometry.perspective", "synthetic.generated"):
        card = load_card(aid)
        assert card and card["version"] == ANALYSES[aid].version and card["known_failure_modes"]
        assert card["results"]["detection"]["true_positive_rate"] is not None


# Generated-image detector --------------------------------------------------------------------------


def test_detector_model_is_bundled_pinned_and_mit():
    m = models.get(generated.MODEL_ID)
    assert m["bundled"] and m["licence"] == "MIT" and models.verify(m)["ok"]
    assert m["kind"] == "detector" and m["trained_on"] and m["version"]


def test_preprocessing_matches_the_authors_protocol():
    img = np.random.default_rng(0).random((600, 900, 3)).astype(np.float32)
    x = generated.preprocess(img)
    assert x.shape == (1, 3, 384, 384) and x.dtype == np.float32
    # resize shorter side to 440 (bilinear, as torchvision on a PIL image), centre crop 384, ImageNet normalisation
    u8 = (img * 255 + 0.5).astype(np.uint8)
    im = Image.fromarray(u8).resize((660, 440), Image.BILINEAR).crop((138, 28, 522, 412))
    ref = ((np.asarray(im, np.float32) / 255 - generated.MEAN) / generated.STD).transpose(2, 0, 1)
    assert np.array_equal(x[0], ref)


def test_detector_scores_a_rendered_scene_as_not_generated(wb, case, image_path):
    r = _run(wb, case, image_path, "synthetic.generated", {})
    f = r["findings"]
    assert 0 <= f["score"] < 0.5 and f["model"]["id"] == generated.MODEL_ID and f["model"]["verified_sha256"]
    assert set(f["scores"]) == {"original", "mirrored"}
    a = next(a for a in wb.analyses() if a["id"] == "synthetic.generated")
    assert a["ai"] and a["unavailable"] is None


def test_detector_output_is_pinned():
    # regression guard for the converted graph and the preprocessing (values from the ONNX model on the CPU)
    assert generated.score(_scene(h=300, w=420, seed=0).astype(np.float32) / 255) == pytest.approx(0.01235, abs=2e-3)
    assert generated.score(_scene(h=300, w=420, seed=3).astype(np.float32) / 255) == pytest.approx(0.07576, abs=5e-3)


def test_lab_slot_does_not_pick_up_the_bundled_detector(wb):
    a = next(a for a in wb.analyses() if a["id"] == "synthetic.learned")
    assert a["unavailable"]
