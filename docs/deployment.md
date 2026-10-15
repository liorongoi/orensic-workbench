# Deployment

## Windows (offline installer)

`Lucida-<version>-Setup.exe` installs everything Lucida needs on a
Windows 10 or 11 x64 machine with no internet connection: a private Python 3.12, every Python
library, FFmpeg and FFprobe, the Visual C++ runtime, the hash-pinned AI models and the built
UI. It needs no Docker, no system Python and no administrator rights (choose "Install for all
users" in the first dialog to install under Program Files for everyone on the machine).

Get the installer from the **Windows installer** workflow in GitHub Actions (artifact
`Lucida-Windows-Setup`, with its `.sha256`), or from a release when a `v*` tag is
pushed. Carry the `.exe` to the offline machine, check its SHA-256 if your lab requires it
(`certutil -hashfile Lucida-0.3.0-Setup.exe SHA256`), and run it. The wizard asks
for:

- the installation folder;
- the case data folder (cases, write-once evidence store, derived outputs, audit log, logs).
  It is kept when Lucida is uninstalled or upgraded. For one user it stays private to that
  account. For all users, only members of the local **Forensic Examiners** group (the account
  that ran the installer is added), administrators and SYSTEM can open it; add each examiner with
  `net localgroup "Forensic Examiners" <account> /add`, and they sign out and in again;
- the local port (default 8000; Lucida listens on 127.0.0.1 only, unless you tick the
  lab-network option, see [Examiners on other PCs](#examiners-on-other-pcs)).

At the end it offers the acceptance self-test: it re-hashes every installed file against the
installer's `SHA256SUMS`, then runs `forensic.selftest`, and saves its output under
`<case data>\logs`. Start Lucida from **Start > Lucida**: a console window
shows the address and log, and the browser opens Lucida; closing the window stops it.
The Start menu also has **Self-test**, **Case data folder**, **User manual** and **Algorithm guide**.

Settings live in `<install folder>\workbench.ini` (`data_dir`, `import_dir`, `host`, `port`, and
optionally `c2pa_trust_anchors`, `allowed_hosts`, the extra names browsers may use to reach it,
`job_workers` and `gpu_jobs`, and `network`, `tls_cert` and `tls_key`; see
[Several examiners](#several-examiners)). Compute settings are
optional too: `threads` (default every core), `cache_mb` (RAM for intermediate results, default a
quarter of RAM up to 32 GB), `frame_cache_mb` (decoded video frames, default an eighth up to 16 GB)
and `ai_device` (`auto`, `gpu` or `cpu`; auto uses the GPU through DirectML when it reproduces results
bit for bit). Running a newer installer upgrades in place and keeps
these settings and the case data. Silent installation for managed rollouts:

```
Lucida-0.3.0-Setup.exe /VERYSILENT /ALLUSERS /DIR="C:\Program Files\Lucida" /DATADIR="D:\Cases" /PORT=8000
```

To build the installer yourself, on a connected Windows x64 machine with Python 3.12 and
Node.js 22: `powershell -ExecutionPolicy Bypass -File deploy\windows\build-installer.ps1`
(Inno Setup 6 is used, and installed with Chocolatey if missing). It writes
`dist\Lucida-<version>-Setup.exe`; `BUILDINFO.txt` in the installation records the
commit and the SHA-256 of the Python, FFmpeg and Visual C++ downloads it was built from.
CI installs every build on a fresh Windows runner, for one user and for all users, blocks the
bundled Python from the network, runs the self-test, starts Lucida, and uninstalls
(`deploy\windows\test-installer.ps1`).

## Several examiners

Lucida keeps its own accounts, since the lab machine is offline. The first person to
open a fresh installation creates the **administrator** account; the administrator then adds
each examiner from the account menu (top right) > **Users**, and can reset a password, make
someone an administrator, or disable an account (which signs it out at once). Every action is
recorded in the audit log under the account's full name. Sessions last 12 hours; five wrong
passwords in a row lock that user name for a minute.

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
- answers only the computer's own names and addresses (the Host check), and needs a signed-in
  Lucida account on every request; the sign-in cookie is sent over HTTPS only;
- on an all-users install, opens its port in Windows Firewall for domain and private networks
  only (never public); uninstalling removes the rule. On a per-user install Windows asks the
  first time Lucida starts.

MCP servers started with `--http` and a `--host` other than loopback refuse to start unless
`FORENSIC_MCP_TOKEN` holds a random secret of at least 24 characters; every agent request must
then send `Authorization: Bearer <token>`.

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
