import argparse
import json
import sys
from pathlib import Path

from . import ModelError, install, lab_dir, manifest, public, verify_all

ap = argparse.ArgumentParser(prog="python -m forensic.models", description="Pinned model registry")
sub = ap.add_subparsers(dest="cmd", required=True)
sub.add_parser("list", help="list bundled and lab models")
sub.add_parser("verify", help="check every model's weights against its pinned SHA-256")
i = sub.add_parser("install", help="install a lab model (ONNX file plus model card)")
i.add_argument("onnx", type=Path)
i.add_argument("--card", type=Path, required=True)
a = ap.parse_args()

if a.cmd == "list":
    for m in manifest():
        print(f"{m['id']:<24} {m['kind']:<14} {'bundled' if m['bundled'] else 'lab':<8} {m['licence']:<12} {m['sha256'][:16]}…  {m['task']}")
    print(f"\nLab models folder: {lab_dir()}")
elif a.cmd == "verify":
    res = verify_all()
    print(json.dumps(res, indent=2))
    sys.exit(0 if all(r["ok"] for r in res) else 1)
else:
    try:
        print(json.dumps(public(install(a.onnx, a.card)), indent=2))
    except ModelError as e:
        sys.exit(str(e))
