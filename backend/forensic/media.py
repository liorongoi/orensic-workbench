"""Probing and decoding of evidence files.

Images are decoded exactly as stored: no EXIF auto-rotation and no colour
management, so pixel values match the file. Video goes through FFmpeg with
frame-accurate selection by decoded frame index.
"""

from __future__ import annotations

import io
import json
import subprocess
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .config import Settings

IMAGE_FORMATS = {"JPEG", "PNG", "TIFF", "BMP", "GIF", "WEBP", "MPO"}


# FFmpeg demuxers that read media from other files or URLs named inside the input.
REFERENCE_FORMATS = frozenset({"hls", "applehttp", "concat", "dash"})


class MediaError(ValueError):
    pass


def _run(cmd: list[str], *, input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True, check=False)
    if proc.returncode != 0:
        raise MediaError(proc.stderr.decode(errors="replace")[-2000:])
    return proc.stdout


@lru_cache(maxsize=1)
def ffmpeg_version(ffmpeg: str = "ffmpeg") -> str:
    try:
        return _run([ffmpeg, "-version"]).decode().splitlines()[0]
    except (MediaError, FileNotFoundError):
        return "unavailable"


def probe(path: Path, settings: Settings) -> dict:
    """Identify the file and return a JSON-serialisable description."""
    try:
        with Image.open(path) as im:
            fmt = im.format
            if fmt in {"JPEG", "MPO"} or (fmt in IMAGE_FORMATS and getattr(im, "n_frames", 1) == 1):
                return {
                    "media_type": "image",
                    "format": fmt,
                    "width": im.width,
                    "height": im.height,
                    "mode": im.mode,
                    "frame_count": 1,
                }
    except Exception:
        pass

    try:
        out = _run(
            [settings.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
        )
    except (MediaError, FileNotFoundError) as e:
        raise MediaError(f"unsupported or unreadable file: {e}") from e
    info = json.loads(out)
    fmt = set((info.get("format", {}).get("format_name") or "").split(","))
    if fmt & REFERENCE_FORMATS:
        # A playlist or concat list makes FFmpeg read other files, so the decoded pixels would not
        # come from the hashed original. Evidence must be self-contained.
        raise MediaError("playlists and other formats that reference outside files are not accepted as evidence")
    vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not vstreams:
        raise MediaError("no video stream found; audio-only evidence is not supported yet")
    v = vstreams[0]
    frame_count = _count_frames(path, settings) if not v.get("nb_frames") else int(v["nb_frames"])
    return {
        "media_type": "video",
        "format": info.get("format", {}).get("format_name"),
        "codec": v.get("codec_name"),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "frame_rate": v.get("avg_frame_rate") or v.get("r_frame_rate"),
        "duration": float(info.get("format", {}).get("duration") or 0.0),
        "frame_count": frame_count,
        "pix_fmt": v.get("pix_fmt"),
        "field_order": v.get("field_order"),
        "streams": info.get("streams", []),
        "container": info.get("format", {}),
    }


def _count_frames(path: Path, settings: Settings) -> int:
    out = _run(
        [
            settings.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "json", str(path),
        ]
    )
    return int(json.loads(out)["streams"][0]["nb_read_frames"])


def frame_table(path: Path, settings: Settings) -> list[dict]:
    """Per-frame type (I/P/B), key flag, timestamp and packet size from the bitstream."""
    out = _run(
        [
            settings.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
            "-show_entries", "frame=pict_type,key_frame,best_effort_timestamp_time,pkt_size,interlaced_frame",
            "-of", "json", str(path),
        ]
    )
    frames = json.loads(out).get("frames", [])
    return [
        {
            "n": i,
            "type": f.get("pict_type", "?"),
            "key": bool(int(f.get("key_frame", 0))),
            "time": float(f["best_effort_timestamp_time"]) if f.get("best_effort_timestamp_time") not in (None, "N/A") else None,
            "size": int(f["pkt_size"]) if f.get("pkt_size") else None,
            "interlaced": bool(int(f.get("interlaced_frame", 0))),
        }
        for i, f in enumerate(frames)
    ]


def to_float(img: np.ndarray) -> np.ndarray:
    """Any decoded array -> float32 RGB in [0, 1], shape (H, W, 3)."""
    if img.dtype == np.uint8:
        f = img.astype(np.float32) / 255.0
    elif img.dtype == np.uint16:
        f = img.astype(np.float32) / 65535.0
    else:
        f = img.astype(np.float32)
    if f.ndim == 2:
        f = np.repeat(f[:, :, None], 3, axis=2)
    elif f.shape[2] == 4:
        f = f[:, :, :3]
    elif f.shape[2] == 1:
        f = np.repeat(f, 3, axis=2)
    return np.ascontiguousarray(f)


def decode_image(path: Path) -> np.ndarray:
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        # Formats OpenCV cannot read (some GIF/WebP variants) fall back to Pillow.
        with Image.open(path) as im:
            img = np.asarray(im.convert("RGB"))
        return to_float(img)
    if img.ndim == 3 and img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    return to_float(img)


def decode_frames(path: Path, settings: Settings, start: int, end: int, width: int, height: int) -> np.ndarray:
    """Decode frames start..end inclusive (0-based decode order) as float32 (N, H, W, 3)."""
    if end < start:
        raise MediaError("frame range end is before start")
    count = end - start + 1
    raw = _run(
        [
            settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
            "-vf", f"select=between(n\\,{start}\\,{end})", "-fps_mode", "passthrough",
            "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
    )
    frame_bytes = width * height * 3
    n = len(raw) // frame_bytes
    if n == 0:
        raise MediaError(f"no frames decoded in range {start}-{end}")
    arr = np.frombuffer(raw[: n * frame_bytes], dtype=np.uint8).reshape(n, height, width, 3)
    return arr.astype(np.float32) / 255.0


def to_uint8(img: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(img * 255.0), 0, 255).astype(np.uint8)


def encode_png(img: np.ndarray) -> bytes:
    """Float RGB -> PNG bytes. Fixed compression level so output bytes are reproducible."""
    u8 = to_uint8(img)
    if u8.ndim == 3:
        u8 = cv2.cvtColor(u8, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".png", u8, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise MediaError("PNG encoding failed")
    return buf.tobytes()


def encode_jpeg_preview(img: np.ndarray, max_side: int | None = None, quality: int = 90) -> bytes:
    u8 = to_uint8(img)
    pil = Image.fromarray(u8)
    if max_side and max(pil.size) > max_side:
        pil.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
