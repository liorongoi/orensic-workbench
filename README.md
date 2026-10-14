# Forensic Workbench

A browser-based workbench for forensic image and video work: **Enhance** (clarify
footage and photos) and **Authenticate** (check whether an image is what it claims
to be), on one audited processing core. This is Phase 0 (foundations) and Phase 1
(MVP) of the build plan *Forensic Image & Video Platform: Build Plan*.

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
# open http://127.0.0.1:8000
```

For front-end development run `npm run dev` in `frontend/` (port 5173, proxies `/api`
to port 8000).

Tests: `cd backend && pytest` (20 tests, about 10 seconds).

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
- *MCP:* `python -m forensic.mcp_server evidence|enhance|authenticate|case [--http PORT]`.

## Rules the code enforces

- Originals are never written after ingest; all work is on derived, content-addressed outputs.
- Live previews are not stored or audited and are never evidential. "Render result" runs the saved chain from the original and records the output hash.
- Steps an agent adds through MCP are marked *proposed*. They stay out of the report's findings until an examiner accepts them in the UI; an agent cannot accept its own proposals.
- Agents can only ingest files from `FORENSIC_IMPORT_DIR`. Text read from evidence (EXIF comments, XMP, filenames) is returned to agents wrapped as untrusted file content.
- OpenCV runs single-threaded with OpenCL off (`FORENSIC_DETERMINISTIC=1`, the default) so results do not depend on thread scheduling.
- Authentication findings are worded "consistent / inconsistent with", with a reading guide per analysis; no single score is presented as proof.

## Connecting an agent

```jsonc
// e.g. Claude Code / Claude Desktop MCP config
{
  "mcpServers": {
    "forensic-evidence":     { "command": "python", "args": ["-m", "forensic.mcp_server", "evidence"],     "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data", "FORENSIC_IMPORT_DIR": "./import" } },
    "forensic-enhance":      { "command": "python", "args": ["-m", "forensic.mcp_server", "enhance"],      "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } },
    "forensic-authenticate": { "command": "python", "args": ["-m", "forensic.mcp_server", "authenticate"], "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } },
    "forensic-case":         { "command": "python", "args": ["-m", "forensic.mcp_server", "case"],         "cwd": "backend", "env": { "FORENSIC_DATA_DIR": "./data" } }
  }
}
```

Set `FORENSIC_AGENT_NAME` and `FORENSIC_AGENT_MODEL` so the audit log records which agent and model acted.

## Not yet (later phases of the plan)

Proprietary DVR formats, multi-frame super resolution and deconvolution, measurement and photogrammetry, PRNU, double-JPEG statistics, clone detection, C2PA validation, video authentication, AI and deepfake detectors, the triage / planner / reviewer agents, Postgres + S3 Object Lock + job queue, user accounts and SSO, sandboxed parsers, and air-gapped packaging. MVP chains are linear; branching graphs use the same document format later.

## Layout

```
backend/
  forensic/
    api.py          HTTP API (FastAPI)
    workbench.py    service layer shared by the API and MCP servers
    graph.py        filter-graph engine, hashing, cache
    filters/        enhancement filters (exposure, geometry, denoise, multi-frame)
    analysis/       authentication analyses (metadata, JPEG structure, ELA, Ghost)
    store.py        write-once evidence store
    audit.py        hash-chained audit log
    report.py       bundle, HTML and PDF reports
    mcp_server.py   MCP servers for agents
  tests/
frontend/           React + TypeScript viewer (Vite)
```
