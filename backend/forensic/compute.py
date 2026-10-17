"""Compute resources: CPU threads, the memory budget for caches, and the GPU.

Everything that runs in parallel here splits work into independent pieces (rows of
an image, colour channels, frames) whose results do not depend on which thread
computed them or in what order, so a chain gives the same pixel hash with one
thread or with sixty-four. tests/test_compute.py checks that for every filter.

Settings (environment variables):

    FORENSIC_THREADS     CPU threads for filters and models (default: all cores)
    FORENSIC_CACHE_MB    RAM for cached intermediate results (default: a quarter of RAM, at most 32 GB)
    FORENSIC_FRAME_CACHE_MB  RAM for decoded video frames (default: an eighth of RAM, at most 16 GB)
    FORENSIC_AI_DEVICE   auto | gpu | cpu for learned models (default auto: the GPU when one is usable)
    FORENSIC_GPU_MEMORY_MB   graphics memory to report when the NVIDIA driver cannot be asked
    FORENSIC_RANGE_MB    RAM one decoded video frame range may take as 32-bit frames
                         (default: a sixth of RAM, between 2 and 24 GB)

The GPU only runs learned models (through ONNX Runtime with DirectML on Windows), tile by
tile on a grid fixed by the image size, so the card's memory never changes a result. The
classical filters stay on the CPU: they give the same bits on every machine, which GPU
kernels do not promise.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

import numpy as np

T = TypeVar("T")
R = TypeVar("R")

GB = 1 << 30
MB = 1 << 20


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def physical_memory() -> int:
    """Installed RAM in bytes (8 GB if it cannot be read)."""
    try:
        if sys.platform == "win32":
            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
        else:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):
        pass
    return 8 * GB


def cpu_threads() -> int:
    return _env_int("FORENSIC_THREADS", os.cpu_count() or 1)


def cache_budget() -> int:
    """Bytes of intermediate results the filter engine keeps in RAM."""
    return _env_int("FORENSIC_CACHE_MB", 0) * MB or max(GB, min(physical_memory() // 4, 32 * GB))


def frame_cache_budget() -> int:
    """Bytes of decoded video frames kept in RAM, so scrubbing and re-runs do not decode again."""
    return _env_int("FORENSIC_FRAME_CACHE_MB", 0) * MB or max(512 * MB, min(physical_memory() // 8, 16 * GB))


def range_budget() -> int:
    """Bytes one decoded frame range may take as float32 frames, so a long range on a 4K video
    is refused with a clear message instead of pushing the machine into swap."""
    return _env_int("FORENSIC_RANGE_MB", 0) * MB or max(2 * GB, min(physical_memory() // 6, 24 * GB))


_gpu_info: dict | None = None
_gpu_lock = threading.Lock()


def _nvidia_smi() -> str | None:
    import shutil

    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        exe = p if os.path.isfile(p) else None
    return exe


def gpu_info() -> dict:
    """The graphics card the models can use: {"name", "memory_mb", "source"}.

    Read once from the NVIDIA driver's nvidia-smi (installed with every NVIDIA driver, no
    network); FORENSIC_GPU_MEMORY_MB overrides the memory figure. Name is "" when unknown.
    """
    global _gpu_info
    with _gpu_lock:
        if _gpu_info is None:
            info = {"name": "", "memory_mb": 0, "source": "unknown"}
            exe = _nvidia_smi()
            if exe:
                import subprocess

                try:
                    out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                         capture_output=True, text=True, timeout=10,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    line = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
                    name, _, mem = line.rpartition(",")
                    if name:
                        info = {"name": name.strip(), "memory_mb": int(float(mem)), "source": "nvidia-smi"}
                except (OSError, ValueError, subprocess.SubprocessError, IndexError):
                    pass
            _gpu_info = info
        info = dict(_gpu_info)
    override = _env_int("FORENSIC_GPU_MEMORY_MB", 0)
    if override:
        info.update(memory_mb=override, source="FORENSIC_GPU_MEMORY_MB")
    return info


def gpu_jobs() -> int:
    return _env_int("FORENSIC_GPU_JOBS", 1)


def ai_device() -> str:
    v = os.environ.get("FORENSIC_AI_DEVICE", "auto").strip().lower()
    return v if v in ("auto", "gpu", "cpu") else "auto"


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_local = threading.local()


def _worker_init():
    _local.in_pool = True


def pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(cpu_threads(), thread_name_prefix="forensic-cpu", initializer=_worker_init)
        return _pool


def pmap(fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
    """`[fn(x) for x in items]`, run on the shared CPU pool, results in input order.

    Each call must be independent of the others. Called from inside a pool worker it
    runs serially, so nested use cannot exhaust the pool and deadlock.
    """
    items = list(items)
    if len(items) <= 1 or cpu_threads() == 1 or getattr(_local, "in_pool", False):
        return [fn(x) for x in items]
    return list(pool().map(fn, items))


def row_blocks(height: int, min_rows: int = 16) -> list[tuple[int, int]]:
    """Split image rows into about four blocks per thread (fewer for small images)."""
    n = max(1, min(cpu_threads() * 4, height // min_rows))
    edges = [round(i * height / n) for i in range(n + 1)]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def configure_opencv() -> None:
    import cv2

    cv2.setNumThreads(cpu_threads())
    # OpenCL kernels can differ from the CPU path in the last bit and between drivers.
    cv2.ocl.setUseOpenCL(False)


def describe() -> str:
    """One line for the report's software environment section."""
    from . import models

    g = gpu_info()
    card = f" ({g['name']}, {g['memory_mb']} MB)" if g["name"] else ""
    return (f"{cpu_threads()} CPU threads; caches {cache_budget() // MB} MB results, "
            f"{frame_cache_budget() // MB} MB video frames of {physical_memory() // MB} MB RAM; "
            f"learned models on {models.device_label()}{card}")


def summary() -> dict:
    """What this computer gives Lucida, for the desktop app's System dialog."""
    from . import models

    g = gpu_info()
    return {
        "cpu_threads": cpu_threads(),
        "ram_mb": physical_memory() // MB,
        "result_cache_mb": cache_budget() // MB,
        "frame_cache_mb": frame_cache_budget() // MB,
        "range_mb": range_budget() // MB,
        "gpu": {"name": g["name"], "memory_mb": g["memory_mb"], "source": g["source"],
                "provider": models.gpu_provider(), "jobs": gpu_jobs()},
        "models_on": models.device_label(),
    }


def by_rows(fn: Callable, frames, out_dtype) -> np.ndarray:
    """Apply `fn` to row blocks of a (N, H, W, ...) stack, or a list of N equal-shape frames,
    and assemble the (H, W, ...) result. A list is never stacked whole: each block is."""
    first = frames[0]
    out = np.empty(first.shape, out_dtype)
    is_list = isinstance(frames, (list, tuple))

    def job(block):
        a, b = block
        out[a:b] = fn(np.stack([f[a:b] for f in frames]) if is_list else frames[:, a:b])

    pmap(job, row_blocks(first.shape[0]))
    return out


def median0(frames) -> np.ndarray:
    """`np.median(np.stack(frames), axis=0)` for float32 frames, bit for bit, on all cores."""
    n = len(frames)

    def med(chunk):
        s = np.sort(chunk, axis=0)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / s.dtype.type(2)

    return by_rows(med, frames, frames[0].dtype)


def mean0(frames, dtype=None) -> np.ndarray:
    """`np.stack(frames).mean(axis=0, dtype=dtype)`, bit for bit, on all cores."""
    res_dtype = frames[0][:1][None].mean(axis=0, dtype=dtype).dtype
    return by_rows(lambda c: c.mean(axis=0, dtype=dtype), frames, res_dtype)
