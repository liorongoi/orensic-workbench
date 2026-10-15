"""The deblur benchmark harness: its measures, its alignment and its dataset loader."""

import numpy as np
from PIL import Image

from forensic.filters.deblur import make_psf
from forensic.validation import deblur as bench
from forensic.validation.run import scene


def _grey(seed, n=96):
    return scene(seed, n, n).mean(axis=2) / 255.0


def test_measures_on_identical_and_noisy_images():
    x = _grey(1)
    assert bench.psnr(x, x) == float("inf") and abs(bench.ssim(x, x) - 1) < 1e-9
    noisy = x + np.random.default_rng(0).normal(0, 0.01, x.shape)
    assert 39 < bench.psnr(noisy, x) < 41  # 20 log10(1 / 0.01)
    assert bench.ssim(noisy, x) < 1


def test_alignment_removes_a_subpixel_shift():
    x = _grey(2, 128)
    moved = np.asarray(Image.fromarray(x.astype(np.float32)).transform(x.shape[::-1], Image.AFFINE,
                                                                      (1, 0, -2.25, 0, 1, 1.5), Image.BILINEAR))
    a, b = bench.align_upto_shift(moved, x)
    assert bench.psnr(a, b) > bench.psnr(moved[15:-15, 15:-15], b) + 10


def test_closest_shape_finds_a_motion_blur():
    k = make_psf({"psf": "motion", "length": 12.0, "angle": 30.0, "radius": 3.0, "sigma": 1.5})
    _, shape = bench.closest_parametric_psf(k)
    assert shape["psf"] == "motion" and abs(shape["length"] - 12) <= 1 and abs(shape["angle"] - 30) <= 3


def test_levin_layout_orientation_and_scores(tmp_path):
    (tmp_path / "gt").mkdir()
    k = make_psf({"psf": "motion", "length": 7.0, "angle": 20.0, "radius": 3.0, "sigma": 1.5})
    k[0, 0] += 0.3  # make it asymmetric so the orientation matters
    k /= k.sum()
    for i in (1, 2):
        x = _grey(10 + i)
        Image.fromarray((x * 255).round().astype(np.uint8)).save(tmp_path / "gt" / f"im{i}.png")
        y = bench._blur(x, k, circular=False)
        Image.fromarray((y * 255).round().astype(np.uint8)).save(tmp_path / f"im{i}_kernel1_img.png")
    # Stored rotated by 180 degrees, as some copies of the dataset are.
    Image.fromarray((k[::-1, ::-1] / k.max() * 255).round().astype(np.uint8)).save(tmp_path / "gt" / "kernel1.png")
    cases = bench.levin_cases(tmp_path)
    assert len(cases) == 2
    assert np.abs(cases[0]["kernel"] - k).sum() < np.abs(cases[0]["kernel"] - k[::-1, ::-1]).sum()

    grid = {"wiener": [{"nsr": 0.01}], "richardson_lucy": [{"iterations": 20, "tv": 0.0}],
            "tv": [{"regularisation": 0.002, "iterations": 3}]}
    res = bench.evaluate(cases, bench.levin_score, grid=grid)
    blurred = np.mean([bench.levin_score(c["blurred"], c["sharp"])["psnr"] for c in cases])
    assert res["tv"]["best"]["psnr"] > blurred + 2
    md = bench.summary_markdown({"levin": {"n": 2, "blurred": {"psnr": blurred, "ssim": 0.5}, "true_kernel": res,
                                           "closest_app_shape": res, "app_motion_estimate": res, "references": {}}})
    assert "Total variation" in md


def test_synthetic_protocol_is_reproducible(tmp_path):
    Image.fromarray(scene(3, 64, 72)).save(tmp_path / "a.png")
    k = make_psf({"psf": "defocus", "length": 9.0, "angle": 0.0, "radius": 2.0, "sigma": 1.5})
    c1 = bench.synthetic_cases(tmp_path, [k], 2.55, "t")
    c2 = bench.synthetic_cases(tmp_path, [k], 2.55, "t")
    assert c1[0]["name"] == "t_a_k1_s2.55" and c1[0]["blurred"].shape == (64, 72, 3)
    assert np.array_equal(c1[0]["blurred"], c2[0]["blurred"])
