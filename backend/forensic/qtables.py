"""Camera and software quantisation-table database.

A JPEG encoder writes its quantisation tables (DQT) into every file. Camera firmware
uses its own tables, often several per model (one per quality setting), while software
encoders use theirs; libjpeg (IJG) scales the ITU-T T.81 Annex K tables by quality.
Matching the evidence's tables against tables recorded from known devices tells which
encoders could have written the file: a match with the camera named in EXIF is
consistent with an original, and a mismatch with every table recorded for that camera
points to re-saving by something else (Farid 2006; Kee, Johnson and Farid 2011).

Three layers, all offline:

* built in: tables recorded from freely licensed sample images (``data/qtables.json``,
  rebuilt by ``tools/build_qtable_db.py``; sources and licences are listed in the file);
* generated: libjpeg at qualities 1-100, computed here rather than stored;
* lab: tables a lab adds from its own reference images with
  ``python -m forensic.qtables add <folder> --source "<name>"``, kept in the data folder.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from .analysis.jpeg import STD_CHROMA, STD_LUMA, ijg_table, is_jpeg, parse_markers

BUILTIN_PATH = Path(__file__).parent / "data" / "qtables.json"
LAB_FILE = "qtables_lab.json"

# Software that re-encodes: a file whose Software tag names one of these did not keep the camera's tables.
EDITOR_RE = re.compile(
    r"photoshop|lightroom|gimp|picasa|paint|acdsee|acd systems|irfanview|capture nx|digital photo professional|"
    r"noise ninja|affinity|snapseed|picsart|photos? \d|windows photo|imagemagick|graphicconverter|xnview|"
    r"faststone|darktable|rawtherapee|capture one|dxo|aperture|iphoto|photoscape|corel|pixelmator|luminar|"
    r"silkypix|stereophoto|photomatix|fixfoto|zoombrowser|photostudio|facebook|instagram|whatsapp",
    re.I,
)


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\x00", "")).strip()


def camera_name(make: str, model: str) -> str:
    """'Canon' + 'Canon EOS 5D' -> 'Canon EOS 5D'; 'NIKON CORPORATION' + 'NIKON D90' -> 'NIKON D90'."""
    make, model = norm(make), norm(model)
    first = make.split(" ")[0].lower() if make else ""
    if first and model.lower().startswith(first):
        return model
    return f"{make} {model}".strip()


def model_key(make: str, model: str) -> str:
    return re.sub(r"[^a-z0-9]", "", camera_name(make, model).lower())


def file_tables(info: dict) -> tuple[list[int], list[int] | None] | None:
    """(luma, chroma) in natural order from a parse_markers result: the tables the frame's
    first and second components use. None when there is no table."""
    dqt = info.get("dqt") or {}
    if not dqt:
        return None
    comps = (info.get("frame") or {}).get("components") or []
    ids = [str(c["table"]) for c in comps] or sorted(dqt)
    luma = dqt.get(ids[0]) or next(iter(dqt.values()))
    chroma = dqt.get(ids[1]) if len(ids) > 1 else None
    flat = lambda t: [int(v) for row in t for v in row]  # noqa: E731
    return flat(luma), (flat(chroma) if chroma is not None else None)


def table_key(luma: list[int], chroma: list[int] | None) -> str:
    b = bytes(np.asarray(luma, np.uint16).tobytes()) + (bytes(np.asarray(chroma, np.uint16).tobytes()) if chroma else b"")
    return hashlib.sha256(b).hexdigest()[:24]


def entry_from_jpeg(data: bytes) -> dict | None:
    """A database entry (tables plus who wrote them) from a JPEG file or just its header.

    Only the bytes up to the first scan are needed. Files without Make/Model, or whose
    Software tag names an editor, are recorded as software entries when a Software tag is
    present and skipped otherwise.
    """
    if not is_jpeg(data):
        return None
    t = file_tables(parse_markers(data))
    if t is None:
        return None
    make = model = software = ""
    try:
        with Image.open(io.BytesIO(data)) as im:
            ex = im.getexif()
            make, model = norm(str(ex.get(0x010F, ""))), norm(str(ex.get(0x0110, "")))
            software = norm(str(ex.get(0x0131, "")))
    except Exception:  # noqa: BLE001 - headers of hostile or truncated files
        pass
    luma, chroma = t
    if (make or model) and not EDITOR_RE.search(software):
        return {"kind": "camera", "camera": camera_name(make, model), "make": make, "model": model,
                "firmware": software, "luma": luma, "chroma": chroma}
    if software:
        return {"kind": "software", "software": software, "luma": luma, "chroma": chroma}
    return None


def _ijg_entries() -> list[dict]:
    out = []
    for q in range(1, 101):
        luma = ijg_table(STD_LUMA, q).ravel().tolist()
        chroma = ijg_table(STD_CHROMA, q).ravel().tolist()
        for c in (chroma, None):
            out.append({"kind": "software", "software": f"libjpeg (IJG) standard tables, quality {q}",
                        "luma": luma, "chroma": c, "source": "generated"})
    return out


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"sources": {}, "entries": []}


@lru_cache(maxsize=4)
def _load(lab_path: str | None, lab_mtime: float) -> dict:
    builtin = _read(BUILTIN_PATH)
    lab = _read(Path(lab_path)) if lab_path else {"sources": {}, "entries": []}
    entries = [dict(e, layer="built-in") for e in builtin["entries"]] + _ijg_entries()
    entries += [dict(e, layer="lab") for e in lab["entries"]]
    index: dict[str, list[dict]] = {}
    for e in entries:
        index.setdefault(table_key(e["luma"], e["chroma"]), []).append(e)
    models: dict[str, set[str]] = {}
    for k, es in index.items():
        for e in es:
            if e["kind"] == "camera":
                models.setdefault(model_key(e["make"], e["model"]), set()).add(k)
    return {"index": index, "models": models, "sources": {**builtin["sources"], **lab["sources"]},
            "built": builtin.get("built"), "entries": entries}


def load(data_dir: Path | None = None) -> dict:
    lab = Path(data_dir) / LAB_FILE if data_dir else None
    mtime = lab.stat().st_mtime if lab and lab.exists() else 0.0
    return _load(str(lab) if lab else None, mtime)


def stats(db: dict) -> dict:
    cams = {e["camera"] for e in db["entries"] if e["kind"] == "camera"}
    return {"tables": len(db["index"]), "camera_models": len(cams),
            "camera_tables": sum(1 for es in db["index"].values() if any(e["kind"] == "camera" for e in es))}


def match(db: dict, luma: list[int], chroma: list[int] | None) -> dict:
    """Exact matches, grouped into cameras and software, plus the nearest camera tables."""
    hits = db["index"].get(table_key(luma, chroma), [])
    cameras = sorted({e["camera"] for e in hits if e["kind"] == "camera"})
    software = sorted({e["software"] for e in hits if e["kind"] == "software"})
    L = np.asarray(luma, float)
    near = []
    for k, es in db["index"].items():
        cam = [e for e in es if e["kind"] == "camera"]
        if not cam or k == table_key(luma, chroma):
            continue
        d = float(np.abs(np.asarray(cam[0]["luma"], float) - L).mean())
        near.append((d, cam[0]["camera"]))
    near.sort()
    return {"cameras": cameras, "software": software,
            "nearest_cameras": [{"camera": c, "mean_abs_difference": round(d, 2)} for d, c in near[:3]],
            "evidence": [{"layer": e.get("layer", "generated"), "source": e.get("source"), "file": e.get("file")}
                         for e in hits[:20]]}


def known_tables_for(db: dict, make: str, model: str) -> set[str]:
    return db["models"].get(model_key(make, model), set())


def add_folder(folder: Path, out: Path, source: str, licence: str = "lab reference images") -> dict:
    """Record the tables of every JPEG under `folder` into the lab database at `out`."""
    db = _read(out)
    have = {(table_key(e["luma"], e["chroma"]), e.get("camera") or e.get("software")): e for e in db["entries"]}
    sid = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-") or "lab"
    db["sources"][sid] = {"name": source, "licence": licence}
    added = skipped = 0
    for p in sorted(folder.rglob("*")):
        if p.suffix.lower() not in (".jpg", ".jpeg") or not p.is_file():
            continue
        with p.open("rb") as f:
            e = entry_from_jpeg(f.read(1 << 20))
        if e is None:
            skipped += 1
            continue
        key = (table_key(e["luma"], e["chroma"]), e.get("camera") or e.get("software"))
        rel = str(p.relative_to(folder))
        if key in have:
            have[key].setdefault("files", []).append(rel)
            continue
        have[key] = dict(e, source=sid, file=rel, files=[rel])
        db["entries"].append(have[key])
        added += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(db, separators=(",", ":")))
    return {"added": added, "skipped": skipped, "entries": len(db["entries"])}


def main(argv: list[str] | None = None) -> None:
    import argparse

    from .config import load_settings

    ap = argparse.ArgumentParser(prog="python -m forensic.qtables", description="Quantisation-table database.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add the tables of every JPEG in a folder of reference images")
    a.add_argument("folder", type=Path)
    a.add_argument("--source", required=True, help="name of the collection, shown with each match")
    a.add_argument("--licence", default="lab reference images")
    sub.add_parser("stats", help="show what the database holds")
    args = ap.parse_args(argv)
    settings = load_settings()
    if args.cmd == "add":
        print(json.dumps(add_folder(args.folder, settings.data_dir / LAB_FILE, args.source, args.licence)))
    else:
        print(json.dumps(stats(load(settings.data_dir))))


if __name__ == "__main__":
    main()
