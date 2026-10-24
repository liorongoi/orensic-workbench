"""Command line for unattended work: batch conversion, verification, backup and restore.

    lucida profile                          hardware profile in use
    lucida batch scan DIR                   what each file looks like
    lucida batch run --input DIR --output DIR [--method auto] [--format copy|h264|ffv1]
                     [--segment SECONDS] [--case CASE_ID] [--workers N] [--json]
    lucida verify [--case CASE_ID]          re-hash originals and check the audit chain
    lucida backup --output FILE [--encrypt] back up the data folder (password from LUCIDA_BACKUP_PASSWORD)
    lucida restore FILE --target DIR        restore into an empty folder and verify everything

Exit codes: 0 everything succeeded, 1 partly succeeded (some files failed), 2 usage error,
3 everything failed (or a verification found a problem).

The data folder is FORENSIC_DATA_DIR (or --data-dir). The examiner name for the audit log is
--examiner (or FORENSIC_EXAMINER, else the login name).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys
import tarfile
import tempfile
from pathlib import Path

EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, EXIT_FAILED = 0, 1, 2, 3
MAGIC = b"LUCIDA-BACKUP-1\n"
ENC_MAGIC = b"LUCIDA-BACKUP-AESGCM-1\n"
CHUNK = 4 << 20
SKIP_DIRS = {"tmp", "cache", "__pycache__"}


def _actor(args):
    from .audit import Actor

    return Actor(args.examiner or os.environ.get("FORENSIC_EXAMINER") or getpass.getuser() or "examiner")


def _wb(args):
    from .config import load_settings
    from .workbench import Workbench

    return Workbench(load_settings(args.data_dir))


def _print(args, obj, text: str) -> None:
    print(json.dumps(obj, indent=2, default=str) if args.json else text)


# Commands ---------------------------------------------------------------------------------------------


def cmd_profile(args) -> int:
    from . import compute

    p = compute.hardware_profile()
    _print(args, p, f"{p['name']} ({p['reason']}): detector {p['detector']}, every {p['frame_step']} frame(s), "
                    f"{p['batch_workers']} parallel conversions")
    return EXIT_OK


def cmd_batch_scan(args) -> int:
    wb = _wb(args)
    files = wb.batch.scan(Path(args.dir), not args.flat)
    lines = []
    for f in files:
        d = f["detected"] or {}
        what = "skipped" if f["skip"] else (d.get("label") or d.get("closed_format") or "not recognised")
        lines.append(f"{f['relative']:<50} {f['size']:>12}  {what}")
    _print(args, files, "\n".join(lines) or "no files")
    return EXIT_OK


def cmd_batch_run(args) -> int:
    wb = _wb(args)
    actor = _actor(args)
    b = wb.batch.create(args.name or Path(args.input).name, actor, input_dir=args.input, output_dir=args.output,
                        method=args.method, output=args.format, frame_rate=args.frame_rate, segment_seconds=args.segment,
                        case_id=args.case, concurrency=args.workers, recursive=not args.flat, include=args.include)
    if not args.json:
        print(f"batch {b['id']}: {b['total']} files, {b['concurrency']} at a time -> {b['output_dir']}")
    wb.batch.start(b["id"], actor, wait=True)
    b = wb.batch.get(b["id"])
    c = b["counts"]
    lines = []
    for it in b["items"]:
        outs = ", ".join(o["file"] for o in it["outputs"] or [])
        lines.append(f"[{it['status']:>9}] {it['input_path']}" + (f" -> {outs}" if outs else "")
                     + (f"  ({it['error_class']}: {(it['error'] or '')[:160]})" if it["error_class"] else ""))
    lines.append(f"done {c['done']}, duplicates {c['duplicate']}, failed {c['failed']}; manifest {b['manifest_sha256']}")
    _print(args, b, "\n".join(lines))
    ok = c["done"] + c["duplicate"]
    if c["failed"] == 0:
        return EXIT_OK
    return EXIT_PARTIAL if ok else EXIT_FAILED


def cmd_verify(args) -> int:
    wb = _wb(args)
    actor = _actor(args)
    cases = [args.case] if args.case else [c["id"] for c in wb.list_cases()]
    results = {"audit": wb.audit.verify(), "cases": {}}
    bad = 0
    for cid in cases:
        r = wb.casework.verify_case(cid, actor)
        results["cases"][cid] = r
        bad += len(r["failed"])
    lines = [f"audit chain: {'intact' if results['audit']['ok'] else 'BROKEN at entry ' + str(results['audit'].get('broken_at'))}"
             f" ({results['audit']['entries']} entries)"]
    for cid, r in results["cases"].items():
        lines.append(f"case {cid}: {r['checked']} items, {len(r['failed'])} failed")
        lines += [f"  FAILED {f['filename']} ({f['evidence_id']})" for f in r["failed"]]
    _print(args, results, "\n".join(lines))
    return EXIT_OK if results["audit"]["ok"] and not bad else EXIT_FAILED


# Backup and restore ------------------------------------------------------------------------------------


def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())


def _encrypt_stream(src, dst, password: str) -> None:
    """AES-256-GCM in 4 MB chunks. Each chunk is authenticated with its index and whether it is the
    last one, so reordered, dropped or truncated chunks are detected."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, base = os.urandom(16), os.urandom(8)
    aes = AESGCM(_derive_key(password, salt))
    dst.write(ENC_MAGIC + salt + base)
    i = 0
    chunk = src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK)
        last = not nxt
        aad = struct.pack(">Q?", i, last)
        ct = aes.encrypt(base + struct.pack(">I", i), chunk, aad)
        dst.write(struct.pack(">I", len(ct)) + ct)
        if last:
            break
        chunk, i = nxt, i + 1


def _decrypt_stream(src, dst, password: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    head = src.read(len(ENC_MAGIC) + 24)
    if not head.startswith(ENC_MAGIC):
        raise ValueError("not an encrypted Lucida backup")
    salt, base = head[len(ENC_MAGIC):len(ENC_MAGIC) + 16], head[len(ENC_MAGIC) + 16:]
    aes = AESGCM(_derive_key(password, salt))
    i = 0
    while True:
        n = src.read(4)
        if len(n) < 4:
            raise ValueError("the backup is truncated")
        ct = src.read(struct.unpack(">I", n)[0])
        for last in (False, True):
            try:
                dst.write(aes.decrypt(base + struct.pack(">I", i), ct, struct.pack(">Q?", i, last)))
                break
            except InvalidTag:
                if last:
                    raise ValueError("wrong password, or the backup was altered") from None
        if last:
            if src.read(1):
                raise ValueError("data after the final chunk: the backup was altered")
            return
        i += 1


def _files(data_dir: Path):
    for p in sorted(data_dir.rglob("*")):
        rel = p.relative_to(data_dir)
        if any(part in SKIP_DIRS or part.startswith(".ingest-") or part.startswith("tmp") for part in rel.parts):
            continue
        if p.is_file() and not p.is_symlink() and not p.name.startswith("forensic.sqlite3"):
            yield p, rel


def cmd_backup(args) -> int:
    from .config import load_settings

    s = load_settings(args.data_dir)
    out = Path(args.output)
    if out.exists():
        print(f"{out} already exists; backups are never overwritten", file=sys.stderr)
        return EXIT_USAGE
    password = None
    if args.encrypt:
        password = os.environ.get("LUCIDA_BACKUP_PASSWORD") or getpass.getpass("Backup password: ")
        if len(password) < 12:
            print("use a password of at least 12 characters", file=sys.stderr)
            return EXIT_USAGE
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "forensic.sqlite3"
        # Consistent copy of the live database (SQLite online backup).
        with sqlite3.connect(str(s.db_path)) as live, sqlite3.connect(str(snap)) as copy:
            live.backup(copy)
        entries = []
        raw = Path(tmp) / "backup.tar"
        with tarfile.open(raw, "w") as tar:
            for p, rel in [(snap, Path("forensic.sqlite3")), *_files(s.data_dir)]:
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for c in iter(lambda: f.read(1 << 20), b""):
                        h.update(c)
                entries.append({"path": str(rel), "size": p.stat().st_size, "sha256": h.hexdigest()})
                info = tar.gettarinfo(str(p), arcname=f"data/{rel}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(p, "rb") as f:
                    tar.addfile(info, f)
            from . import __version__
            from .db import now_iso

            manifest = {"format": 1, "application": f"Lucida {__version__}", "created_at": now_iso(),
                        "source": str(s.data_dir), "files": entries, "encrypted": bool(password)}
            mj = json.dumps(manifest, indent=2, sort_keys=True).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(mj)
            tar.addfile(info, io.BytesIO(mj))
        digest = hashlib.sha256(mj).hexdigest()
        with open(raw, "rb") as src, open(out, "xb") as dst:
            if password:
                _encrypt_stream(src, dst, password)
            else:
                dst.write(MAGIC)
                for c in iter(lambda: src.read(1 << 20), b""):
                    dst.write(c)
    fh = hashlib.sha256(out.read_bytes()).hexdigest()
    Path(str(out) + ".sha256").write_text(f"{fh}  {out.name}\n")
    _print(args, {"output": str(out), "files": len(entries), "sha256": fh, "manifest_sha256": digest,
                  "encrypted": bool(password)},
           f"backed up {len(entries)} files to {out} ({'AES-256-GCM encrypted' if password else 'not encrypted'})\n"
           f"sha256 {fh}")
    return EXIT_OK


def cmd_restore(args) -> int:
    src, target = Path(args.file), Path(args.target)
    if target.exists() and any(target.iterdir()):
        print(f"{target} is not empty; restore only into an empty folder", file=sys.stderr)
        return EXIT_USAGE
    side = Path(str(src) + ".sha256")
    if side.exists() and side.read_text().split()[0] != hashlib.sha256(src.read_bytes()).hexdigest():
        print("the backup file does not match its .sha256", file=sys.stderr)
        return EXIT_FAILED
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "backup.tar"
        with open(src, "rb") as f:
            head = f.read(len(ENC_MAGIC))
            f.seek(0)
            try:
                with open(raw, "wb") as out:
                    if head.startswith(ENC_MAGIC):
                        password = os.environ.get("LUCIDA_BACKUP_PASSWORD") or getpass.getpass("Backup password: ")
                        _decrypt_stream(f, out, password)
                    elif f.read(len(MAGIC)) == MAGIC:
                        for c in iter(lambda: f.read(1 << 20), b""):
                            out.write(c)
                    else:
                        print("not a Lucida backup", file=sys.stderr)
                        return EXIT_USAGE
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return EXIT_FAILED
        target.mkdir(parents=True, exist_ok=True)
        problems = []
        with tarfile.open(raw) as tar:
            manifest = json.loads(tar.extractfile("manifest.json").read())
            want = {e["path"]: e for e in manifest["files"]}
            for m in tar.getmembers():
                if m.name == "manifest.json":
                    continue
                rel = Path(m.name).relative_to("data")
                if not m.isfile() or ".." in rel.parts or rel.is_absolute():
                    problems.append(f"unexpected entry {m.name}")
                    continue
                data = tar.extractfile(m).read()
                e = want.pop(str(rel), None)
                if not e or hashlib.sha256(data).hexdigest() != e["sha256"]:
                    problems.append(f"{rel} does not match the manifest")
                    continue
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                if rel.parts[0] == "originals":
                    os.chmod(dest, 0o444)
            problems += [f"{p} is missing from the backup" for p in want]
    # The restored copy must open and pass its own checks.
    from .audit import Actor

    args.data_dir = str(target)
    wb = _wb(args)
    audit = wb.audit.verify()
    if not audit["ok"]:
        problems.append(f"audit chain broken at entry {audit.get('broken_at')}")
    bad = []
    for c in wb.list_cases():
        for ev in wb.list_evidence(c["id"]):
            r = wb.store.verify(wb.evidence_path(ev["id"]), ev["sha256"])
            if not r["ok"]:
                bad.append(ev["filename"])
    problems += [f"evidence {b} fails its hash" for b in bad]
    wb.audit.append(Actor(args.examiner or os.environ.get("FORENSIC_EXAMINER") or getpass.getuser() or "examiner"),
                    "system.restore", params={"backup": src.name, "files": len(manifest["files"]), "problems": problems})
    _print(args, {"ok": not problems, "files": len(manifest["files"]), "problems": problems},
           f"restored {len(manifest['files'])} files into {target}; audit chain intact, " if not problems else
           "restore problems:\n" + "\n".join(problems))
    return EXIT_OK if not problems else EXIT_FAILED


# Entry point ---------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lucida", description="Lucida forensic workbench: command line")
    p.add_argument("--data-dir", default=None, help="data folder (default FORENSIC_DATA_DIR or ./data)")
    p.add_argument("--examiner", default=None, help="name recorded in the audit log")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile", help="show the hardware profile").set_defaults(fn=cmd_profile)
    b = sub.add_parser("batch", help="batch conversion").add_subparsers(dest="bcmd", required=True)
    s = b.add_parser("scan", help="identify the files in a folder")
    s.add_argument("dir")
    s.add_argument("--flat", action="store_true", help="do not look in sub-folders")
    s.set_defaults(fn=cmd_batch_scan)
    r = b.add_parser("run", help="convert every file in a folder")
    r.add_argument("--input", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--name")
    r.add_argument("--method", default="auto", choices=["auto", "parse", "remux", "carve"])
    r.add_argument("--format", default="copy", choices=["copy", "h264", "ffv1"])
    r.add_argument("--frame-rate", type=float, default=25.0, help="for carved streams with no timing")
    r.add_argument("--segment", type=float, default=None, help="split outputs into pieces of this many seconds")
    r.add_argument("--case", default=None, help="also add originals and outputs to this case")
    r.add_argument("--workers", type=int, default=None)
    r.add_argument("--include", default="*", help="file name pattern, e.g. '*.dav'")
    r.add_argument("--flat", action="store_true")
    r.set_defaults(fn=cmd_batch_run)
    v = sub.add_parser("verify", help="re-hash evidence and check the audit chain")
    v.add_argument("--case")
    v.set_defaults(fn=cmd_verify)
    bk = sub.add_parser("backup", help="back up the data folder")
    bk.add_argument("--output", required=True)
    bk.add_argument("--encrypt", action="store_true", help="AES-256-GCM; password from LUCIDA_BACKUP_PASSWORD or prompt")
    bk.set_defaults(fn=cmd_backup)
    rs = sub.add_parser("restore", help="restore a backup into an empty folder and verify it")
    rs.add_argument("file")
    rs.add_argument("--target", required=True)
    rs.set_defaults(fn=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        return args.fn(args)
    except (ValueError, FileNotFoundError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
