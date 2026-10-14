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

Inference runs on the CPU through ONNX Runtime with one thread, so the same
build on the same CPU family gives bit-identical results.
"""

from __future__ import annotations

import hashlib
import json
import os
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


_lock = threading.Lock()


@lru_cache(maxsize=32)
def _session(path: str, sha256: str):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.log_severity_level = 3
    return ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])


def session(model_id: str):
    """A verified ONNX Runtime session (CPU, single thread)."""
    m = get(model_id)
    v = verify(m)
    if not v["ok"]:
        raise ModelError(f"{model_id}: {v['problem']}; refusing to run it")
    return _session(m["path"], m["sha256"])


def run(model_id: str, x: np.ndarray) -> np.ndarray:
    sess = session(model_id)
    name = sess.get_inputs()[0].name
    with _lock:  # one inference at a time keeps results independent of server load
        return sess.run(None, {name: np.ascontiguousarray(x, dtype=np.float32)})[0]


def face_detector(width: int, height: int, score_threshold: float = 0.6):
    """OpenCV's YuNet wrapper (decoding and non-maximum suppression) on the verified bundled weights."""
    import cv2

    m = get("face.yunet")
    v = verify(m)
    if not v["ok"]:
        raise ModelError(f"face.yunet: {v['problem']}; refusing to run it")
    return cv2.FaceDetectorYN.create(m["path"], "", (int(width), int(height)), score_threshold, 0.3, 5000)


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
    if any(m["id"] == card["id"] and m["bundled"] for m in manifest()):
        raise ModelError(f"'{card['id']}' is a bundled model id")
    sha = file_sha256(Path(onnx_file))
    if card.get("sha256") and card["sha256"] != sha:
        raise ModelError("the card's sha256 does not match the file")
    d = lab_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{card['id']}.onnx"
    shutil.copyfile(onnx_file, d / name)
    card = card | {"file": name, "sha256": sha}
    (d / f"{card['id']}.json").write_text(json.dumps(card, indent=2) + "\n")
    _session.cache_clear()
    return card
