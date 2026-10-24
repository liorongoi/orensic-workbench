"""Windows launcher for an installed Lucida.

Runs with the Python bundled in the installation (<app>\\python\\python.exe):

    workbench_launcher.py desktop              start Lucida in its own window (pythonw.exe, no console)
    workbench_launcher.py run [--no-browser]   start Lucida with a console and open it in the browser
    workbench_launcher.py selftest [--pause]   check the installed files, then the acceptance self-test
    workbench_launcher.py verify [--report F]  check the installed files against the installer's SHA256SUMS
    workbench_launcher.py cli ARGS...          the `lucida` command line (lucida.cmd in the install folder)

Settings come from <app>\\workbench.ini (written by the installer); environment
variables of the same name (FORENSIC_DATA_DIR, ...) override it. Nothing here
makes a network connection other than to Lucida on this machine.
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import hashlib
import json
import os
import socket
import ssl
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
    }
    if cfg.get("c2pa_trust_anchors"):
        env["FORENSIC_C2PA_TRUST_ANCHORS"] = cfg["c2pa_trust_anchors"]
    # How many queued jobs run at once: model-free work, and work on the graphics card.
    if cfg.get("job_workers"):
        env["FORENSIC_JOB_WORKERS"] = cfg["job_workers"]
    if cfg.get("gpu_jobs"):
        env["FORENSIC_GPU_JOBS"] = cfg["gpu_jobs"]
    # Compute settings (see forensic/compute.py); unset means use every core, a share of RAM and the GPU.
    for key, var in (("threads", "FORENSIC_THREADS"), ("cache_mb", "FORENSIC_CACHE_MB"),
                     ("frame_cache_mb", "FORENSIC_FRAME_CACHE_MB"), ("range_mb", "FORENSIC_RANGE_MB"),
                     ("ai_device", "FORENSIC_AI_DEVICE"), ("gpu_device", "FORENSIC_GPU_DEVICE"),
                     # Hardware profile for detection and batch work, and sign-in for a shared server.
                     ("profile", "FORENSIC_PROFILE"), ("auth", "FORENSIC_AUTH"),
                     ("session_minutes", "FORENSIC_SESSION_MINUTES")):
        if cfg.get(key):
            env[var] = cfg[key]
    for k, v in env.items():
        os.environ.setdefault(k, v)
    # Validation tools call ffmpeg by name; make sure they find the bundled one first.
    os.environ["PATH"] = str(APP / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")
    for d in (os.environ["FORENSIC_DATA_DIR"], os.environ["FORENSIC_IMPORT_DIR"]):
        Path(d).mkdir(parents=True, exist_ok=True)
    # network = local (default): only this computer. network = lab: examiners' PCs on the lab
    # network connect over HTTPS. Add auth = accounts for sign-in, roles and case membership.
    lab = (os.environ.get("FORENSIC_NETWORK") or cfg.get("network") or "local").strip().lower() == "lab"
    host = os.environ.get("FORENSIC_HOST") or ("0.0.0.0" if lab else cfg.get("host") or "127.0.0.1")
    # The API answers only host names it is told about; add a configured lab address and any
    # allowed_hosts from the ini (comma-separated) to 127.0.0.1 and localhost.
    extra = [h.strip() for h in (cfg.get("allowed_hosts") or "").split(",") if h.strip()]
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::"):
        extra.append(host)
    tls = None
    if lab:
        from forensic.tls import ensure_certificate, fingerprint, machine_names

        names, addrs = machine_names()
        extra += [n for n in names + addrs if n not in ("localhost", "127.0.0.1")]
        if cfg.get("tls_cert") and cfg.get("tls_key"):
            tls = {"cert": Path(cfg["tls_cert"]), "key": Path(cfg["tls_key"]), "authority": None,
                   "authority_sha256": fingerprint(Path(cfg["tls_cert"])), "names": names}
        else:
            tls = ensure_certificate(Path(os.environ["FORENSIC_DATA_DIR"]) / "tls", names, addrs)
        os.environ["FORENSIC_SECURE_COOKIE"] = "1"
    if extra:
        os.environ.setdefault("FORENSIC_ALLOWED_HOSTS", ",".join(dict.fromkeys(extra)))
    return {"host": host, "port": int(os.environ.get("FORENSIC_PORT") or cfg.get("port") or 8000), "tls": tls}


def _health(url: str, timeout: float = 1.0, cafile: Path | None = None) -> dict | None:
    ctx = ssl.create_default_context(cafile=str(cafile) if cafile else None) if url.startswith("https") else None
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=timeout, context=ctx) as r:
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
    tls = net["tls"]
    browse_host = "127.0.0.1" if net["host"] in ("0.0.0.0", "") else net["host"]
    url = f"{'https' if tls else 'http'}://{browse_host}:{net['port']}"
    cafile = tls and (tls["authority"] or tls["cert"])
    if _health(url, cafile=cafile):
        print(f"Lucida is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    if not _port_free(net["host"], net["port"]):
        print(f"Port {net['port']} is used by another program. Set a different port in {CONFIG}")
        return 1

    def _open_when_ready():
        for _ in range(240):
            if _health(url, cafile=cafile):
                webbrowser.open(url)
                return
            time.sleep(0.5)

    print("Lucida")
    print(f"  Address:     {url}")
    print(f"  Case data:   {os.environ['FORENSIC_DATA_DIR']}")
    print(f"  Import from: {os.environ['FORENSIC_IMPORT_DIR']}")
    if tls:
        lan = next((n for n in tls["names"] if n != "localhost" and "." in n), None) or socket.gethostname().lower()
        print(f"  Lab network: https://{lan}:{net['port']}")
        if tls["authority"]:
            print(f"  Certificate: install {tls['authority']} on each examiner PC once")
            print(f"               (Trusted Root Certification Authorities); SHA-256 {tls['authority_sha256']}")
    print("Close this window to stop Lucida.\n")
    if open_browser:
        threading.Thread(target=_open_when_ready, daemon=True).start()

    import uvicorn

    extra = {"ssl_certfile": str(tls["cert"]), "ssl_keyfile": str(tls["key"])} if tls else {}
    uvicorn.run("forensic.api:app", host=net["host"], port=net["port"], log_level="info", **extra)
    return 0


def _windowed() -> bool:
    """True under pythonw.exe (the desktop shortcut), where there is no console to print to."""
    return sys.stdout is None or sys.stderr is None


def _tell(message: str, error: bool = True) -> None:
    print(message)
    if _windowed() or getattr(sys.stdout, "_lucida_log", False):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Lucida", 0x10 if error else 0x40)
        except Exception:  # noqa: BLE001
            pass


def desktop() -> int:
    """Lucida in its own window (forensic.desktop), on this computer only.

    With the lab-network option the shared HTTPS server is what examiners use, so this starts
    that, as `run` does, and opens the browser. If Lucida is already running here, the browser
    opens on it rather than starting a second server on the same case data.
    """
    net = configure()
    if _windowed():
        logs = Path(os.environ["FORENSIC_DATA_DIR"]) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = open(logs / f"desktop-{dt.datetime.now():%Y%m%d}.txt", "a", encoding="utf-8", buffering=1)
        log._lucida_log = True  # type: ignore[attr-defined]
        sys.stdout = sys.stderr = log
        print(f"\n== Lucida desktop, {dt.datetime.now().isoformat(timespec='seconds')}")
    if net["tls"]:
        return run(True)
    url = f"http://127.0.0.1:{net['port']}"
    if _health(url):
        print(f"Lucida is already running at {url}; opening it in the browser")
        webbrowser.open(url)
        return 0
    if not _port_free("127.0.0.1", net["port"]):
        _tell(f"Port {net['port']} is used by another program. Set a different port in {CONFIG}")
        return 1
    from forensic import desktop as shell

    return shell.main(["--port", str(net["port"])])


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _check_files(entries: list[tuple[str, str]], sizes: dict[str, int]) -> list[dict]:
    bad = []
    for digest, rel in entries:
        p = APP / rel.replace("/", os.sep)
        want = sizes.get(rel)
        try:
            got_size = p.stat().st_size
            if want is not None and got_size != want:
                bad.append({"file": rel, "problem": "size", "expected": want, "found": got_size})
            elif _sha256(p) != digest:
                bad.append({"file": rel, "problem": "content", "expected": want, "found": got_size})
        except FileNotFoundError:
            bad.append({"file": rel, "problem": "missing", "expected": want, "found": None})
        except OSError as e:
            bad.append({"file": rel, "problem": f"unreadable ({e.strerror or e})", "expected": want, "found": None})
    return bad


def _describe(b: dict) -> str:
    if b["problem"] == "size":
        return f"{b['file']}: {b['found']} bytes, expected {b['expected']}"
    if b["problem"] == "content":
        return f"{b['file']}: same size, different content"
    if b["problem"] == "missing":
        return f"{b['file']}: missing"
    return f"{b['file']}: {b['problem']}"


def verify_installation(retries: int = 3, wait_s: float = 5.0) -> tuple[bool, str]:
    """Re-hash every installed file against the SHA256SUMS written when the installer was built.

    Files that differ are checked again a few times: antivirus software can hold or scan a file for
    some seconds just after it is written. A file that still differs is reported with its expected
    and actual size, which usually tells why (truncated copy, quarantined or replaced file).
    """
    sums = APP / "SHA256SUMS"
    if not sums.is_file():
        return False, f"{sums} is missing"
    entries = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        if rel:
            entries.append((digest, rel.lstrip("*")))
    sizes: dict[str, int] = {}
    size_file = APP / "FILESIZES"
    if size_file.is_file():
        for line in size_file.read_text(encoding="utf-8").splitlines():
            n, _, rel = line.partition("  ")
            if rel and n.isdigit():
                sizes[rel] = int(n)
    bad = _check_files(entries, sizes)
    for _ in range(retries):
        if not bad:
            break
        time.sleep(wait_s)
        recheck = {b["file"] for b in bad}
        bad = _check_files([e for e in entries if e[1] in recheck], sizes)
    n = len(entries)
    if bad:
        hint = ("Usually antivirus software removed or changed these files while Setup copied them (add the "
                "installation folder to its exclusions, then run Setup again), or the Setup file itself was not "
                "copied completely (compare its SHA-256 with the .sha256 file that came with it).")
        return False, f"{len(bad)} of {n} file(s) differ from the installer: " + "; ".join(_describe(b) for b in bad[:10]) + ". " + hint
    return True, f"{n} file(s) match the installer's SHA256SUMS"


def verify(report: str | None) -> int:
    """Check the installed files only (used by Setup right after copying them)."""
    ok, detail = verify_installation()
    text = f"{'PASS' if ok else 'FAIL'}  installed files: {detail}"
    print(text)
    if report:
        Path(report).write_text(text + "\n", encoding="utf-8")
    return 0 if ok else 1


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
            print(f"Lucida acceptance self-test, {dt.datetime.now().isoformat(timespec='seconds')}")
            print(f"Installation: {APP}")
            print(f"Computer:     {os.environ.get('COMPUTERNAME', '?')}  Python {sys.version.split()[0]}\n")
            ok, detail = verify_installation()
            print(f"{'PASS' if ok else 'FAIL'}  installed files: {detail}")
            from forensic import selftest as st

            code = st.main()
            if not ok:
                code = code or 1
                print("Self-test FAILED: the installed files differ from the installer (see the first line).")
            print(f"\nLog saved to {log_path}")
        finally:
            sys.stdout = out
    if pause:
        input("\nPress Enter to close this window.")
    return code


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["cli"]:
        # Batch conversion, verify, backup and restore with this installation's settings and FFmpeg.
        configure()
        from forensic import cli

        return cli.main(argv[1:])
    ap = argparse.ArgumentParser(prog="workbench_launcher")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("desktop")
    r = sub.add_parser("run")
    r.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("selftest")
    s.add_argument("--pause", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--report", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        return verify(a.report)
    try:
        if a.cmd == "desktop":
            code = desktop()
        else:
            code = run(not a.no_browser) if a.cmd == "run" else selftest(a.pause)
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        if a.cmd == "desktop":
            _tell(f"Lucida stopped: {e}")
        else:
            print(f"\nLucida stopped: {e}")
        code = 1
    # Keep a window opened from a shortcut on screen long enough to read why it stopped.
    if code and sys.stdin and sys.stdin.isatty() and not (a.cmd == "selftest" and a.pause):
        input("Press Enter to close this window.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
