"""The filter-graph engine.

A chain is a JSON document: a source (one evidence file, optionally a frame or
frame range) and an ordered list of filter nodes. The canonical JSON of the
normalised chain is hashed into the report, and running it again on the
original must give the same pixel hash.

MVP chains are linear; the node list is the graph. Branching (one source, many
outputs) is a later extension of the same document.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import compute
from .db import canonical_json
from .filters import FilterError, get_filter

GRAPH_SCHEMA = 1

# OpenCV splits its work into row blocks, so the result does not depend on the thread
# count (tests/test_compute.py checks every filter); OpenCL stays off.
compute.configure_opencv()


def normalise(graph: dict) -> dict:
    """Validate a chain document and fill in defaults, versions and node ids."""
    if not isinstance(graph, dict):
        raise FilterError("chain must be an object")
    src = graph.get("source") or {}
    if not src.get("evidence_id"):
        raise FilterError("chain source needs an evidence_id")
    frames = src.get("frames")
    if frames is not None:
        start, end = int(frames.get("start", 0)), int(frames.get("end", frames.get("start", 0)))
        if start < 0 or end < start:
            raise FilterError("invalid frame range")
        frames = {"start": start, "end": end}
    source = {"evidence_id": str(src["evidence_id"]), "frames": frames}
    if src.get("sha256"):
        source["sha256"] = str(src["sha256"])

    nodes = []
    seen = set()
    for i, raw in enumerate(graph.get("nodes") or []):
        f = get_filter(raw.get("filter", ""))
        nid = str(raw.get("id") or f"n{i + 1}")
        if nid in seen:
            raise FilterError(f"duplicate node id {nid}")
        seen.add(nid)
        version = raw.get("version") or f.version
        if version != f.version:
            raise FilterError(
                f"{f.id} is pinned to version {version} but this build provides {f.version}; "
                "re-running would not reproduce the original result"
            )
        node = {
            "id": nid,
            "filter": f.id,
            "version": version,
            "params": f.validate(raw.get("params")),
            "enabled": bool(raw.get("enabled", True)),
        }
        if raw.get("note"):
            node["note"] = str(raw["note"])
        if raw.get("justification") and f.generative:
            node["justification"] = str(raw["justification"]).strip()[:4000]
        if raw.get("proposed_by"):
            node["proposed_by"] = str(raw["proposed_by"])
            if raw.get("accepted_by"):
                node["accepted_by"] = str(raw["accepted_by"])
        nodes.append(node)
    return {"schema": GRAPH_SCHEMA, "source": source, "nodes": nodes}


def pending_proposals(graph: dict) -> list[str]:
    """Ids of enabled nodes an agent proposed that no examiner has accepted yet."""
    return [n["id"] for n in graph["nodes"] if n["enabled"] and n.get("proposed_by") and not n.get("accepted_by")]


def graph_hash(graph: dict) -> str:
    return hashlib.sha256(canonical_json(graph).encode()).hexdigest()


def pixel_hash(data: np.ndarray) -> str:
    """Hash of the 8-bit output pixels plus their shape: what an examiner would see and export."""
    u8 = np.clip(np.rint(data * 255.0), 0, 255).astype(np.uint8)
    h = hashlib.sha256(f"u8:{'x'.join(map(str, u8.shape))};".encode())
    h.update(np.ascontiguousarray(u8).tobytes())
    return h.hexdigest()


@dataclass
class StepResult:
    node_id: str
    filter: str
    version: str
    output_hash: str
    shape: tuple
    skipped: bool = False


@dataclass
class RunResult:
    data: np.ndarray
    graph_hash: str
    output_hash: str
    source_hash: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def is_stack(self) -> bool:
        return self.data.ndim == 4


SourceLoader = Callable[..., np.ndarray]  # (source, use_cache=True) -> pixels


class _Cache:
    """LRU of intermediate results keyed by the hash of the chain prefix, bounded in bytes."""

    def __init__(self, max_items: int = 256, max_bytes: int | None = None):
        self.max_items = max_items
        self.max_bytes = compute.cache_budget() if max_bytes is None else max_bytes
        self._d: OrderedDict[str, np.ndarray] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value):
        if self.max_items <= 0 or value.nbytes > self.max_bytes // 2:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._bytes -= old.nbytes
            self._d[key] = value
            self._bytes += value.nbytes
            while self._d and (len(self._d) > self.max_items or self._bytes > self.max_bytes):
                _, v = self._d.popitem(last=False)
                self._bytes -= v.nbytes


class Engine:
    def __init__(self, load_source: SourceLoader, cache_items: int = 256):
        self.load_source = load_source
        self.cache = _Cache(cache_items, None if cache_items else 0)

    def run(self, graph: dict, upto: str | None = None, use_cache: bool = True) -> RunResult:
        """Execute the chain. `upto` stops after the node with that id (for previews)."""
        g = normalise(graph)
        prefix = {"schema": g["schema"], "source": g["source"], "nodes": []}
        key = graph_hash(prefix)
        data = self.cache.get(key) if use_cache else None
        if data is None:
            data = self.load_source(g["source"], use_cache=use_cache)
            data.setflags(write=False)
            if use_cache:
                self.cache.put(key, data)
        source_hash = pixel_hash(data)
        steps: list[StepResult] = []
        for node in g["nodes"]:
            f = get_filter(node["filter"])
            prefix["nodes"].append(node)
            key = graph_hash(prefix)
            if not node["enabled"]:
                steps.append(StepResult(node["id"], f.id, f.version, pixel_hash(data), data.shape, skipped=True))
            else:
                cached = self.cache.get(key) if use_cache else None
                if cached is None:
                    cached = self._apply(f, node["params"], data)
                    cached.setflags(write=False)  # filters must never modify their input
                    if use_cache:
                        self.cache.put(key, cached)
                data = cached
                steps.append(StepResult(node["id"], f.id, f.version, pixel_hash(data), data.shape))
            if upto is not None and node["id"] == upto:
                break
        return RunResult(
            data=data,
            graph_hash=graph_hash(g),
            output_hash=pixel_hash(data),
            source_hash=source_hash,
            steps=steps,
        )

    @staticmethod
    def _apply(f, params, data: np.ndarray) -> np.ndarray:
        if f.input_kind == "stack":
            if data.ndim != 4:
                raise FilterError(
                    f"'{f.name}' works on several frames: set a frame range on the source "
                    "(Range start / Range end) covering more than one frame"
                )
            out = f.fn(data, params)
        elif data.ndim == 4:
            out = np.stack(compute.pmap(lambda frame: f.fn(frame, params), data))
        else:
            out = f.fn(data, params)
        out = np.asarray(out, dtype=np.float32)
        if out.ndim not in (3, 4) or out.shape[-1] != 3:
            raise FilterError(f"'{f.name}' produced an invalid array of shape {out.shape}")
        return np.ascontiguousarray(out)


def frame_of(data: np.ndarray, index: int = 0) -> np.ndarray:
    """A single image to show or export from a result that may be a frame stack."""
    if data.ndim == 4:
        return data[min(max(index, 0), data.shape[0] - 1)]
    return data
