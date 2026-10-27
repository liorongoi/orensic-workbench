# Deployment

Lucida comes in two forms, from the same code and the same installer:

| | Desktop app | Web app (office server) |
|---|---|---|
| Who uses it | One examiner on their own workstation | Several examiners from their own PCs, in a browser |
| How it runs | **Start > Lucida** opens Lucida in its own window; nothing listens on the network | One machine runs Lucida; examiners open `https://<server name>:<port>` in Edge or Chrome |
| Install | The Windows installer below, defaults | The Windows installer with **Let examiners connect from other PCs** ticked, then `auth = accounts` in `workbench.ini` ([Sign-in](#sign-in-for-a-shared-server)); or the [Linux server package](#linux-server-ubuntu-or-red-hat-nvidia-gpu) (Ubuntu or Red Hat, NVIDIA GPU); or the [container](#container) |
| Sign-in | None: one examiner, named at the top right | Accounts with roles, and per-case membership |

Both keep evidence on the machine Lucida runs on, and neither sends anything outside it.

## Windows (offline installer)

`Lucida-<version>-Setup.exe` installs everything Lucida needs on a
Windows 10 or 11 x64 machine with no internet connection: a private Python 3.12, every Python
library, FFmpeg and FFprobe, the Visual C++ runtime, the hash-pinned AI models and the built
UI. It needs no Docker, no system Python and no administrator rights (choose "Install for all
users" in the first dialog to install under Program Files for everyone on the machine).

Get the installer from the **Windows installer** workflow in GitHub Actions (artifact
`Lucida-Windows-Setup`, with its `.sha256`), or from a release when a `v*` tag is
pushed. Carry the `.exe` to the offline machine, check its SHA-256 if your lab requires it
(`certutil -hashfile Lucida-0.3.0-Setup.exe SHA256`), and run it. The wizard opens with a
welcome page, the licence agreement and a list of what is included, then a **system check**.
It reads the installed memory, the graphics card and the NVIDIA driver version. Lucida is sized for
an NVIDIA RTX A4000 (16 GB) with 48 to 128 GB of RAM. The page warns, without stopping the install,
when memory is under 48 GB, when no NVIDIA card is found, or when the driver is older than R470.
Learned models use the GPU through DirectML, which is in the installer and needs only the NVIDIA
display driver (no CUDA). A model runs on the GPU only when two runs give identical output, and
otherwise on the CPU. The check also sizes `workbench.ini`: `job_workers` is one per 16 GB of RAM
(3 at 48 GB, 6 from 96 GB, never more than half the processor threads), `gpu_jobs = 1` and
`ai_device = auto`. The caches size themselves from RAM when Lucida starts (a quarter, at most 32 GB,
for results and an eighth, at most 16 GB, for video frames). An upgrade keeps values you changed.
The wizard then asks for:

- the installation folder;
- the case data folder (cases, write-once evidence store, derived outputs, audit log, logs).
  It is kept when Lucida is uninstalled or upgraded. For one user it stays private to that
  account. For all users, only members of the local **Forensic Examiners** group (the account
  that ran the installer is added), administrators and SYSTEM can open it; add each examiner with
  `net localgroup "Forensic Examiners" <account> /add`, and they sign out and in again;
- the local port (default 8000; Lucida listens on 127.0.0.1 only, unless you tick the
  lab-network option, see [Examiners on other PCs](#examiners-on-other-pcs));
- Start menu and desktop shortcuts (both ticked by default). **Lucida** opens the desktop
  window (no console, no browser; see the desktop app section). **Lucida in the browser** starts
  Lucida with a console window and opens the browser, as before. With the lab-network option,
  both start the shared HTTPS server and open the browser.

The desktop window runs on the Microsoft Edge WebView2 runtime, which Windows 11 includes. On a
Windows 10 machine without it, Setup installs it from the copy inside the installer, offline.
The desktop window's messages go to `<case data>\logs\desktop-<date>.txt`.

A summary page shows every choice before anything is installed. The last page offers to run
the acceptance self-test and to start Lucida straight away.

**Uninstalling** (Settings > Apps, or **Start > Lucida > Uninstall Lucida**) removes the
programs and shortcuts, then asks whether to delete the case data folder too. The answer
defaults to **No**, and deleting needs a second confirmation. A drive root or a whole Documents,
Desktop or profile folder is never deleted, even if it was chosen as the case data folder. A
silent uninstall (`unins000.exe /VERYSILENT`) keeps case data unless you add `/DELETECASEDATA`.

At the end it offers the acceptance self-test: it re-hashes every installed file against the
installer's `SHA256SUMS`, then runs `forensic.selftest`, and saves its output under
`<case data>\logs`. Start Lucida from **Start > Lucida**: a console window
shows the address and log, and the browser opens Lucida; closing the window stops it.
The Start menu also has **Self-test**, **Case data folder**, **User manual**, **Algorithm guide** and **Amped vs Lucida** (the algorithm-by-algorithm comparison).

Settings live in `<install folder>\workbench.ini` (`data_dir`, `import_dir`, `host`, `port`, and
optionally `c2pa_trust_anchors`, `allowed_hosts`, the extra names browsers may use to reach it,
`job_workers` and `gpu_jobs`, and `network`, `tls_cert` and `tls_key`; see
[Several examiners](#several-examiners)). Compute settings are
optional too: `threads` (default every core), `cache_mb` (RAM for intermediate results, default a
quarter of RAM up to 32 GB), `frame_cache_mb` (decoded video frames, default an eighth up to 16 GB)
`range_mb` (RAM one video frame range may use), `ai_device` (`auto`, `gpu` or `cpu`; auto uses
the GPU through DirectML when it reproduces results bit for bit) and `gpu_device` (the DirectML
adapter number, if the NVIDIA card is not picked on its own). Running a newer installer upgrades in place and keeps
these settings and the case data. Silent installation for managed rollouts:

```
Lucida-0.3.0-Setup.exe /VERYSILENT /ALLUSERS /DIR="C:\Program Files\Lucida" /DATADIR="D:\Cases" /PORT=8000
```

Add `/TASKS="startmenu,desktopicon"` to choose shortcuts (and `labnetwork` for the lab-network
option); a `/TASKS` list replaces the defaults, so name every task you want.

To build the installer yourself, on a connected Windows x64 machine with Python 3.12 and
Node.js 22: `powershell -ExecutionPolicy Bypass -File deploy\windows\build-installer.ps1`
(Inno Setup 6 is used, and installed with Chocolatey if missing). It writes
`dist\Lucida-<version>-Setup.exe`; `BUILDINFO.txt` in the installation records the
commit and the SHA-256 of the Python, FFmpeg and Visual C++ downloads it was built from.
CI installs every build on a fresh Windows runner, for one user and for all users, blocks the
bundled Python from the network, runs the self-test, starts Lucida, and uninstalls, once keeping
and once deleting the case data (`deploy\windows\test-installer.ps1`). A further job clicks
through the real wizard and uninstaller and keeps a screenshot of every page (artifact
`Lucida-Setup-wizard-screenshots`, `deploy\windows\capture-wizard.ps1`).

## Linux server (Ubuntu or Red Hat, NVIDIA GPU)

`Lucida-<version>-linux-x86_64.tar.gz` installs the web app on an x86_64 Linux server, offline,
for examiners who use it in Chrome from their own PCs. One package serves Ubuntu 22.04 / 24.04 LTS
and Red Hat Enterprise Linux 8 / 9 (and Rocky Linux and AlmaLinux): it carries a private Python
3.12 with every library, ONNX Runtime for CUDA 12 with NVIDIA's CUDA 12 and cuDNN 9 libraries, and
static FFmpeg, so the server needs only the NVIDIA driver (R525 or newer; 550+ recommended), no
CUDA toolkit. It is tuned for 64 GB of RAM and an NVIDIA L40S (48 GB), where it runs 4 jobs at once
on the CPU and 2 on the GPU.

The step-by-step guide, from the driver to the first sign-in in Chrome, with troubleshooting, is
[linux-server-install.pdf](linux-server-install.pdf) (also inside the package). In short:

```bash
sha256sum -c Lucida-0.3.0-linux-x86_64.tar.gz.sha256
tar -xzf Lucida-0.3.0-linux-x86_64.tar.gz && cd Lucida-0.3.0-linux-x86_64
./check-server.sh          # driver, GPU, memory, disk, port; changes nothing
sudo ./install.sh          # see --help for --port, --data, --hostname, --local-only, --no-sign-in
```

The installer puts the programs in `/opt/lucida`, the settings in `/etc/lucida/workbench.ini`
(the same keys as on Windows) and the case data in `/var/lib/lucida`, owned by a `lucida` system
account. It starts the `lucida` systemd service on HTTPS port 8443 with `network = lab` and
`auth = accounts`, opens the port in ufw or firewalld, restores SELinux labels on Red Hat, runs the
self-test and prints the addresses and the fingerprint of the certificate authority examiner PCs
install once (a copy goes to the installing user's home as `lucida-ca.crt`). The interface
addresses of the server are put in the certificate, so `https://<IP address>:8443` works without
a DNS name. `lucida gpu` shows where each model runs; on the GPU the CUDA provider uses
deterministic kernels, the default cuDNN algorithm (no timed search) and no TF32, so results stay
the same after a restart. Upgrading is running a newer package's `install.sh`; it keeps the
settings, the data and the certificate authority. `sudo /opt/lucida/uninstall.sh` removes it and
keeps the data unless `--purge` is given.

Get the package from the **Linux server package** workflow in GitHub Actions (artifact
`Lucida-Linux-Server`) or from a release. To build it yourself on a connected x86_64 Linux machine
with Python 3.11+, Node.js 22, curl and xz: `deploy/linux/build-package.sh`. CI builds it on
Ubuntu 22.04, then installs it on fresh Ubuntu 22.04 and 24.04 machines and in Red Hat's UBI 8 and
9 images with systemd: it checks the service runs as `lucida`, reaches it over HTTPS on the LAN
address with only the CA trusted, loads it in headless Chrome, runs the self-test with the network
blocked, upgrades in place, and uninstalls with and without `--purge`
(`deploy/linux/test-package.sh`). The guide is generated by `deploy/linux/make_install_guide.py`.

## Desktop app

`python -m forensic.desktop` (or `lucida-desktop`) runs Lucida in its own window instead of a
browser tab. It starts Lucida on 127.0.0.1 on a free port and shows it in a native WebView2
window (Edge's engine, built into Windows 10 and 11; it needs the optional `pywebview` package,
`pip install "lucida-forensic[desktop]"`). Without `pywebview`, or with `--browser`, it opens
the default browser. The window adds:

- a menu bar (File, Edit, View, Tools, Help) with keyboard shortcuts; F1 lists them all, and
  letter shortcuts follow the physical key, so they work on a Hebrew or Russian layout too;
- **Ctrl+O** opens a native Open dialog; the chosen files are hashed into the evidence store
  straight from their folder, with no upload through the window;
- native Save dialogs for the JSON bundle, exports and originals; HTML and PDF reports open
  in their own window;
- resizable evidence and tools panels (drag the edge; double-click resets), **Alt+1 / Alt+2**
  to hide them, **Alt+3** for the viewer alone, **F11** for full screen;
- **View > Viewer in new window** (Ctrl+Shift+N) opens a second window on the other monitor,
  showing only the viewer for the same item;
- **1** shows actual pixels on any monitor scaling: one image pixel on one screen pixel at
  125 %, 150 % or 200 % Windows scaling, with no resampling.

**Tools > This computer** shows the graphics card, where the models run, the CPU threads and
the RAM caches. On a workstation with an RTX A4000 (16 GB) and 48 to 128 GB of RAM:

- Learned models run on the A4000 through DirectML. Lucida asks DirectML for the
  high-performance GPU, so the NVIDIA card is used rather than the processor's own graphics
  (`FORENSIC_GPU_DEVICE=<n>` picks an adapter by number). Images are processed in tiles on a
  grid fixed by the image size, so the card's memory never changes a result; at those tile
  sizes a model run needs about a gigabyte, well within 16 GB. The models are loaded and
  checked in the background when the window opens, so the first enhancement does not wait.
- Classical filters stay on the CPU, on every core, because they give the same bits on every
  machine.
- RAM: results are cached in a quarter of RAM (12 GB at 48 GB, 32 GB at 128 GB), decoded
  video frames in an eighth (6 GB, 16 GB). One frame range may take a sixth of RAM as 32-bit
  frames (8 GB at 48 GB, about 21 GB at 128 GB; `FORENSIC_RANGE_MB` changes it): a range
  that would not fit is refused with the number of frames that do, instead of pushing the
  machine into swap. Filters on a range write into one result array, so a range needs one
  copy in memory, not two.

## Several examiners

By default Lucida has no sign-in (for sign-in on a shared server, see
[Sign-in for a shared server](#sign-in-for-a-shared-server)). Every request then acts as one
built-in local examiner, and every action is recorded in the audit log under that examiner's name. The name starts as the computer account's
full name (or `FORENSIC_EXAMINER`, if set before the first start) and is changed by clicking it
at the top right; the change is audited.

Heavy work (rendering a chain, analyses, format conversion, camera references, triage, the
review agent and reports) goes through one shared job queue, so several examiners can start
work at the same time without overloading the machine. **Jobs** in the top bar shows what is
running and waiting for everyone, with each job's owner and place in line; the owner or an
administrator can remove a job that has not started. The screens that started a job still wait
for its result as before; scripts can instead queue work with `POST /api/jobs` and follow it
with `GET /api/jobs/{id}`.

Jobs run in two lanes. Jobs that run a learned model share the graphics card, one at a time by
default (`gpu_jobs` in `workbench.ini`, or `FORENSIC_GPU_JOBS`); the rest run four at a time
(`job_workers`, or `FORENSIC_JOB_WORKERS`). On a 128 GB machine with an RTX A4000 (16 GB),
`job_workers = 6` and `gpu_jobs = 1` are a reasonable start; raise `gpu_jobs` to 2 only if
the models in use fit twice in the card's memory. Within a lane the next job goes to the
examiner with the fewest jobs running, then to whoever has waited longest for a turn, so one
examiner's batch does not hold up the others. A job that was running when Lucida
stopped is marked failed and must be started again; jobs still waiting resume.

### Examiners on other PCs

By default Lucida answers only on the computer it runs on. To let examiners use it from
their own PCs, tick **Let examiners connect from other PCs on the lab network** in the installer
(silent install: `/TASKS="labnetwork"`), or set `network = lab` in `workbench.ini` and restart.
Lucida then:

- listens on the lab network over **HTTPS** only, at `https://<computer name>:<port>`;
- creates, with no internet, a certificate authority for this machine and a server
  certificate naming its host names and addresses (in `<case data>\tls`, re-issued
  automatically when they change). Install `workbench-ca.crt` once on each examiner PC
  (double-click > Install Certificate > Local Machine > Trusted Root Certification Authorities,
  or `certutil -addstore Root workbench-ca.crt` as administrator), after comparing its SHA-256
  fingerprint with the one Lucida window prints. A lab with its own certificate sets
  `tls_cert` and `tls_key` in `workbench.ini` instead;
- answers only the computer's own names and addresses (the Host check). Without sign-in, anyone
  who can reach the address can use Lucida, so on a shared network also turn on
  [sign-in](#sign-in-for-a-shared-server);
- on an all-users install, opens its port in Windows Firewall for domain and private networks
  only (never public); uninstalling removes the rule. On a per-user install Windows asks the
  first time Lucida starts.

MCP servers started with `--http` and a `--host` other than loopback refuse to start unless
`FORENSIC_MCP_TOKEN` holds a random secret of at least 24 characters; every agent request must
then send `Authorization: Bearer <token>`.

### Sign-in for a shared server

With several people on one server, turn sign-in on: add `auth = accounts` to `workbench.ini`
(or set `FORENSIC_AUTH=accounts` for the container) and restart Lucida. Then:

1. The first person to open Lucida creates the **administrator** (user name, full name, password of
   at least 12 characters).
2. The administrator opens **their name > Users…** at the top right and adds each examiner with a
   role: **admin** (everything, every case, user management), **examiner** (works on cases they are
   a member of), **reviewer** (reads member cases and records review decisions, notes and bookmarks)
   or **viewer** (reads member cases only).
3. In each case, **Case > Who can open this case** lists the members; the administrator adds and
   removes them. A case an examiner creates is theirs automatically.

Sessions end after 30 minutes without use (`session_minutes` in `workbench.ini`, or
`FORENSIC_SESSION_MINUTES`) and after 12 hours at most. Five wrong passwords lock an account for
15 minutes; the administrator can reset a password, which also ends that person's sessions. Every
action is recorded under the signed-in person's name, and refused requests are recorded too.
Passwords are stored as scrypt hashes. On the lab network, Lucida only listens over HTTPS, so
passwords never cross the network in clear.

## Command line: batch conversion, verification, backup

The `lucida` command works on the same data folder without the browser. On Windows it is
`lucida.cmd` in the installation folder (it uses the installation's settings, Python and FFmpeg);
where Lucida was installed with pip, `lucida` is on the PATH.

```bash
lucida profile                                  # hardware profile in use
lucida batch scan D:\export                     # what each file is
lucida batch run --input D:\export --output E:\converted --format copy
lucida verify                                   # re-hash every original, check the audit log
lucida backup --output F:\lucida.backup --encrypt   # password from LUCIDA_BACKUP_PASSWORD or a prompt
lucida restore F:\lucida.backup --target D:\restored
```

Exit codes: 0 success, 1 partly succeeded, 2 usage error, 3 failed. A backup is consistent while
Lucida runs (SQLite online backup) and lists every file's SHA-256; `--encrypt` uses AES-256-GCM with
a key derived from the password by scrypt. A restore writes only into an empty folder and, before
reporting success, checks every file against the backup's list, re-hashes every original and
verifies the audit chain. Keep backups on separate media and test a restore now and then.

`profile = light|standard|high` in `workbench.ini` (or `FORENSIC_PROFILE`) fixes the hardware
profile used for detection, plate reading and batch conversion; by default it is chosen from the
processor, memory and graphics card.

## Container

```bash
docker build -t lucida:0.3.0 .
docker compose up            # http://127.0.0.1:8000
docker compose --profile agents up   # also the authenticate MCP server on the internal network
```

Case data lives in `./data`; `./import` is the only folder agents may ingest from. The
container starts as root only to hand `/data` to its unprivileged `forensic` user (Docker
creates a missing bind-mounted folder owned by root), then runs Lucida as that user.

Without Compose:

```bash
docker run -p 127.0.0.1:8000:8000 -v "$PWD/data:/data" lucida:0.3.0
```

Lucida joins a plain network so its port can reach your browser (Docker does not
publish ports from internal-only networks) and an internal network shared with the MCP
servers, which have no route out. Nothing in Lucida makes outbound calls; to enforce
that, run it on an air-gapped host or firewall the host.

The API answers only requests whose Host is `127.0.0.1` or `localhost`. If a TLS proxy or a
lab hostname fronts it, set `FORENSIC_ALLOWED_HOSTS` to that name (comma-separated for several),
or the browser gets `400 Invalid host header`.

## Air-gapped installation

On a connected machine with the same OS, CPU architecture and Python version as the target:

```bash
deploy/airgap/build-bundle.sh            # add --docker to include the container image
```

This writes `dist/lucida-<version>-airgap/` with every Python wheel, the backend
with its model weights, the built UI, the docs and `SHA256SUMS`. Carry it across, install
FFmpeg from the target OS's offline packages, then:

```bash
./install.sh /opt/lucida
/opt/lucida/run.sh
```

`install.sh` checks every file against `SHA256SUMS`, installs with `pip --no-index`, and runs
the acceptance self-test.

## Acceptance self-test

```bash
python -m forensic.selftest
```

Checks FFmpeg, re-hashes every model, checks ONNX Runtime gives bit-identical output on this
machine and names the device (GPU or CPU) the models run on, loads the face detector and the C2PA reader, then runs a small case end to end
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
  Lucida; nothing in Lucida calls an external model API.
