import type {
  CameraReference,
  Catalogue,
  Measurement,
  PlanResult,
  Triage,
  AnalysisRun,
  AnalysisSchema,
  AuditEntry,
  Case,
  Chain,
  CodecInfo,
  CodecSettings,
  Evidence,
  FilterSchema,
  FrameInfo,
  Graph,
  StepInfo,
  ChainPolicy,
  ModelInfo,
  Review,
  User,
  Job,
  JobOverview,
} from "./types";

// The signed-in examiner's full name; the server records actions under the signed-in account.
let examiner = "";

export function getExaminer() {
  return examiner;
}

export function setExaminer(name: string) {
  examiner = name;
}

/** Fired when the server says the session has ended, so the app can show the sign-in screen. */
export const SIGNED_OUT = "workbench-signed-out";

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
  if (res.status === 401 && !url.startsWith("/api/auth/")) window.dispatchEvent(new Event(SIGNED_OUT));
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  authStatus: () => req<{ setup_required: boolean; user: User | null }>("GET", "/api/auth/status"),
  setup: (username: string, fullName: string, password: string) =>
    req<{ user: User }>("POST", "/api/auth/setup", { username, full_name: fullName, password }),
  login: (username: string, password: string) => req<{ user: User }>("POST", "/api/auth/login", { username, password }),
  logout: () => req("POST", "/api/auth/logout"),
  changePassword: (current: string, next: string) => req("POST", "/api/auth/password", { current, new: next }),
  users: () => req<User[]>("GET", "/api/users"),
  createUser: (u: { username: string; full_name: string; password: string; role: string }) =>
    req<User>("POST", "/api/users", u),
  updateUser: (username: string, change: { role?: string; disabled?: boolean; password?: string }) =>
    req<User>("PATCH", `/api/users/${encodeURIComponent(username)}`, change),
  jobs: () => req<JobOverview>("GET", "/api/jobs"),
  cancelJob: (id: string) => req<Job>("POST", `/api/jobs/${id}/cancel`),
  filters: () => req<FilterSchema[]>("GET", "/api/filters"),
  analyses: () => req<AnalysisSchema[]>("GET", "/api/analyses"),
  cases: () => req<Case[]>("GET", "/api/cases"),
  createCase: (name: string, examinerName: string) =>
    req<Case>("POST", "/api/cases", { name, examiner: examinerName }),
  catalogue: () => req<Catalogue>("GET", "/api/catalogue"),
  caseDetail: (id: string) =>
    req<{
      case: Case;
      evidence: Evidence[];
      removed_evidence: Evidence[];
      chains: Chain[];
      removed_chains: Chain[];
      analyses: AnalysisRun[];
      camera_references: CameraReference[];
      measurements: Measurement[];
      triage: Triage[];
      review: Review | null;
    }>("GET", `/api/cases/${id}`),
  models: () => req<ModelInfo[]>("GET", "/api/models"),
  review: (caseId: string, rerunAnalyses = true) =>
    req<Review>("POST", `/api/cases/${caseId}/review`, { rerun_analyses: rerunAnalyses }),
  convert: (evidenceId: string, method: string, frameRate: number) =>
    req<Evidence>("POST", `/api/evidence/${evidenceId}/convert`, { method, frame_rate: frameRate }),
  triage: (caseId: string, evidenceIds?: string[]) =>
    req<{ counts: Record<string, number>; items: { evidence_id: string; level: string; line: string }[] }>(
      "POST",
      `/api/cases/${caseId}/triage`,
      { evidence_ids: evidenceIds ?? null },
    ),
  smartReport: (evidenceId: string) => req<Triage>("POST", `/api/evidence/${evidenceId}/smart-report`),
  createReference: (caseId: string, name: string, camera: string, evidenceIds: string[]) =>
    req<CameraReference>("POST", `/api/cases/${caseId}/camera-references`, { name, camera, evidence_ids: evidenceIds }),
  measure: (evidenceId: string, body: { kind: string; inputs: Record<string, unknown>; label: string; chain_id: string | null; frame: number }) =>
    req<Measurement>("POST", `/api/evidence/${evidenceId}/measurements`, body),
  plan: (caseId: string, body: { evidence_id: string; goal: string; roi: number[] | null; frames: { start: number; end: number } | null; workflow?: string | null }) =>
    req<PlanResult>("POST", `/api/cases/${caseId}/plan`, body),
  estimateBlur: (evidenceId: string, body: { graph: Graph | null; frame: number; roi: number[] | null }) =>
    req<{ length: number; angle: number; strength: number; note: string }>("POST", `/api/evidence/${evidenceId}/estimate-blur`, body),
  upload: (caseId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<Evidence>("POST", `/api/cases/${caseId}/evidence`, fd);
  },
  verifyEvidence: (id: string) =>
    req<{ ok: boolean; read_only: boolean; sha256: string }>("POST", `/api/evidence/${id}/verify`),
  removeEvidence: (id: string, reason: string) => req<Evidence>("POST", `/api/evidence/${id}/remove`, { reason }),
  restoreEvidence: (id: string) => req<Evidence>("POST", `/api/evidence/${id}/restore`),
  removeChain: (id: string, reason = "") => req<Chain>("POST", `/api/chains/${id}/remove`, { reason }),
  restoreChain: (id: string) => req<Chain>("POST", `/api/chains/${id}/restore`),
  frames: (id: string) => req<FrameInfo[]>("GET", `/api/evidence/${id}/frames`),
  codecInfo: (id: string, n: number) => req<CodecInfo>("GET", `/api/evidence/${id}/codec/${n}`),
  createChain: (caseId: string, evidenceId: string, name: string, graph?: Partial<Graph>) =>
    req<Chain>("POST", `/api/cases/${caseId}/chains`, { evidence_id: evidenceId, name, graph }),
  updateChain: (id: string, graph: Graph, name?: string) => req<Chain>("PUT", `/api/chains/${id}`, { graph, name }),
  acceptProposals: (id: string) => req<Chain>("POST", `/api/chains/${id}/accept`, {}),
  render: (id: string) =>
    req<{
      output_hash: string;
      graph_hash: string;
      outputs: { frame: number; png: string; watermarked?: boolean }[];
      steps: StepInfo[];
      pending_proposals: string[];
      policy: ChainPolicy;
      models: { node: string; model: string; sha256: string }[];
    }>(
      "POST",
      `/api/chains/${id}/render`,
    ),
  verifyChain: (id: string) =>
    req<{ reproduced: boolean; expected_hash: string; rerun_hash: string }>("POST", `/api/chains/${id}/verify`),
  runAnalysis: (evidenceId: string, analysis: string, params: Record<string, unknown>) =>
    req<AnalysisRun>("POST", `/api/evidence/${evidenceId}/analyses`, { analysis, params }),
  audit: (caseId: string) =>
    req<{ verification: { ok: boolean; entries: number; broken_at: number | null }; entries: AuditEntry[] }>(
      "GET",
      `/api/cases/${caseId}/audit`,
    ),
  addNote: (caseId: string, text: string) => req("POST", `/api/cases/${caseId}/notes`, { text }),
};

export async function fetchPreview(
  evidenceId: string,
  graph: Graph,
  upto: string | null,
  frame: number,
  signal?: AbortSignal,
): Promise<{ blob: Blob; info: { output_hash: string; shape: number[]; steps: StepInfo[] } }> {
  const res = await fetch(`/api/evidence/${evidenceId}/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph, upto, frame }),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return { blob: await res.blob(), info: JSON.parse(res.headers.get("X-Preview-Info") ?? "{}") };
}

export const urls = {
  frame: (evidenceId: string, n: number) => `/api/evidence/${evidenceId}/frame/${n}.png`,
  codec: (evidenceId: string, n: number, s: CodecSettings) =>
    `/api/evidence/${evidenceId}/codec/${n}.png?layer=${s.layer}&mv=${s.mv ? 1 : 0}&grid=${s.grid ? 1 : 0}`,
  derived: (digest: string) => `/api/derived/${digest}.png`,
  original: (evidenceId: string) => `/api/evidence/${evidenceId}/original`,
  exportPng: (chainId: string, frame: number) => `/api/chains/${chainId}/export.png?frame=${frame}`,
  report: (caseId: string, kind: "html" | "pdf" | "json") => `/api/cases/${caseId}/report.${kind}`,
};
