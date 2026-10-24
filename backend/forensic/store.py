"""Evidence store.

Originals are hashed (SHA-256 and MD5) while they are copied in, stored under
their SHA-256 and made read-only. Nothing in the app ever opens them for
writing again; all work happens on derived, content-addressed outputs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO

from .config import Settings

CHUNK = 1 << 20


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> tuple[str, str, int]:
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size


def hash_file_full(path: Path) -> tuple[str, str, int, str]:
    """SHA-256, MD5, size and SHA-512 in one read."""
    sha, md5, s512, size = hashlib.sha256(), hashlib.md5(), hashlib.sha512(), 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            sha.update(chunk)
            md5.update(chunk)
            s512.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size, s512.hexdigest()


class EvidenceStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ingest(self, src: BinaryIO, filename: str) -> dict:
        """Copy a stream into the write-once store. Returns hashes and the stored path."""
        sha, md5, sha512, size = hashlib.sha256(), hashlib.md5(), hashlib.sha512(), 0
        suffix = Path(filename).suffix.lower()[:12]
        fd, tmp_name = tempfile.mkstemp(dir=self.settings.originals_dir, prefix=".ingest-")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK):
                    sha.update(chunk)
                    md5.update(chunk)
                    sha512.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            digest = sha.hexdigest()
            dest = self.settings.originals_dir / digest[:2] / f"{digest}{suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                os.unlink(tmp_name)
            else:
                os.replace(tmp_name, dest)
                os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return {"sha256": digest, "md5": md5.hexdigest(), "sha512": sha512.hexdigest(), "size": size, "path": dest}

    def ingest_path(self, path: Path) -> dict:
        with open(path, "rb") as f:
            return self.ingest(f, path.name)

    def verify(self, stored_path: Path, sha256: str, sha512: str | None = None) -> dict:
        if not stored_path.exists():
            return {"ok": False, "sha256": None, "md5": None, "sha512": None, "size": 0, "read_only": False,
                    "missing": True}
        current, md5, size, cur512 = hash_file_full(stored_path)
        mode = stat.S_IMODE(os.stat(stored_path).st_mode)
        return {
            "ok": current == sha256 and (sha512 is None or cur512 == sha512),
            "sha256": current,
            "sha512": cur512,
            "md5": md5,
            "size": size,
            "read_only": not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)),
        }

    # Derived outputs -----------------------------------------------------

    def put_derived(self, data: bytes, ext: str) -> tuple[str, Path]:
        digest = hash_bytes(data)
        dest = self.settings.derived_dir / digest[:2] / f"{digest}.{ext}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        return digest, dest

    def derived_path(self, digest: str, ext: str) -> Path:
        return self.settings.derived_dir / digest[:2] / f"{digest}.{ext}"

    def export_copy(self, src: Path, dest: Path) -> None:
        shutil.copyfile(src, dest)
