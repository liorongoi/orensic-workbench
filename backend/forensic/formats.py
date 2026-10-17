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
from . import dvr
from .media import MediaError, ffmpeg_version

# (family, label, signature regex over the first bytes, vendor notes)
SIGNATURES: list[tuple[str, str, bytes, str]] = [
    ("dahua_dav", "Dahua DAV", rb"DHAV", "Dahua and OEM recorders (Amcrest, Lorex, IC Realtime and others). Native parser "
     "reads the recorder's per-frame timing and date; FFmpeg also has a demuxer (dhav)."),
    ("hikvision", "Hikvision (IMKH / MPEG-PS)", rb"^IMKH", "Hikvision and OEM recorders (Annke, LTS, Swann and others): "
     "a 40-byte IMKH header before an MPEG program stream. Native parser reads the PES timestamps."),
    ("hxvs", "HiSilicon HXVS / HXVT", rb"^HXV[ST]", "IP cameras on HiSilicon chips (many unbranded and budget cameras): "
     ".264 / .265 recordings on the SD card. Native parser reads the per-frame timestamps."),
    ("xmeye", "Xiongmai / XMEye", rb"\x00\x00\x01[\xfc\xfe][\x01-\x03\x00][\x00-\x3c][\x01-\xff][\x01-\xff]",
     "Xiongmai (XMEye, NetSurveillance, Sofia) recorders and their many OEM brands: .h264 / .h265 exports with "
     "vendor frame headers. Native parser reads the frame rate and recorder date from each key frame."),
    ("hikvision_old", "Hikvision (old 4HKH header)", rb"^4HKH", "Older Hikvision exports with a 4HKH file header. No "
     "documented layout: use carve."),
    ("mpeg_ps", "MPEG program stream", rb"^\x00\x00\x01\xba", "Generic program stream; common in DVR exports "
     "(exacqVision .ps, many recorders' .mpg / .vob)."),
    ("mpeg_ts", "MPEG transport stream", rb"^(?:\x47.{187}\x47|....\x47.{191}\x47)", "Transport stream (also M2TS). Native parser "
     "finds video under vendor or private stream types and reports lost packets."),
    ("annexb_h264", "Raw H.264 elementary stream", rb"^\x00\x00\x00?\x01[\x67\x27\x47]", "Bare H.264 bitstream (starts with an SPS)."),
    ("annexb_hevc", "Raw H.265 elementary stream", rb"^\x00\x00\x00?\x01\x40\x01", "Bare H.265 bitstream (starts with a VPS)."),
    ("annexb_mpeg4", "Raw MPEG-4 Part 2 elementary stream", rb"^\x00\x00\x01[\xb0\xb5\x20-\x2f]",
     "Bare MPEG-4 ASP bitstream, used by many older DVRs."),
    ("annexb_mpeg2", "Raw MPEG-2 video elementary stream", rb"^\x00\x00\x01\xb3", "Bare MPEG-1/2 video bitstream."),
    ("pcap", "Network capture (pcap / pcapng)", rb"^(?:\xd4\xc3\xb2\xa1|\xa1\xb2\xc3\xd4|\x4d\x3c\xb2\xa1|\xa1\xb2\x3c\x4d|\x0a\x0d\x0d\x0a)",
     "Wireshark / tcpdump capture of a camera's or recorder's RTSP stream. Native parser rebuilds the H.264, H.265 "
     "or MPEG-PS video from RTP over UDP or RTSP-interleaved TCP, with RTP timing and the capture clock."),
    ("avi", "AVI", rb"^RIFF....AVI ", "AVI; many DVRs (Geovision, Hikvision, Mace and others) use non-standard FourCCs "
     "that players reject. Native parser reads the video chunks whatever the FourCC."),
    ("isobmff", "MP4 / MOV", rb"^....ftyp", "Standard ISO base media file."),
    ("asf", "ASF / WMV", rb"^\x30\x26\xb2\x75\x8e\x66\xcf\x11", "Windows Media container."),
    ("matroska", "Matroska / WebM", rb"^\x1a\x45\xdf\xa3", "Matroska container."),
]

SEARCHED = {f for f, _, pattern, _ in SIGNATURES if not pattern.startswith(b"^")}

# Byte patterns of container headers that can sit between NAL units in a vendor stream.
# Carving cuts a NAL unit where one of these begins, so container bytes are not fed to the decoder.
CONTAINER_MARKERS = [b"DHAV", b"dhav", b"IMKH", b"4HKH", b"HXVS", b"HXVT", b"HXVF", b"HXAF", b"HXFI"]


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
    # A signature that matches at the very start of the file outranks one found further in.
    hits.sort(key=lambda h: (h["at"] != 0 or h["family"] in SEARCHED))
    family = hits[0]["family"] if hits else None
    return {"candidates": hits, "family": family, "label": hits[0]["label"] if hits else None,
            "elementary_codec": codec, "parser": family in dvr.PARSERS}


def _nal_units(data: bytes):
    """Yield (offset_of_payload, start_code_length) for every Annex B start code."""
    for m in re.finditer(rb"\x00\x00\x01", data):
        s = m.start()
        sc = 4 if s > 0 and data[s - 1] == 0 else 3
        yield s + 3, sc


def guess_codec(data: bytes) -> str | None:
    """Which elementary stream the bytes carry: 'h264', 'hevc', 'mpeg4', 'mpeg2video', 'mjpeg' or None."""
    h264 = {7: 0, 8: 0, 5: 0, 1: 0}
    hevc = {32: 0, 33: 0, 34: 0, 19: 0, 1: 0}
    mpeg = {"vol": 0, "vop": 0, "seq": 0, "pic": 0}
    for off, _ in _nal_units(data[: 4 << 20]):
        if off >= len(data):
            break
        b = data[off]
        if 0x20 <= b <= 0x2F:
            mpeg["vol"] += 1
        elif b == 0xB6:
            mpeg["vop"] += 1
        elif b == 0xB3:
            mpeg["seq"] += 1
        elif b == 0x00:
            mpeg["pic"] += 1
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
    # MPEG-4 VOPs (B6) and MPEG-1/2 sequence headers (B3) have the forbidden bit set, so they never
    # start an H.264 NAL unit; MPEG-2 slice start codes do look like H.264 NAL types, so check these first.
    if mpeg["vol"] and mpeg["vop"] >= 2:
        return "mpeg4"
    if mpeg["seq"] and mpeg["pic"] >= 2:
        return "mpeg2video"
    if h264[7] and h264[8] and (h264[5] or h264[1]):
        return "h264"
    if len(_jpeg_frames(data[: 4 << 20], limit=3)) >= 3:
        return "mjpeg"
    return None


# Valid start code values per codec for carving (the byte after 00 00 01).
MPEG_START_CODES = {
    "mpeg4": set(range(0x00, 0x30)) | {0xB0, 0xB1, 0xB2, 0xB3, 0xB5, 0xB6},
    "mpeg2video": set(range(0x00, 0xB0)) | {0xB2, 0xB3, 0xB5, 0xB7, 0xB8},
}


def _jpeg_frames(data: bytes, limit: int | None = None) -> list[tuple[int, int]]:
    """(start, end) of every complete baseline/progressive JPEG found by walking its markers."""
    found = []
    pos = 0
    while limit is None or len(found) < limit:
        s = data.find(b"\xff\xd8\xff", pos)
        if s < 0:
            break
        p = s + 2
        end = None
        while p + 4 <= len(data):
            if data[p] != 0xFF:
                break
            marker = data[p + 1]
            if marker == 0xFF:
                p += 1
                continue
            seglen = int.from_bytes(data[p + 2:p + 4], "big")
            if seglen < 2:
                break
            p += 2 + seglen
            if marker == 0xDA:  # start of scan: entropy-coded data until a marker other than RSTn/stuffing
                while True:
                    q = data.find(b"\xff", p)
                    if q < 0 or q + 1 >= len(data):
                        p = len(data)
                        break
                    nxt = data[q + 1]
                    if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                        p = q + (1 if nxt == 0xFF else 2)
                        continue
                    p = q
                    break
                if data[p:p + 2] == b"\xff\xd9":
                    end = p + 2
                    break
        if end:
            found.append((s, end))
            pos = end
        else:
            pos = s + 3
    return found


def _valid_nal(codec: str, data: bytes, off: int) -> bool:
    if off + 1 >= len(data):
        return False
    b = data[off]
    if codec in MPEG_START_CODES:
        return b in MPEG_START_CODES[codec]
    if b & 0x80:  # forbidden_zero_bit
        return False
    if codec == "h264":
        return 1 <= (b & 0x1F) <= 23
    t = (b >> 1) & 0x3F
    return t <= 40 and data[off + 1] != 0 and (data[off + 1] & 0x07) != 0  # temporal id + 1 must be non-zero


def carve(data: bytes, codec: str) -> tuple[bytes, dict]:
    """Extract an elementary stream of `codec` from arbitrary container bytes.

    H.264/H.265 and MPEG-4/MPEG-2: keeps every syntactically valid start-code unit, cut at the
    next start code or the first known container marker, whichever comes first. Motion JPEG:
    keeps every complete JPEG image. Returns the stream and statistics.
    """
    if codec == "mjpeg":
        frames = _jpeg_frames(data)
        return b"".join(data[a:b] for a, b in frames), {"jpeg_frames_kept": len(frames)}
    starts = list(_nal_units(data))
    out = bytearray()
    kept = dropped = trimmed = 0
    marker_re = re.compile(b"|".join(re.escape(m) for m in CONTAINER_MARKERS))
    prefix = b"\x00\x00\x01" if codec in MPEG_START_CODES else b"\x00\x00\x00\x01"
    for i, (off, sc) in enumerate(starts):
        mpeg = codec in MPEG_START_CODES
        # MPEG-1/2/4 syntax can end in zero bytes, so only the three start code bytes are cut there;
        # in H.264/H.265 a trailing zero always belongs to the next start code or to padding.
        end = (starts[i + 1][0] - (3 if mpeg else starts[i + 1][1])) if i + 1 < len(starts) else len(data)
        if not _valid_nal(codec, data, off):
            dropped += 1
            continue
        chunk = data[off:end]
        m = marker_re.search(chunk)
        if m:
            chunk = chunk[: m.start()]
            trimmed += 1
        if not mpeg:
            chunk = chunk.rstrip(b"\x00")
        if len(chunk) < 2 and not mpeg:
            dropped += 1
            continue
        out += prefix + chunk
        kept += 1
    return bytes(out), {"nal_units_kept": kept, "nal_units_dropped": dropped, "nal_units_trimmed": trimmed}


def _ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise MediaError(proc.stderr.decode(errors="replace")[-2000:])


# Closed or encrypted formats with no public layout: what to do instead, keyed by file extension.
CLOSED_FORMATS = {
    ".avk": "Avigilon AVK is encrypted. Export from Avigilon Control Center / Unity as MP4 or AVI (native format).",
    ".g64": "Genetec G64 is proprietary. Export from Genetec Security Desk as MP4 or ASF, or keep G64 and use the Genetec player.",
    ".g64x": "Genetec G64X is encrypted. Export from Genetec Security Desk as MP4 or ASF.",
    ".sec": "Samsung/Hanwha SEC is a self-contained player bundle. Export from Wisenet as AVI or MP4.",
    ".exe": "A self-playing export (the player is bundled with the video). Ask for the recorder's native files, "
    "or export from the player as AVI or MP4.",
    ".nvf": "Dedicated Micros NVF has no public layout. Export as AVI from the recorder's software.",
    ".vxg": "Verint / Nice export with no public layout. Export as AVI or MP4 from the vendor software.",
}


def closed_format_hint(filename: str) -> str | None:
    return CLOSED_FORMATS.get(Path(filename).suffix.lower())


METHODS = {
    "parse": "Read the vendor container record by record with Lucida's own parser for that format, keep every "
    "compressed frame byte-identical, and write them into MPEG transport stream with the recorder's own frame "
    "timing (Motion JPEG goes into Matroska). For Dahua DAV, Hikvision, Xiongmai/XMEye, HiSilicon HXVS, MPEG "
    "program and transport streams, AVI with vendor FourCCs and network captures (pcap) of RTSP streams. Gaps "
    "and timestamp anomalies are listed in the record.",
    "remux": "Stream copy into MPEG transport stream with FFmpeg (no re-encoding; the recorder's own timestamps "
    "are kept). For containers FFmpeg can demux, including Dahua DAV and most MPEG program/transport streams.",
    "carve": "Carve the raw video stream (H.264, H.265, MPEG-4 Part 2, MPEG-2 or Motion JPEG) out of the file and "
    "drop container bytes. For unknown containers. A carved stream carries no timestamps: frame times are computed "
    "from the frame rate given.",
}

EXTENSIONS = {"remux": "ts", "parse": "ts", "h264": "h264", "hevc": "hevc", "mpeg4": "m4v", "mpeg2video": "m2v",
              "mjpeg": "mkv"}


def output_extension(method: str, codec: str | None) -> str:
    if method in ("remux", "parse"):
        return "mkv" if method == "parse" and codec == "mjpeg" else "ts"
    return EXTENSIONS.get(codec or "", "h264")


def convert(src: Path, dest: Path, method: str, settings: Settings, frame_rate: float = 25.0,
            codec: str | None = None) -> dict:
    """Convert `src` into a standard file. `dest` is the output path without regard to its suffix:
    the file is written at `record["output_path"]`, whose extension fits what was found.
    Returns a provenance record."""
    if method not in METHODS:
        raise ValueError(f"unknown conversion method '{method}'")
    if not (0 < frame_rate <= 240):
        raise ValueError("frame rate must be between 0 and 240")
    record: dict = {"method": method, "description": METHODS[method], "ffmpeg": ffmpeg_version(settings.ffmpeg)}
    head = src.read_bytes()[: 1 << 16]
    record["identified"] = sniff(head)
    if method == "remux":
        dest = dest.with_suffix(".ts")
        fmt_args = []
        if record["identified"]["family"] == "hikvision":
            # The IMKH header is vendor metadata; FFmpeg reads the program stream after it.
            fmt_args = ["-f", "mpeg", "-skip_initial_bytes", "40"]
        cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-y", *fmt_args, "-i", str(src),
               "-map", "0:v:0", "-c", "copy", "-fflags", "+bitexact", "-map_metadata", "-1", "-f", "mpegts", str(dest)]
        _ffmpeg(cmd)
        record["command"] = _redact(cmd, src, dest)
        record["output_path"] = str(dest)
        return record
    data = src.read_bytes()
    if method == "parse":
        family = record["identified"]["family"]
        parser = dvr.PARSERS.get(family or "")
        if not parser:
            raise MediaError("Lucida has no dedicated parser for this container; use remux or carve")
        try:
            parsed = parser(data, frame_rate)
        except dvr.ParseError as e:
            raise MediaError(f"{record['identified']['label']} parser: {e}") from e
        if parsed.codec not in ("h264", "hevc", "mpeg4", "mpeg2video", "mjpeg"):
            raise MediaError("the recorder's video codec was not recognised; try carve")
        times, timing = dvr.settle_timing(parsed, frame_rate)
        frames = [d for _, d in parsed.frames]
        if parsed.codec == "mjpeg":
            dest = dest.with_suffix(".mkv")
            rate = timing["measured_frame_rate"] or frame_rate
            cmd = dvr.write_mjpeg_mkv(dest, frames, rate, settings.ffmpeg)
            record["command"] = _redact(cmd, Path("pipe:0"), dest)
            timing["note"] = "Motion JPEG is written at the measured constant rate; per-frame recorder times are in this record"
        else:
            dest = dest.with_suffix(".ts")
            dvr.write_ts(dest, parsed.codec, frames, times)
        record.update(codec=parsed.codec, parser=parser.__name__, timing=timing, parser_stats=parsed.stats,
                      notes=parsed.notes, width=parsed.width, height=parsed.height, output_path=str(dest))
        return record
    codec = codec or guess_codec(data)
    if codec not in ("h264", "hevc", "mpeg4", "mpeg2video", "mjpeg"):
        raise MediaError("no H.264, H.265, MPEG-4, MPEG-2 or JPEG video found to carve; this container needs a dedicated parser")
    stream, stats = carve(data, codec)
    if not stream:
        raise MediaError("carving found no usable video")
    dest = dest.with_suffix("." + output_extension("carve", codec))
    if codec == "mjpeg":
        frames = [stream[a:b] for a, b in _jpeg_frames(stream)]
        cmd = dvr.write_mjpeg_mkv(dest, frames, frame_rate, settings.ffmpeg)
        record["command"] = _redact(cmd, Path("pipe:0"), dest)
    else:
        dest.write_bytes(stream)
    record.update(codec=codec, frame_rate=frame_rate, carve=stats, container_markers=[m.decode() for m in CONTAINER_MARKERS],
                  timestamps="synthetic: frame n is at n / frame_rate seconds", output_path=str(dest))
    return record


def _redact(cmd: list[str], src: Path, dest: Path) -> str:
    """The command with local paths replaced by placeholders, for the report."""
    return " ".join("<input>" if c == str(src) else "<output>" if c == str(dest) else c for c in cmd)
