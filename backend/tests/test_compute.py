"""Parallel execution, RAM caches and the GPU path must never change a result."""

import cv2
import numpy as np
import pytest

from forensic import compute, media, models
from forensic.filters import REGISTRY
from forensic.graph import Engine, pixel_hash

from .conftest import _scene, ingest

# Non-default settings for filters whose defaults leave most of the code idle.
EXTRA = {
    "deblur.richardson_lucy": {"psf": "defocus", "radius": 2.5, "iterations": 5, "tv": 0.004},
    "geometry.rotate": {"angle": 7.3},
    "stack.integrate": {"method": "sigma_clip"},
    "stack.stabilise": {"motion": "affine"},
    "stack.super_resolution": {"iterations": 3},
}


def _inputs():
    img = _scene(120, 160).astype(np.float32) / 255
    shift = lambda dx, dy: cv2.warpAffine(  # noqa: E731
        img, np.float32([[1, 0, dx], [0, 1, dy]]), (160, 120), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    return img, np.stack([shift(0.4 * i, -0.3 * i) for i in range(4)])


def _all_hashes():
    img, stack = _inputs()
    out = {}
    for fid, f in REGISTRY.items():
        if fid == "geometry.perspective":  # needs four user points
            continue
        p = f.validate(EXTRA.get(fid, {}))
        x = stack if f.input_kind == "stack" else img
        out[fid] = pixel_hash(np.asarray(f.fn(x, p), np.float32))
    return out


def _threads(monkeypatch, n):
    monkeypatch.setenv("FORENSIC_THREADS", str(n))
    compute.configure_opencv()
    models._session.cache_clear()


def test_every_filter_is_identical_on_one_thread_and_many(monkeypatch):
    _threads(monkeypatch, 1)
    one = _all_hashes()
    _threads(monkeypatch, 4)
    many = _all_hashes()
    assert one == many
    _threads(monkeypatch, compute.cpu_threads())


def test_parallel_median_and_mean_match_numpy():
    rng = np.random.default_rng(3)
    for n in (2, 5, 8):
        xs = [rng.random((97, 61, 3), dtype=np.float32) for _ in range(n)]
        st = np.stack(xs)
        assert np.array_equal(compute.median0(xs), np.median(st, axis=0))
        assert np.array_equal(compute.median0(st), np.median(st, axis=0))
        assert np.array_equal(compute.mean0(xs), st.mean(axis=0))
        assert np.array_equal(compute.mean0(st, np.float64), st.mean(axis=0, dtype=np.float64))


def test_pmap_keeps_order_and_nests():
    assert compute.pmap(lambda i: compute.pmap(lambda j: i * 10 + j, range(3)), range(5)) == [
        [i * 10 + j for j in range(3)] for i in range(5)]


def test_lookup_conversion_matches_division():
    u8 = np.arange(256, dtype=np.uint8).reshape(16, 16)
    assert np.array_equal(media.u8_to_float(u8), u8.astype(np.float32) / 255.0)


def test_frame_cache_matches_fresh_decode(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    src = {"evidence_id": ev["id"], "frames": {"start": 3, "end": 7}}
    fresh = wb._load_source(src, use_cache=False)
    cached = wb._load_source(src)  # decodes a window and fills the cache
    again = wb._load_source(src)  # served from RAM
    assert np.array_equal(fresh, cached) and np.array_equal(fresh, again)
    # Later frames came in with the window, and match a fresh decode of each.
    for n in (8, 12):
        one = {"evidence_id": ev["id"], "frames": {"start": n, "end": n}}
        assert np.array_equal(wb._load_source(one), wb._load_source(one, use_cache=False))


def test_frame_cache_respects_its_budget(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    wb.frames = media.FrameCache(160 * 120 * 3 * 10)  # room for ten frames
    for n in range(0, 25, 4):
        wb._load_source({"evidence_id": ev["id"], "frames": {"start": n, "end": n}})
    assert wb.frames._bytes <= wb.frames.max_bytes


def test_engine_cache_is_bounded_in_bytes():
    img = np.zeros((100, 100, 3), np.float32)
    e = Engine(lambda src, use_cache=True: img.copy())
    e.cache.max_bytes = img.nbytes * 3
    for i in range(10):
        e.cache.put(str(i), img.copy())
    assert e.cache._bytes <= e.cache.max_bytes and len(e.cache._d) == 3


def test_models_report_their_device():
    assert models.device("sr.fsrcnn_x2") in ("cpu",) or models.device("sr.fsrcnn_x2").startswith("gpu")
    assert "CPU threads" in compute.describe()


@pytest.mark.parametrize("value", ["cpu", "nonsense"])
def test_cpu_can_be_forced(monkeypatch, value):
    monkeypatch.setenv("FORENSIC_AI_DEVICE", value)
    models._session.cache_clear()
    if value == "cpu":
        assert models.gpu_provider() is None
        assert models.device("sr.fsrcnn_x2") == "cpu"
    else:
        assert compute.ai_device() == "auto"
    models._session.cache_clear()
