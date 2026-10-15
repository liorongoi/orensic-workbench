"""Video in network captures: RTP streams (RTSP from cameras and recorders) in pcap / pcapng files.

When a recorder cannot export, or its export format is closed, labs capture the camera's or
recorder's RTSP stream with Wireshark or tcpdump. This module reads such a capture without any
third-party library:

* pcap (microsecond and nanosecond) and pcapng (section, interface and enhanced/simple packet blocks);
* Ethernet (with VLAN tags), Linux cooked capture v1/v2, raw IP and BSD loopback link layers; IPv4 and IPv6;
* RTP over UDP, and RTP interleaved in the RTSP TCP connection (`$` framing, RFC 2326 10.12);
* H.264 (RFC 6184: single NAL, STAP-A, FU-A), H.265 (RFC 7798: single NAL, AP, FU) and
  MPEG program stream over RTP (as Hikvision and GB/T 28181 devices send it).

Frames are timed by the RTP timestamp (90 kHz); the capture clock of the first frame's packet gives
the wall-clock start. Lost RTP packets (sequence number gaps) are counted and reported.
"""

from __future__ import annotations

import datetime as dt
import re
import struct

from .dvr import Parsed, ParseError, frames_from_es, parse_ps


def _packets(data: bytes):
    """Yield (capture time in seconds, link type, frame bytes) from a pcap or pcapng file."""
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        e = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
        div = 1e9 if magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1e6
        link = struct.unpack_from(e + "I", data, 20)[0] & 0x0FFFFFFF
        pos = 24
        while pos + 16 <= len(data):
            sec, frac, incl, _ = struct.unpack_from(e + "IIII", data, pos)
            yield sec + frac / div, link, data[pos + 16:pos + 16 + incl]
            pos += 16 + incl
        return
    if magic != b"\x0a\x0d\x0d\x0a":
        raise ParseError("not a pcap or pcapng capture")
    pos = 0
    e = "<"
    ifaces: list[tuple[int, float]] = []
    while pos + 12 <= len(data):
        if data[pos:pos + 4] == b"\x0a\x0d\x0d\x0a":
            e = "<" if data[pos + 8:pos + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        btype, blen = struct.unpack_from(e + "II", data, pos)
        if blen < 12 or pos + blen > len(data):
            break
        body = data[pos + 8:pos + blen - 4]
        if btype == 1:  # interface description: link type, then options (if_tsresol = 9)
            link = struct.unpack_from(e + "H", body, 0)[0]
            res = 1e-6
            q = 8
            while q + 4 <= len(body):
                code, ln = struct.unpack_from(e + "HH", body, q)
                if code == 0:
                    break
                if code == 9 and ln >= 1:
                    v = body[q + 4]
                    res = 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
                q += 4 + ln + (-ln % 4)
            ifaces.append((link, res))
        elif btype == 6 and ifaces:  # enhanced packet
            iface, hi, lo, cap = struct.unpack_from(e + "IIII", body, 0)
            link, res = ifaces[iface] if iface < len(ifaces) else ifaces[0]
            yield ((hi << 32) | lo) * res, link, body[20:20 + cap]
        elif btype == 3 and ifaces:  # simple packet: no timestamp
            yield None, ifaces[0][0], body[4:]
        pos += blen


def _ip_payload(link: int, frame: bytes):
    """(protocol, src, dst, sport, dport, payload, tcp seq) or None."""
    if link == 1:  # Ethernet
        p, et = 14, struct.unpack_from(">H", frame, 12)[0] if len(frame) >= 14 else 0
        while et in (0x8100, 0x88A8) and len(frame) >= p + 4:
            et = struct.unpack_from(">H", frame, p + 2)[0]
            p += 4
    elif link == 113:  # Linux cooked v1
        p, et = 16, struct.unpack_from(">H", frame, 14)[0] if len(frame) >= 16 else 0
    elif link == 276:  # Linux cooked v2
        p, et = 20, struct.unpack_from(">H", frame, 0)[0] if len(frame) >= 20 else 0
    elif link in (101, 12, 14):  # raw IP
        p, et = 0, 0x0800 if frame[:1] and frame[0] >> 4 == 4 else 0x86DD
    elif link == 0:  # BSD loopback
        fam = struct.unpack_from("<I", frame, 0)[0] if len(frame) >= 4 else 0
        p, et = 4, 0x0800 if fam == 2 else 0x86DD
    else:
        return None
    if et == 0x0800 and len(frame) >= p + 20:
        ihl = (frame[p] & 0x0F) * 4
        proto = frame[p + 9]
        total = struct.unpack_from(">H", frame, p + 2)[0]
        if struct.unpack_from(">H", frame, p + 6)[0] & 0x3FFF:
            return None  # IP fragments are not reassembled
        src, dst = frame[p + 12:p + 16], frame[p + 16:p + 20]
        seg = frame[p + ihl:p + total] if total else frame[p + ihl:]
    elif et == 0x86DD and len(frame) >= p + 40:
        proto = frame[p + 6]
        src, dst = frame[p + 8:p + 24], frame[p + 24:p + 40]
        seg = frame[p + 40:p + 40 + struct.unpack_from(">H", frame, p + 4)[0]]
    else:
        return None
    if proto == 17 and len(seg) >= 8:
        sport, dport = struct.unpack_from(">HH", seg, 0)
        return 17, src, dst, sport, dport, seg[8:], None
    if proto == 6 and len(seg) >= 20:
        sport, dport, seq = struct.unpack_from(">HHI", seg, 0)
        off = (seg[12] >> 4) * 4
        return 6, src, dst, sport, dport, seg[off:], seq
    return None


def _rtp(pkt: bytes):
    """(payload type, marker, seq, timestamp, ssrc, payload) for an RTP v2 packet, else None."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    cc, ext, pad = pkt[0] & 0x0F, pkt[0] & 0x10, pkt[0] & 0x20
    pt, marker = pkt[1] & 0x7F, pkt[1] >> 7
    if 72 <= pt <= 79:  # RTCP
        return None
    seq, ts, ssrc = struct.unpack_from(">HII", pkt, 2)
    p = 12 + 4 * cc
    if ext:
        if len(pkt) < p + 4:
            return None
        p += 4 + 4 * struct.unpack_from(">H", pkt, p + 2)[0]
    end = len(pkt) - (pkt[-1] if pad and pkt else 0)
    if p > end:
        return None
    return pt, marker, seq, ts, ssrc, pkt[p:end]


def _tcp_streams(segments):
    """Reassemble each TCP direction by sequence number (retransmissions and overlaps dropped)."""
    flows: dict = {}
    for key, seq, payload in segments:
        flows.setdefault(key, []).append((seq, payload))
    for key, segs in flows.items():
        segs.sort(key=lambda s: s[0])
        base = segs[0][0]
        buf = bytearray()
        for seq, payload in segs:
            off = (seq - base) & 0xFFFFFFFF
            if off >= len(buf):
                buf += b"\x00" * (off - len(buf)) + payload  # a gap stays as zeros; RTP framing resyncs
            elif off + len(payload) > len(buf):
                buf += payload[len(buf) - off:]
        yield key, bytes(buf)


def _interleaved(stream: bytes):
    """RTP packets carried in an RTSP connection: '$', channel, 16-bit length, packet."""
    pos = 0
    while True:
        pos = stream.find(b"$", pos)
        if pos < 0 or pos + 4 > len(stream):
            return
        ch, ln = stream[pos + 1], struct.unpack_from(">H", stream, pos + 2)[0]
        pkt = stream[pos + 4:pos + 4 + ln]
        if len(pkt) == ln and pkt[:1] and pkt[0] >> 6 == 2:
            yield ch, pkt
            pos += 4 + ln
        else:
            pos += 1


def _depacketize(codec: str, payloads: list[bytes]) -> tuple[bytes, int]:
    """Annex B bytes for one frame's RTP payloads; returns (bytes, malformed payload count)."""
    out = bytearray()
    bad = 0
    for pl in payloads:
        if not pl:
            continue
        if codec == "h264":
            t = pl[0] & 0x1F
            if 1 <= t <= 23:
                out += b"\x00\x00\x00\x01" + pl
            elif t == 24:  # STAP-A
                q = 1
                while q + 2 <= len(pl):
                    n = struct.unpack_from(">H", pl, q)[0]
                    out += b"\x00\x00\x00\x01" + pl[q + 2:q + 2 + n]
                    q += 2 + n
            elif t == 28 and len(pl) > 2:  # FU-A
                if pl[1] & 0x80:
                    out += b"\x00\x00\x00\x01" + bytes([(pl[0] & 0xE0) | (pl[1] & 0x1F)])
                out += pl[2:]
            else:
                bad += 1
        else:
            if len(pl) < 3:
                bad += 1
                continue
            t = (pl[0] >> 1) & 0x3F
            if t < 48:
                out += b"\x00\x00\x00\x01" + pl
            elif t == 48:  # aggregation packet
                q = 2
                while q + 2 <= len(pl):
                    n = struct.unpack_from(">H", pl, q)[0]
                    out += b"\x00\x00\x00\x01" + pl[q + 2:q + 2 + n]
                    q += 2 + n
            elif t == 49:  # fragmentation unit
                if pl[2] & 0x80:
                    out += b"\x00\x00\x00\x01" + bytes([(pl[0] & 0x81) | ((pl[2] & 0x3F) << 1), pl[1]])
                out += pl[3:]
            else:
                bad += 1
    return bytes(out), bad


def _guess_rtp_codec(payloads: list[bytes], sdp: dict, pt: int) -> str | None:
    name = sdp.get(pt, "").upper()
    if name in ("H264", "H265", "MP2P", "PS"):
        return {"H264": "h264", "H265": "hevc"}.get(name, "ps")
    if any(p[:4] == b"\x00\x00\x01\xba" for p in payloads[:50]):
        return "ps"
    from .formats import guess_codec

    for codec in ("hevc", "h264"):
        es, bad = _depacketize(codec, payloads[:400])
        if not bad and guess_codec(es) == codec:
            return codec
    return None


def parse_pcap(data: bytes, frame_rate: float = 25.0) -> Parsed:
    udp: dict[int, list] = {}  # ssrc -> [(capture time, rtp tuple)]
    tcp_segments = []
    tcp_times: dict = {}
    sdp: dict[int, str] = {}
    packets = 0
    for t, link, frame in _packets(data):
        packets += 1
        ip = _ip_payload(link, frame)
        if not ip:
            continue
        proto, src, dst, sport, dport, payload, seq = ip
        if b"a=rtpmap:" in payload:
            for m in re.finditer(rb"a=rtpmap:(\d+) ([A-Za-z0-9.-]+)/", payload):
                sdp[int(m.group(1))] = m.group(2).decode()
        if proto == 17:
            r = _rtp(payload)
            if r:
                udp.setdefault(r[4], []).append((t, r))
        elif payload:
            key = (src, sport, dst, dport)
            tcp_segments.append((key, seq, payload))
            tcp_times.setdefault(key, t)
    for key, stream in _tcp_streams(tcp_segments):
        for _, pkt in _interleaved(stream):
            r = _rtp(pkt)
            if r:
                udp.setdefault(r[4], []).append((tcp_times.get(key), r))
    if not udp:
        raise ParseError(f"no RTP packets found among {packets} captured packets")
    # The video stream: the RTP stream with the most data that depacketizes as video.
    candidates = sorted(udp.items(), key=lambda kv: -sum(len(r[5]) for _, r in kv[1]))
    chosen = codec = None
    for ssrc, pkts in candidates:
        c = _guess_rtp_codec([r[5] for _, r in pkts], sdp, pkts[0][1][0])
        if c:
            chosen, codec = ssrc, c
            break
    if chosen is None:
        raise ParseError("the capture's RTP streams carry no H.264, H.265 or MPEG-PS video")
    pkts = udp[chosen]
    # Order by extended sequence number; drop duplicates; count losses.
    ext, last = [], None
    for t, r in pkts:
        # Extend the 16-bit sequence number relative to the previous packet (handles wrap and reordering).
        last = r[2] if last is None else last + ((r[2] - last + 32768) % 65536) - 32768
        ext.append((last, t, r))
    ext.sort(key=lambda x: x[0])
    uniq, seen = [], set()
    for e in ext:
        if e[0] not in seen:
            seen.add(e[0])
            uniq.append(e)
    lost = (uniq[-1][0] - uniq[0][0] + 1) - len(uniq)
    out = Parsed(codec=None if codec == "ps" else codec,
                 timing_source="RTP timestamps (90 kHz) written by the camera; wall clock from the capture",
                 clock_precision="capture clock of the first packet (network delay not removed)")
    first_t = next((t for _, t, _ in uniq if t is not None), None)
    if first_t is not None:
        out.clock_start = dt.datetime.fromtimestamp(first_t, dt.timezone.utc).replace(tzinfo=None)
    bad = 0
    if codec == "ps":
        res = parse_ps(b"".join(r[5] for _, _, r in uniq), frame_rate)
        res.timing_source, res.clock_start, res.clock_precision = out.timing_source.replace(
            "RTP timestamps", "PES timestamps in the MPEG-PS payload"), out.clock_start, out.clock_precision
        out = res
    else:
        # One frame per RTP timestamp, in arrival (sequence) order.
        es, stamps = bytearray(), []
        cur_ts, cur = None, []
        base_ts, wraps, prev = None, 0, None

        def flush():
            nonlocal bad
            if cur:
                b, nbad = _depacketize(codec, cur)
                bad += nbad
                stamps.append((len(es), len(b), cur_ts))
                es.extend(b)

        for _, _, r in uniq:
            ts = r[3]
            if prev is not None and ts < prev and prev - ts > 1 << 31:
                wraps += 1
            prev = ts
            ts += wraps << 32
            if base_ts is None:
                base_ts = ts
            if ts != cur_ts:
                flush()
                cur_ts, cur = ts, []
            cur.append(r[5])
        flush()
        frames_from_es(out, bytes(es), stamps, "RTP stream")
    out.stats = {**out.stats, "captured_packets": packets, "rtp_streams": len(udp), "video_ssrc": f"{chosen:#010x}",
                 "rtp_payload_type": pkts[0][1][0], "rtp_packets": len(uniq), "lost_rtp_packets": lost,
                 "malformed_payloads": bad, "sdp_rtpmap": {str(k): v for k, v in sdp.items()}}
    if lost:
        out.notes.append(f"{lost} RTP packets are missing from the capture (sequence gaps); frames around them may be damaged")
    if len(udp) > 1:
        out.notes.append(f"the capture holds {len(udp)} RTP streams; SSRC {chosen:#010x} was extracted")
    return out
