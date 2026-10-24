"""Command line: exit codes, batch run, verify, backup and restore (plain and encrypted)."""

import json
import os
import subprocess

import pytest

from forensic import cli
from forensic.config import load_settings
from forensic.workbench import Workbench

from .conftest import EXAMINER, ingest


@pytest.fixture()
def data(tmp_path, jpeg_path):
    d = tmp_path / "data"
    wb = Workbench(load_settings(d))
    c = wb.create_case("CLI", "Examiner", EXAMINER)
    ev = ingest(wb, c, jpeg_path)
    wb.db.close()
    return d, c, ev


def run(*a):
    return cli.main(list(a))


def test_usage_errors():
    assert run() == cli.EXIT_USAGE
    assert run("batch", "run", "--input", "x") == cli.EXIT_USAGE


def test_profile(capsys):
    assert run("--json", "profile") == 0
    assert json.loads(capsys.readouterr().out)["name"] in ("light", "standard", "high")


def test_batch_exit_codes(tmp_path, data, capsys):
    d, _, _ = data
    good = tmp_path / "in_good"
    good.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=25", "-t", "0.4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(good / "a.mp4")], check=True)
    assert run("--data-dir", str(d), "batch", "run", "--input", str(good), "--output", str(tmp_path / "o1")) == cli.EXIT_OK
    (good / "junk.bin").write_bytes(os.urandom(3000))
    assert run("--data-dir", str(d), "batch", "run", "--input", str(good), "--output", str(tmp_path / "o2")) == cli.EXIT_PARTIAL
    bad = tmp_path / "in_bad"
    bad.mkdir()
    (bad / "junk.bin").write_bytes(os.urandom(3000))
    assert run("--data-dir", str(d), "batch", "run", "--input", str(bad), "--output", str(tmp_path / "o3")) == cli.EXIT_FAILED
    assert run("--data-dir", str(d), "batch", "run", "--input", str(bad), "--output", str(bad / "x")) == cli.EXIT_USAGE


def test_verify_detects_tamper(data):
    d, _, ev = data
    assert run("--data-dir", str(d), "verify") == cli.EXIT_OK
    p = next((d / "originals").rglob(f"{ev['sha256']}*"))
    os.chmod(p, 0o600)
    p.write_bytes(p.read_bytes() + b"x")
    assert run("--data-dir", str(d), "verify") == cli.EXIT_FAILED


@pytest.mark.parametrize("encrypt", [False, True])
def test_backup_restore_roundtrip(tmp_path, data, monkeypatch, encrypt):
    d, c, ev = data
    monkeypatch.setenv("LUCIDA_BACKUP_PASSWORD", "correct horse battery")
    out = tmp_path / "b.lucida"
    args = ["--data-dir", str(d), "backup", "--output", str(out)] + (["--encrypt"] if encrypt else [])
    assert run(*args) == 0
    assert run(*args) == cli.EXIT_USAGE  # never overwrites
    if encrypt:
        assert b"forensic.sqlite3" not in out.read_bytes()
    target = tmp_path / "restored"
    assert run("restore", str(out), "--target", str(target)) == 0
    wb = Workbench(load_settings(target))
    assert wb.get_evidence(ev["id"])["sha256"] == ev["sha256"]
    assert wb.verify_evidence(ev["id"], EXAMINER)["ok"] and wb.audit.verify()["ok"]
    wb.db.close()
    assert run("restore", str(out), "--target", str(target)) == cli.EXIT_USAGE  # not empty
    if encrypt:
        monkeypatch.setenv("LUCIDA_BACKUP_PASSWORD", "wrong password!!")
        assert run("restore", str(out), "--target", str(tmp_path / "r2")) == cli.EXIT_FAILED
        # A flipped byte is caught (the .sha256 sidecar first; without it, the GCM tag).
        data_ = bytearray(out.read_bytes())
        data_[len(data_) // 2] ^= 1
        out.write_bytes(bytes(data_))
        os.remove(str(out) + ".sha256")
        monkeypatch.setenv("LUCIDA_BACKUP_PASSWORD", "correct horse battery")
        assert run("restore", str(out), "--target", str(tmp_path / "r3")) == cli.EXIT_FAILED
