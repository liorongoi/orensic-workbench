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
  params: ParamSchema[];
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
}

export interface Case {
  id: string;
  name: string;
  examiner: string;
  notes: string;
  created_at: string;
  evidence_count?: number;
}

export interface Probe {
  media_type: "image" | "video";
  format?: string;
  width: number;
  height: number;
  frame_count: number;
  codec?: string;
  frame_rate?: string;
  duration?: number;
  field_order?: string;
}

export interface Evidence {
  id: string;
  case_id: string;
  filename: string;
  sha256: string;
  md5: string;
  size: number;
  media_type: "image" | "video";
  probe: Probe;
  ingested_by: string;
  ingested_at: string;
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
}

export interface Flag {
  level: "info" | "warning";
  text: string;
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
}

export interface StepInfo {
  node_id: string;
  filter: string;
  output_hash: string;
  shape: number[];
  skipped: boolean;
}
