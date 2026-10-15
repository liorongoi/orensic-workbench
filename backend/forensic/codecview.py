"""Macroblock, quantiser and motion-vector views of compressed video.

What the decoder saw in each 16x16 macroblock of a frame: how it was coded (intra, predicted
from an earlier or a later frame, from both, direct, or skipped), its quantiser (QP: lower means
more detail kept) and its motion vectors. Examiners use it to pick the frames worth enhancing or
averaging (intra-coded, low QP), to see which regions the encoder actually refreshed and which
were copied from another frame, and to spot re-encoding.

Everything is read from FFmpeg's own decoder (``-debug mb_type+qp`` and ``+export_mvs``), so it
is available for the codecs FFmpeg exposes it for: H.264, MPEG-1/2, MPEG-4 Part 2 and H.263.
Frames are numbered as everywhere else: by position in the decoded (display-order) stream.
"""

from __future__ import annotations

import re
import subprocess
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .config import Settings
from .media import MediaError

CODECS = ("h264", "mpeg2video", "mpeg1video", "mpeg4", "h263", "h263p", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3")
MB = 16

# Block classes, in legend order: id, label, RGB colour, overlay opacity.
# Colours follow the frame strip: red intra (like I-frames), blue forward (P), green backward (B).
KINDS = [
    ("intra", "Intra", (229, 72, 77), 0.5),
    ("fwd", "Forward", (62, 155, 255), 0.45),
    ("bwd", "Backward", (70, 167, 88), 0.45),
    ("bi", "Bi", (171, 92, 210), 0.45),
    ("direct", "Direct", (255, 197, 61), 0.45),
    ("skip", "Skip", (128, 128, 128), 0.3),
    ("other", "Other", (255, 255, 255), 0.2),
]
KIND_INDEX = {k[0]: i for i, k in enumerate(KINDS)}
# FFmpeg's mb_type symbols (libavcodec/mpegutils.c): P PCM, A AC-predicted intra, i intra 4x4,
# I intra 16x16, > list 0 only, < list 1 only, X both lists, D direct, d direct skip, S skip,
# G global motion compensation, g GMC skip.
SYMBOL = {
    "P": "intra", "A": "intra", "i": "intra", "I": "intra",
    ">": "fwd", "G": "fwd", "<": "bwd", "X": "bi", "D": "direct",
    "S": "skip", "d": "skip", "g": "skip",
}
CODE = {s: KIND_INDEX[k] for s, k in SYMBOL.items()}
OTHER = KIND_INDEX["other"]
CELL = re.compile(r"(\d+)([^\s\d])")
# Forward and backward motion-vector arrow colours.
MV_FWD = (255, 236, 120)
MV_BWD = (90, 230, 255)


def supported(codec: str | None) -> bool:
    return (codec or "") in CODECS


def qp_range(codec: str | None) -> tuple[int, int]:
    """The codec's quantiser scale, so heat-map colours mean the same QP on every frame."""
    return (0, 51) if codec == "h264" else (1, 31)


def blocks(path: Path, settings: Settings, start: int, count: int, width: int, height: int) -> dict[int, dict]:
    """Macroblock classes and QP for frames start..start+count-1, keyed by frame number.

    Each value holds ``type`` (picture type), ``kinds`` (uint8 grid of KINDS indices) and ``qp``
    (uint8 grid). The decoder prints every frame from the first, so earlier frames are only counted.
    """
    mbw = (width + MB - 1) // MB
    cmd = [
        settings.ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-threads", "1", "-debug", "mb_type+qp",
        "-i", str(path), "-map", "0:v:0", "-frames:v", str(start + count), "-f", "null", "-",
    ]
    out: dict[int, dict] = {}
    n = -1
    cur: dict | None = None
    rows: list[tuple[list[int], list[int]]] = []

    def close():
        if cur is not None and rows:
            cur["kinds"] = np.array([r[0] for r in rows], np.uint8)
            cur["qp"] = np.array([r[1] for r in rows], np.uint8)
            out[n] = cur

    tail: deque[str] = deque(maxlen=8)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for raw in proc.stderr:
            line = raw.decode("latin-1")
            m = re.search(r"New frame, type: (\w)", line)
            if m:
                close()
                n += 1
                cur = {"type": m.group(1)} if n >= start else None
                rows = []
                continue
            if cur is None or not line.startswith("[") or "] " not in line:
                tail.append(line.strip())
                continue
            cells = CELL.findall(line.split("] ", 1)[1])
            # A row has exactly one cell per macroblock column; anything else is other log output.
            if len(cells) != mbw:
                continue
            rows.append(([CODE.get(t, OTHER) for _, t in cells], [min(255, int(q)) for q, _ in cells]))
        close()
    finally:
        proc.stderr.close()
        code = proc.wait()
    if not out and code != 0:
        raise MediaError(" ".join(tail)[-500:] or "decoding failed")
    return out


def motion_vectors(path: Path, settings: Settings, n: int, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """FFmpeg's motion-vector arrows for frame n, drawn on black: (forward, backward) as float masks in [0, 1]."""
    graph = (
        f"select=eq(n\\,{n}),lutyuv=y=0:u=128:v=128,split[a][b];"
        "[a]codecview=mv=pf+bf[f];[b]codecview=mv=bb[k];[f][k]hstack"
    )
    cmd = [
        settings.ffmpeg, "-v", "error", "-nostdin", "-flags2", "+export_mvs", "-i", str(path), "-map", "0:v:0",
        "-vf", graph, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise MediaError(proc.stderr.decode(errors="replace")[-500:])
    buf = np.frombuffer(proc.stdout, np.uint8)
    if buf.size < width * 2 * height:
        raise MediaError(f"no motion vectors decoded for frame {n}")
    a = buf[: width * 2 * height].reshape(height, width * 2).astype(np.float32)
    # Arrows are drawn at luma 100 with anti-aliased edges; the background is black.
    m = np.clip(a / 100.0, 0.0, 1.0)
    return m[:, :width], m[:, width:]


def summary(b: dict, codec: str | None) -> dict:
    """Counts per block class and QP statistics for one frame, plus the grids for the cursor readout."""
    kinds = b["kinds"]
    qp = b["qp"].astype(np.float64)
    total = int(kinds.size)
    counts = np.bincount(kinds.ravel(), minlength=len(KINDS))
    return {
        "type": b["type"],
        "mb_size": MB,
        "cols": int(kinds.shape[1]),
        "rows": int(kinds.shape[0]),
        "counts": {KINDS[i][0]: int(counts[i]) for i in range(len(KINDS))},
        "share": {KINDS[i][0]: round(float(counts[i]) / total, 4) if total else 0.0 for i in range(len(KINDS))},
        "qp": {"min": int(qp.min()), "max": int(qp.max()), "mean": round(float(qp.mean()), 2), "scale": list(qp_range(codec))},
        "kinds": ["".join("ifbBdso"[k] for k in row) for row in kinds.tolist()],
        "qp_grid": b["qp"].tolist(),
    }


def legend() -> list[dict]:
    return [{"id": k, "label": label, "color": "#%02x%02x%02x" % c} for k, label, c, _ in KINDS]


def _upscale(grid: np.ndarray, width: int, height: int) -> np.ndarray:
    """One value per macroblock -> one per pixel (nearest), cropped to the frame."""
    rows, cols = grid.shape[:2]
    big = np.repeat(np.repeat(grid, MB, axis=0), MB, axis=1)
    if rows * MB < height or cols * MB < width:
        # Field-coded pictures report half the macroblock rows: stretch them over the frame.
        big = cv2.resize(grid, (width, height), interpolation=cv2.INTER_NEAREST)
    return big[:height, :width]


def render(frame: np.ndarray, b: dict | None, codec: str | None, layer: str = "type", grid: bool = False,
           mv: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """Draw the chosen layers over an 8-bit RGB frame and return the 8-bit RGB result."""
    h, w = frame.shape[:2]
    out = frame.astype(np.float32)
    if b is not None and layer == "type":
        colours = np.array([k[2] for k in KINDS], np.float32)
        alpha = np.array([k[3] for k in KINDS], np.float32)
        kinds = _upscale(b["kinds"], w, h)
        a = alpha[kinds][..., None]
        out = out * (1 - a) + colours[kinds] * a
    elif b is not None and layer == "qp":
        lo, hi = qp_range(codec)
        q = np.clip((b["qp"].astype(np.float32) - lo) / max(1, hi - lo), 0, 1)
        heat = cv2.applyColorMap((q * 255).astype(np.uint8), cv2.COLORMAP_TURBO)[..., ::-1]
        out = out * 0.45 + _upscale(heat, w, h).astype(np.float32) * 0.55
    if grid:
        out[:, ::MB] = out[:, ::MB] * 0.4
        out[::MB, :] = out[::MB, :] * 0.4
    if mv is not None:
        for m, c in zip(mv, (MV_FWD, MV_BWD)):
            a = m[..., None]
            out = out * (1 - a) + np.array(c, np.float32) * a
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)
