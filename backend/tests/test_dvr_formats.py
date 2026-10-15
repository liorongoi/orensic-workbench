"""Proprietary recorder containers: native parsers, timing and carving of every supported codec.

There are no vendor sample files in the repository, so each test builds a file in the vendor's
layout (from the same public open-source demuxers the parsers follow) around a real encoded
stream, and checks that the working copy decodes to exactly the same frames as that stream,
at the timing the container recorded.
"""

import re
import struct
import subprocess

import pytest

from forensic import dvr, formats, media, report

from .conftest import EXAMINER, ingest

N = 25


def _encode(path, codec, fmt, *extra):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "1",
                    "-c:v", codec, *extra, "-f", fmt, str(path)], check=True)
    return path.read_bytes()


def _framemd5(data_or_path, tmp_path, fmt=None):
    """Per-frame MD5 of the decoded pictures, as FFmpeg computes them."""
    src = data_or_path
    if isinstance(data_or_path, bytes):
        src = tmp_path / f"es.{fmt}"
        src.write_bytes(data_or_path)
    out = subprocess.run(["ffmpeg", "-v", "error", *(["-f", fmt] if fmt and isinstance(data_or_path, bytes) else []),
                          "-i", str(src), "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "framemd5", "-"], capture_output=True, check=True).stdout
    return [line.split(b",")[-1].strip() for line in out.splitlines() if line and not line.startswith(b"#")]


def _access_units(es: bytes, codec: str) -> list[bytes]:
    """Split an Annex B stream into one chunk per picture (one slice per picture, as x264/x265 emit here)."""
    starts = [m.start() for m in re.finditer(rb"\x00\x00\x00\x01|(?<!\x00)\x00\x00\x01", es)] + [len(es)]
    frames, cur = [], b""
    for a, b in zip(starts, starts[1:]):
        nal = es[a:b]
        hdr = nal[4] if nal[2] == 0 else nal[3]
        vcl = (hdr & 0x1F) in (1, 5) if codec == "h264" else ((hdr >> 1) & 0x3F) < 32
        cur += nal
        if vcl:
            frames.append(cur)
            cur = b""
    assert len(frames) == N
    return frames


def _is_key(au: bytes) -> bool:
    return b"\x00\x00\x01\x65" in au or b"\x00\x00\x01\x67" in au or b"\x00\x00\x01\x40" in au


def _packed(y, mo, d, h, mi, s):
    return ((y - 2000) << 26) | (mo << 22) | (d << 17) | (h << 12) | (mi << 6) | s


@pytest.fixture(scope="module")
def h264(tmp_path_factory):
    d = tmp_path_factory.mktemp("es")
    es = _encode(d / "a.h264", "libx264", "h264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p")
    return es, _access_units(es, "h264")


@pytest.fixture(scope="module")
def hevc(tmp_path_factory):
    d = tmp_path_factory.mktemp("es")
    es = _encode(d / "a.hevc", "libx265", "hevc", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p",
                 "-x265-params", "log-level=error")
    return es, _access_units(es, "hevc")


# Builders -------------------------------------------------------------------------------------------


def build_dhav(frames, times_ms, start=(2026, 3, 14, 9, 26, 50), fps=25, codec_byte=2, extra_channel=True):
    """Dahua DAV: DHAV header, extension records (0x80 size, 0x81 codec/fps), payload, dhav trailer."""
    out = bytearray(b"\x00" * 16)  # some exports start with padding
    base = (start[3] * 3600 + start[4] * 60 + start[5]) * 1000

    def rec(ftype, ch, n, payload, t_ms, ext=b""):
        secs = (base + int(t_ms)) // 1000
        date = _packed(*start[:3], secs // 3600, secs // 60 % 60, secs % 60)
        ms = (base + int(t_ms)) & 0xFFFF
        length = 24 + len(ext) + len(payload) + 8
        return (b"DHAV" + bytes([ftype, 0, ch, 0]) + struct.pack("<IIIHBB", n, length, date, ms, len(ext), 0)
                + ext + payload + b"dhav" + struct.pack("<I", length))

    for i, (f, t) in enumerate(zip(frames, times_ms)):
        key = _is_key(f)
        ext = bytes([0x80, 0, 160 // 8, 120 // 8, 0x81, 0, codec_byte, fps]) if key else b""
        out += rec(0xFD if key else 0xFC, 0, i, f, t, ext)
        if i % 5 == 0:
            out += rec(0xF0, 0, i, b"\x55" * 320, t, bytes([0x83, 1, 0x0E, 2]))  # G.711 audio
        if extra_channel and i % 3 == 0:
            out += rec(0xFD, 1, i, frames[0], t)  # a second camera's record interleaved
    return bytes(out)


def build_xm(frames, fps=25, start=(2026, 5, 2, 23, 59, 58), codec=2, jpeg=False):
    """Xiongmai: 00 00 01 FC (key, 16-byte header with codec, fps, size/8, date) / FD (P, 8 bytes) / FA audio / F9 info."""
    out = bytearray(b"\x00\x00\x01\xf9\x01\x00" + struct.pack("<H", 12) + b"XM-info-blk!")
    for i, f in enumerate(frames):
        t = i / fps
        secs = start[3] * 3600 + start[4] * 60 + start[5] + int(t)
        d = start[2] + secs // 86400
        secs %= 86400
        date = _packed(start[0], start[1], d, secs // 3600, secs // 60 % 60, secs % 60)
        if jpeg:
            out += b"\x00\x00\x01\xfe" + struct.pack("<BBBBII", 0, fps, 160 // 8, 120 // 8, date, len(f)) + f
        elif _is_key(f):
            out += b"\x00\x00\x01\xfc" + struct.pack("<BBBBII", codec, fps, 160 // 8, 120 // 8, date, len(f)) + f
        else:
            out += b"\x00\x00\x01\xfd" + struct.pack("<I", len(f)) + f
        if i % 4 == 0:
            out += b"\x00\x00\x01\xfa\x0e\x02" + struct.pack("<H", 160) + b"\xd5" * 160
    return bytes(out)


def build_hxvs(frames, times_ms, tag=b"HXVS"):
    """HiSilicon: 16-byte HXVS/HXVT header, one HXVF per NAL unit, HXAF audio, HXFI index at the end."""
    out = bytearray(tag + struct.pack("<III", 160, 120, 0))
    t0 = 1_234_567
    index = []
    for f, t in zip(frames, times_ms):
        key = _is_key(f)
        nals = [f[m.start():] for m in re.finditer(rb"\x00\x00\x00\x01", f)]
        nals = [n[: n.find(b"\x00\x00\x00\x01", 4)] if n.find(b"\x00\x00\x00\x01", 4) > 0 else n for n in nals]
        for nal in nals:
            if key:
                index.append((len(out), int(t)))
            out += b"HXVF" + struct.pack("<III", len(nal), t0 + int(t), 1 if key else 0) + nal
        out += b"HXAF" + struct.pack("<III", 4 + 160, t0 + int(t), 0) + b"\x00\x01\xa0\x00" + b"\xd5" * 160
    idx = b"".join(struct.pack("<II", o, t) for o, t in index)
    out += b"HXFI" + struct.pack("<III", len(idx), int(times_ms[-1]), 0) + idx
    return bytes(out)


def wrap_unknown(es: bytes, codec: str, marker=b"QVR1") -> bytes:
    """An undocumented container: a vendor header before every frame or start-code unit."""
    if codec == "mjpeg":
        cuts = [a for a, _ in formats._jpeg_frames(es)]
    else:
        cuts = [m.start() for m in re.finditer(rb"\x00\x00\x01", es)]
        if codec in ("h264", "hevc"):  # MPEG-1/2/4 data may itself end in a zero byte
            cuts = [c - 1 if c and es[c - 1] == 0 else c for c in cuts]
    cuts = cuts + [len(es)]
    out = bytearray(marker + b"\x10" * 60)
    for a, b in zip(cuts, cuts[1:]):
        out += b"DHAV" + bytes(range(20)) + es[a:b] + b"dhav" + b"\x11\x22\x33\x44"
    return bytes(out)


def _convert(wb, case, path, method, **kw):
    ev = ingest(wb, case, path)
    return ev, wb.convert_evidence(ev["id"], method, EXAMINER, **kw)


# Dahua DAV ---------------------------------------------------------------------------------------------


def test_dahua_parse_keeps_frames_and_recorder_timing(wb, case, h264, tmp_path):
    es, aus = h264
    times = [i * 40.0 for i in range(N)]
    times[12:] = [t + 360 for t in times[12:]]  # 9 frames missing at frame 12
    p = tmp_path / "ch01_20260314.dav"
    p.write_bytes(build_dhav(aus, times))
    ident = formats.sniff(p.read_bytes()[:65536])
    assert ident["family"] == "dahua_dav" and ident["parser"]
    ev, conv = _convert(wb, case, p, "parse")
    assert conv["media_type"] == "video" and conv["probe"]["codec"] == "h264" and conv["probe"]["frame_count"] == N
    rec = conv["conversion"]
    assert rec["timing"]["recorder_clock_start"] == "2026-03-14T09:26:50"
    assert rec["timing"]["gap_count"] == 1 and rec["timing"]["gaps"][0] == {"frame": 12, "gap_ms": 400.0}
    assert rec["timing"]["measured_frame_rate"] == 25.0 and rec["timing"]["recorder_frame_rate"] == 25.0
    assert rec["parser_stats"]["video_records_per_channel"] == {"0": N, "1": 9} and rec["parser_stats"]["audio_records"] == 5
    assert rec["notes"] and "channel 0" in rec["notes"][0]
    # Every picture decodes exactly as the recorder's stream does.
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, "h264")
    table = wb.frame_table(conv["id"])
    assert [f["time"] for f in table[10:14]] == [0.4, 0.44, 0.84, 0.88]
    assert table[12]["recorder_time"] == "2026-03-14T09:26:50.840" and not table[0].get("synthetic_time")
    text = report.conversion_text(conv, {ev["id"]: ev["filename"]})
    assert "parse_dhav read 25 H264 frames" in text and "Timing gaps: 1" in text and "channel 0" in text
    # Reproducible: the same original gives the same bytes.
    assert wb.convert_evidence(ev["id"], "parse", EXAMINER)["sha256"] == conv["sha256"]


def test_dahua_hevc(wb, case, hevc, tmp_path):
    es, aus = hevc
    p = tmp_path / "hevc.dav"
    p.write_bytes(build_dhav(aus, [i * 40.0 for i in range(N)], codec_byte=0xC, extra_channel=False))
    _, conv = _convert(wb, case, p, "parse")
    assert conv["probe"]["codec"] == "hevc" and conv["probe"]["frame_count"] == N
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, "hevc")


def test_dahua_timestamp_going_backwards_is_reported(h264):
    _, aus = h264
    times = [i * 40.0 for i in range(N)]
    times[7] = 100.0  # recorder clock steps back
    parsed = dvr.parse_dhav(build_dhav(aus, times, extra_channel=False))
    t, summary = dvr.settle_timing(parsed, 25)
    assert all(b > a for a, b in zip(t, t[1:]))
    assert summary["timestamp_anomaly_count"] == 1 and summary["timestamp_anomalies"][0]["frame"] == 7


def test_dahua_survives_damaged_records(h264):
    _, aus = h264
    data = bytearray(build_dhav(aus, [i * 40.0 for i in range(N)], extra_channel=False))
    cut = data.find(b"DHAV\xfc", 2000)
    data[cut + 12:cut + 16] = b"\x01\x00\x00\x00"  # corrupt one P-frame record's length
    parsed = dvr.parse_dhav(bytes(data))
    assert parsed.stats["resyncs"] >= 1 and len(parsed.frames) == N - 1


# Xiongmai / XMEye ------------------------------------------------------------------------------------


def test_xmeye_parse(wb, case, h264, tmp_path):
    es, aus = h264
    p = tmp_path / "2026-05-02.h264"
    p.write_bytes(build_xm(aus))
    ident = formats.sniff(p.read_bytes()[:65536])
    assert ident["family"] == "xmeye" and ident["parser"]
    _, conv = _convert(wb, case, p, "parse")
    rec = conv["conversion"]
    assert conv["probe"]["frame_count"] == N and rec["codec"] == "h264"
    assert rec["timing"]["recorder_clock_start"] == "2026-05-02T23:59:58" and rec["timing"]["key_frames"] == 3
    assert rec["parser_stats"]["audio_records"] == 7 and rec["parser_stats"]["records_skipped_bytes"] == 20
    assert rec["width"] == 160 and rec["height"] == 120 and not rec["parser_stats"]["clock_jumps"]
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, "h264")


def test_xmeye_clock_jump(h264):
    _, aus = h264
    data = build_xm(aus, start=(2026, 5, 2, 10, 20, 30))
    # Move the third key frame's date forward by a minute.
    pos = [m.start() for m in re.finditer(rb"\x00\x00\x01\xfc", data)][2]
    date = struct.unpack_from("<I", data, pos + 8)[0]
    data = data[:pos + 8] + struct.pack("<I", date + (1 << 6)) + data[pos + 12:]
    parsed = dvr.parse_xm(data)
    assert parsed.stats["clock_jumps"][0]["frame"] == 20 and parsed.notes


def test_xmeye_motion_jpeg(wb, case, tmp_path):
    mj = _encode(tmp_path / "a.mjpeg", "mjpeg", "mjpeg", "-q:v", "5")
    jpegs = [mj[a:b] for a, b in formats._jpeg_frames(mj)]
    assert len(jpegs) == N
    p = tmp_path / "snap.h264"
    p.write_bytes(build_xm(jpegs, fps=10, jpeg=True))
    _, conv = _convert(wb, case, p, "parse")
    assert conv["filename"].endswith(".mkv") and conv["probe"]["codec"] == "mjpeg" and conv["probe"]["frame_count"] == N
    assert conv["conversion"]["timing"]["measured_frame_rate"] == 10.0
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(mj, tmp_path, "mjpeg")


# HiSilicon HXVS ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["h264", "hevc"])
def test_hxvs_parse(wb, case, h264, hevc, tmp_path, which):
    es, aus = h264 if which == "h264" else hevc
    times = [i * 66.0 for i in range(N)]  # 15 fps camera
    p = tmp_path / ("rec.264" if which == "h264" else "rec.265")
    p.write_bytes(build_hxvs(aus, times, b"HXVS" if which == "h264" else b"HXVT"))
    assert formats.sniff(p.read_bytes()[:65536])["family"] == "hxvs"
    _, conv = _convert(wb, case, p, "parse")
    assert conv["probe"]["codec"] == which and conv["probe"]["frame_count"] == N
    assert conv["conversion"]["timing"]["median_frame_interval_ms"] == 66.0
    assert conv["conversion"]["parser_stats"]["audio_records"] == N
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, which)
    assert wb.frame_table(conv["id"])[3]["time"] == 0.198


# Hikvision / MPEG program stream ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["vob", "mpeg"])  # MPEG-2 and MPEG-1 program streams
def test_hikvision_parse(wb, case, tmp_path, fmt):
    ps = _encode(tmp_path / "a.mpg", "libx264", fmt, "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p")
    imkh = b"IMKH" + bytes([1, 2, 0, 0, 2, 0, 0x01, 0x10, 0x71, 0x01, 0x10, 0, 0x40, 0x1F, 0, 0, 0, 0xFA, 0, 0]) + b"\x00" * 16
    # Hikvision interleaves private-stream packets with vendor metadata.
    priv = b"\x00\x00\x01\xbd" + struct.pack(">H", 20) + b"HKMI" + b"\x00" * 16
    body = ps.replace(b"\x00\x00\x01\xba", priv + b"\x00\x00\x01\xba")
    p = tmp_path / "ch01_00000000001000100.mp4"
    p.write_bytes(imkh + body[len(priv):])
    ident = formats.sniff(p.read_bytes()[:65536])
    assert ident["family"] == "hikvision" and ident["parser"]
    _, conv = _convert(wb, case, p, "parse")
    rec = conv["conversion"]
    assert conv["probe"]["frame_count"] == N and rec["codec"] == "h264" and rec["parser_stats"]["private_stream_packets"] > 0
    assert rec["timing"]["measured_frame_rate"] == 25.0 and rec["timing"]["gap_count"] == 0
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(tmp_path / "a.mpg", tmp_path)


# Carving older codecs out of unknown containers ----------------------------------------------------------------


@pytest.mark.parametrize("codec,fmt,demux,ext", [("mpeg4", "m4v", "m4v", "m4v"),
                                                 ("mpeg2video", "mpeg2video", "mpegvideo", "m2v"),
                                                 ("mjpeg", "mjpeg", "mjpeg", "mkv")])
def test_carve_older_codecs(wb, case, tmp_path, monkeypatch, codec, fmt, demux, ext):
    es = _encode(tmp_path / f"a.{fmt}", codec, fmt, "-q:v", "4")
    p = tmp_path / "export.dvr"
    p.write_bytes(wrap_unknown(es, codec))
    assert formats.guess_codec(p.read_bytes()) == codec
    real_probe = media.probe

    def probe(path, settings):  # stand in for a container FFmpeg cannot open
        if path.suffix == ".dvr":
            raise media.MediaError("Invalid data found when processing input")
        return real_probe(path, settings)

    monkeypatch.setattr(media, "probe", probe)
    ev, conv = _convert(wb, case, p, "carve", frame_rate=12.5)
    assert ev["media_type"] == "container" and ev["probe"]["identified"]["elementary_codec"] == codec
    assert conv["filename"].endswith(f".{ext}") and conv["probe"]["frame_count"] == N
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, demux)
    assert wb.frame_table(conv["id"])[5]["time"] == 0.4
    assert "synthetic at 12.5 fps" in report.conversion_text(conv, {})


def test_ts_writer_crc():
    # CRC-32/MPEG-2 check value for "123456789".
    assert dvr._crc32_mpeg(b"123456789") == 0x0376E6E7


def test_parse_refused_without_parser(wb, tmp_path):
    p = tmp_path / "x.264"
    p.write_bytes(b"4HKH" + b"\x00" * 60 + b"\x00\x00\x00\x01\x67" + b"\x11" * 3000)
    assert formats.sniff(p.read_bytes())["family"] == "hikvision_old" and not formats.sniff(p.read_bytes())["parser"]
    with pytest.raises(Exception, match="no dedicated parser"):
        formats.convert(p, tmp_path / "out", "parse", wb.settings)


# AVI with vendor FourCCs ---------------------------------------------------------------------------------------


def _chunk(cid: bytes, body: bytes) -> bytes:
    return cid + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")


def _list(kind: bytes, body: bytes) -> bytes:
    return b"LIST" + struct.pack("<I", len(body) + 4) + kind + body


def build_avi(frames, fourcc=b"HKVI", rate=(25, 1), prefix=b"", drop_at=None):
    """A DVR-style AVI: valid RIFF structure, a FourCC no decoder knows, optional per-frame vendor header,
    and an empty chunk (a dropped frame) at `drop_at`."""
    scale, r = rate[1], rate[0]
    avih = struct.pack("<IIIIIIIIII", int(1e6 * scale / r), 0, 0, 0x10, len(frames), 0, 2, 0, 160, 120) + b"\x00" * 16
    strh = b"vids" + fourcc + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, scale, r, 0, len(frames), 0, 0, 0, 0, 0, 160, 120)
    strf = struct.pack("<IiiHH", 40, 160, 120, 1, 24) + fourcc + struct.pack("<IiiII", 160 * 120 * 3, 0, 0, 0, 0)
    auds = b"auds" + b"\x00" * 4 + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, 1, 8000, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    hdrl = _list(b"hdrl", _chunk(b"avih", avih) + _list(b"strl", _chunk(b"strh", strh) + _chunk(b"strf", strf))
                 + _list(b"strl", _chunk(b"strh", auds) + _chunk(b"strf", struct.pack("<HHIIHH", 6, 1, 8000, 8000, 1, 8))))
    movi = bytearray()
    idx = bytearray()
    for i, f in enumerate(frames):
        if i == drop_at:
            movi += _chunk(b"00dc", b"")
        off = len(movi) + 4
        movi += _chunk(b"00dc", prefix + f)
        idx += b"00dc" + struct.pack("<III", 0x10 if _is_key(f) else 0, off, len(prefix + f))
        if i % 5 == 0:
            movi += _chunk(b"01wb", b"\xd5" * 320)
    body = b"AVI " + hdrl + _chunk(b"JUNK", b"\x00" * 30) + _list(b"movi", bytes(movi)) + _chunk(b"idx1", bytes(idx))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_avi_vendor_fourcc(wb, case, h264, tmp_path):
    es, aus = h264
    p = tmp_path / "CH01.avi"
    p.write_bytes(build_avi(aus, prefix=b"HKVI" + bytes(range(12)), drop_at=12))
    with pytest.raises(media.MediaError):
        media.probe(p, wb.settings)  # FFmpeg cannot decode it as stored
    ev, conv = _convert(wb, case, p, "parse")
    assert ev["media_type"] == "container" and ev["probe"]["identified"]["family"] == "avi"
    rec = conv["conversion"]
    assert conv["probe"]["codec"] == "h264" and conv["probe"]["frame_count"] == N
    assert rec["parser_stats"]["fourcc_handler"] == "HKVI" and rec["parser_stats"]["frames_with_vendor_prefix"] == N
    assert rec["parser_stats"]["dropped_frame_chunks"] == 1 and rec["timing"]["gaps"] == [{"frame": 12, "gap_ms": 80.0}]
    assert rec["timing"]["recorder_frame_rate"] == 25.0
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, "h264")


def test_avi_geovision_mpeg4(wb, case, tmp_path):
    m4v = _encode(tmp_path / "a.m4v", "mpeg4", "m4v", "-q:v", "4", "-g", "10")
    aus = [m4v[a:b] for a, b in dvr.access_units(m4v, "mpeg4")]
    assert len(aus) == N
    p = tmp_path / "geo.avi"
    p.write_bytes(build_avi(aus, fourcc=b"GMP4", rate=(30000, 1001)))
    parsed = dvr.parse_avi(p.read_bytes())
    assert parsed.codec == "mpeg4" and parsed.frame_rate == 29.97003 and parsed.stats["fourcc_handler"] == "GMP4"
    _, conv = _convert(wb, case, p, "parse")
    assert conv["probe"]["codec"] == "mpeg4" and conv["probe"]["frame_count"] == N
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(m4v, tmp_path, "m4v")


# Transport streams with vendor stream types ------------------------------------------------------------------------


def _patch_pmt_stream_type(ts: bytes, new_type: int) -> bytes:
    out = bytearray(ts)
    for i in range(0, len(out), 188):
        pkt = out[i:i + 188]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == 0x1000 and pkt[1] & 0x40:
            p = 4 + 1 + pkt[4]
            sec = p
            slen = ((pkt[sec + 1] & 0x0F) << 8) | pkt[sec + 2]
            pil = ((pkt[sec + 10] & 0x0F) << 8) | pkt[sec + 11]
            out[i + sec + 12 + pil] = new_type
            crc = dvr._crc32_mpeg(bytes(out[i + sec:i + sec + 3 + slen - 4]))
            out[i + sec + 3 + slen - 4:i + sec + 3 + slen] = struct.pack(">I", crc)
    return bytes(out)


def test_ts_private_stream_type_and_lost_packets(wb, case, h264, tmp_path):
    es, _ = h264
    ts = _encode(tmp_path / "a.ts", "libx264", "mpegts", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p")
    vendor = _patch_pmt_stream_type(ts, 0x80)  # as some recorders declare H.264
    p = tmp_path / "ch2.ts"
    p.write_bytes(vendor)
    ev, conv = _convert(wb, case, p, "parse")
    rec = conv["conversion"]
    assert ev["probe"]["identified"]["family"] == "mpeg_ts"
    assert conv["probe"]["frame_count"] == N and rec["parser_stats"]["declared_stream_type"] == "0x80"
    assert rec["parser_stats"]["lost_packets"] == 0 and rec["timing"]["measured_frame_rate"] == 25.0
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, "h264")
    # A capture with packets missing: the loss is counted and reported.
    pkts = [vendor[i:i + 188] for i in range(0, len(vendor), 188)]
    vid = [k for k, q in enumerate(pkts) if ((q[1] & 0x1F) << 8 | q[2]) == 0x100]
    del pkts[vid[40]]
    parsed = dvr.parse_ts(b"".join(pkts))
    assert parsed.stats["lost_packets"] == 1 and any("missing" in n for n in parsed.notes)


def test_m2ts_192_byte_packets(h264, tmp_path):
    ts = _encode(tmp_path / "a.ts", "libx264", "mpegts", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p")
    m2ts = b"".join(struct.pack(">I", k * 1000) + ts[i:i + 188] for k, i in enumerate(range(0, len(ts), 188)))
    assert formats.sniff(m2ts[:65536])["family"] == "mpeg_ts"
    parsed = dvr.parse_ts(m2ts)
    assert parsed.stats["packet_size"] == 192 and len(parsed.frames) == N


# Network captures (RTSP / RTP) -------------------------------------------------------------------------------------


def _rtp_packets(aus, codec, ssrc=0x1234ABCD, pt=96, mtu=400, seq0=65530):
    """RFC 6184 / 7798 packetization: parameter sets aggregated, large NAL units fragmented."""
    pkts = []
    seq = seq0
    for i, au in enumerate(aus):
        ts = (4_000_000_000 + i * 3600) & 0xFFFFFFFF  # wraps during the clip
        nals = [n for n in re.split(rb"\x00\x00\x00\x01|\x00\x00\x01", au) if n]
        params = [n for n in nals if (n[0] & 0x1F if codec == "h264" else (n[0] >> 1) & 0x3F) in (7, 8, 32, 33, 34)]
        rest = [n for n in nals if n not in params]
        payloads = []
        if params:
            if codec == "h264":
                payloads.append(bytes([0x78]) + b"".join(struct.pack(">H", len(n)) + n for n in params))
            else:
                payloads.append(bytes([48 << 1, 1]) + b"".join(struct.pack(">H", len(n)) + n for n in params))
        for n in rest:
            if len(n) <= mtu:
                payloads.append(n)
                continue
            hdr = 1 if codec == "h264" else 2
            body = n[hdr:]
            chunks = [body[k:k + mtu] for k in range(0, len(body), mtu)]
            for k, c in enumerate(chunks):
                s, e = (0x80 if k == 0 else 0), (0x40 if k == len(chunks) - 1 else 0)
                if codec == "h264":
                    payloads.append(bytes([(n[0] & 0xE0) | 28, s | e | (n[0] & 0x1F)]) + c)
                else:
                    t = (n[0] >> 1) & 0x3F
                    payloads.append(bytes([(n[0] & 0x81) | (49 << 1), n[1], s | e | t]) + c)
        for k, pl in enumerate(payloads):
            marker = 0x80 if k == len(payloads) - 1 else 0
            pkts.append((i, struct.pack(">BBHII", 0x80, marker | pt, seq & 0xFFFF, ts, ssrc) + pl))
            seq += 1
    return pkts


def _udp_frame(payload, sport=5004, dport=50000):
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0, bytes([10, 0, 0, 2]), bytes([10, 0, 0, 1]))
    return b"\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00" + ip + udp


def _tcp_frame(payload, seq, sport=554, dport=50001):
    tcp = struct.pack(">HHIIBBHHH", sport, dport, seq, 0, 0x50, 0x18, 65535, 0, 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0x4000, 64, 6, 0, bytes([10, 0, 0, 2]), bytes([10, 0, 0, 1]))
    return b"\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x81\x00\x00\x05\x08\x00" + ip + tcp  # with a VLAN tag


T0 = 1_773_480_410.25  # 2026-03-14 09:26:50.25 UTC


def build_pcap(frames_with_times):
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for t, frame in frames_with_times:
        out += struct.pack("<IIII", int(t), int(round((t % 1) * 1e6)), len(frame), len(frame)) + frame
    return bytes(out)


def build_pcapng(frames_with_times):
    def block(btype, body):
        body += b"\x00" * (-len(body) % 4)
        return struct.pack("<II", btype, len(body) + 12) + body + struct.pack("<I", len(body) + 12)

    out = bytearray(block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)))
    out += block(1, struct.pack("<HHI", 1, 0, 65535) + struct.pack("<HHB", 9, 1, 9) + b"\x00" * 3 + b"\x00" * 4)  # ns clock
    for t, frame in frames_with_times:
        ns = int(round(t * 1e9))
        out += block(6, struct.pack("<IIIII", 0, ns >> 32, ns & 0xFFFFFFFF, len(frame), len(frame)) + frame)
    return bytes(out)


@pytest.mark.parametrize("which", ["h264", "hevc"])
def test_pcap_rtp_udp(wb, case, h264, hevc, tmp_path, which):
    es, aus = h264 if which == "h264" else hevc
    pkts = _rtp_packets(aus, which)
    noise = struct.pack(">BBHII", 0x80, 0x80 | 8, 1, 0, 0x55) + b"\xd5" * 160  # an audio RTP stream
    frames = []
    for k, (i, pkt) in enumerate(pkts):
        frames.append((T0 + i * 0.04 + k * 1e-5, _udp_frame(pkt)))
        if k % 7 == 0:
            frames.append((T0 + i * 0.04, _udp_frame(noise, 5006, 50002)))
    frames.insert(5, frames.pop(8))  # out-of-order arrival
    p = tmp_path / "camera.pcap"
    p.write_bytes(build_pcap(frames))
    ident = formats.sniff(p.read_bytes()[:65536])
    assert ident["family"] == "pcap" and ident["parser"]
    ev, conv = _convert(wb, case, p, "parse")
    rec = conv["conversion"]
    assert ev["media_type"] == "container" and conv["probe"]["codec"] == which and conv["probe"]["frame_count"] == N
    assert rec["parser_stats"]["rtp_streams"] == 2 and rec["parser_stats"]["lost_rtp_packets"] == 0
    assert rec["timing"]["median_frame_interval_ms"] == 40.0 and rec["timing"]["recorder_clock_start"].startswith("2026-03-14T09:26:50.25")
    assert _framemd5(wb.evidence_path(conv["id"]), tmp_path) == _framemd5(es, tmp_path, which)


def test_pcapng_rtsp_interleaved_tcp_with_loss(h264):
    _, aus = h264
    pkts = _rtp_packets(aus, "h264", pt=97)
    sdp = b"RTSP/1.0 200 OK\r\nContent-Type: application/sdp\r\n\r\nm=video 0 RTP/AVP 97\r\na=rtpmap:97 H264/90000\r\n"
    stream = bytearray(sdp)
    for k, (_, pkt) in enumerate(pkts):
        if k == 30:
            continue  # one RTP packet lost before the capture point
        stream += b"$\x00" + struct.pack(">H", len(pkt)) + pkt
    segs = [bytes(stream[k:k + 1400]) for k in range(0, len(stream), 1400)]
    frames = [(T0 + k * 0.01, _tcp_frame(seg, 1000 + 1400 * k)) for k, seg in enumerate(segs)]
    frames.insert(3, frames[2])  # a TCP retransmission
    parsed = dvr.parse_pcap(build_pcapng(frames))
    assert parsed.codec == "h264" and parsed.stats["sdp_rtpmap"] == {"97": "H264"}
    assert parsed.stats["lost_rtp_packets"] == 1 and any("missing" in n for n in parsed.notes)
    assert N - 1 <= len(parsed.frames) <= N
    assert parsed.clock_start.isoformat().startswith("2026-03-14T09:26:50.25")


def test_pcap_ps_over_rtp(tmp_path):
    ps = _encode(tmp_path / "a.vob", "libx264", "vob", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p")
    pkts, seq = [], 0
    for k in range(0, len(ps), 1200):
        pkts.append(struct.pack(">BBHII", 0x80, 96, seq, k * 10, 0xBEEF) + ps[k:k + 1200])
        seq += 1
    parsed = dvr.parse_pcap(build_pcap([(T0 + i * 0.001, _udp_frame(q)) for i, q in enumerate(pkts)]))
    assert parsed.codec == "h264" and len(parsed.frames) == N
    times, timing = dvr.settle_timing(parsed, 25)
    assert timing["measured_frame_rate"] == 25.0 and "MPEG-PS" in timing["source"]


def test_closed_format_hint(wb, case, tmp_path):
    p = tmp_path / "export.g64"
    p.write_bytes(b"\x01\x02GENETEC" + bytes(range(256)) * 20)
    with pytest.raises(media.MediaError, match="Genetec Security Desk"):
        ingest(wb, case, p)
