"""Charts described in plain words.

The request is read with a small fixed grammar, and the reply says exactly how it was understood
(and which words were ignored), so a chart in a report can be traced back to its wording:

    <measure> by <dimension> [and <dimension>] [as bar|line|pie|stacked] [for <filters>]

measures:   detections (default) · images · false alarms · missed (added by hand) · corrections
dimensions: hour · date · weekday · camera · lighting · status · class
filters:    day · night · twilight · night_ir · approved · edited · detected · camera <label> ·
            from YYYY-MM-DD · to YYYY-MM-DD

Examples: "detections by hour and lighting as stacked", "images by status as pie",
"false alarms by camera for night", "detections by date for camera North gate from 2024-05-01".
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime

from ..audit import Actor

MEASURES = OrderedDict([("false alarms", "false_alarms"), ("false alarm", "false_alarms"), ("missed", "missed"),
                        ("corrections", "corrections"), ("detections", "detections"), ("vehicles", "detections"),
                        ("images", "images")])
DIMS = {"hour": "hour", "hours": "hour", "date": "date", "day of week": "weekday", "weekday": "weekday", "camera": "camera",
        "cameras": "camera", "lighting": "lighting", "status": "status", "class": "class", "classes": "class", "type": "class",
        "days": "date"}
TYPES = ("bar", "line", "pie", "stacked")
LIGHTS = ("night_ir", "twilight", "night", "day")
STATUSES = ("approved", "edited", "detected", "pending")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse(text: str, camera_labels: list[str]) -> dict:
    t = " " + re.sub(r"\s+", " ", text.strip().lower()) + " "
    used: list[str] = []
    spec: dict = {"measure": "detections", "dims": [], "type": "bar", "lighting": None, "status": None, "camera": None,
                  "from": None, "to": None}
    for word, m in MEASURES.items():
        if f" {word} " in t:
            spec["measure"] = m
            used.append(word)
            break
    for m in re.finditer(r"\b(from|since|after)\s+(\d{4}-\d\d-\d\d)", t):
        spec["from"] = m.group(2)
        used.append(m.group(0).strip())
    for m in re.finditer(r"\b(to|until|before)\s+(\d{4}-\d\d-\d\d)", t):
        spec["to"] = m.group(2)
        used.append(m.group(0).strip())
    for lab in sorted(camera_labels, key=len, reverse=True):
        if lab and re.search(r"\bcamera\s+" + re.escape(lab.lower()) + r"\b", t):
            spec["camera"] = lab
            used.append(f"camera {lab.lower()}")
            t = re.sub(r"\bcamera\s+" + re.escape(lab.lower()) + r"\b", " ", t)
            break
    m = re.search(r"\b(?:by|per|over)\s+([a-z ]+?)(?:\s+(?:and|vs|per|by)\s+([a-z ]+?))?(?=\s+(?:as|for|in|from|to|since|until|with)\b|\s*$)",
                  t.strip())
    if m:
        for g in m.groups():
            if not g:
                continue
            g = g.strip()
            key = next((DIMS[k] for k in sorted(DIMS, key=len, reverse=True) if g == k or g.endswith(" " + k) or g.startswith(k)), None)
            if key and key not in spec["dims"]:
                spec["dims"].append(key)
                used.append(g)
    for ty in TYPES:
        if re.search(rf"\b(as|in)?\s*(a\s+)?{ty}\b", t):
            spec["type"] = ty
            used.append(ty)
            break
    for lt in LIGHTS:
        if re.search(rf"\b{lt}(time)?\b", t) and lt not in spec["dims"]:
            spec["lighting"] = lt
            used.append(lt)
            break
    for st in STATUSES:
        if re.search(rf"\b{st}\b(?! by)", t):
            spec["status"] = st
            used.append(st)
            break
    if not spec["dims"]:
        spec["dims"] = ["date"]
    if spec["type"] == "pie" and len(spec["dims"]) > 1:
        spec["dims"] = spec["dims"][:1]
    known = set(" ".join(used).split()) | {"by", "and", "as", "for", "per", "in", "a", "the", "of", "only", "chart", "graph",
                                           "show", "from", "to", "over", "vs", "with", "camera", "time"}
    spec["ignored"] = sorted({w for w in re.findall(r"[a-z_]+", t) if w not in known})
    return spec


def chart(wb, case_id: str, text: str, actor: Actor) -> dict:
    T = wb.traffic
    images = T.images(case_id, actor)
    cams = {c["id"]: c["label"] for c in wb.sync.cameras(case_id)}
    spec = parse(text, list(cams.values()))
    cam_id = next((k for k, v in cams.items() if v == spec["camera"]), None)
    rows: list[dict] = []
    for im in images:
        if spec["lighting"] and im["lighting"] != spec["lighting"]:
            continue
        if spec["status"] and im["status"] != spec["status"]:
            continue
        if cam_id and im["camera_id"] != cam_id:
            continue
        lt = im["local_time"]
        day = lt[:10] if lt else None
        if spec["from"] and (not day or day < spec["from"]):
            continue
        if spec["to"] and (not day or day > spec["to"]):
            continue
        base = {"hour": int(lt[11:13]) if lt else None, "date": day,
                "weekday": WEEKDAYS[datetime.fromisoformat(lt).weekday()] if lt else None,
                "camera": cams.get(im["camera_id"], "?"), "lighting": im["lighting"] or "unknown", "status": im["status"]}
        if spec["measure"] == "images":
            rows.append({**base, "class": None, "value": 1})
        elif spec["measure"] == "detections":
            dets = T.detections(im["evidence_id"])
            if "class" in spec["dims"]:
                rows += [{**base, "class": d["cls"], "value": 1} for d in dets]
            else:
                rows.append({**base, "class": None, "value": len(dets)})
        else:
            kinds = {"false_alarms": ("false_alarm",), "missed": ("missed",),
                     "corrections": ("false_alarm", "missed", "box_edit", "class_edit", "restored")}[spec["measure"]]
            q = ",".join("?" * len(kinds))
            fb = wb.db.all(f"SELECT cls FROM traffic_feedback WHERE evidence_id = ? AND kind IN ({q})", (im["evidence_id"], *kinds))
            rows += [{**base, "class": f["cls"], "value": 1} for f in fb]
    d1 = spec["dims"][0]
    d2 = spec["dims"][1] if len(spec["dims"]) > 1 else None
    xs = _order(d1, {r[d1] for r in rows if r[d1] is not None})
    series_names = _order(d2, {r[d2] for r in rows if r[d2] is not None}) if d2 else [_measure_label(spec["measure"])]
    data = {s: [0] * len(xs) for s in series_names}
    for r in rows:
        if r[d1] is None or (d2 and r[d2] is None):
            continue
        data[r[d2] if d2 else series_names[0]][xs.index(r[d1])] += r["value"]
    title = f"{_measure_label(spec['measure'])} by {d1}" + (f" and {d2}" if d2 else "")
    filters = [f for f in (spec["lighting"], spec["status"], spec["camera"] and f"camera {spec['camera']}",
                           spec["from"] and f"from {spec['from']}", spec["to"] and f"to {spec['to']}") if f]
    if filters:
        title += " (" + ", ".join(filters) + ")"
    missing_time = sum(1 for im in images if not im["local_time"]) if d1 in ("hour", "date", "weekday") else 0
    return {"query": text, "spec": spec, "title": title, "x_label": d1, "y_label": _measure_label(spec["measure"]),
            "x": [str(x) for x in xs], "series": [{"name": str(s), "values": data[s]} for s in series_names],
            "notes": ([f"{missing_time} images have no capture time and are not on a time axis"] if missing_time else []) +
                     ([f"words not understood: {', '.join(spec['ignored'])}"] if spec["ignored"] else []),
            "local_time_note": "hours and dates are local time (UTC + each camera's offset)"}


def _order(dim: str | None, values: set) -> list:
    if dim == "hour":
        return list(range(0, 24)) if values else []
    if dim == "weekday":
        return [d for d in WEEKDAYS if d in values]
    if dim == "lighting":
        return [x for x in ("day", "twilight", "night", "night_ir", "unknown") if x in values]
    if dim == "status":
        return [x for x in ("pending", "detected", "edited", "approved") if x in values]
    return sorted(values, key=str)


def _measure_label(m: str) -> str:
    return {"detections": "Detections", "images": "Images", "false_alarms": "False alarms", "missed": "Missed (added by hand)",
            "corrections": "Corrections"}[m]
