<img src="brand/lucida-icon.svg" width="64" height="64" alt="Lucida logo">

# Lucida

Lucida is a browser-based workbench for forensic image and video work: **Enhance** (clarify
footage and photos) and **Authenticate** (check whether an image is what it claims
to be), on one audited processing core. This is Phase 0 (foundations), Phase 1
(MVP), Phase 2 (depth) and Phase 3 (AI, deepfake checks, full parity) of the build plan *Forensic Image & Video Platform: Build Plan*.

Assumed user: a general forensic examiner working alone on one machine (the plan's
open question about target users is not answered yet). Multi-user access control,
SSO and object storage come with the lab deployment.

## Run it

Requirements: Python 3.11+, Node 20+, FFmpeg (`ffmpeg` and `ffprobe` on the PATH).

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# front end (built once; served by the backend)
cd ../frontend
npm install
npm run build

# start
cd ../backend
FORENSIC_DATA_DIR=./data uvicorn forensic.api:app --port 8000
# open http://127.0.0.1:8000; the first visit creates the administrator account
```

For front-end development run `npm run dev` in `frontend/` (port 5173, proxies `/api`
to port 8000).

How to use it: [docs/user-manual.md](docs/user-manual.md). How every algorithm works and why it
was chosen: [docs/algorithms.md](docs/algorithms.md).

Windows (offline installer), Docker and air-gapped installation: see
[docs/deployment.md](docs/deployment.md). After any
installation, `python -m forensic.selftest` checks the tools, models and a full case end to end.

Tests: `cd backend && pytest` (about 60 tests, a couple of minutes; includes a quick
validation run that fails if detector accuracy drops below the published floor).

## What is in it

**Forensic core (Phase 0)**

| Plan item | Where | How |
|---|---|---|
| Evidence store, hashes | `forensic/store.py` | SHA-256 and MD5 computed while copying in; stored under its SHA-256 and made read-only. "Verify original" re-hashes on demand and at report time. |
| Filter-graph engine | `forensic/graph.py`, `forensic/filters/` | A chain is a JSON document (source + ordered filter nodes). Each filter has a stable id, semantic version, parameter schema, description and reference. Chains pin the source file's SHA-256 and each filter's version. |
| Audit log | `forensic/audit.py` | Append-only, hash-chained: every entry contains the previous entry's hash, so edits or deletions are detected. Agent actions are tagged with agent name and model. |
| Report generator v1 | `forensic/report.py` | HTML, PDF and a JSON bundle: evidence and integrity, every step with parameters and references, before/after images, analyses with maps and reading guidance, the audit trail and software versions. |
| Web viewer | `frontend/` | Pixel-exact zoom (nearest neighbour above 100%), pixel inspector, hold **B** to compare with the original, frame scrubber with I/P/B frame types. |
| **Gate: re-run is byte-identical** | `tests/test_core.py` | Every filter is rendered, then re-run from the original with no cache; the output pixel hash must match. The report re-runs every chain and says "reproduced bit for bit" or "DID NOT REPRODUCE". |

**MVP (Phase 1)**

- *Formats:* anything Pillow/OpenCV reads for stills (JPEG, PNG, TIFF, BMP, WebP); anything FFmpeg decodes for video, with frame-accurate decoding by frame index.
- *Enhance:* levels, brightness/contrast, curves (monotone), CLAHE, histogram equalisation, colour balance (manual / grey world), greyscale/channel, invert; crop, rotate, flip, resize, aspect-ratio correction, perspective correction (click four corners), lens distortion (Brown-Conrady), deinterlace (field / blend / split); median, Gaussian, bilateral, non-local means, FFT periodic-noise removal; unsharp mask; multi-frame stabilisation (ECC, with optional region of interest), frame integration (mean / median / sigma-clipped), pick frame.
- *Authenticate:* file structure and metadata (EXIF, GPS, XMP, JPEG marker walk, quantisation tables with libjpeg-quality match, thumbnail consistency, trailing data; container, frame types, GOP and timestamp gaps for video), Error Level Analysis, JPEG Ghost.
- *MCP:* `python -m forensic.mcp_server evidence|enhance|authenticate|measure|case [--http PORT]`.

**Depth (Phase 2)**

| Plan item | Where | How |
|---|---|---|
| DVR vendor formats | `forensic/formats.py`, `forensic/dvr.py` | Recorder exports are identified by signature (Dahua DAV, Hikvision, Xiongmai/XMEye, HiSilicon HXVS, MPEG-PS/TS, raw H.264/H.265/MPEG-4/MPEG-2 and others). "Make working copy" either reads the vendor container with Lucida's own parser (Dahua, Hikvision, XMEye, HiSilicon, MPEG-PS) and keeps the recorder's per-frame timing, clock, gaps and anomalies, or stream-copies into MPEG-TS with FFmpeg, or carves the H.264/H.265/MPEG-4/MPEG-2/Motion JPEG stream out of an unknown container with synthetic timestamps at a stated frame rate. Nothing is re-encoded. The copy is new evidence linked to the original, with the method recorded. |
| Deblur | `forensic/filters/deblur.py` | Wiener and Richardson-Lucy (optional total-variation regularisation) for motion, defocus and Gaussian blur. "Estimate blur" suggests a motion length and angle from the cepstrum. |
| Multi-frame super resolution | `forensic/filters/multiframe.py` | Registers frames to a reference (ECC), then iterative back-projection with robust median fusion (Irani-Peleg / Farsiu), 2-4x. |
| Authenticate detectors added after the Amped comparison | `forensic/analysis/qtdb.py`, `dimples.py`, `gradient.py`, `fusion.py`, `camera.py`; `forensic/qtables.py` | Quantisation tables matched against an offline camera and software database (built from CC0 and free sample collections, growable from a lab's own reference images); JPEG dimples (Agarwal-Farid) with a missing-dimple map; luminance gradient (LGA) map; PRNU tampering map (Chen et al. 2008); Dempster-Shafer fusion map. Each with a detector card except LGA, a visual aid. |
| Measurement | `forensic/measure.py` | Height by single-view metrology (Criminisi): vanishing point and ground horizon from traced lines plus one reference height. Distance and speed through a ground-plane homography from surveyed points; speed uses the frames' own timestamps. Every result carries a seeded Monte Carlo 95 % interval from clicking and survey uncertainty, and can be taken on an enhanced result. |
| Double JPEG | `forensic/analysis/double_jpeg.py` | Aligned double compression (Bianchi-Piva) with a first-quality estimate and a per-block map of single-compressed (pasted) regions; non-aligned grid detection for crops between saves. |
| PRNU | `forensic/prnu.py`, `forensic/analysis/camera.py` | Camera references are built from the case's own images (ML fingerprint, Binghamton-style residual). Matching reports peak-to-correlation energy (decision at PCE 60) with rotation, crop and scale search, and the number of hypotheses tried. |
| Triage agent | `forensic/triage.py`, `forensic/agents/triage.py` | "Triage all" runs the Smart Report battery on every item and assigns red / yellow / green by fixed, versioned rules, so examiners know where to look first. Its notes are labelled agent opinion. |
| Planner agent | `forensic/agents/planner.py` | Given a goal ("make the plate readable", "brighten the dark scene"), picks a workflow, renders candidate chains on the region of interest, scores them with no-reference quality metrics and proposes the best two as chains whose steps the examiner must accept. |
| **Gate: detector accuracy published** | `forensic/validation/`, `docs/validation/` | `python -m forensic.validation run` measures every authentication detector on a seeded corpus and writes a detector card (task, threshold, TPR, FPR, AUC, conditions, known failure modes). The app shows each card's headline next to the detector and the report prints it. Add `--pristine <folder of camera JPEGs>` to measure on real images. |

**AI, deepfake checks and full parity (Phase 3)**

| Plan item | Where | How |
|---|---|---|
| AI models | `forensic/models/`, [docs/ai-models.md](docs/ai-models.md) | A pinned manifest: every model's weights SHA-256, source, licence, training data and limits. Weights ship in the package and are verified before every load; inference runs on the GPU (DirectML on Windows) when a probe shows it reproduces bit for bit, otherwise on every CPU core; the device is printed in the report. Labs add their own models with a model card (`python -m forensic.models install`). |
| AI-assisted enhancement | `forensic/filters/ai.py`, `forensic/policy.py` | AI super resolution (FSRCNN / ESPCN, 2-4x) and face redaction (YuNet). Both are badged "AI" in the UI and report with the model's hash. Super resolution is *generative*: watermarked "illustrative only", measurement refused, and it cannot be accepted or exported until an examiner writes a justification, which is logged. Agents cannot write one. |
| AI-generated images | `forensic/analysis/synthetic.py` | Content Credentials (C2PA) read and validated offline, AI-generation declarations in IPTC/XMP and generator text chunks; camera-pipeline traces (CFA demosaicing and resampling); a slot for a learned detector the lab licenses and validates (none is bundled; see the licence policy). |
| Deepfake / face swap | `forensic/analysis/faces.py` | Faces found by YuNet (or marked by hand) are compared with 200 random places in the same image for noise and detail, and with the image's double-JPEG history; on video, faces are tracked over sampled frames and flicker is measured. Results are z-scores with a reliability grade by face size. |
| Video authentication | `forensic/analysis/video.py` | Double encoding from the variation of prediction footprint (former I-frames re-coded as P-frames), with the first GOP estimated; frame deletion (jumps), duplication (repeated moving segments), repeated frames and timestamp gaps. |
| Photogrammetry | `forensic/measure.py` | Height by camera resection: at least six surveyed 3D points give focal length, position and pose; the target's height is read where the ray through its top meets the vertical through its base. Monte Carlo interval from clicking and survey uncertainty. |
| Reviewer agent | `forensic/agents/reviewer.py` | Re-hashes originals, checks the audit chain, re-runs every rendered chain twice from the originals, re-runs analyses and measurements and compares hashes, checks model weights, and flags generative, unjustified and unaccepted steps. Its review is logged, shown in the Audit tab and printed in the report. |
| Air-gapped deployment | `Dockerfile`, `docker-compose.yml`, `deploy/airgap/`, `forensic/selftest.py` | A container on an internal-only network, or an offline bundle (wheels, UI, models, `SHA256SUMS`) installed with `pip --no-index` and accepted by the self-test. Nothing calls out at run time; an LLM agent connects through MCP, so a self-hosted model works. |
| Validation | `forensic/validation/phase3.py` | Cards for camera traces, face consistency, double encoding and frame integrity, with the conditions where each fails. |

## Rules the code enforces

- Every API request needs a signed-in local account; actions are audited under that account's full name, and a name sent by the browser is ignored. Administrators add examiners under **Users** in the account menu.
- Renders, analyses, conversions, reviews and reports run through one shared job queue, visible to everyone under **Jobs** (see [docs/deployment.md](docs/deployment.md#several-examiners)).
- Originals are never written after ingest; all work is on derived, content-addressed outputs.
- Live previews are not stored or audited and are never evidential. "Render result" runs the saved chain from the original and records the output hash.
- Steps an agent adds through MCP are marked *proposed*. They stay out of the report's findings until an examiner accepts them in the UI; an agent cannot accept its own proposals, and cannot remove, disable or reorder a step the examiner wrote or accepted.
- Evidence must be self-contained: playlists and other formats that make FFmpeg read outside files are rejected at ingest.
- The API answers only requests addressed to `127.0.0.1` or `localhost`, which blocks DNS-rebinding pages. Add any other name the lab uses to reach it to `FORENSIC_ALLOWED_HOSTS` (comma-separated).
- Agents can only ingest files from `FORENSIC_IMPORT_DIR`. Text read from evidence (EXIF comments, XMP, filenames) is returned to agents wrapped as untrusted file content.
- Filters use every CPU core, but only by splitting work into independent pieces (row blocks, channels, frames), so a result is bit-identical on one thread or many; a test checks this for every filter. OpenCL stays off. `FORENSIC_THREADS`, `FORENSIC_CACHE_MB`, `FORENSIC_FRAME_CACHE_MB` and `FORENSIC_AI_DEVICE` tune it (see `forensic/compute.py`).
- Authentication findings are worded "consistent / inconsistent with", with a reading guide per analysis; no single score is presented as proof.
- Learned models run only from pinned, verified weights; their id and hash are printed next to every result they produced, and AI-assisted steps and analyses are labelled as such.
- Generative steps are illustrative only: watermarked, never measured on, and exported only with an examiner's logged justification.

## Connecting an agent

```jsonc
// e.g. Claude Code / Claude Desktop MCP config
{
  "mcpServers": {
    "forensic-evidence":     { "command": "python", "args": ["-m", "forensic.mcp_server", "evidence"],     "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data", "FORENSIC_IMPORT_DIR": "./import" } },
    "forensic-enhance":      { "command": "python", "args": ["-m", "forensic.mcp_server", "enhance"],      "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } },
    "forensic-authenticate": { "command": "python", "args": ["-m", "forensic.mcp_server", "authenticate"], "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } },
    "forensic-measure":      { "command": "python", "args": ["-m", "forensic.mcp_server", "measure"],      "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } },
    "forensic-case":         { "command": "python", "args": ["-m", "forensic.mcp_server", "case"],         "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } }
  }
}
```

Set `FORENSIC_AGENT_NAME` and `FORENSIC_AGENT_MODEL` so the audit log records which agent and model acted.

## Not yet

- No learned AI-image or deepfake detector is bundled: the published ones reviewed carry non-commercial licences or lack validation on current generators. The slot (`synthetic.learned`) takes one the lab licenses and validates.
- Every detector is so far validated on synthetic data only, including the phase 3 checks; no real photographs, real face swaps or recorder footage have been run. Run the validation with real material before relying on a detector in casework.
- Clone detection, OCR, multi-view triangulation, native parsers for vendor formats FFmpeg cannot demux (they are carved instead), Postgres + S3 Object Lock, SSO, MCP agents sharing the API's job queue, and sandboxed parsers. Chains are linear; branching graphs use the same document format later.

## Layout

```
backend/
  forensic/
    api.py          HTTP API (FastAPI)
    workbench.py    service layer shared by the API and MCP servers
    graph.py        filter-graph engine, hashing, cache
    filters/        enhancement filters (exposure, geometry, denoise, deblur, multi-frame, SR, AI)
    analysis/       authentication analyses (metadata, JPEG, ELA, Ghost, double JPEG, PRNU,
                    provenance and camera traces, faces, video double encoding and frame integrity)
    models/         pinned model manifest, bundled weights and licences
    policy.py       rules for generative (AI) steps
    selftest.py     installation acceptance test
    formats.py      recorder export identification, remux and carving
    dvr.py          native parsers for vendor recorder containers
    measure.py      height, distance and speed with uncertainty
    prnu.py         sensor fingerprint estimation and matching
    triage.py       Smart Report rules
    agents/         triage, planner and reviewer agents
    validation/     detector validation lab and detector cards
    store.py        write-once evidence store
    audit.py        hash-chained audit log
    report.py       bundle, HTML and PDF reports
    mcp_server.py   MCP servers for agents
  tools/            model conversion (source graphs to ONNX)
  tests/
deploy/airgap/      offline bundle build and install scripts
frontend/           React + TypeScript viewer (Vite)
docs/validation/    latest published validation results
docs/ai-models.md   bundled models, generative policy, installing a lab detector
docs/deployment.md  Windows installer, Docker, air-gapped installation, self-test
docs/user-manual.md how to operate the workbench
docs/algorithms.md  every algorithm, its parameters, and why it was chosen over the alternatives
```
