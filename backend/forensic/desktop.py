"""Lucida as a desktop application: the workbench in its own window, with no browser.

    python -m forensic.desktop [--port N] [--data-dir DIR] [--debug]

The window is a native WebView2 window (Edge's engine, part of Windows 10 and 11) showing
the same interface as the browser version, served by Lucida on this computer only
(127.0.0.1). The desktop shell adds what a browser cannot give a web page:

- native Open dialogs: evidence is hashed into the evidence store straight from its folder,
  with no upload through the window, so a 20 GB recorder export is read once;
- native Save dialogs for reports, exports and originals;
- more windows (a second viewer for another monitor) and full screen.

It needs the optional `pywebview` package (``pip install "lucida-forensic[desktop]"``).
Without it, or with --browser, Lucida opens in the default browser instead.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

TITLE = "Lucida"
TOKEN_TTL = 15 * 60  # seconds a chosen file stays offered to the window that chose it

EVIDENCE_TYPES = (
    "All files (*.*)",
    "Images (*.jpg;*.jpeg;*.png;*.tif;*.tiff;*.bmp;*.webp;*.heic;*.dng)",
    "Video and recorder exports (*.mp4;*.mov;*.avi;*.mkv;*.ts;*.mts;*.m2ts;*.dav;*.h264;*.264;*.h265;*.265;*.hevc;*.asf;*.wmv;*.mpg;*.mpeg;*.exe)",
)


@dataclass
class _Offer:
    path: Path
    expires: float


class LocalFiles:
    """Files the examiner picked in a native Open dialog, offered to the window by token.

    The API ingests a local file only through a token from here, so a web page (or a client on
    the lab network) cannot ask Lucida to read an arbitrary path: tokens come only from the
    dialog in this process, are random, single use and expire.
    """

    def __init__(self):
        self._d: dict[str, _Offer] = {}
        self._lock = threading.Lock()

    def offer(self, path: str | os.PathLike) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._d = {k: v for k, v in self._d.items() if v.expires > now}
            self._d[token] = _Offer(Path(path), now + TOKEN_TTL)
        return token

    def take(self, token: str) -> Path | None:
        with self._lock:
            o = self._d.pop(str(token), None)
        if o is None or o.expires < time.monotonic():
            return None
        return o.path


local_files = LocalFiles()


class Bridge:
    """Functions the interface calls as ``window.pywebview.api.<name>(...)``.

    Attributes are private: pywebview exposes every public attribute to the page.
    """

    def __init__(self, base_url: str):
        self._base = base_url
        self._windows: list = []

    def _window(self):
        import webview

        return webview.active_window() or (self._windows[0] if self._windows else None)

    def open_evidence(self):
        """Native Open dialog; returns [{token, name, size}] for the chosen files ([] if cancelled)."""
        import webview

        w = self._window()
        paths = w.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=EVIDENCE_TYPES) if w else None
        out = []
        for p in paths or ():
            path = Path(p)
            if path.is_file():
                out.append({"token": local_files.offer(path), "name": path.name, "size": path.stat().st_size})
        return out

    def open_window(self, route: str = "#/", title: str = TITLE):
        """Another Lucida window (for example the viewer on a second monitor). `route` is the
        part of the address after '#', so only Lucida's own pages can be opened."""
        import webview

        route = str(route or "#/")
        if not route.startswith("#"):
            return False
        place = {"width": 1400, "height": 900}
        try:
            screens = webview.screens
            screens = list(screens() if callable(screens) else screens or [])
            if len(screens) > 1:
                # Open on a monitor the main window is not on, filling it.
                main = self._windows[0] if self._windows else None
                mx = (getattr(main, "x", 0) or 0) + 50
                other = next((s for s in screens if not (s.x <= mx < s.x + s.width)), screens[-1])
                place = {"x": other.x + 40, "y": other.y + 40, "width": other.width - 80,
                         "height": other.height - 80, "maximized": True}
        except Exception:  # noqa: BLE001  (screen layout unknown: a normal window)
            pass
        w = webview.create_window(str(title)[:120] or TITLE, self._base + "/" + route, js_api=self,
                                  min_size=(900, 600), background_color="#0f1216", text_select=True, **place)
        self._windows.append(w)
        return True

    def open_page(self, path: str, title: str = TITLE):
        """A Lucida page that is not part of the interface, such as an HTML or PDF report, in its
        own window (signed in like the main one). Only paths on this Lucida are accepted."""
        import webview

        path = str(path or "")
        if not path.startswith("/") or path.startswith("//"):
            return False
        w = webview.create_window(str(title)[:120] or TITLE, self._base + path, js_api=self,
                                  width=1100, height=900, min_size=(600, 400), text_select=True)
        self._windows.append(w)
        return True

    def toggle_fullscreen(self):
        w = self._window()
        if w:
            w.toggle_fullscreen()
        return True

    def platform(self):
        return {"desktop": True, "os": sys.platform}


def _free_port(preferred: int | None) -> int:
    for port in ([preferred] if preferred else []) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free local port")


def serve(port: int):
    """Start Lucida's API and interface on 127.0.0.1:port in a background thread."""
    import uvicorn

    from .api import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, name="lucida-server", daemon=True)
    t.start()
    for _ in range(600):
        if server.started:
            return server
        if not t.is_alive():
            raise RuntimeError("Lucida's server stopped while starting; see the messages above")
        time.sleep(0.05)
    raise RuntimeError("Lucida's server did not start")


def _warm_models():
    try:
        from . import models

        models.warm()
    except Exception:  # noqa: BLE001  (warming is only a head start)
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m forensic.desktop", description="Lucida in its own window")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FORENSIC_PORT") or 0),
                    help="local port (default: a free one)")
    ap.add_argument("--data-dir", help="case data folder (default: FORENSIC_DATA_DIR or ./data)")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--debug", action="store_true", help="allow the developer tools in the window")
    args = ap.parse_args(argv)
    if args.data_dir:
        os.environ["FORENSIC_DATA_DIR"] = str(Path(args.data_dir).resolve())
    os.environ.setdefault("FORENSIC_DESKTOP", "1")

    port = _free_port(args.port or None)
    server = serve(port)
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=_warm_models, name="lucida-warm-models", daemon=True).start()

    try:
        if args.browser:
            raise ImportError
        import webview
    except ImportError:
        print(f"Lucida is running at {base} (close this window to stop it)")
        webbrowser.open(base)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return 0

    # Downloads (JSON bundles, exports, originals) get a native Save dialog.
    webview.settings["ALLOW_DOWNLOADS"] = True
    bridge = Bridge(base)
    main_window = webview.create_window(TITLE, base + "/", js_api=bridge, width=1600, height=1000,
                                        min_size=(1024, 680), maximized=True, background_color="#0f1216",
                                        text_select=True)
    bridge._windows.append(main_window)
    data = Path(os.environ.get("FORENSIC_DATA_DIR", "./data")).resolve()
    # A persistent profile keeps the panel layout between sessions.
    webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug=args.debug,
                  private_mode=False, storage_path=str(data / "desktop-webview"))
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
