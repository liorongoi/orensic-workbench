"""Windows launcher for an installed Forensic Workbench.

Runs with the Python bundled in the installation (<app>\\python\\python.exe):

    workbench_launcher.py app                  the desktop app: the workbench in its own window
    workbench_launcher.py run [--no-browser]   start the workbench in a console and open it in the browser
    workbench_launcher.py selftest [--pause]   check the installed files, then the acceptance self-test

Settings come from <app>\\workbench.ini (written by the installer); environment
variables of the same name (FORENSIC_DATA_DIR, ...) override it. Nothing here
makes a network connection other than to the workbench on this machine.
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
CONFIG = APP / "workbench.ini"


def configure() -> dict:
    """Read workbench.ini and set the FORENSIC_* environment the backend reads at import."""
    ini = configparser.ConfigParser()
    ini.read(CONFIG, encoding="utf-8")
    cfg = ini["workbench"] if ini.has_section("workbench") else {}
    default_data = Path(os.environ.get("LOCALAPPDATA", APP)) / "ForensicWorkbench" / "data"
    data = Path(os.environ.get("FORENSIC_DATA_DIR") or cfg.get("data_dir") or default_data)
    env = {
        "FORENSIC_DATA_DIR": str(data),
        "FORENSIC_IMPORT_DIR": os.environ.get("FORENSIC_IMPORT_DIR") or cfg.get("import_dir") or str(data / "import"),
        "FORENSIC_FRONTEND_DIST": str(APP / "frontend" / "dist"),
        "FORENSIC_FFMPEG": str(APP / "ffmpeg" / "ffmpeg.exe"),
        "FORENSIC_FFPROBE": str(APP / "ffmpeg" / "ffprobe.exe"),
        "FORENSIC_DETERMINISTIC": "1",
    }
    if cfg.get("c2pa_trust_anchors"):
        env["FORENSIC_C2PA_TRUST_ANCHORS"] = cfg["c2pa_trust_anchors"]
    for k, v in env.items():
        os.environ.setdefault(k, v)
    # Validation tools call ffmpeg by name; make sure they find the bundled one first.
    os.environ["PATH"] = str(APP / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")
    for d in (os.environ["FORENSIC_DATA_DIR"], os.environ["FORENSIC_IMPORT_DIR"]):
        Path(d).mkdir(parents=True, exist_ok=True)
    host = os.environ.get("FORENSIC_HOST") or cfg.get("host") or "127.0.0.1"
    # The API answers only host names it is told about; add a configured lab address and any
    # allowed_hosts from the ini (comma-separated) to 127.0.0.1 and localhost.
    extra = [h.strip() for h in (cfg.get("allowed_hosts") or "").split(",") if h.strip()]
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::"):
        extra.append(host)
    if extra:
        os.environ.setdefault("FORENSIC_ALLOWED_HOSTS", ",".join(extra))
    return {"host": host, "port": int(os.environ.get("FORENSIC_PORT") or cfg.get("port") or 8000)}


def _health(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def run(open_browser: bool) -> int:
    net = configure()
    browse_host = "127.0.0.1" if net["host"] in ("0.0.0.0", "") else net["host"]
    url = f"http://{browse_host}:{net['port']}"
    if _health(url):
        print(f"The Forensic Workbench is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    if not _port_free(net["host"], net["port"]):
        print(f"Port {net['port']} is used by another program. Set a different port in {CONFIG}")
        return 1

    def _open_when_ready():
        for _ in range(240):
            if _health(url):
                webbrowser.open(url)
                return
            time.sleep(0.5)

    print("Forensic Workbench")
    print(f"  Address:     {url}")
    print(f"  Case data:   {os.environ['FORENSIC_DATA_DIR']}")
    print(f"  Import from: {os.environ['FORENSIC_IMPORT_DIR']}")
    print("Close this window to stop the workbench.\n")
    if open_browser:
        threading.Thread(target=_open_when_ready, daemon=True).start()

    import uvicorn

    uvicorn.run("forensic.api:app", host=net["host"], port=net["port"], log_level="info")
    return 0


def _message_box(text: str, title: str = "Forensic Workbench", flags: int = 0x40) -> int:
    """A Windows message box (pythonw has no console). Falls back to printing elsewhere."""
    try:
        import ctypes

        return ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)  # MB_SETFOREGROUND
    except Exception:  # noqa: BLE001
        print(f"{title}: {text}")
        return 1


def _log_to_file(name: str) -> Path:
    """pythonw.exe has no console: send output to the logs folder in the case data instead."""
    logs = Path(os.environ["FORENSIC_DATA_DIR"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"{name}-{dt.datetime.now():%Y%m%d}.txt"
    f = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the process
    f.write(f"\n--- {dt.datetime.now().isoformat(timespec='seconds')} ---\n")
    sys.stdout = sys.stderr = f
    return path


def _start_server(net: dict):
    """Run uvicorn in a background thread of this process; returns the server to stop it."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config("forensic.api:app", host=net["host"], port=net["port"],
                                           log_level="info"))
    threading.Thread(target=server.run, name="workbench-server", daemon=True).start()
    return server


def app(smoke_test: bool = False) -> int:
    """The desktop app: the workbench in its own window (Microsoft Edge WebView2).

    If the workbench is already running on this computer the window opens on it; otherwise this
    process starts it and stops it when the window closes. Without WebView2 the workbench opens
    in the default browser and a small dialog stops it.
    """
    net = configure()
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001
        pass
    if not smoke_test and (sys.stdout is None or not sys.stdout.isatty()):
        log = _log_to_file("workbench")
        print(f"Desktop app, log {log}")
    browse_host = "127.0.0.1" if net["host"] in ("0.0.0.0", "") else net["host"]
    url = f"http://{browse_host}:{net['port']}"
    server = None
    if not _health(url):
        if not _port_free(net["host"], net["port"]):
            _message_box(f"Port {net['port']} is used by another program.\n\nSet a different port in {CONFIG}.",
                         flags=0x10)
            return 1
        server = _start_server(net)
        for _ in range(240):
            if _health(url):
                break
            if not server_thread_alive():
                break
            time.sleep(0.25)
        if not _health(url):
            _message_box("The workbench did not start. See the log in the case data folder.", flags=0x10)
            return 1
    shared = net["host"] not in ("127.0.0.1", "localhost")
    print(f"Workbench at {url} ({'started here' if server else 'already running'})")
    try:
        code = _window(url, confirm_close=bool(server and shared), smoke_test=smoke_test)
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        if smoke_test:
            print(f"FAIL  desktop window: {e}")
            code = 1
        else:
            # No WebView2 on this computer: use the browser, and keep the workbench running until
            # the examiner says to stop it.
            webbrowser.open(url)
            if server:
                _message_box(f"The Forensic Workbench is open in your browser at {url}.\n\n"
                             "Click OK to stop it.")
            code = 0
    if server:
        server.should_exit = True
        time.sleep(1)
    return code


def server_thread_alive() -> bool:
    return any(t.name == "workbench-server" and t.is_alive() for t in threading.enumerate())


def _window(url: str, confirm_close: bool, smoke_test: bool) -> int:
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    storage = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ForensicWorkbench" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    window = webview.create_window("Forensic Workbench", url, width=1440, height=900, min_size=(1024, 640),
                                   confirm_close=confirm_close, text_select=True)
    window.events.shown += lambda: _set_window_icon(window)
    result = {"code": 0}
    if smoke_test:
        def check():
            ok = False
            for _ in range(120):
                try:
                    ok = bool(window.evaluate_js("!!document.getElementById('root') && "
                                                 "document.getElementById('root').children.length > 0"))
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    break
                time.sleep(0.5)
            print(f"{'PASS' if ok else 'FAIL'}  desktop window rendered the workbench UI from {url}")
            result["code"] = 0 if ok else 1
            window.destroy()

        webview.start(check, gui="edgechromium", private_mode=False, storage_path=str(storage))
    else:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage))
    return result["code"]


APP_ID = "ForensicWorkbench.Desktop"  # matches the AppUserModelID of the installer's shortcuts
ICON = APP / "workbench.ico"


def _set_window_icon(window) -> None:
    """Give the window and its taskbar button the workbench icon instead of Python's."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        hwnd = ctypes.c_void_p(int(window.native.Handle.ToInt64()))
        for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
            h = user32.LoadImageW(None, str(ICON), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                user32.SendMessageW(hwnd, 0x80, ctypes.c_void_p(which), ctypes.c_void_p(h))  # WM_SETICON
    except Exception as e:  # noqa: BLE001
        print(f"window icon not set: {e}")


def verify_installation() -> tuple[bool, str]:
    """Re-hash every installed file against the SHA256SUMS written when the installer was built."""
    sums = APP / "SHA256SUMS"
    if not sums.is_file():
        return False, f"{sums} is missing"
    bad, n = [], 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        if not rel:
            continue
        n += 1
        p = APP / rel.lstrip("*").replace("/", os.sep)
        try:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            if h.hexdigest() != digest:
                bad.append(f"changed: {rel}")
        except OSError:
            bad.append(f"missing: {rel}")
    if bad:
        return False, f"{len(bad)} of {n} file(s) differ from the installer: " + "; ".join(bad[:10])
    return True, f"{n} file(s) match the installer's SHA256SUMS"


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def selftest(pause: bool) -> int:
    configure()
    logs = Path(os.environ["FORENSIC_DATA_DIR"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"selftest-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
    with open(log_path, "w", encoding="utf-8") as log:
        out = sys.stdout
        sys.stdout = _Tee(out, log)
        try:
            print(f"Forensic Workbench acceptance self-test, {dt.datetime.now().isoformat(timespec='seconds')}")
            print(f"Installation: {APP}")
            print(f"Computer:     {os.environ.get('COMPUTERNAME', '?')}  Python {sys.version.split()[0]}\n")
            ok, detail = verify_installation()
            print(f"{'PASS' if ok else 'FAIL'}  installed files: {detail}")
            from forensic import selftest as st

            code = st.main()
            if not ok:
                code = code or 1
            print(f"\nLog saved to {log_path}")
        finally:
            sys.stdout = out
    if pause:
        input("\nPress Enter to close this window.")
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="workbench_launcher")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("app")
    w.add_argument("--smoke-test", action="store_true", help="open the window, check the UI renders, close it")
    r = sub.add_parser("run")
    r.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("selftest")
    s.add_argument("--pause", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "app":
            code = app(a.smoke_test)
        elif a.cmd == "run":
            code = run(not a.no_browser)
        else:
            code = selftest(a.pause)
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\nThe Forensic Workbench stopped: {e}")
        if a.cmd == "app" and not a.smoke_test:
            _message_box(f"The Forensic Workbench stopped: {e}", flags=0x10)
        code = 1
    # Keep a window opened from a shortcut on screen long enough to read why it stopped.
    if code and a.cmd != "app" and sys.stdin and sys.stdin.isatty() and not (a.cmd == "selftest" and a.pause):
        input("Press Enter to close this window.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
