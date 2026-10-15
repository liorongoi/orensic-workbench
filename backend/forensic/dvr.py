"""Native parsers for proprietary CCTV / DVR containers.

Each parser walks a vendor container record by record, keeps the compressed video frames exactly
as the recorder wrote them, and reads the recorder's own timing (per-frame millisecond counters,
wall-clock fields, or the frame rate written in key-frame headers). The frames are then written
into a standard MPEG transport stream (or Matroska for Motion JPEG) with those timestamps, so a
working copy plays at the recorder's real timing, not a guessed frame rate.

Formats (layouts from public open-source demuxers; see docs/algorithms.md section 18):

* Dahua DAV (`DHAV` ... `dhav` records): FFmpeg libavformat/dhav.c.
* Xiongmai / XMEye `.h264` exports and NetSurveillance streams (`00 00 01 FC/FD/FE/FA/F9` frame
  headers): OpenIPC python-dvr, dvrip.py.
* HiSilicon IP camera `.264` / `.265` files (`HXVS` / `HXVT`, `HXVF` frames): FFmpeg
  libavformat/hxvs.c and VLC modules/demux/hx.c.
* MPEG program stream, including Hikvision's `IMKH`-headed exports: ISO/IEC 13818-1.

Nothing here decodes or re-encodes video, and no third-party library is needed.
"""

from __future__ import annotations

import bisect
import datetime as dt
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class ParseError(ValueError):
    pass


@dataclass
class Parsed:
    codec: str | None
    frames: list[tuple[float | None, bytes]] = field(default_factory=list)  # (time in ms or None, frame bytes)
    keys: list[bool] = field(default_factory=list)
    frame_rate: float | None = None  # as written by the recorder, when it writes one
    width: int | None = None
    height: int | None = None
    clock_start: dt.datetime | None = None  # recorder wall clock of the first frame
    clock_precision: str | None = None
    timing_source: str = ""
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add(self, t: float | None, data: bytes, key: bool) -> None:
        self.frames.append((t, data))
        self.keys.append(key)


def packed_datetime(v: int) -> dt.datetime | None:
    """The 32-bit packed date used by Dahua and Xiongmai (6 bits year from 2000, 4 month, 5 day...)."""
    try:
        return dt.datetime(((v >> 26) & 0x3F) + 2000, (v >> 22) & 0x0F, (v >> 17) & 0x1F,
                           (v >> 12) & 0x1F, (v >> 6) & 0x3F, v & 0x3F)
    except ValueError:
        return None


def codec_from_payload(frames: list[tuple[float | None, bytes]]) -> str | None:
    from .formats import guess_codec

    return guess_codec(b"".join(d for _, d in frames[:60]))


# Dahua DAV -----------------------------------------------------------------------------------------

DHAV_CODECS = {0x1: "mpeg4", 0x3: "mjpeg", 0x2: "h264", 0x4: "h264", 0x8: "h264", 0xC: "hevc"}
# Extension records: type -> total size in bytes (libavformat/dhav.c parse_ext).
DHAV_EXT_SIZES = {0x80: 4, 0x81: 4, 0x82: 8, 0x83: 4, 0x88: 8, 0x8C: 8, 0x91: 8, 0x92: 8, 0x93: 8, 0x95: 8,
                  0x9A: 8, 0x9B: 8, 0xB3: 8, 0x84: 4, 0x85: 4, 0x8B: 4, 0x94: 4, 0x96: 4, 0xA0: 4, 0xB2: 4, 0xB4: 4}


def parse_dhav(data: bytes, frame_rate: float = 25.0) -> Parsed:
    out = Parsed(codec=None, timing_source="per-frame millisecond counter and date field written by the recorder",
                 clock_precision="1 s (date field); frame spacing from the millisecond counter")
    pos = data.find(b"DHAV")
    if pos < 0:
        raise ParseError("no DHAV record found")
    skipped = pos
    resyncs = audio = aux = truncated = 0
    channels: dict[int, int] = {}
    channel = None
    codec_byte = None
    ch_state: dict = {}
    while pos + 24 <= len(data):
        if data[pos:pos + 4] != b"DHAV":
            nxt = data.find(b"DHAV", pos + 1)
            if nxt < 0:
                skipped += len(data) - pos
                break
            skipped += nxt - pos
            resyncs += 1
            pos = nxt
            continue
        ftype, _sub, ch = data[pos + 4], data[pos + 5], data[pos + 6]
        fnum, flen, date = struct.unpack_from("<III", data, pos + 8)
        if flen < 24:
            pos += 4
            resyncs += 1
            continue
        if pos + flen > len(data):
            truncated += 1
            break
        if ftype == 0xF1:
            aux += 1
            pos += flen
            continue
        ts, extlen = struct.unpack_from("<HB", data, pos + 20)
        p, end = pos + 24, pos + 24 + extlen
        while p < end:
            t = data[p]
            if t == 0x80:
                out.width, out.height = 8 * data[p + 2], 8 * data[p + 3]
            elif t == 0x81:
                codec_byte = data[p + 2]
                if data[p + 3]:
                    out.frame_rate = float(data[p + 3])
            elif t == 0x82:
                out.width, out.height = struct.unpack_from("<HH", data, p + 4)
            if t not in DHAV_EXT_SIZES:
                break
            p += DHAV_EXT_SIZES[t]
        trailer = 8 if data[pos + flen - 8:pos + flen - 4] == b"dhav" else 0
        payload = data[end:pos + flen - trailer]
        pos += flen
        if ftype == 0xF0:
            audio += 1
            continue
        channels[ch] = channels.get(ch, 0) + 1
        if channel is None:
            channel = ch
        if ch != channel:
            continue
        # Timing as in libavformat/dhav.c get_pts: the date field to the second, then the 16-bit
        # millisecond counter (wrapping) for the spacing of frames within that second.
        when = packed_datetime(date)
        secs = when.replace(tzinfo=dt.timezone.utc).timestamp() if when else None
        if out.clock_start is None and when:
            out.clock_start = when
        st = ch_state
        if st and secs is not None and st["secs"] == secs:
            diff = ts - st["ts"]
            if diff < -32768:
                diff += 65536  # the 16-bit counter wrapped; a smaller step back is a real clock anomaly
            if diff == 0:
                diff = (fnum - st["fnum"]) * 1000.0 / (out.frame_rate or frame_rate)
            t_ms = st["t"] + diff
        elif secs is not None:
            t_ms = secs * 1000.0
        else:
            t_ms = None
        if t_ms is not None:
            ch_state.update(secs=secs, ts=ts, fnum=fnum, t=t_ms)
        out.add(t_ms, payload, ftype == 0xFD)
    if not out.frames:
        raise ParseError("DHAV records found but none carried video")
    out.codec = DHAV_CODECS.get(codec_byte) if codec_byte is not None else None
    out.codec = out.codec or codec_from_payload(out.frames)
    out.stats = {"records_skipped_bytes": skipped, "resyncs": resyncs, "audio_records": audio, "aux_records": aux,
                 "truncated_records": truncated, "channel_extracted": channel,
                 "video_records_per_channel": {str(k): v for k, v in sorted(channels.items())}}
    if len(channels) > 1:
        out.notes.append(f"the file holds {len(channels)} camera channels; channel {channel} was extracted")
    return out


# Xiongmai / XMEye -----------------------------------------------------------------------------------

XM_CODECS = {1: "mpeg4", 2: "h264", 3: "hevc"}
XM_HEADER = re.compile(rb"\x00\x00\x01[\xf9\xfa\xfc\xfd\xfe]")


def parse_xm(data: bytes, frame_rate: float = 25.0) -> Parsed:
    out = Parsed(codec=None, timing_source="frame rate and date written in each key-frame header",
                 clock_precision="1 s (key-frame date field)")
    m = re.search(rb"\x00\x00\x01[\xfc\xfe]", data)
    if not m:
        raise ParseError("no Xiongmai key-frame header found")
    pos = skipped = m.start()
    resyncs = audio = info = truncated = 0
    t_ms = 0.0
    fps = None
    n = 0
    clock_jumps = []
    codec_byte = None
    while pos + 8 <= len(data):
        kind = data[pos + 3] if data[pos:pos + 3] == b"\x00\x00\x01" else None
        if kind not in (0xF9, 0xFA, 0xFC, 0xFD, 0xFE):
            nxt = XM_HEADER.search(data, pos + 1)
            if not nxt:
                skipped += len(data) - pos
                break
            skipped += nxt.start() - pos
            resyncs += 1
            pos = nxt.start()
            continue
        if kind in (0xFA, 0xF9):
            (length,) = struct.unpack_from("<H", data, pos + 6)
            audio += kind == 0xFA
            info += kind == 0xF9
            pos += 8 + length
            continue
        if kind in (0xFC, 0xFE):
            if pos + 16 > len(data):
                truncated += 1
                break
            media, f, w8, h8, date, length = struct.unpack_from("<BBBBII", data, pos + 4)
            hdr = 16
            if kind == 0xFC:
                codec_byte = media
            if f:
                fps = float(f)
                out.frame_rate = out.frame_rate or fps
            out.width, out.height = w8 * 8, h8 * 8
            when = packed_datetime(date)
            if when:
                if out.clock_start is None:
                    out.clock_start = when
                else:
                    expected = out.clock_start + dt.timedelta(milliseconds=t_ms)
                    if abs((when - expected).total_seconds()) > 2:
                        clock_jumps.append({"frame": n, "recorder_clock": when.isoformat(),
                                            "expected_from_frame_rate": expected.isoformat(timespec="milliseconds")})
        else:
            (length,) = struct.unpack_from("<I", data, pos + 4)
            hdr = 8
        if pos + hdr + length > len(data):
            truncated += 1
            break
        out.add(t_ms, data[pos + hdr:pos + hdr + length], kind != 0xFD)
        if kind == 0xFE:
            out.codec = "mjpeg"
        n += 1
        t_ms += 1000.0 / (fps or frame_rate)
        pos += hdr + length
    if not out.frames:
        raise ParseError("Xiongmai headers found but no video frames")
    out.codec = out.codec or XM_CODECS.get(codec_byte) or codec_from_payload(out.frames)
    if fps is None:
        out.notes.append(f"no frame rate in the file; {frame_rate:g} fps was used")
    if clock_jumps:
        out.notes.append(f"the recorder clock departs from the frame count at {len(clock_jumps)} key frames "
                         "(missing frames or a clock change)")
    out.stats = {"records_skipped_bytes": skipped, "resyncs": resyncs, "audio_records": audio, "info_records": info,
                 "truncated_records": truncated, "clock_jumps": clock_jumps[:100]}
    return out


# HiSilicon HXVS / HXVT -------------------------------------------------------------------------------


def parse_hxvs(data: bytes, frame_rate: float = 25.0) -> Parsed:
    tag = data[:4]
    if tag not in (b"HXVS", b"HXVT"):
        raise ParseError("no HXVS/HXVT header")
    w, h = struct.unpack_from("<II", data, 4)
    out = Parsed(codec="h264" if tag == b"HXVS" else "hevc", width=w, height=h,
                 timing_source="per-frame millisecond timestamps written by the camera")
    pos = 16
    resyncs = audio = truncated = skipped = 0
    cur_t, cur, cur_key = None, bytearray(), False
    while pos + 16 <= len(data):
        tag = data[pos:pos + 4]
        if tag == b"HXFI":
            break
        if tag not in (b"HXVF", b"HXAF"):
            m = re.compile(rb"HXVF|HXAF|HXFI").search(data, pos + 1)
            if not m:
                skipped += len(data) - pos
                break
            skipped += m.start() - pos
            resyncs += 1
            pos = m.start()
            continue
        length, ts, flags = struct.unpack_from("<III", data, pos + 4)
        if pos + 16 + length > len(data):
            truncated += 1
            break
        if tag == b"HXAF":
            audio += 1
        else:
            nal = data[pos + 16:pos + 16 + length]
            if not nal.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
                nal = b"\x00\x00\x00\x01" + nal
            # One HXVF holds one NAL unit; NAL units with the same timestamp make one frame.
            if cur and ts != cur_t:
                out.add(float(cur_t), bytes(cur), cur_key)
                cur, cur_key = bytearray(), False
            cur_t = ts
            cur += nal
            cur_key = cur_key or flags == 1
        pos += 16 + length
    if cur:
        out.add(float(cur_t), bytes(cur), cur_key)
    if not out.frames:
        raise ParseError("HXVS header found but no video frames")
    out.stats = {"records_skipped_bytes": skipped, "resyncs": resyncs, "audio_records": audio, "truncated_records": truncated}
    return out


# MPEG program stream (Hikvision IMKH, exacqVision .ps and other PS exports) ------------------------------


def _pes_pts(data: bytes, p: int) -> int:
    return (((data[p] >> 1) & 0x07) << 30) | (data[p + 1] << 22) | ((data[p + 2] >> 1) << 15) | (data[p + 3] << 7) | (data[p + 4] >> 1)


def parse_ps(data: bytes, frame_rate: float = 25.0) -> Parsed:
    out = Parsed(codec=None, timing_source="PES presentation timestamps (90 kHz) written by the recorder")
    start = 40 if data[:4] == b"IMKH" else 0
    if start:
        out.notes.append("40-byte Hikvision IMKH header skipped")
    pos = data.find(b"\x00\x00\x01\xba", start)
    if pos < 0:
        raise ParseError("no MPEG program stream pack header found")
    skipped = pos - start
    resyncs = private = audio = 0
    video_id = None
    es = bytearray()
    pes_pts: list[tuple[int, int, int]] = []  # (ES offset of the PES payload, its length, PTS)
    last_pts = None
    wraps = 0
    while pos + 6 <= len(data):
        if data[pos:pos + 3] != b"\x00\x00\x01":
            nxt = data.find(b"\x00\x00\x01\xba", pos + 1)
            if nxt < 0:
                skipped += len(data) - pos
                break
            skipped += nxt - pos
            resyncs += 1
            pos = nxt
            continue
        sid = data[pos + 3]
        if sid == 0xBA:
            pos += 14 + (data[pos + 13] & 7) if (data[pos + 4] >> 6) == 1 else 12
            continue
        if sid == 0xB9:
            pos += 4
            continue
        if sid < 0xBB:
            pos += 3
            resyncs += 1
            continue
        (plen,) = struct.unpack_from(">H", data, pos + 4)
        body_end = min(pos + 6 + plen, len(data))
        if 0xE0 <= sid <= 0xEF and plen:
            if video_id is None:
                video_id = sid
            if sid == video_id:
                if (data[pos + 6] >> 6) == 2:  # MPEG-2 PES header
                    payload_at = pos + 9 + data[pos + 8]
                    pts_at = pos + 9 if data[pos + 7] & 0x80 else None
                else:  # MPEG-1 PES header: stuffing, optional STD buffer size, then PTS / PTS+DTS / 0x0F
                    q = pos + 6
                    while q < body_end and data[q] == 0xFF:
                        q += 1
                    if q < body_end and (data[q] >> 6) == 1:
                        q += 2
                    if q >= body_end:
                        pos = body_end
                        continue
                    pts_at = q if (data[q] >> 4) in (2, 3) else None
                    payload_at = q + (5 if (data[q] >> 4) == 2 else 10 if (data[q] >> 4) == 3 else 1)
                payload = data[payload_at:body_end]
                if pts_at is not None:
                    raw = _pes_pts(data, pts_at)
                    if last_pts is not None and raw + wraps * (1 << 33) < last_pts - (1 << 32):
                        wraps += 1  # 33-bit wrap
                    last_pts = raw + wraps * (1 << 33)
                    pes_pts.append((len(es), len(payload), last_pts))
                es += payload
        elif 0xC0 <= sid <= 0xDF:
            audio += 1
        elif sid in (0xBD, 0xBF):
            private += 1  # Hikvision and others put vendor metadata in private streams
        pos = body_end
    missing = frames_from_es(out, bytes(es), pes_pts, "program stream")
    out.stats = {"records_skipped_bytes": skipped, "resyncs": resyncs, "audio_packets": audio,
                 "private_stream_packets": private, "video_stream_id": hex(video_id or 0),
                 "frames_without_own_timestamp": missing}
    return out


def frames_from_es(out: Parsed, es: bytes, pes_pts: list[tuple[int, int, int]], what: str, clock_hz: int = 90000) -> int:
    """Split a joined elementary stream into pictures and time them from (ES offset, length, timestamp)
    records: a timestamp belongs to the first picture that begins in its packet (ISO/IEC 13818-1
    2.4.3.7, and the same rule for RTP). Returns how many pictures had no timestamp of their own."""
    from .formats import guess_codec

    out.codec = out.codec or guess_codec(es)
    if not es or out.codec is None:
        raise ParseError(f"{what} holds no recognisable video")
    units = access_units(es, out.codec)
    starts = [a for a, _ in units]
    times: dict[int, float] = {}
    base = pes_pts[0][2] if pes_pts else 0
    for off, length, pts in pes_pts:
        i = bisect.bisect_left(starts, off)
        if i < len(starts) and starts[i] < off + max(length, 1) and i not in times:
            times[i] = (pts - base) * 1000.0 / clock_hz
    for i, (a, b) in enumerate(units):
        out.add(times.get(i), es[a:b], is_key(out.codec, es[a:b]))
    missing = len(units) - len(times)
    if missing:
        out.notes.append(f"{missing} frames carry no timestamp of their own; their times are "
                         "interpolated between the neighbouring timestamps")
    return missing


def access_units(es: bytes, codec: str) -> list[tuple[int, int]]:
    """Split an elementary stream into pictures: (start, end) byte ranges."""
    sc = [m.start() for m in re.finditer(rb"\x00\x00\x01", es)]
    begins = []
    seen_picture = False
    for s in sc:
        if s + 4 >= len(es):
            break
        b = es[s + 3]
        start = s - 1 if s and es[s - 1] == 0 else s
        if codec == "h264":
            t = b & 0x1F
            vcl = t in (1, 5)
            first = vcl and es[s + 4] & 0x80  # first_mb_in_slice == 0
            new = (t in (6, 7, 8, 9) and seen_picture) or (first and seen_picture)
        elif codec == "hevc":
            t = (b >> 1) & 0x3F
            vcl = t < 32
            first = vcl and s + 5 < len(es) and es[s + 5] & 0x80  # first_slice_segment_in_pic_flag
            new = (t in (32, 33, 34, 35, 39) and seen_picture) or (first and seen_picture)
        elif codec == "mpeg2video":
            vcl = b == 0x00
            new = seen_picture and b in (0x00, 0xB3, 0xB8)
        else:  # mpeg4
            vcl = b == 0xB6
            new = seen_picture and (b in (0xB0, 0xB3, 0xB5, 0xB6) or 0x20 <= b <= 0x2F)
        if new or not begins:
            begins.append(start)
            seen_picture = False
        seen_picture = seen_picture or vcl
    if codec == "mjpeg":
        from .formats import _jpeg_frames

        return _jpeg_frames(es)
    return list(zip(begins, begins[1:] + [len(es)]))


def is_key(codec: str, frame: bytes) -> bool:
    if codec == "h264":
        return any(frame[m.end()] & 0x1F == 5 for m in re.finditer(rb"\x00\x00\x01", frame[:-1]))
    if codec == "hevc":
        return any(16 <= (frame[m.end()] >> 1) & 0x3F <= 21 for m in re.finditer(rb"\x00\x00\x01", frame[:-1]))
    if codec == "mpeg2video":
        i = frame.find(b"\x00\x00\x01\x00")
        return i >= 0 and i + 5 < len(frame) and (frame[i + 5] >> 3) & 7 == 1
    if codec == "mpeg4":
        i = frame.find(b"\x00\x00\x01\xb6")
        return i >= 0 and i + 4 < len(frame) and frame[i + 4] >> 6 == 0
    return True


# AVI with vendor FourCCs (Geovision, Hikvision, Mace and other DVRs) ----------------------------------------

AVI_CHUNK = re.compile(rb"[0-9]{2}(?:dc|db|wb|tx|pc)|ix[0-9]{2}|LIST|JUNK|idx1|RIFF")


def parse_avi(data: bytes, frame_rate: float = 25.0) -> Parsed:
    """Read the video chunks of an AVI (RIFF, OpenDML RIFF-AVIX parts included) whatever its FourCC.

    The frame rate comes from the video stream header (dwRate / dwScale); a zero-length chunk is a
    frame the recorder dropped, so it advances the clock and shows as a gap. Vendor bytes before the
    first start code of a frame (some DVRs prefix each frame with a private header) are removed and
    counted.
    """
    if data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        raise ParseError("not a RIFF AVI file")
    out = Parsed(codec=None, timing_source="frame rate in the AVI stream header; one chunk per frame")
    vids_index = None
    handler = compression = b""
    rate = None
    stream_no = -1
    # Header: walk hdrl for strh/strf of the first video stream.
    hdrl = data.find(b"hdrl", 12)
    movi = data.find(b"movi", 12)
    if movi < 0:
        raise ParseError("no movi list")
    p = hdrl + 4 if 0 <= hdrl < movi else movi
    while p + 8 <= movi:
        cid, size = data[p:p + 4], struct.unpack_from("<I", data, p + 4)[0]
        if cid == b"LIST":
            p += 12
            continue
        body = data[p + 8:p + 8 + size]
        if cid == b"strh" and len(body) >= 28:
            stream_no += 1
            if body[:4] == b"vids" and vids_index is None:
                vids_index = stream_no
                handler = body[4:8]
                scale, r = struct.unpack_from("<II", body, 20)
                if scale and r:
                    rate = r / scale
        elif cid == b"strf" and vids_index == stream_no and len(body) >= 20 and out.width is None:
            out.width, h = struct.unpack_from("<ii", body, 4)
            out.height = abs(h)
            compression = body[16:20]
        elif cid == b"avih" and len(body) >= 4 and rate is None:
            usec = struct.unpack_from("<I", body, 0)[0]
            if usec:
                rate = 1e6 / usec
        p += 8 + size + (size & 1)
    if vids_index is None:
        raise ParseError("no video stream header")
    out.frame_rate = round(rate, 6) if rate else None
    step = 1000.0 / (rate or frame_rate)
    tag = b"%02d" % vids_index
    pos = movi + 4
    n = 0
    skipped = resyncs = dropped = audio = prefixed = 0
    stripped = 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        if not AVI_CHUNK.fullmatch(cid):
            m = AVI_CHUNK.search(data, pos + 1)
            if not m:
                skipped += len(data) - pos
                break
            skipped += m.start() - pos
            resyncs += 1
            pos = m.start()
            continue
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"RIFF":  # OpenDML continuation: RIFF size AVIX LIST size movi
            pos += 12
            continue
        if cid == b"LIST":
            pos += 12  # descend into movi / rec lists
            continue
        if pos + 8 + size > len(data):
            break
        if cid[:2] == tag and cid[2:] in (b"dc", b"db"):
            if size == 0:
                dropped += 1
            else:
                frame = data[pos + 8:pos + 8 + size]
                sc = frame.find(b"\x00\x00\x01")
                if sc > 0 and not frame.startswith(b"\xff\xd8"):
                    cut = sc - 1 if frame[sc - 1] == 0 else sc
                    if cut:
                        stripped += cut
                        prefixed += 1
                        frame = frame[cut:]
                out.add(n * step, frame, False)
            n += 1
        elif cid[2:] == b"wb":
            audio += 1
        pos += 8 + size + (size & 1)
    if not out.frames:
        raise ParseError("no video chunks found")
    out.codec = codec_from_payload(out.frames)
    if out.codec:
        out.keys = [is_key(out.codec, d) for _, d in out.frames]
    out.stats = {"fourcc_handler": handler.decode("latin-1"), "fourcc_compression": compression.decode("latin-1"),
                 "video_stream": vids_index, "records_skipped_bytes": skipped, "resyncs": resyncs,
                 "dropped_frame_chunks": dropped, "audio_chunks": audio, "frames_with_vendor_prefix": prefixed,
                 "vendor_prefix_bytes_removed": stripped}
    if dropped:
        out.notes.append(f"{dropped} empty video chunks (frames the recorder dropped) are kept as gaps")
    if prefixed:
        out.notes.append(f"{prefixed} frames carried a vendor header before the video data; it was removed")
    return out


# MPEG transport stream, including vendor stream types and damaged captures -----------------------------------------

VIDEO_STREAM_TYPES = {0x01: "mpeg2video", 0x02: "mpeg2video", 0x10: "mpeg4", 0x1B: "h264", 0x24: "hevc"}


def _ts_packet_size(data: bytes) -> tuple[int, int]:
    """(packet size, offset of the sync byte in it): 188 (TS), 192 (M2TS / Blu-ray timecode) or 204 (with RS)."""
    for size, off in ((188, 0), (192, 4), (204, 0)):
        start = data.find(b"\x47", off)
        while 0 <= start < 2000:
            if all(start + k * size < len(data) and data[start + k * size] == 0x47 for k in range(8)):
                return size, start
            start = data.find(b"\x47", start + 1)
    raise ParseError("no transport stream packet sync found")


def parse_ts(data: bytes, frame_rate: float = 25.0) -> Parsed:
    """Demultiplex the video PID of a transport stream, including streams whose PMT marks the video with a
    vendor or private stream type (which FFmpeg will not decode) and captures with lost packets."""
    out = Parsed(codec=None, timing_source="PES decoding timestamps (90 kHz) written by the recorder")
    size, pos = _ts_packet_size(data)
    pmt_pids: set[int] = set()
    declared: dict[int, int] = {}
    pes: dict[int, list] = {}  # pid -> [bytearray es, [(offset, len, ts)], cur_offset]
    cc_last: dict[int, int] = {}
    lost = resyncs = 0
    while pos + 188 <= len(data):
        if data[pos] != 0x47:
            nxt = data.find(b"\x47", pos + 1)
            if nxt < 0:
                break
            resyncs += 1
            pos = nxt
            continue
        pkt = data[pos:pos + 188]
        pos += size
        pusi = pkt[1] & 0x40
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 3
        cc = pkt[3] & 0x0F
        if pkt[1] & 0x80 or pid == 0x1FFF:
            continue
        p = 4
        if afc & 2:
            p += 1 + pkt[4]
        if not afc & 1 or p >= 188:
            continue
        if afc & 1 and pid in cc_last and cc != (cc_last[pid] + 1) & 0x0F and cc != cc_last[pid]:
            lost += (cc - cc_last[pid] - 1) & 0x0F
        cc_last[pid] = cc
        payload = pkt[p:]
        if pid == 0 and pusi:
            sec = payload[1 + payload[0]:]
            n = (((sec[1] & 0x0F) << 8) | sec[2]) - 9
            for k in range(0, max(n, 0), 4):
                prog, ppid = struct.unpack_from(">HH", sec, 8 + k)
                if prog:
                    pmt_pids.add(ppid & 0x1FFF)
            continue
        if pid in pmt_pids and pusi:
            sec = payload[1 + payload[0]:]
            slen = ((sec[1] & 0x0F) << 8) | sec[2]
            pil = ((sec[10] & 0x0F) << 8) | sec[11]
            q = 12 + pil
            while q + 5 <= 3 + slen - 4:
                st, epid, eil = sec[q], ((sec[q + 1] & 0x1F) << 8) | sec[q + 2], ((sec[q + 3] & 0x0F) << 8) | sec[q + 4]
                declared[epid] = st
                q += 5 + eil
            continue
        if pid in pmt_pids or pid < 0x20:
            continue
        st = pes.setdefault(pid, [bytearray(), [], 0])
        if pusi and payload[:3] == b"\x00\x00\x01":
            sid = payload[3]
            if not (0xE0 <= sid <= 0xEF or sid in (0xBD, 0xFD)):
                st[2] = -1  # not a video PES on this PID
                continue
            flags, hl = payload[7], payload[8]
            ts = None
            if flags & 0x40:  # DTS
                ts = _pes_pts(payload, 14)
            elif flags & 0x80:
                ts = _pes_pts(payload, 9)
            body = payload[9 + hl:]
            if ts is not None:
                st[1].append((len(st[0]), len(body), ts))
            st[0] += body
            st[2] = 1
        elif st[2] == 1:
            st[0] += payload
    from .formats import guess_codec

    chosen = None
    for pid, (es, _, _) in sorted(pes.items(), key=lambda kv: -len(kv[1][0])):
        if guess_codec(bytes(es[:1 << 20])):
            chosen = pid
            break
    if chosen is None:
        raise ParseError("no video found in the transport stream")
    es, stamps, _ = pes[chosen]
    # Unwrap 33-bit timestamps.
    unwrapped, wraps, last = [], 0, None
    for off, ln, t in stamps:
        if last is not None and t + wraps * (1 << 33) < last - (1 << 32):
            wraps += 1
        last = t + wraps * (1 << 33)
        unwrapped.append((off, ln, last))
    missing = frames_from_es(out, bytes(es), unwrapped, "transport stream")
    stype = declared.get(chosen)
    out.stats = {"packet_size": size, "video_pid": chosen, "declared_stream_type": hex(stype) if stype is not None else None,
                 "lost_packets": lost, "resyncs": resyncs, "frames_without_own_timestamp": missing,
                 "other_pids": sorted(p for p in pes if p != chosen)}
    if stype is not None and stype not in VIDEO_STREAM_TYPES:
        out.notes.append(f"the stream declares type {stype:#x} for its video (a vendor or private type that standard "
                         f"players do not decode); the payload is {out.codec}")
    if lost:
        out.notes.append(f"{lost} transport packets are missing (continuity counter gaps); frames around them may be damaged")
    return out


def parse_pcap(data: bytes, frame_rate: float = 25.0) -> Parsed:
    from .dvr_net import parse_pcap as parse

    return parse(data, frame_rate)


PARSERS = {"dahua_dav": parse_dhav, "xmeye": parse_xm, "hxvs": parse_hxvs, "hikvision": parse_ps, "mpeg_ps": parse_ps,
           "avi": parse_avi, "mpeg_ts": parse_ts, "pcap": parse_pcap}


# Timing ---------------------------------------------------------------------------------------------------


def settle_timing(p: Parsed, frame_rate: float) -> tuple[list[float], dict]:
    """Per-frame times in ms from 0, strictly increasing, plus a timing summary for the report.

    Where the recorder's times are missing or go backwards, a time is filled in from the frame
    rate and the frame is listed as an anomaly, so nothing is hidden.
    """
    raw = [t for t, _ in p.frames]
    known = [(i, t) for i, t in enumerate(raw) if t is not None]
    steps = [(tb - ta) / (ib - ia) for (ia, ta), (ib, tb) in zip(known, known[1:]) if tb > ta]
    median = sorted(steps)[len(steps) // 2] if steps else 1000.0 / (p.frame_rate or frame_rate)
    # Frames without a time of their own are placed evenly between the neighbouring known times.
    filled: list[float | None] = list(raw)
    for (ia, ta), (ib, tb) in zip(known, known[1:]):
        for k in range(ia + 1, ib):
            filled[k] = ta + (tb - ta) * (k - ia) / (ib - ia)
    times: list[float] = []
    anomalies = []
    first = known[0][1] if known else 0.0
    for i, t in enumerate(filled):
        if t is None or (times and t - first <= times[-1]):
            fill = times[-1] + median if times else 0.0
            if t is not None:
                anomalies.append({"frame": i, "recorder_ms": round(t - first, 3), "placed_at_ms": round(fill, 3)})
            times.append(fill)
        else:
            times.append(t - first)
    gaps = [{"frame": i + 1, "gap_ms": round(b - a, 3)} for i, (a, b) in enumerate(zip(times, times[1:]))
            if b - a > 1.5 * median]
    summary = {
        "source": p.timing_source,
        "frames": len(times),
        "key_frames": sum(p.keys),
        "duration_s": round(times[-1] / 1000.0, 3) if times else 0.0,
        "median_frame_interval_ms": round(median, 3),
        "measured_frame_rate": round(1000.0 / median, 3) if median else None,
        "recorder_frame_rate": p.frame_rate,
        "gaps": gaps[:200],
        "gap_count": len(gaps),
        "timestamp_anomalies": anomalies[:200],
        "timestamp_anomaly_count": len(anomalies),
    }
    if p.clock_start:
        end = p.clock_start + dt.timedelta(milliseconds=times[-1])
        summary.update(recorder_clock_start=p.clock_start.isoformat(), recorder_clock_end=end.isoformat(timespec="seconds"),
                       recorder_clock_precision=p.clock_precision)
    return times, summary


# MPEG transport stream writer ---------------------------------------------------------------------------------

TS_STREAM_TYPES = {"h264": 0x1B, "hevc": 0x24, "mpeg4": 0x10, "mpeg2video": 0x02}
PMT_PID, VIDEO_PID = 0x1000, 0x100


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _psi(table: bytes) -> bytes:
    return table + struct.pack(">I", _crc32_mpeg(table))


def _ts_packets(pid: int, payload: bytes, cc: list[int], pcr: int | None = None) -> bytes:
    out = bytearray()
    first = True
    pos = 0
    while pos < len(payload) or first:
        af = b""
        if first and pcr is not None:
            base, ext = pcr // 300, pcr % 300
            af = bytes([0x10, (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                        ((base & 1) << 7) | 0x7E | (ext >> 8), ext & 0xFF])
        room = 184 - (1 + len(af) if af else 0)
        chunk = payload[pos:pos + room]
        if af or len(chunk) < 184:
            fill = 184 - len(chunk) - 1  # bytes of adaptation field after its length byte
            if fill == 0 and not af:
                adapt = b"\x00"
            else:
                body = af or b"\x00"
                adapt = bytes([fill]) + body + b"\xff" * (fill - len(body))
            afc = 0x30
        else:
            adapt, afc = b"", 0x10
        out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, afc | cc[0]]) + adapt + chunk
        cc[0] = (cc[0] + 1) & 0x0F
        pos += len(chunk)
        first = False
    return bytes(out)


def write_ts(path: Path, codec: str, frames: list[bytes], times_ms: list[float]) -> None:
    """Write video frames with the given presentation times into an MPEG transport stream."""
    pat = _psi(bytes([0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))
    pmt = _psi(bytes([0x02, 0xB0, 18, 0x00, 0x01, 0xC1, 0x00, 0x00, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00,
                      TS_STREAM_TYPES[codec], 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00]))
    ccs = {0: [0], PMT_PID: [0], VIDEO_PID: [0]}
    with open(path, "wb") as f:
        for i, (frame, t) in enumerate(zip(frames, times_ms)):
            if i % 25 == 0:
                f.write(_ts_packets(0, b"\x00" + pat, ccs[0]))
                f.write(_ts_packets(PMT_PID, b"\x00" + pmt, ccs[PMT_PID]))
            pts = int(round(t * 90))
            pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + bytes([
                0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF,
                ((pts << 1) & 0xFE) | 1])
            f.write(_ts_packets(VIDEO_PID, pes + frame, ccs[VIDEO_PID], pcr=max(0, pts - 9000) * 300))  # PCR leads PTS by 100 ms


def write_mjpeg_mkv(path: Path, frames: list[bytes], frame_rate: float, ffmpeg: str) -> list[str]:
    """Motion JPEG has no transport stream mapping: stream-copy the JPEG frames into Matroska."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "mjpeg", "-framerate", f"{frame_rate:.6g}", "-i", "pipe:0",
           "-c", "copy", "-fflags", "+bitexact", "-map_metadata", "-1", "-f", "matroska", str(path)]
    proc = subprocess.run(cmd, input=b"".join(frames), capture_output=True, check=False)
    if proc.returncode != 0:
        raise ParseError(proc.stderr.decode(errors="replace")[-2000:])
    return cmd
