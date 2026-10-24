// Still-image traffic detection: types and API calls (backend: forensic/traffic, forensic/api_traffic.py).
import { req } from "./api";
import type { Job } from "./types";

export type Box = [number, number, number, number];
export type Point = [number, number];

export interface Region {
  kind: "include" | "exclude";
  label: string;
  points: Point[];
}

export interface TrafficSettings {
  model: string;
  preprocess: "auto" | "none" | "clahe" | "dehaze";
  tile: "auto" | boolean;
  members: { yolox: boolean; lights: boolean; change: boolean };
  auto_train_after: number;
}

export interface Profile {
  id: string;
  camera_id: string;
  version: number;
  status: "adopted" | "rejected";
  sha256: string;
  metrics: {
    baseline: { f1: number; precision: number; recall: number; threshold: number };
    cross_validated: { f1: number; precision: number; recall: number; threshold: number };
    decision: string;
    reason?: string;
  };
  training: { images: string[]; positives: number; negatives: number; unrecoverable_misses: number; method: string };
  created_at: string;
}

export interface Camera {
  camera_id: string;
  case_id: string;
  label: string;
  location: string;
  utc_offset_hours: number;
  regions: Region[];
  static_lights: Box[];
  settings: TrafficSettings;
  profile: Profile | null;
  backgrounds: { id: string; lighting: string; size: [number, number]; images: number; created_at: string }[];
  counts?: Record<string, number>;
  pending_feedback?: number;
}

export type Status = "pending" | "detected" | "edited" | "approved";

export interface TrafficImage {
  evidence_id: string;
  camera_id: string;
  image_name: string;
  sha256?: string;
  capture_utc: string | null;
  local_time: string | null;
  utc_offset_hours: number;
  time_source: string;
  lighting: string | null;
  lighting_auto: string | null;
  lighting_overridden: boolean;
  scene: { stats?: Record<string, number>; indicators?: string[]; rules_version?: string };
  detection_count: number;
  status: Status;
  run_id: string | null;
  approved_by: string | null;
  approved_at: string | null;
  regions: Region[] | null;
}

export interface Detection {
  id: number;
  evidence_id: string;
  cls: string;
  score: number | null;
  box: Box;
  origin: "auto" | "manual";
  state: "active" | "false_alarm";
  members: Record<string, number>;
  edited: boolean;
  note: string;
  actor: string;
  training_job?: Job | null;
  learned_static_light?: boolean;
}

export interface RunObject {
  idx: number;
  cls: string;
  box: Box;
  fused_score: number;
  score: number;
  members: Record<string, number>;
  in_region: boolean;
  kept: boolean;
}

export interface Run {
  id: string;
  profile_id: string | null;
  params: Record<string, unknown>;
  models: { model: string; sha256: string; licence: string; device: string }[];
  scene: { lighting: string; indicators: string[] };
  members: Record<string, Record<string, unknown>>;
  output_hash: string;
  created_at: string;
  objects: RunObject[];
}

export interface ImageDetail extends TrafficImage {
  detections: Detection[];
  run: Run | null;
  camera: { camera_id: string; label: string; utc_offset_hours: number; regions: Region[] };
  regions_effective: Region[];
  width: number;
  height: number;
  training_job?: Job | null;
}

export interface Notice {
  id: number;
  kind: string;
  message: string;
  created_at: string;
  read_at: string | null;
}

export interface Chart {
  query: string;
  title: string;
  x_label: string;
  y_label: string;
  x: string[];
  series: { name: string; values: number[] }[];
  notes: string[];
  local_time_note: string;
  spec: { type: "bar" | "line" | "pie" | "stacked" };
}

export const VEHICLE_CLASSES = ["car", "truck", "bus", "motorcycle", "bicycle", "vehicle",
  "vehicle_headlights", "vehicle_taillights", "vehicle_lamp"];

export const traffic = {
  cameras: (caseId: string) => req<Camera[]>("GET", `/api/cases/${caseId}/traffic/cameras`),
  createCamera: (caseId: string, label: string, utcOffset: number) =>
    req<Camera>("POST", `/api/cases/${caseId}/traffic/cameras`, { label, utc_offset_hours: utcOffset }),
  setCamera: (cameraId: string, body: { utc_offset_hours?: number; regions?: Region[]; settings?: Partial<TrafficSettings> }) =>
    req<Camera>("PUT", `/api/traffic/cameras/${cameraId}`, body),
  learnBackground: (cameraId: string) =>
    req<{ learned: { lighting: string; images: number }[]; skipped: { lighting: string; images: number }[]; note: string }>(
      "POST", `/api/traffic/cameras/${cameraId}/background`, {}),
  train: (cameraId: string) => req<Job>("POST", `/api/traffic/cameras/${cameraId}/train`, {}),
  profiles: (cameraId: string) => req<Profile[]>("GET", `/api/traffic/cameras/${cameraId}/profiles`),
  images: (caseId: string) => req<TrafficImage[]>("GET", `/api/cases/${caseId}/traffic/images`),
  detectBatch: (caseId: string, body: { only?: "pending" | "unedited"; camera_id?: string; evidence_ids?: string[] }) =>
    req<Job>("POST", `/api/cases/${caseId}/traffic/detect`, body),
  image: (evidenceId: string) => req<ImageDetail>("GET", `/api/traffic/images/${evidenceId}`),
  setImage: (evidenceId: string, body: Record<string, unknown>) => req<ImageDetail>("PATCH", `/api/traffic/images/${evidenceId}`, body),
  detect: (evidenceId: string, force = false, reason = "") =>
    req<{ detections: number }>("POST", `/api/traffic/images/${evidenceId}/detect`, { force, reason }),
  add: (evidenceId: string, box: Box, cls: string) =>
    req<Detection>("POST", `/api/traffic/images/${evidenceId}/detections`, { box, cls }),
  update: (id: number, body: { box?: Box; cls?: string }) => req<Detection>("PATCH", `/api/traffic/detections/${id}`, body),
  falseAlarm: (id: number, note = "") => req<Detection>("POST", `/api/traffic/detections/${id}/false-alarm`, { note }),
  restore: (id: number) => req<Detection>("POST", `/api/traffic/detections/${id}/restore`),
  approve: (evidenceId: string, approved = true) =>
    req<ImageDetail>("POST", `/api/traffic/images/${evidenceId}/approve`, { approved }),
  notifications: (caseId: string, unread = true) =>
    req<Notice[]>("GET", `/api/cases/${caseId}/traffic/notifications?unread=${unread}`),
  readNotifications: (caseId: string, ids?: number[]) =>
    req<{ marked: number }>("POST", `/api/cases/${caseId}/traffic/notifications/read`, { ids }),
  chart: (caseId: string, q: string) => req<Chart>("GET", `/api/cases/${caseId}/traffic/chart?q=${encodeURIComponent(q)}`),
  exportUrl: (caseId: string, fmt: "xlsx" | "csv" | "json") => `/api/cases/${caseId}/traffic/export.${fmt}`,
  datasetUrl: (caseId: string) => `/api/cases/${caseId}/traffic/dataset.zip`,
  reportUrl: (caseId: string, kind: "html" | "pdf", charts: string[]) =>
    `/api/cases/${caseId}/traffic/report.${kind}` + (charts.length ? "?" + charts.map((c) => "chart=" + encodeURIComponent(c)).join("&") : ""),
};
