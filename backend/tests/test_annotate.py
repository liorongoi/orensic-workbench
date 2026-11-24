"""Items from the 24 November 2026 review: colour channel actions, uncropped perspective correction, the
annotation and measurement filters, and the 0-1.0 face-check score."""

import numpy as np
import pytest

from forensic.analysis.faces import SCORE_THRESHOLD, Z_FLAG, face_score
from forensic.filters import FilterError, get_filter
from forensic.graph import normalise


def _img(h=240, w=320):
    rng = np.random.default_rng(3)
    return rng.random((h, w, 3), dtype=np.float32)


def _run(fid, img, **params):
    f = get_filter(fid)
    return np.asarray(f.fn(img, f.validate(params)), np.float32)


# Greyscale / colour channel -----------------------------------------------------------


def test_colour_actions_keep_or_remove_the_selected_colour():
    img = _img()
    keep = _run("color.grayscale", img, source="green", action="keep_only_this_colour")
    assert np.array_equal(keep[..., 1], img[..., 1]) and not keep[..., [0, 2]].any()
    remove = _run("color.grayscale", img, source="green", action="remove_this_colour")
    assert not remove[..., 1].any() and np.array_equal(remove[..., [0, 2]], img[..., [0, 2]])
    grey = _run("color.grayscale", img, source="red", action="show_this_channel_as_grey")
    assert all(np.array_equal(grey[..., c], img[..., 0]) for c in range(3))


def test_a_colour_other_than_luma_keeps_colours_by_default():
    out = _run("color.grayscale", _img(), source="blue")
    assert out[..., 2].any() and not out[..., :2].any()


def test_chains_saved_with_the_old_greyscale_version_still_reproduce():
    g = normalise({"source": {"evidence_id": "e1"}, "nodes": [{"filter": "color.grayscale", "version": "1.0.0", "params": {"source": "red"}}]})
    node = g["nodes"][0]
    assert node["version"] == get_filter("color.grayscale").version
    assert node["params"]["action"] == "show_this_channel_as_grey"


# Perspective correction ---------------------------------------------------------------

CORNERS = [[60, 40], [260, 55], [270, 200], [50, 190]]


def test_perspective_keeps_the_whole_picture_unless_asked_to_crop():
    img = _img()
    whole = _run("geometry.perspective", img, points=CORNERS)
    cropped = _run("geometry.perspective", img, points=CORNERS, crop=True)
    assert whole.shape[0] > cropped.shape[0] and whole.shape[1] > cropped.shape[1]
    assert whole.shape[0] < 3 * img.shape[0] + 2 and whole.shape[1] < 3 * img.shape[1] + 2


def test_perspective_canvas_is_bounded_near_the_horizon():
    img = _img()
    out = _run("geometry.perspective", img, points=[[150, 5], [170, 5], [310, 235], [10, 235]])
    assert out.shape[0] <= 3 * img.shape[0] + 2 and out.shape[1] <= 3 * img.shape[1] + 2


def test_old_perspective_chains_still_crop():
    g = normalise({"source": {"evidence_id": "e1"}, "nodes": [{"filter": "geometry.perspective", "version": "1.0.0", "params": {"points": CORNERS}}]})
    assert g["nodes"][0]["params"]["crop"] is True


def test_unknown_old_versions_are_still_refused():
    with pytest.raises(FilterError, match="pinned"):
        normalise({"source": {"evidence_id": "e1"}, "nodes": [{"filter": "geometry.crop", "version": "0.9.0", "params": {}}]})


# Annotation ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape,points", [
    ("arrow", [[20, 20], [120, 90]]), ("line", [[10, 200], [200, 210]]), ("rectangle", [[100, 60], [200, 140]]),
    ("circle", [[250, 60], [280, 60]]), ("text", [[20, 230]]), ("zoom", [[140, 90], [170, 115]]),
])
def test_annotation_marks_only_change_pixels_near_the_mark(shape, points):
    img = _img()
    out = _run("annotate.mark", img, shape=shape, position=points, text="Exhibit A")
    changed = np.any(out != img, axis=2)
    assert changed.any() and out.shape == img.shape
    assert changed.mean() < 0.5  # the rest of the picture is untouched
    assert np.array_equal(out, _run("annotate.mark", img, shape=shape, position=points, text="Exhibit A"))


def test_zoom_inset_shows_the_original_pixels_enlarged():
    img = _img()
    out = _run("annotate.mark", img, shape="zoom", position=[[40, 40], [59, 59]], zoom=3, zoom_position="bottom_right",
               text="")
    # 20 x 20 area at x3 = 60 x 60, placed with a margin at the bottom right; its centre holds original pixels.
    m = 4
    inset = out[240 - m - 60 : 240 - m, 320 - m - 60 : 320 - m]
    assert np.array_equal(inset[30, 30], img[50, 50])


def test_annotation_without_a_position_leaves_the_picture_as_it_is():
    img = _img()
    assert np.array_equal(_run("annotate.mark", img), img)


def test_text_mark_needs_text():
    with pytest.raises(FilterError, match="text"):
        _run("annotate.mark", _img(), shape="text", position=[[10, 10]])


# Measurement --------------------------------------------------------------------------


def test_measurement_draws_lines_and_needs_a_reference_for_real_units():
    img = _img()
    out = _run("annotate.measure", img, lines=[[10, 20], [210, 20]])
    assert np.any(out != img)
    with pytest.raises(FilterError, match="reference"):
        _run("annotate.measure", img, lines=[[10, 20], [210, 20]], reference_length=30)
    scaled = _run("annotate.measure", img, lines=[[10, 20], [210, 20]], reference=[[10, 100], [110, 100]],
                  reference_length=30, unit="cm", show_angle=True)
    assert np.any(scaled != out)


def test_measurement_runs_after_other_steps_in_a_chain(wb, case, jpeg_path):
    from .conftest import EXAMINER, ingest

    ev = ingest(wb, case, jpeg_path)
    nodes = [{"filter": "geometry.perspective", "params": {"points": CORNERS, "crop": True}},
             {"filter": "annotate.measure", "params": {"lines": [[5, 5], [150, 5]], "reference": [[5, 50], [105, 50]],
                                                       "reference_length": 20, "unit": "cm"}}]
    ch = wb.create_chain(case["id"], ev["id"], "measured", EXAMINER, {"nodes": nodes})
    assert wb.render_chain(ch["id"], EXAMINER)


# Face-check score -----------------------------------------------------------------------


def test_face_score_runs_from_0_to_1_with_the_threshold_at_one_half():
    assert face_score(0.0, 0.0) == 0.0
    assert face_score(Z_FLAG, 0.0) == SCORE_THRESHOLD == 0.5
    assert face_score(Z_FLAG - 0.01, None) < 0.5  # never shown as 0.50 below the threshold
    assert face_score(-2 * Z_FLAG, 1.0) == 0.8
    assert 0.99 <= face_score(1e6, None) <= 1.0
    assert face_score(None, None) is None
    assert face_score(0.0, 0.0, jpeg_share_face=0.9, jpeg_share_image=0.1) >= 0.5
