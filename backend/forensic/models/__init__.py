"""Learned models: a pinned, offline registry.

Every model the workbench runs is listed in a manifest with the SHA-256 of its
weights, its source, licence, training data and known limits. Weights are
verified before every load, so a swapped file is refused rather than used, and
the model's hash is printed in the report next to each step that used it.

Bundled models ship inside this package (forensic/models/weights), so nothing is
downloaded at run time and the workbench works air-gapped. A lab adds its own
models (for example a learned AI-image detector it has licensed and validated)
by installing an ONNX file plus a model card into FORENSIC_MODELS_DIR:

    python -m forensic.models list
    python -m forensic.models verify
    python -m forensic.models install detector.onnx --card detector.json

Inference runs through ONNX Runtime: on the GPU when one is usable (DirectML on
Windows, CUDA where onnxruntime-gpu is installed), otherwise on every CPU core.
A GPU is only used after a probe shows it reproduces a model's output bit for bit;
otherwise that model falls back to the CPU. The device each model ran on is
recorded in the report's software environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

PACKAGE_DIR = Path(__file__).parent
WEIGHTS_DIR = PACKAGE_DIR / "weights"
KINDS = ("enhancement", "face_detector", "detector")
CARD_FIELDS = ("id", "file", "sha256", "kind", "task", "architecture", "reference", "source", "licence", "training_data")


class ModelError(ValueError):
    pass


def lab_dir() -> Path:
    env = os.environ.get("FORENSIC_MODELS_DIR")
    if env:
        return Path(env).resolve()
    return Path(os.environ.get("FORENSIC_DATA_DIR", "./data")).resolve() / "models"


def _bundled() -> list[dict]:
    data = json.loads((PACKAGE_DIR / "manifest.json").read_text())
    return [m | {"path": str(WEIGHTS_DIR / m["file"]), "bundled": True} for m in data["models"]]


def _lab() -> list[dict]:
    d = lab_dir()
    if not d.is_dir():
        return []
    out = []
    for card in sorted(d.glob("*.json")):
        try:
            m = json.loads(card.read_text())
        except json.JSONDecodeError:
            continue
        if not all(k in m for k in CARD_FIELDS):
            continue
        out.append(m | {"path": str(d / Path(m["file"]).name), "bundled": False})
    return out


def manifest() -> list[dict]:
    """All known models: bundled first, then the lab's own."""
    seen, out = set(), []
    for m in _bundled() + _lab():
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        out.append(m)
    return out


def get(model_id: str) -> dict:
    for m in manifest():
        if m["id"] == model_id:
            return m
    raise ModelError(f"model '{model_id}' is not installed")


def public(m: dict) -> dict:
    """What the catalogue, report and audit log show about a model (no local paths)."""
    return {k: v for k, v in m.items() if k != "path"}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(m: dict) -> dict:
    path = Path(m["path"])
    if not path.is_file():
        return {"id": m["id"], "ok": False, "problem": "weights file missing"}
    got = file_sha256(path)
    if got != m["sha256"]:
        return {"id": m["id"], "ok": False, "problem": f"weights SHA-256 {got} does not match the manifest"}
    return {"id": m["id"], "ok": True, "sha256": got}


def verify_all() -> list[dict]:
    return [verify(m) for m in manifest()]


# One inference at a time per process: keeps results independent of server load, and
# the single GPU is shared by every job (DirectML sessions also must not run concurrently).
_lock = threading.Lock()
GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider")
_devices: dict[str, str] = {}


def gpu_provider() -> str | None:
    """The GPU execution provider this ONNX Runtime build offers, if any and if allowed."""
    from .. import compute

    if compute.ai_device() == "cpu":
        return None
    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
    except Exception:  # noqa: BLE001
        return None
    return next((p for p in GPU_PROVIDERS if p in available), None)


def _options(gpu: bool):
    import onnxruntime as ort

    from .. import compute

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1 if gpu else compute.cpu_threads()
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.log_severity_level = 3
    if gpu:
        opts.enable_mem_pattern = False  # required by DirectML
    return opts


def _probe_input(sess) -> np.ndarray:
    shape = [d if isinstance(d, int) and d > 0 else 64 for d in sess.get_inputs()[0].shape]
    return np.random.default_rng(0).random(shape).astype(np.float32)


def provider_options(provider: str) -> dict:
    """Which card a GPU provider uses.

    A workstation often has the processor's own graphics next to the NVIDIA card, and DirectML
    takes the first adapter Windows lists unless told otherwise. Ask for the high-performance
    GPU (the RTX A4000 on the lab machines), or the adapter number in FORENSIC_GPU_DEVICE.
    """
    dev = os.environ.get("FORENSIC_GPU_DEVICE", "").strip()
    if provider == "DmlExecutionProvider":
        if dev.isdigit():
            return {"device_id": dev}
        return {"performance_preference": "high_performance", "device_filter": "gpu"}
    if provider == "CUDAExecutionProvider":
        return {"device_id": dev if dev.isdigit() else "0"}
    return {}


def _gpu_session(path: str, provider: str):
    import onnxruntime as ort

    try:
        return ort.InferenceSession(path, _options(True), providers=[(provider, provider_options(provider)),
                                                                      "CPUExecutionProvider"])
    except Exception:  # noqa: BLE001  (an older runtime that does not know these options)
        return ort.InferenceSession(path, _options(True), providers=[provider, "CPUExecutionProvider"])


@lru_cache(maxsize=32)
def _session(path: str, sha256: str):
    import onnxruntime as ort

    provider = gpu_provider()
    if provider:
        try:
            sess = _gpu_session(path, provider)
            if sess.get_providers()[0] == provider:
                x = {sess.get_inputs()[0].name: _probe_input(sess)}
                a, b = sess.run(None, x)[0], sess.run(None, x)[0]
                if np.array_equal(a, b):
                    _devices[sha256] = provider
                    return sess
        except Exception:  # noqa: BLE001  (driver or memory problems: use the CPU)
            pass
    _devices[sha256] = "CPUExecutionProvider"
    return ort.InferenceSession(path, _options(False), providers=["CPUExecutionProvider"])


def session(model_id: str):
    """A verified ONNX Runtime session (GPU when usable and reproducible, otherwise all CPU cores)."""
    m = get(model_id)
    v = verify(m)
    if not v["ok"]:
        raise ModelError(f"{model_id}: {v['problem']}; refusing to run it")
    return _session(m["path"], m["sha256"])


def device(model_id: str) -> str:
    """Where a model runs: 'gpu (DmlExecutionProvider)' or 'cpu'."""
    session(model_id)
    p = _devices.get(get(model_id)["sha256"], "CPUExecutionProvider")
    return "cpu" if p == "CPUExecutionProvider" else f"gpu ({p})"


def device_label() -> str:
    p = gpu_provider()
    return f"GPU via {p} when reproducible, else CPU" if p else "CPU"


def warm(model_ids: list[str] | None = None) -> dict[str, str]:
    """Load, verify and probe models ahead of use (the desktop app does this in the background
    at start), so the first enhancement does not wait for the GPU to compile the network.
    Returns each model's device; a model that cannot load is reported, not raised."""
    out = {}
    for mid in model_ids or [m["id"] for m in manifest() if m["bundled"] and m["kind"] == "enhancement"]:
        try:
            out[mid] = device(mid)
        except Exception as e:  # noqa: BLE001
            out[mid] = f"not loaded: {e}"
    return out


def run(model_id: str, x: np.ndarray) -> np.ndarray:
    sess = session(model_id)
    name = sess.get_inputs()[0].name
    with _lock:
        return sess.run(None, {name: np.ascontiguousarray(x, dtype=np.float32)})[0]


def face_detector(width: int, height: int, score_threshold: float = 0.6):
    """OpenCV's YuNet wrapper (decoding and non-maximum suppression) on the verified bundled weights."""
    import cv2

    m = get("face.yunet")
    v = verify(m)
    if not v["ok"]:
        raise ModelError(f"face.yunet: {v['problem']}; refusing to run it")
    return cv2.FaceDetectorYN.create(m["path"], "", (int(width), int(height)), score_threshold, 0.3, 5000)


_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def install(onnx_file: Path, card_file: Path) -> dict:
    """Copy a lab model and its card into FORENSIC_MODELS_DIR, pinning the weights' hash."""
    card = json.loads(Path(card_file).read_text())
    missing = [k for k in CARD_FIELDS if k not in card and k not in ("file", "sha256")]
    if missing:
        raise ModelError(f"model card is missing {', '.join(missing)}")
    if card["kind"] not in KINDS:
        raise ModelError(f"kind must be one of {', '.join(KINDS)}")
    if card["kind"] == "detector":
        for k in ("input", "output", "trained_on", "version"):
            if k not in card:
                raise ModelError(f"a detector card needs '{k}' (see docs/ai-models.md)")
    if not isinstance(card["id"], str) or not _PLAIN_ID.fullmatch(card["id"]):
        raise ModelError("model id must be a plain name of letters, digits, '.', '_' or '-'")
    if any(m["id"] == card["id"] and m["bundled"] for m in manifest()):
        raise ModelError(f"'{card['id']}' is a bundled model id")
    sha = file_sha256(Path(onnx_file))
    if card.get("sha256") and card["sha256"] != sha:
        raise ModelError("the card's sha256 does not match the file")
    d = lab_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{card['id']}.onnx"
    for dest in (d / name, d / f"{card['id']}.json"):
        if dest.resolve().parent != d.resolve():
            raise ModelError("model id would place files outside the models folder")
    shutil.copyfile(onnx_file, d / name)
    card = card | {"file": name, "sha256": sha}
    (d / f"{card['id']}.json").write_text(json.dumps(card, indent=2) + "\n")
    _session.cache_clear()
    return card
