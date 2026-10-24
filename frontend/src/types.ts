export type ParamType = "float" | "int" | "bool" | "choice" | "points" | "text";

export interface ParamSchema {
  name: string;
  type: ParamType;
  default: unknown;
  label: string;
  help: string;
  min?: number;
  max?: number;
  step?: number;
  count?: number;
  choices?: string[];
}

export interface FilterSchema {
  id: string;
  name: string;
  version: string;
  category: string;
  description: string;
  reference: string;
  input_kind: "image" | "stack";
  output_kind: "image" | "stack";
  generative: boolean;
  ai?: boolean;
  params: ParamSchema[];
}

export interface ModelInfo {
  id: string;
  file: string;
  sha256: string;
  kind: string;
  task: string;
  architecture?: string;
  reference?: string;
  source: string;
  licence: string;
  training_data: string;
  generative?: boolean;
  limits?: string;
  bundled: boolean;
}

export interface AnalysisSchema {
  id: string;
  name: string;
  version: string;
  category: string;
  description: string;
  reference: string;
  applies_to: string[];
  reading_guide: string;
  params: ParamSchema[];
  card?: DetectorCard | null;
  ai?: boolean;
  unavailable?: string | null;
}

export interface DetectorCard {
  detector: string;
  version: string;
  validated_on: string;
  corpus: string;
  seeded?: boolean;
  results: Record<string, Record<string, unknown>>;
  known_failure_modes: string[];
}

export interface Case {
  id: string;
  name: string;
  examiner: string;
  notes: string;
  created_at: string;
  evidence_count?: number;
}

export type MediaType = "image" | "video" | "container";

export interface Probe {
  media_type: MediaType;
  format?: string;
  width: number;
  height: number;
  frame_count: number;
  codec?: string;
  frame_rate?: string;
  duration?: number;
  field_order?: string;
  timestamps?: string;
  identified?: { family: string | null; label: string | null; elementary_codec: string | null; parser?: boolean };
}

export interface Conversion {
  method: string;
  description: string;
  source_sha256: string;
  output_sha256: string;
  command?: string;
  codec?: string;
  frame_rate?: number;
  carve?: { nal_units_kept?: number; nal_units_dropped?: number; nal_units_trimmed?: number; jpeg_frames_kept?: number };
  parser?: string;
  notes?: string[];
  timing?: RecorderTiming;
}

/** Timing read from a vendor container by Lucida's own parser. */
export interface RecorderTiming {
  source: string;
  frames: number;
  key_frames: number;
  duration_s: number;
  median_frame_interval_ms: number;
  measured_frame_rate: number | null;
  recorder_frame_rate: number | null;
  gaps: { frame: number; gap_ms: number }[];
  gap_count: number;
  timestamp_anomaly_count: number;
  recorder_clock_start?: string;
  recorder_clock_end?: string;
}

export interface Evidence {
  id: string;
  case_id: string;
  filename: string;
  sha256: string;
  md5: string;
  size: number;
  media_type: MediaType;
  probe: Probe;
  ingested_by: string;
  ingested_at: string;
  derived_from?: string | null;
  conversion?: Conversion | null;
  removed?: Removal | null;
}

export interface Removal {
  by: string;
  at: string;
  reason: string;
}

export interface ChainNode {
  id: string;
  filter: string;
  version?: string;
  params: Record<string, unknown>;
  enabled: boolean;
  note?: string;
  proposed_by?: string;
  accepted_by?: string;
  justification?: string;
}

export interface Graph {
  schema?: number;
  source: { evidence_id?: string; sha256?: string; frames: { start: number; end: number } | null };
  nodes: ChainNode[];
}

export interface Chain {
  id: string;
  case_id: string;
  evidence_id: string;
  name: string;
  graph: Graph;
  graph_hash: string;
  output_hash: string | null;
  created_at: string;
  updated_at: string;
  removed?: Removal | null;
}

export interface Flag {
  level: "info" | "warning";
  text: string;
  code?: string;
}

export interface AnalysisRun {
  id: string;
  evidence_id: string;
  name: string;
  version: string;
  params: Record<string, unknown>;
  result: { summary: string; findings: Record<string, unknown>; flags: Flag[]; maps: Record<string, string> };
  output_hash: string;
  actor: string;
  created_at: string;
}

export interface AuditEntry {
  seq: number;
  case_id: string | null;
  ts: string;
  actor: string;
  actor_kind: "human" | "agent";
  action: string;
  target: string | null;
  params: Record<string, unknown>;
  output_hash: string | null;
  entry_hash: string;
}

export interface FrameInfo {
  n: number;
  type: string;
  key: boolean;
  time: number | null;
  size: number | null;
  synthetic_time?: boolean;
  recorder_time?: string;
}

export interface CameraReference {
  id: string;
  case_id: string;
  name: string;
  camera: string;
  evidence: string[];
  width: number;
  height: number;
  fingerprint_sha256: string;
  stats: { images: number };
  actor: string;
  created_at: string;
}

export interface MeasureMethod {
  name: string;
  version: string;
  reference: string;
  description: string;
  assumptions: string[];
}

export interface Measurement {
  id: string;
  evidence_id: string;
  chain_id: string | null;
  kind: string;
  label: string;
  frame: number;
  inputs: Record<string, unknown>;
  result: {
    value: number;
    unit: string;
    interval_95: [number, number];
    value_kmh?: number;
    interval_95_kmh?: [number, number];
    time_s?: number;
    distance_m?: number;
    name: string;
    assumptions: string[];
    warning?: string;
    plane_fit_residual_m?: number;
    camera?: { focal_length_px: number; position_m: number[]; reprojection_rms_px: number };
    fit_warning?: string;
  };
  output_hash: string;
  actor: string;
  created_at: string;
}

export type TriageLevel = "green" | "yellow" | "red";

export interface Triage {
  id: string;
  evidence_id: string;
  level: TriageLevel;
  result: { level: TriageLevel; line: string; reasons: { code: string; level: TriageLevel; reason: string; detail: string; analysis: string | null }[] };
  actor: string;
  created_at: string;
}

export interface Catalogue {
  conversion_methods: Record<string, string>;
  measurements: Record<string, MeasureMethod>;
  planner_workflows: Record<string, string>;
  triage_rules: { version: string; rules: { code: string; level: TriageLevel; reason: string }[] };
}

export interface PlanResult {
  workflow: string;
  workflow_description: string;
  candidates: { label: string; score: number | null; error?: string; chain_id?: string }[];
  proposed_chains: string[];
  score_note: string;
}

export interface StepInfo {
  node_id: string;
  filter: string;
  output_hash: string;
  shape: number[];
  skipped: boolean;
}

export interface ChainPolicy {
  generative_steps: string[];
  unjustified_steps: string[];
  illustrative_only: boolean;
  evidential_export: boolean;
}

export type ReviewLevel = "pass" | "warn" | "fail";

export interface Review {
  id: string;
  case_id: string;
  level: ReviewLevel;
  result: {
    level: ReviewLevel;
    line: string;
    counts: Record<ReviewLevel, number>;
    items: { check: string; level: ReviewLevel; text: string; target: string | null }[];
    reviewer_version: string;
  };
  actor: string;
  created_at: string;
}

export interface User {
  username: string;
  full_name: string;
  role: "admin" | "examiner" | "reviewer" | "viewer";
  disabled?: boolean;
  created_at?: string;
}

export interface Job {
  id: string;
  kind: string;
  label: string;
  lane: "cpu" | "gpu";
  case_id: string | null;
  owner: string;
  owner_name: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  error: string | null;
  position: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobOverview {
  slots: { cpu: number; gpu: number };
  running: { cpu: number; gpu: number };
  queued: { cpu: number; gpu: number };
  active: Job[];
  recent: Job[];
}

export type CodecLayer = "type" | "qp" | "none";

export interface CodecSettings {
  layer: CodecLayer;
  mv: boolean;
  grid: boolean;
}

/** How the encoder coded one video frame, per 16×16 macroblock. */
export interface CodecInfo {
  frame: number;
  codec: string;
  type: string;
  mb_size: number;
  cols: number;
  rows: number;
  counts: Record<string, number>;
  share: Record<string, number>;
  qp: { min: number; max: number; mean: number; scale: [number, number] };
  /** One letter per macroblock: i intra, f forward, b backward, B bi, d direct, s skip, o other. */
  kinds: string[];
  qp_grid: number[][];
  legend: { id: string; label: string; color: string }[];
}

export interface SystemInfo {
  cpu_threads: number;
  ram_mb: number;
  result_cache_mb: number;
  frame_cache_mb: number;
  range_mb: number;
  gpu: { name: string; memory_mb: number; source: string; provider: string | null; jobs: number };
  models_on: string;
}
