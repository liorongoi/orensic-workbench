"""Capture time of a still image, in UTC, and the local time shown to the examiner.

Order of sources (the one used is recorded with the time):
1. an examiner-entered time;
2. EXIF DateTimeOriginal (or DateTime), converted to UTC with OffsetTimeOriginal when present;
3. a date and time in the file name (20240131_235959, 2024-01-31 23-59-59, 2024-01-31T23:59:59 ...).

Without an offset tag the camera clock is taken as UTC, which is how most traffic and CCTV cameras
are set; the local time is then UTC + the hours the examiner entered for the camera. Both the
assumption and the offset are printed next to every time in exports and reports.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

from PIL import Image

_NAME = re.compile(r"(?<!\d)(20\d\d|19\d\d)[-_.]?(\d\d)[-_.]?(\d\d)[ _T-]?(\d\d)[-_.:h]?(\d\d)[-_.:m]?(\d\d)(?!\d)")


def from_exif(data: bytes) -> tuple[datetime, str] | None:
    try:
        with Image.open(io.BytesIO(data)) as im:
            ex = im.getexif()
            sub = ex.get_ifd(0x8769)
    except Exception:  # noqa: BLE001 - not an image Pillow can read: no EXIF time
        return None
    raw = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)
    if not raw:
        return None
    try:
        dt = datetime.strptime(str(raw).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    off = sub.get(0x9011) or sub.get(0x9010)
    if off:
        m = re.match(r"([+-])(\d\d):?(\d\d)", str(off).strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3))) * sign
            return (dt - delta).replace(tzinfo=timezone.utc), f"exif (offset {off} applied)"
    return dt.replace(tzinfo=timezone.utc), "exif (camera clock taken as UTC)"


def from_name(name: str) -> tuple[datetime, str] | None:
    m = _NAME.search(name)
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt, "file name (taken as UTC)"


def capture_time(data: bytes, name: str) -> tuple[str | None, str]:
    for found in (from_exif(data), from_name(name)):
        if found:
            return found[0].isoformat(timespec="seconds"), found[1]
    return None, "unknown"


def local(utc_iso: str | None, offset_hours: float) -> str | None:
    if not utc_iso:
        return None
    dt = datetime.fromisoformat(utc_iso)
    tz = timezone(timedelta(hours=offset_hours))
    return dt.astimezone(tz).isoformat(timespec="seconds")


def parse_user_time(value: str, offset_hours: float, is_local: bool) -> str:
    try:
        dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError:
        raise ValueError("give the time as YYYY-MM-DD HH:MM:SS") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=offset_hours)) if is_local else timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
