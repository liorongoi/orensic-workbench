"""The desktop app: files from the native Open dialog, the System dialog, and the memory and
GPU settings for a workstation (RTX A4000, 48-128 GB RAM)."""

import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient

from forensic import api, compute, desktop, models
from forensic.config import load_settings
from forensic.filters import REGISTRY, FilterError
from forensic.graph import Engine
from forensic.workbench import Workbench

from .conftest import ingest, sign_in


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield sign_in(c)
    api.jobs().stop()
    api._wb.db.close()
    api._wb = None


def test_offered_files_are_single_use(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    files = desktop.LocalFiles()
    t = files.offer(f)
    assert files.take("guess") is None
    assert files.take(t) == f
    assert files.take(t) is None


def test_offers_expire(tmp_path, monkeypatch):
    files = desktop.LocalFiles()
    t = files.offer(tmp_path / "a.jpg")
    now = desktop.time.monotonic()
    monkeypatch.setattr(desktop.time, "monotonic", lambda: now + desktop.TOKEN_TTL + 1)
    assert files.take(t) is None


def test_local_ingest_needs_a_token_from_the_dialog(client, jpeg_path):
    case = client.post("/api/cases", json={"name": "Desktop", "examiner": "x"}).json()
    r = client.post(f"/api/cases/{case['id']}/evidence/local", json={"token": "made-up"})
    assert r.status_code == 404
    token = desktop.local_files.offer(jpeg_path)
    ev = client.post(f"/api/cases/{case['id']}/evidence/local", json={"token": token}).json()
    assert ev["filename"] == "scene.jpg" and ev["media_type"] == "image"
    # Same file through the browser upload gives the same hash.
    with open(jpeg_path, "rb") as f:
        up = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("scene.jpg", f)}).json()
    assert up["sha256"] == ev["sha256"]
    # The token was used up.
    assert client.post(f"/api/cases/{case['id']}/evidence/local", json={"token": token}).status_code == 404


def test_local_ingest_needs_sign_in(tmp_path, jpeg_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    try:
        with TestClient(api.app) as c:
            token = desktop.local_files.offer(jpeg_path)
            assert c.post("/api/cases/x/evidence/local", json={"token": token}).status_code == 401
            assert c.get("/api/system").status_code == 401
    finally:
        api.jobs().stop()
        api._wb.db.close()
        api._wb = None


def test_system_summary(client):
    s = client.get("/api/system").json()
    assert s["cpu_threads"] >= 1 and s["ram_mb"] > 0
    assert s["result_cache_mb"] > 0 and s["frame_cache_mb"] > 0 and s["range_mb"] >= 2048
    assert set(s["gpu"]) >= {"name", "memory_mb", "provider", "jobs"}


def test_gpu_is_read_from_the_nvidia_driver(monkeypatch):
    monkeypatch.setattr(compute, "_gpu_info", None)
    monkeypatch.setattr(compute, "_nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="NVIDIA RTX A4000, 16376\n", stderr=""))
    g = compute.gpu_info()
    assert g == {"name": "NVIDIA RTX A4000", "memory_mb": 16376, "source": "nvidia-smi"}
    assert "NVIDIA RTX A4000, 16376 MB" in compute.describe()
    monkeypatch.setenv("FORENSIC_GPU_MEMORY_MB", "8000")
    assert compute.gpu_info()["memory_mb"] == 8000
    monkeypatch.setattr(compute, "_gpu_info", None)


def test_no_nvidia_driver_means_no_card_named(monkeypatch):
    monkeypatch.setattr(compute, "_gpu_info", None)
    monkeypatch.setattr(compute, "_nvidia_smi", lambda: None)
    assert compute.gpu_info()["name"] == ""
    monkeypatch.setattr(compute, "_gpu_info", None)


def test_directml_asks_for_the_discrete_card(monkeypatch):
    monkeypatch.delenv("FORENSIC_GPU_DEVICE", raising=False)
    assert models.provider_options("DmlExecutionProvider") == {"performance_preference": "high_performance",
                                                               "device_filter": "gpu"}
    monkeypatch.setenv("FORENSIC_GPU_DEVICE", "1")
    assert models.provider_options("DmlExecutionProvider") == {"device_id": "1"}
    assert models.provider_options("CPUExecutionProvider") == {}


@pytest.mark.parametrize("ram_gb, expect_gb", [(48, 8), (64, 64 / 6), (128, 128 / 6), (256, 24), (4, 2)])
def test_memory_budgets_scale_with_ram(monkeypatch, ram_gb, expect_gb):
    monkeypatch.setattr(compute, "physical_memory", lambda: ram_gb * compute.GB)
    assert compute.range_budget() == max(2 * compute.GB, min(ram_gb * compute.GB // 6, 24 * compute.GB))
    assert abs(compute.range_budget() / compute.GB - expect_gb) < 0.01
    # Caches together leave most of the RAM to the jobs themselves.
    assert compute.cache_budget() + compute.frame_cache_budget() <= max(1.5 * compute.GB, 0.375 * ram_gb * compute.GB)


def test_a_range_too_big_for_memory_is_refused_clearly(wb, case, video_path, monkeypatch):
    ev = ingest(wb, case, video_path)
    monkeypatch.setenv("FORENSIC_RANGE_MB", "1")  # 1 MB: about 4 frames of 160x120
    with pytest.raises(FilterError, match="Shorten the range"):
        wb._load_source({"evidence_id": ev["id"], "frames": {"start": 0, "end": 20}})
    assert wb._load_source({"evidence_id": ev["id"], "frames": {"start": 0, "end": 3}}).shape[0] == 4


def test_per_frame_filters_on_a_stack_match_frame_by_frame():
    rng = np.random.default_rng(1)
    stack = rng.random((5, 40, 50, 3), dtype=np.float32)
    picked = [f for f in REGISTRY.values() if f.input_kind == "image" and not f.ai and f.id != "geometry.perspective"]
    assert len(picked) > 10
    for f in picked[::4]:
        fid, p = f.id, f.validate({})
        try:
            expect = np.stack([np.asarray(f.fn(fr, p), np.float32) for fr in stack])
        except FilterError:  # needs points or a region drawn by the examiner
            continue
        assert np.array_equal(Engine._per_frame(f, p, stack), expect), fid


def test_warm_reports_each_model():
    out = models.warm(["sr.fsrcnn_x2", "no.such.model"])
    assert out["sr.fsrcnn_x2"] == "cpu" or out["sr.fsrcnn_x2"].startswith("gpu")
    assert out["no.such.model"].startswith("not loaded")
