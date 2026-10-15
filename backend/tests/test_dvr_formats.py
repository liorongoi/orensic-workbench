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
                          "-i", str(src), "-map", "0:v:0", "-f", "framemd5", "-"], capture_output=True, check=True).stdout
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
