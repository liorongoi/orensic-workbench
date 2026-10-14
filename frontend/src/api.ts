import type {
  AnalysisRun,
  AnalysisSchema,
  AuditEntry,
  Case,
  Chain,
  Evidence,
  FilterSchema,
  FrameInfo,
  Graph,
  StepInfo,
} from "./types";

let examiner = "";
try {
  examiner = localStorage.getItem("examiner") ?? "";
} catch {
  /* storage unavailable */
}

export function getExaminer() {
  return examiner;
}

export function setExaminer(name: string) {
  examiner = name;
  try {
    localStorage.setItem("examiner", name);
  } catch {
    /* storage unavailable */
  }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Examiner": examiner };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(url, { method, headers, body: payload });
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
  filters: () => req<FilterSchema[]>("GET", "/api/filters"),
  analyses: () => req<AnalysisSchema[]>("GET", "/api/analyses"),
  cases: () => req<Case[]>("GET", "/api/cases"),
  createCase: (name: string, examinerName: string) =>
    req<Case>("POST", "/api/cases", { name, examiner: examinerName }),
  caseDetail: (id: string) =>
    req<{ case: Case; evidence: Evidence[]; chains: Chain[]; analyses: AnalysisRun[] }>("GET", `/api/cases/${id}`),
  upload: (caseId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<Evidence>("POST", `/api/cases/${caseId}/evidence`, fd);
  },
  verifyEvidence: (id: string) =>
    req<{ ok: boolean; read_only: boolean; sha256: string }>("POST", `/api/evidence/${id}/verify`),
  frames: (id: string) => req<FrameInfo[]>("GET", `/api/evidence/${id}/frames`),
  createChain: (caseId: string, evidenceId: string, name: string, graph?: Partial<Graph>) =>
    req<Chain>("POST", `/api/cases/${caseId}/chains`, { evidence_id: evidenceId, name, graph }),
  updateChain: (id: string, graph: Graph, name?: string) => req<Chain>("PUT", `/api/chains/${id}`, { graph, name }),
  acceptProposals: (id: string) => req<Chain>("POST", `/api/chains/${id}/accept`, {}),
  render: (id: string) =>
    req<{ output_hash: string; graph_hash: string; outputs: { frame: number; png: string }[]; steps: StepInfo[]; pending_proposals: string[] }>(
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
    headers: { "Content-Type": "application/json", "X-Examiner": examiner },
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
  derived: (digest: string) => `/api/derived/${digest}.png`,
  original: (evidenceId: string) => `/api/evidence/${evidenceId}/original`,
  report: (caseId: string, kind: "html" | "pdf" | "json") =>
    `/api/cases/${caseId}/report.${kind}?examiner=${encodeURIComponent(examiner)}`,
};
