# Lucida platform architecture (v0.4)

This document answers the "before implementation" steps of the master project prompt:
what the repository already contains, the architecture and module breakdown, which
features need special decoders, licensed parts, GPUs or external models, the interfaces
between subsystems, the milestones, and the first vertical slice.

## 1. What was already here (v0.3)

Lucida started as a single-examiner forensic image and video workbench:

| Area | Existing module | Notes |
|---|---|---|
| Evidence store | `forensic/store.py` | Write-once, content-addressed originals; SHA-256 + MD5 at ingest; re-hash on demand. |
| Audit | `forensic/audit.py` | Append-only, hash-chained log; any edit or deletion breaks the chain. |
| Processing graph | `forensic/graph.py`, `forensic/filters/` | JSON chain (source + ordered nodes); filters have id, semver, parameter schema, reference; byte-identical re-run gate. |
| Enhancement | 38 filters | Exposure, colour, geometry, denoise, deblur (Wiener/RL/TV/blind/variable), multi-frame (ECC stabilise, integration, SR, temporal denoise, turbulence), AI SR (walled off as generative), face redaction. |
| Authentication | 23 analyses | Metadata, ELA, Ghost, double JPEG (aligned/non-aligned), dimples, noise, PRNU (camera ID and tamper map), copy-move, LGA, fusion, geometry, C2PA, CFA traces, learned GAN/diffusion detector, faces, video double encoding and frame integrity. |
| Measurement | `forensic/measure.py` | Single-view metrology, planar homography distance/speed, camera resection; Monte Carlo 95 % intervals. |
| DVR | `forensic/formats.py`, `forensic/dvr.py` | Signature identification, native parsers, remux, carving. |
| Reports | `forensic/report.py` | JSON bundle → HTML/PDF with re-run verification. |
| Agents | `forensic/agents/` | Triage, planner, reviewer; MCP servers. |
| UI | `frontend/` (React + TS, Vite) | Case list, three-pane workspace (evidence, viewer, Enhance/Authenticate/Measure/Audit). |

Stack: Python 3.11+ / FastAPI / SQLite (WAL) / OpenCV / NumPy / FFmpeg / ONNX Runtime,
React 19 + TypeScript front end served by the backend, optional pywebview desktop
wrapper and an offline Windows installer.

## 2. Gaps against the master prompt, and where they are closed

| Prompt section | Gap in v0.3 | New module (v0.4) |
|---|---|---|
| 3A Evidence management | No case numbers, legal authority, sub-cases, tags, bookmarks, chain of custody, SHA-512, duplicate detection, search, legal hold/retention, export manifests | `forensic/casework.py` |
| 4A/4B Image tools | No exposure in stops, contrast stretch, homomorphic, shadows/highlights, neutral-area white balance, colour spaces, channel mixer, threshold, edges, morphology, Fourier view/band filters, Wiener denoise, deblocking, Laplacian sharpen, fisheye, 360° unwrap | `forensic/filters/extra.py` |
| 4C/5 Comparison and video ops | No clip extraction, frame export, scene detection, image-sequence ↔ video, concatenation, optical flow, background subtraction, frame difference, similarity metrics, processed-video export | `forensic/videoops.py`, `forensic/filters/extra.py` |
| 7 Branching workflows | Linear chains only | Chain fork + presets in `forensic/casework.py` |
| 8 Multi-camera | None | `forensic/sync.py` |
| 9 Detection, tracking, LPR | None | `forensic/intel/` (detector adapters, tracker, LPR, index) |
| 10 Batch conversion + CLI | Single-item convert only | `forensic/batch.py`, `forensic/cli.py` |
| 12 Annotation, redaction, exhibits | Face redaction filter only | `forensic/redaction.py` |
| 14 Report templates | One full-case report | Templates and new sections in `forensic/report.py` |
| 17 Security | No sign-in (deliberate for single-examiner installs) | Optional lab mode `FORENSIC_AUTH=accounts`: sign-in, roles, case membership, lockout, session timeout (`forensic/security.py`); backup/restore with verification and optional AES-256-GCM encryption |
| 19 API | Unversioned routes, no event stream | `/api/v1/*` alias, server-sent events for jobs, pagination, rate limit |

## 3. Module breakdown and interfaces

```
            ┌───────────── React UI (frontend/) ─────────────┐
            │ Cases · Evidence · Enhance · Authenticate ·    │
            │ Measure · Intelligence · Multi-camera · Redact │
            │ · Batch conversion · Jobs · Audit · Admin      │
            └───────────────────────┬────────────────────────┘
                     REST /api/v1 + SSE (forensic/api.py)
            ┌───────────────────────┴────────────────────────┐
            │ Workbench service layer (forensic/workbench.py) │  ← MCP servers and CLI call the same layer
            ├──────────┬──────────┬───────────┬──────────────┤
            │ casework │ videoops │ redaction │ sync          │
            │ batch    │ intel/   │ security  │ report        │
            ├──────────┴──────────┴───────────┴──────────────┤
            │ graph engine · filters · analyses · measure     │
            │ media (FFmpeg/OpenCV adapters) · formats/dvr    │
            ├────────────────────────────────────────────────┤
            │ EvidenceStore (write-once originals, derived)   │
            │ Database (SQLite → Postgres) · AuditLog (chain) │
            └────────────────────────────────────────────────┘
```

Interfaces between subsystems:

- **Evidence references.** Every module refers to evidence by `evidence.id` and pins the
  original's SHA-256. Derived media are stored content-addressed (`derived/<sha256>.<ext>`)
  or, when they are new evidence items (clips, conversions, redacted copies), as linked
  evidence rows with `derived_from` and a `conversion` record describing the operation.
- **Operations.** Every state-changing call takes an `Actor` and writes one audit entry with
  parameters and an output hash. Anything heavy goes through the shared `JobQueue`.
- **Decoder adapter.** `media.probe / decode_image / decode_frames_u8 / frame_table`
  (FFmpeg/OpenCV/Pillow) plus `formats.sniff / convert` and `dvr.parse` for recorder
  formats. New decoders register a signature and a converter.
- **Filter plug-in contract.** `@register(id, name, version, category, description,
  reference, params, input_kind)` on a pure function `(pixels, params) -> pixels`.
- **Analysis plug-in contract.** `@register_analysis` returning `summary / findings / flags / maps`
  with a detector card for validated detectors.
- **Detector adapter contract** (`forensic/intel/detectors.py`):
  `Detector.detect(frame_bgr_u8, classes, min_score) -> [Detection(cls, score, box)]`
  with `id`, `version`, `model_sha256`, `licence`. The bundled adapter runs YOLOX-Tiny
  (Apache-2.0) through ONNX Runtime; a lab can register another model.
- **OCR adapter contract** (`forensic/intel/lpr.py`): `OcrEngine.read(plate_u8) ->
  [Candidate(text, confidence, per_char)]`. Bundled adapter: Tesseract 5 (Apache-2.0)
  when installed; otherwise LPR reports "OCR engine not installed" and still stores
  localised, rectified crops for manual reading.
- **Plate localisation:** LPD-YuNet (OpenCV Zoo, Apache-2.0), returning four corners
  used for perspective rectification.

## 4. Features that need special decoders, licences, GPUs or models

| Feature | Dependency | Status |
|---|---|---|
| Closed DVR formats (Avigilon AVK, Genetec G64, encrypted exports) | Vendor SDK / player | Refused at ingest with export advice (no universal-support claim). |
| HEVC macroblock view | FFmpeg export of CU data | Not available; H.264/MPEG only. |
| Object detection | YOLOX-Tiny ONNX weights (Apache-2.0), ONNX Runtime | Bundled, hash-pinned. GPU optional (CPU fallback). |
| Plate localisation | LPD-YuNet ONNX (Apache-2.0) | Bundled, hash-pinned. |
| Plate OCR | Tesseract 5 binary | Optional system dependency; adapter detects it. |
| Face detection | YuNet (MIT) | Already bundled. |
| AI super resolution | FSRCNN/ESPCN/Real-ESRGAN | Already bundled; generative policy applies. |
| Postgres / object storage with Object Lock | Lab deployment | Schema is SQLite today; documented mapping. |
| Encryption at rest of the live store | OS volume encryption (BitLocker/LUKS) | Documented; backups get AES-256-GCM in-app. |

## 5. Milestones

1. **Vertical slice** (already worked in v0.3, re-verified by `tests/test_vertical_slice.py`):
   create case → ingest image/video → SHA-256/512 → metadata → non-destructive chain →
   rendered derivative → export package with manifest → report documenting it.
2. **Evidence management** — casework fields, custody, tags, bookmarks, duplicate detection, search, holds, export manifests.
3. **Image and video depth** — new filters, video operations, processed-video export, scene detection, similarity.
4. **Batch conversion + CLI** — queue, profiles, remux vs transcode, frame-count checks, manifests, exit codes.
5. **Redaction and exhibits** — annotations, keyframed and detector-driven redaction, verification.
6. **Multi-camera** — cameras, offsets/drift by timestamp, event, audio and visual correlation, synchronized frames, timeline.
7. **Intelligence** — detection, tracking, LPR with review queue, searchable index.
8. **Reporting, security, operations** — templates, lab-mode accounts and roles, backup/restore, API v1 + SSE.
9. **Validation and showcase report** — tests per milestone and the algorithm-effects PDF.
10. **Still-image traffic detection** (v0.5, [traffic.md](traffic.md)) — per-image database with local
    time, day/night lighting, polygon scan areas and masks, ensemble of learned detector + vehicle
    lamps + background change, learned scenery lights, review and corrections, training from
    corrections with notifications and re-run of unedited images only, Excel export, report and
    charts from text. Gate: `tests/test_traffic.py`.

Every milestone ends with the test suite passing and the front end building.
