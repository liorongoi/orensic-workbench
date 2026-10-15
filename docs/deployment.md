# Deployment

## Windows (offline installer)

`ForensicWorkbench-<version>-Setup.exe` installs everything the workbench needs on a
Windows 10 or 11 x64 machine with no internet connection: a private Python 3.12, every Python
library, FFmpeg and FFprobe, the Visual C++ runtime, the hash-pinned AI models and the built
UI. It needs no Docker, no system Python and no administrator rights (choose "Install for all
users" in the first dialog to install under Program Files for everyone on the machine).

Get the installer from the **Windows installer** workflow in GitHub Actions (artifact
`ForensicWorkbench-Windows-Setup`, with its `.sha256`), or from a release when a `v*` tag is
pushed. Carry the `.exe` to the offline machine, check its SHA-256 if your lab requires it
(`certutil -hashfile ForensicWorkbench-0.3.0-Setup.exe SHA256`), and run it. The wizard asks
for:

- the installation folder;
- the case data folder (cases, write-once evidence store, derived outputs, audit log, logs).
  It is kept when the workbench is uninstalled or upgraded. For one user it stays private to that
  account. For all users, only members of the local **Forensic Examiners** group (the account
  that ran the installer is added), administrators and SYSTEM can open it; add each examiner with
  `net localgroup "Forensic Examiners" <account> /add`, and they sign out and in again;
- the local port (default 8000; the workbench listens on 127.0.0.1 only).

At the end it offers the acceptance self-test: it re-hashes every installed file against the
installer's `SHA256SUMS`, then runs `forensic.selftest`, and saves its output under
`<case data>\logs`.

**Start > Forensic Workbench** (and the desktop shortcut) opens the workbench as a desktop app,
in its own window, through the Microsoft Edge WebView2 runtime that Windows 10 and 11 include.
Closing the window stops the workbench; its log goes to `<case data>\logs\workbench-<date>.txt`.
If the workbench is already running on this computer (for example from the console shortcut
below, serving other computers on the lab network), the window opens on it and closing the
window leaves it running. When `host` in `workbench.ini` is a network address, closing a window
that started the workbench asks first, because other computers may be using it. On a computer
without WebView2 the app opens the workbench in the default browser instead, with a small
dialog that stops it.

**Forensic Workbench (browser and server console)** runs the workbench in a console window that
shows the address and log, and opens the browser; closing the console stops it. Use it to serve
the workbench to other computers. The Start menu also has **Self-test** and **Case data folder**.

Settings live in `<install folder>\workbench.ini` (`data_dir`, `import_dir`, `host`, `port`, and
optionally `c2pa_trust_anchors` and `allowed_hosts`, the extra names browsers may use to reach it). Running a newer installer upgrades in place and keeps
these settings and the case data. Silent installation for managed rollouts:

```
ForensicWorkbench-0.3.0-Setup.exe /VERYSILENT /ALLUSERS /DIR="C:\Program Files\Forensic Workbench" /DATADIR="D:\Cases" /PORT=8000
```

To build the installer yourself, on a connected Windows x64 machine with Python 3.12 and
Node.js 22: `powershell -ExecutionPolicy Bypass -File deploy\windows\build-installer.ps1`
(Inno Setup 6 is used, and installed with Chocolatey if missing). It writes
`dist\ForensicWorkbench-<version>-Setup.exe`; `BUILDINFO.txt` in the installation records the
commit and the SHA-256 of the Python, FFmpeg and Visual C++ downloads it was built from.
CI installs every build on a fresh Windows runner, for one user and for all users, blocks the
bundled Python from the network, runs the self-test, starts the workbench, and uninstalls
(`deploy\windows\test-installer.ps1`).

## Container

```bash
docker build -t forensic-workbench:0.3.0 .
docker compose up            # http://127.0.0.1:8000
docker compose --profile agents up   # also the authenticate MCP server on the internal network
```

Case data lives in `./data`; `./import` is the only folder agents may ingest from. The
container starts as root only to hand `/data` to its unprivileged `forensic` user (Docker
creates a missing bind-mounted folder owned by root), then runs the workbench as that user.

Without Compose:

```bash
docker run -p 127.0.0.1:8000:8000 -v "$PWD/data:/data" forensic-workbench:0.3.0
```

The workbench joins a plain network so its port can reach your browser (Docker does not
publish ports from internal-only networks) and an internal network shared with the MCP
servers, which have no route out. Nothing in the workbench makes outbound calls; to enforce
that, run it on an air-gapped host or firewall the host.

The API answers only requests whose Host is `127.0.0.1` or `localhost`. If a TLS proxy or a
lab hostname fronts it, set `FORENSIC_ALLOWED_HOSTS` to that name (comma-separated for several),
or the browser gets `400 Invalid host header`.

## Air-gapped installation

On a connected machine with the same OS, CPU architecture and Python version as the target:

```bash
deploy/airgap/build-bundle.sh            # add --docker to include the container image
```

This writes `dist/forensic-workbench-<version>-airgap/` with every Python wheel, the backend
with its model weights, the built UI, the docs and `SHA256SUMS`. Carry it across, install
FFmpeg from the target OS's offline packages, then:

```bash
./install.sh /opt/forensic-workbench
/opt/forensic-workbench/run.sh
```

`install.sh` checks every file against `SHA256SUMS`, installs with `pip --no-index`, and runs
the acceptance self-test.

## Acceptance self-test

```bash
python -m forensic.selftest
```

Checks FFmpeg, re-hashes every model, checks ONNX Runtime gives bit-identical output on this
CPU, loads the face detector and the C2PA reader, then runs a small case end to end
(ingest, render, reproduce, triage, measure, review, PDF report) in a temporary folder. Run
it after every installation or upgrade and keep its output with the lab's validation records.

## No network at run time

- Models are bundled or installed locally; weights are verified before every load.
- C2PA manifests are validated offline (no OCSP or remote-manifest fetching). To recognise
  signers your lab trusts, set `FORENSIC_C2PA_TRUST_ANCHORS` to a PEM file of root
  certificates; without it signatures are checked for integrity and reported as from an
  untrusted signer.
- The built-in agents (triage, planner, reviewer) are deterministic programs, not LLM calls.
- An LLM agent connects through the MCP servers (`python -m forensic.mcp_server <server>
  --http PORT`). Any MCP client works, so a self-hosted model on the same network can drive
  the workbench; nothing in the workbench calls an external model API.
