"""Rebuild forensic/data/qtables.json, the built-in quantisation-table database.

Run from backend/ with network access (never on the evidence machine):

    python tools/build_qtable_db.py

Only the first 384 KB of each file is downloaded (the tables sit before the image data).
Each source is pinned to a commit so the build is reproducible. Only the tables and the
EXIF Make, Model and Software fields are kept; no image content is stored.
"""

from __future__ import annotations

import concurrent.futures as cf
import http.client
import json
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forensic.qtables import BUILTIN_PATH, entry_from_jpeg, table_key  # noqa: E402

SOURCES = {
    "digicam-corpus": {
        "name": "digicam corpus (Drew Noakes and contributors): camera originals at every quality setting",
        "repo": "drewnoakes/digicam_corpus", "commit": "6385e5eacf2dd242ff71e5828ff56ed691bfdc77",
        "licence": "CC0 1.0 Universal",
    },
    "metadata-extractor-images": {
        "name": "metadata-extractor sample images (Drew Noakes and contributors)",
        "repo": "drewnoakes/metadata-extractor-images", "commit": "651ad0e67aa8d43d358ad05f9bc07b52d8b9ac6e",
        "licence": "README: 'You are free to use these media files however you wish.'",
        "skip_prefixes": ("fuzzing/", "jpg/ImageTestSuite/"),
    },
}


def list_files(repo: str, commit: str) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"https://github.com/{repo}.git", d], check=True)
        out = subprocess.run(["git", "-C", d, "ls-tree", "-r", "--name-only", commit], check=True, capture_output=True, text=True)
    return out.stdout.splitlines()


def head(repo: str, commit: str, path: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/" + urllib.parse.quote(path)
    req = urllib.request.Request(url, headers={"Range": "bytes=0-393215"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except (OSError, http.client.HTTPException):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return b""


def main() -> None:
    entries, seen = [], {}
    for sid, s in SOURCES.items():
        files = [p for p in list_files(s["repo"], s["commit"])
                 if p.lower().endswith((".jpg", ".jpeg")) and not p.startswith(s.get("skip_prefixes", ()))]
        with cf.ThreadPoolExecutor(8) as ex:
            heads = list(ex.map(lambda p: head(s["repo"], s["commit"], p), files))
        for p, data in zip(files, heads):
            e = entry_from_jpeg(data)
            if e is None:
                continue
            key = (table_key(e["luma"], e["chroma"]), e.get("camera") or e.get("software"))
            if key in seen:
                seen[key]["files"].append(p)
                continue
            seen[key] = dict(e, source=sid, file=p, files=[p])
            entries.append(seen[key])
        print(f"{sid}: {len(files)} files", file=sys.stderr)
    entries.sort(key=lambda e: (e["kind"], (e.get("camera") or e.get("software") or "").lower(), e["file"]))
    db = {"built": date.today().isoformat(), "builder": "tools/build_qtable_db.py",
          "sources": {k: {kk: v for kk, v in s.items() if kk != "skip_prefixes"} for k, s in SOURCES.items()},
          "entries": entries}
    BUILTIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUILTIN_PATH.write_text(json.dumps(db, separators=(",", ":")) + "\n")
    print(f"{len(entries)} entries written to {BUILTIN_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
