"""JPEG file structure: marker walk, quantisation tables, quality estimate.

The parser reads the file as untrusted input: every length is bounds-checked and
the walk stops at the first inconsistency rather than guessing.
"""

from __future__ import annotations

import numpy as np

MARKER_NAMES = {
    0xD8: "SOI", 0xD9: "EOI", 0xDA: "SOS", 0xDB: "DQT", 0xC4: "DHT", 0xDD: "DRI", 0xFE: "COM",
    0xC0: "SOF0 (baseline)", 0xC1: "SOF1 (extended)", 0xC2: "SOF2 (progressive)", 0xC3: "SOF3 (lossless)",
    0xC5: "SOF5", 0xC6: "SOF6", 0xC7: "SOF7", 0xC9: "SOF9", 0xCA: "SOF10", 0xCB: "SOF11",
    0xCD: "SOF13", 0xCE: "SOF14", 0xCF: "SOF15", 0xCC: "DAC",
}
for _n in range(16):
    MARKER_NAMES[0xE0 + _n] = f"APP{_n}"
for _n in range(8):
    MARKER_NAMES[0xD0 + _n] = f"RST{_n}"

# Zig-zag order: position k in the DQT segment -> (row, col) in the 8x8 table.
ZIGZAG = np.array([
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
])

# ITU-T T.81 Annex K example tables (natural order), the base of libjpeg's quality scaling.
STD_LUMA = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
]).reshape(8, 8)
STD_CHROMA = np.array([
    17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
]).reshape(8, 8)


def ijg_table(base: np.ndarray, quality: int) -> np.ndarray:
    q = max(1, min(100, quality))
    scale = 5000 // q if q < 50 else 200 - 2 * q
    return np.clip((base * scale + 50) // 100, 1, 255)


def is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def parse_markers(data: bytes) -> dict:
    """Walk the marker segments up to the first scan, then locate EOI.

    Returns markers (name, offset, length, extra), DQT tables (natural order),
    frame info and structural warnings.
    """
    markers: list[dict] = []
    tables: dict[int, list[list[int]]] = {}
    frame: dict | None = None
    warnings: list[str] = []
    if not is_jpeg(data):
        return {"markers": [], "dqt": {}, "frame": None, "warnings": ["not a JPEG file"], "trailing_bytes": 0}
    markers.append({"name": "SOI", "offset": 0, "length": 0})
    pos = 2
    n = len(data)
    scans = 0
    while pos < n:
        if data[pos] != 0xFF:
            warnings.append(f"expected a marker at offset {pos}; structure is irregular")
            break
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        code = data[pos]
        start = pos - 1
        pos += 1
        name = MARKER_NAMES.get(code, f"0xFF{code:02X}")
        if code == 0xD9:
            markers.append({"name": "EOI", "offset": start, "length": 0})
            pos = start + 2
            break
        if 0xD0 <= code <= 0xD7 or code == 0x01:
            markers.append({"name": name, "offset": start, "length": 0})
            continue
        if pos + 2 > n:
            warnings.append(f"truncated segment {name} at offset {start}")
            break
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > n:
            warnings.append(f"segment {name} at offset {start} has an invalid length {length}")
            break
        seg = data[pos + 2 : pos + length]
        entry = {"name": name, "offset": start, "length": length}
        if 0xE0 <= code <= 0xEF:
            ident = seg[:32].split(b"\x00", 1)[0]
            entry["identifier"] = ident.decode("latin-1", errors="replace")
        elif code == 0xFE:
            entry["comment"] = seg[:2000].decode("utf-8", errors="replace")
        elif code == 0xDB:
            _parse_dqt(seg, tables, warnings, start)
        elif code in (0xC0, 0xC1, 0xC2, 0xC3) and len(seg) >= 6:
            comps = seg[5]
            frame = {
                "type": name,
                "precision": seg[0],
                "height": int.from_bytes(seg[1:3], "big"),
                "width": int.from_bytes(seg[3:5], "big"),
                "components": [
                    {"id": seg[6 + 3 * i], "h": seg[7 + 3 * i] >> 4, "v": seg[7 + 3 * i] & 15, "table": seg[8 + 3 * i]}
                    for i in range(comps)
                    if 8 + 3 * i < len(seg)
                ],
            }
            entry["extra"] = f"{frame['width']}x{frame['height']}, {comps} components"
        markers.append(entry)
        pos += length
        if code == 0xDA:
            scans += 1
            # Skip entropy-coded data: stop at the next marker that is not a stuffed byte or RST.
            while pos < n - 1:
                if data[pos] == 0xFF and data[pos + 1] not in (0x00,) and not (0xD0 <= data[pos + 1] <= 0xD7):
                    break
                pos += 1
    trailing = max(0, n - pos)
    if trailing:
        warnings.append(f"{trailing} bytes after the end-of-image marker")
    if frame and frame["components"]:
        frame["subsampling"] = _subsampling_label(frame["components"])
    return {
        "markers": markers,
        "dqt": {str(k): v for k, v in sorted(tables.items())},
        "frame": frame,
        "scans": scans,
        "warnings": warnings,
        "trailing_bytes": trailing,
    }


def _subsampling_label(comps: list[dict]) -> str:
    if len(comps) < 3:
        return "greyscale"
    y = comps[0]
    if y["h"] == 2 and y["v"] == 2:
        return "4:2:0"
    if y["h"] == 2 and y["v"] == 1:
        return "4:2:2"
    if y["h"] == 1 and y["v"] == 1:
        return "4:4:4"
    return f"{y['h']}x{y['v']}"


def _parse_dqt(seg: bytes, tables: dict, warnings: list, offset: int) -> None:
    i = 0
    while i < len(seg):
        pq, tq = seg[i] >> 4, seg[i] & 15
        size = 128 if pq else 64
        vals = seg[i + 1 : i + 1 + size]
        if len(vals) < size:
            warnings.append(f"truncated quantisation table at offset {offset}")
            return
        if pq:
            zz = [int.from_bytes(vals[2 * k : 2 * k + 2], "big") for k in range(64)]
        else:
            zz = list(vals)
        natural = np.zeros(64, dtype=int)
        natural[ZIGZAG] = zz
        tables[tq] = natural.reshape(8, 8).tolist()
        i += 1 + size


def estimate_quality(dqt: dict) -> dict:
    """Compare the tables with libjpeg (IJG) scaling of the standard tables.

    An exact match at some quality is consistent with libjpeg-family encoders at that
    setting; a non-match points to another encoder (camera firmware, editing software,
    platform re-compression), which the DQT database will identify in a later phase.
    """
    if not dqt:
        return {"standard_ijg_match": None, "estimated_quality": None}
    luma = np.array(dqt.get("0") or next(iter(dqt.values())))
    chroma = np.array(dqt["1"]) if "1" in dqt else None
    exact = None
    best_q, best_err = None, None
    for q in range(1, 101):
        tl = ijg_table(STD_LUMA, q)
        err = float(np.abs(tl - luma).mean())
        if chroma is not None:
            err = (err + float(np.abs(ijg_table(STD_CHROMA, q) - chroma).mean())) / 2
        if best_err is None or err < best_err:
            best_q, best_err = q, err
        if err == 0 and exact is None:
            exact = q
    return {
        "standard_ijg_match": exact,
        "estimated_quality": best_q,
        "mean_abs_difference": round(best_err or 0.0, 3),
    }
