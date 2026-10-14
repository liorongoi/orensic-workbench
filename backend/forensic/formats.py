"""Proprietary CCTV / DVR exports: identification, conversion and stream carving.

Recorders often export video in vendor containers that ordinary players cannot
open. This module:

* identifies the container family from signatures in the file (`sniff`),
* converts what FFmpeg can demux into a standard Matroska file by stream copy
  (no re-encoding, so every compressed frame is bit-identical to the original),
* and, as the fallback for unknown containers, carves the raw H.264 / H.265
  elementary stream out of the file by its Annex B start codes and wraps that.

A conversion never touches the original: its output is ingested as a new evidence
item that records the original's hash, the method, the exact command and the
FFmpeg version, so a second examiner can repeat it and get the same bytes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Settings
from .media import MediaError, ffmpeg_version

# (family, label, signature regex over the first bytes, vendor notes)
SIGNATURES: list[tuple[str, str, bytes, str]] = [
    ("dahua_dav", "Dahua DAV", rb"DHAV", "Dahua and OEM recorders; FFmpeg has a native demuxer (dhav)."),
    ("hikvision", "Hikvision (IMKH / MPEG-PS)", rb"^IMKH", "Hikvision and OEM recorders; a 40-byte IMKH header before an MPEG program stream."),
    ("mpeg_ps", "MPEG program stream", rb"^\x00\x00\x01\xba", "Generic program stream; common in DVR exports."),
    ("mpeg_ts", "MPEG transport stream", rb"^\x47.{187}\x47", "Generic transport stream."),
    ("annexb_h264", "Raw H.264 elementary stream", rb"^\x00\x00\x00?\x01[\x67\x27\x47]", "Bare H.264 bitstream (starts with an SPS)."),
    ("annexb_hevc", "Raw H.265 elementary stream", rb"^\x00\x00\x00?\x01\x40\x01", "Bare H.265 bitstream (starts with a VPS)."),
    ("avi", "AVI", rb"^RIFF....AVI ", "Standard AVI; some DVRs use non-standard FourCCs."),
    ("isobmff", "MP4 / MOV", rb"^....ftyp", "Standard ISO base media file."),
    ("asf", "ASF / WMV", rb"^\x30\x26\xb2\x75\x8e\x66\xcf\x11", "Windows Media container."),
    ("matroska", "Matroska / WebM", rb"^\x1a\x45\xdf\xa3", "Matroska container."),
]

# Byte patterns of container headers that can sit between NAL units in a vendor stream.
# Carving cuts a NAL unit where one of these begins, so container bytes are not fed to the decoder.
CONTAINER_MARKERS = [b"DHAV", b"dhav", b"IMKH", b"HXVS", b"HXVF", b"HXAF"]


def sniff(head: bytes) -> dict:
    """Identify the container family from the first part of the file (64 KB is enough)."""
    hits = []
    for family, label, pattern, notes in SIGNATURES:
        if pattern.startswith(b"^"):
            if re.match(pattern[1:], head, re.S):
                hits.append({"family": family, "label": label, "notes": notes, "at": 0})
        else:
            m = re.search(pattern, head[:65536], re.S)
            if m:
                hits.append({"family": family, "label": label, "notes": notes, "at": m.start()})
    codec = guess_codec(head)
    return {"candidates": hits, "family": hits[0]["family"] if hits else None,
            "label": hits[0]["label"] if hits else None, "elementary_codec": codec}


def _nal_units(data: bytes):
    """Yield (offset_of_payload, start_code_length) for every Annex B start code."""
    for m in re.finditer(rb"\x00\x00\x01", data):
        s = m.start()
        sc = 4 if s > 0 and data[s - 1] == 0 else 3
        yield s + 3, sc


def guess_codec(data: bytes) -> str | None:
    """Which elementary stream the start codes look like: 'h264', 'hevc' or None."""
    h264 = {7: 0, 8: 0, 5: 0, 1: 0}
    hevc = {32: 0, 33: 0, 34: 0, 19: 0, 1: 0}
    for off, _ in _nal_units(data[: 4 << 20]):
        if off >= len(data):
            break
        b = data[off]
        if not b & 0x80:
            t = b & 0x1F
            if t in h264:
                h264[t] += 1
            if off + 1 < len(data) and data[off + 1] == 0x01:
                t2 = (b >> 1) & 0x3F
                if t2 in hevc:
                    hevc[t2] += 1
    if hevc[32] and hevc[33] and hevc[34]:
        return "hevc"
    if h264[7] and h264[8] and (h264[5] or h264[1]):
        return "h264"
    return None


def _valid_nal(codec: str, data: bytes, off: int) -> bool:
    if off + 1 >= len(data):
        return False
    b = data[off]
    if b & 0x80:  # forbidden_zero_bit
        return False
    if codec == "h264":
        return 1 <= (b & 0x1F) <= 23
    t = (b >> 1) & 0x3F
    return t <= 40 and data[off + 1] != 0 and (data[off + 1] & 0x07) != 0  # temporal id + 1 must be non-zero


def carve(data: bytes, codec: str) -> tuple[bytes, dict]:
    """Extract an Annex B elementary stream of `codec` from arbitrary container bytes.

    Keeps every syntactically valid NAL unit, cut at the next start code or the first
    known container marker, whichever comes first. Returns the stream and statistics.
    """
    starts = list(_nal_units(data))
    out = bytearray()
    kept = dropped = trimmed = 0
    marker_re = re.compile(b"|".join(re.escape(m) for m in CONTAINER_MARKERS))
    for i, (off, sc) in enumerate(starts):
        end = starts[i + 1][0] - starts[i + 1][1] if i + 1 < len(starts) else len(data)
        if not _valid_nal(codec, data, off):
            dropped += 1
            continue
        chunk = data[off:end]
        m = marker_re.search(chunk)
        if m:
            chunk = chunk[: m.start()]
            trimmed += 1
        # Trailing zero bytes belong to the next start code or to padding.
        chunk = chunk.rstrip(b"\x00")
        if len(chunk) < 2:
            dropped += 1
            continue
        out += b"\x00\x00\x00\x01" + chunk
        kept += 1
    return bytes(out), {"nal_units_kept": kept, "nal_units_dropped": dropped, "nal_units_trimmed": trimmed}


def _ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise MediaError(proc.stderr.decode(errors="replace")[-2000:])


METHODS = {
    "remux": "Stream copy into MPEG transport stream with FFmpeg (no re-encoding; the recorder's own timestamps "
    "are kept). For containers FFmpeg can demux, including Dahua DAV and most MPEG program/transport streams.",
    "carve": "Carve the raw H.264/H.265 elementary stream by its start codes and drop container bytes. For "
    "unknown containers. A carved stream carries no timestamps: frame times are computed from the frame rate given.",
}

EXTENSIONS = {"remux": "ts", "h264": "h264", "hevc": "hevc"}


def output_extension(method: str, codec: str | None) -> str:
    return "ts" if method == "remux" else EXTENSIONS.get(codec or "", "h264")


def convert(src: Path, dest: Path, method: str, settings: Settings, frame_rate: float = 25.0,
            codec: str | None = None) -> dict:
    """Convert `src` into a standard file at `dest`. Returns a provenance record."""
    if method not in METHODS:
        raise ValueError(f"unknown conversion method '{method}'")
    record: dict = {"method": method, "description": METHODS[method], "ffmpeg": ffmpeg_version(settings.ffmpeg)}
    head = src.read_bytes()[: 1 << 16]
    record["identified"] = sniff(head)
    if method == "remux":
        fmt_args = []
        if record["identified"]["family"] == "hikvision":
            # The IMKH header is vendor metadata; FFmpeg reads the program stream after it.
            fmt_args = ["-f", "mpeg", "-skip_initial_bytes", "40"]
        cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-y", *fmt_args, "-i", str(src),
               "-map", "0:v:0", "-c", "copy", "-fflags", "+bitexact", "-map_metadata", "-1", "-f", "mpegts", str(dest)]
        _ffmpeg(cmd)
        record["command"] = _redact(cmd, src, dest)
        return record
    data = src.read_bytes()
    codec = codec or guess_codec(data)
    if codec not in ("h264", "hevc"):
        raise MediaError("no H.264 or H.265 stream found to carve; this container needs a dedicated parser")
    if not (0 < frame_rate <= 240):
        raise ValueError("frame rate must be between 0 and 240")
    stream, stats = carve(data, codec)
    if not stream:
        raise MediaError("carving found no usable NAL units")
    dest.write_bytes(stream)
    record.update(codec=codec, frame_rate=frame_rate, carve=stats, container_markers=[m.decode() for m in CONTAINER_MARKERS],
                  timestamps="synthetic: frame n is at n / frame_rate seconds")
    return record


def _redact(cmd: list[str], src: Path, dest: Path) -> str:
    """The command with local paths replaced by placeholders, for the report."""
    return " ".join("<input>" if c == str(src) else "<output>" if c == str(dest) else c for c in cmd)
