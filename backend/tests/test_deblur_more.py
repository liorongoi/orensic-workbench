"""Drawn-path and light-streak blur, blind deblur, variable motion and turbulence removal."""

import numpy as np
import pytest

from forensic.filters.base import REGISTRY, FilterError
from forensic.filters.deblur import estimate_kernel, make_psf
from forensic.validation import deblur as bench
from forensic.validation.run import scene


def _f(fid, x, **p):
    f = REGISTRY[fid]
    return f.fn(x, f.validate(p))


def _grey(seed, n=128):
    return scene(seed, n, n).mean(axis=2) / 255.0


def _err(a, b, m=16):
    return float(np.abs(a - b)[m:-m, m:-m].mean())


def test_path_psf_follows_the_drawn_path():
    straight = make_psf({"psf": "path", "path": [[10, 10], [22, 10]]})
    line = make_psf({"psf": "motion", "length": 12.0, "angle": 0.0, "radius": 3.0, "sigma": 1.5})
    assert abs(straight.sum() - 1) < 1e-9 and np.count_nonzero(straight.sum(axis=0)) in (12, 13, 14)
    assert bench._kernel_distance(straight, line) < 0.01
    bent = make_psf({"psf": "path", "path": [[0, 0], [8, 0], [8, 8]]})
    assert bent.shape[0] >= 9 and bent[bent.shape[0] // 2 + 4, :].sum() > 0  # mass below the centre row
    with pytest.raises(FilterError):
        make_psf({"psf": "path", "path": [[0, 0]]})


def test_streak_psf_reads_the_kernel_from_a_point_light():
    k = make_psf({"psf": "path", "path": [[0, 0], [6, 2], [9, 8]]})
    assert bench._kernel_distance(bench.light_streak(k), k) < 0.02
    img = np.full((40, 40, 3), 0.1, np.float32)
    with pytest.raises(FilterError):
        make_psf({"psf": "streak", "streak": [[5, 5], [30, 30]]}, img)  # nothing brighter than the background


def test_filters_accept_a_streak_from_the_image():
    x = np.dstack([_grey(3)] * 3).astype(np.float32)
    x[96:124, 96:124] = 0.05
    x[110, 108] = 1.0  # a small light in a dark area
    k = make_psf({"psf": "motion", "length": 7.0, "angle": 30.0, "radius": 3.0, "sigma": 1.5})
    y = np.dstack([bench._blur(x[..., c], k, circular=False) for c in range(3)]).astype(np.float32)
    out = _f("deblur.tv", y, psf="streak", streak=[[101, 102], [117, 118]], regularisation=0.001)
    assert _err(out, x) < 0.8 * _err(y, x)


def test_blind_estimates_a_curved_shake():
    x = _grey(5, 160)
    k = make_psf({"psf": "path", "path": bench.shake_trajectory(11.0, seed=2).tolist()})
    y = bench._blur(x, k, circular=False) + np.random.default_rng(0).normal(0, 0.003, x.shape)
    est = estimate_kernel(y, 21, 0.01, 5)
    assert bench._kernel_distance(est, k) < bench._kernel_distance(bench._fit_delta(21), k)
    out = _f("deblur.blind", np.dstack([y] * 3).astype(np.float32), kernel_size=21, smoothness=0.01, regularisation=0.001)
    assert _err(out[..., 0], x) < 0.85 * _err(y, x)


def test_variable_motion_beats_one_uniform_blur_on_a_camera_roll():
    x = _grey(7, 192)
    h, w = x.shape
    centre = (w * 0.3, h * 0.35)
    y = bench.rotation_blur(x, 4.0, centre).astype(np.float32)
    streaks = bench.rotation_streaks(4.0, centre, h, w)
    y3 = np.dstack([y] * 3)
    var = _f("deblur.variable", y3, streaks=streaks, tiles=6, regularisation=0.002)[..., 0]
    seg = np.asarray(streaks[:2])
    v = seg[1] - seg[0]
    one = _f("deblur.tv", y3, psf="motion", length=float(np.hypot(*v)),
             angle=float(np.degrees(np.arctan2(-v[1], v[0]))), regularisation=0.002)[..., 0]
    assert _err(var, x) < _err(one, x) and _err(var, x) < _err(y, x)
    with pytest.raises(FilterError):
        _f("deblur.variable", y3, streaks=streaks[:3])


def test_turbulence_removal_beats_one_frame_and_the_plain_mean():
    x = _grey(9, 128)
    stack = bench.turbulence_stack(x, 10, 1.5, 1.0, 2.0, seed=0)
    out = _f("stack.turbulence", stack)[..., 0]
    assert _err(out, x) < _err(stack[5, ..., 0], x) and _err(out, x) < _err(stack.mean(axis=0)[..., 0], x)


def test_new_filters_are_deterministic():
    x = np.dstack([_grey(11, 96)] * 3).astype(np.float32)
    a = _f("deblur.blind", x, kernel_size=11)
    assert np.array_equal(a, _f("deblur.blind", x, kernel_size=11))
