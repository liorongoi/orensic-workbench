"""Helper programs (FFmpeg...) start with no console window on Windows, so nothing flashes on screen."""

import subprocess

from forensic import _noconsole


def _install_on_fake_windows(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            seen.clear()
            seen.update(kwargs)

    monkeypatch.setattr(_noconsole.sys, "platform", "win32")
    monkeypatch.setattr(_noconsole, "_FLAG", 0x08000000)
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x08, raising=False)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    _noconsole.install()
    return seen


def test_every_helper_program_gets_no_window(monkeypatch):
    seen = _install_on_fake_windows(monkeypatch)
    subprocess.Popen(["ffmpeg"], stdout=subprocess.PIPE)
    assert seen["creationflags"] == 0x08000000
    subprocess.Popen(["ffmpeg"], creationflags=0x200)
    assert seen["creationflags"] == 0x200 | 0x08000000


def test_a_requested_console_is_kept(monkeypatch):
    seen = _install_on_fake_windows(monkeypatch)
    subprocess.Popen(["cmd"], creationflags=0x10)
    assert seen["creationflags"] == 0x10


def test_install_once_only(monkeypatch):
    _install_on_fake_windows(monkeypatch)
    patched = subprocess.Popen
    _noconsole.install()
    assert subprocess.Popen is patched


def test_nothing_changes_elsewhere():
    if _noconsole.sys.platform != "win32":
        assert not getattr(subprocess.Popen, "_lucida_no_console", False)
