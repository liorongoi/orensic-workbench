import { useState } from "react";
import { api } from "../api";
import type { Evidence, Graph, PlanResult } from "../types";

interface Props {
  caseId: string;
  evidence: Evidence;
  frame: number;
  range: { start: number; end: number } | null;
  roi: number[][] | undefined;
  picking: boolean;
  workflows: Record<string, string>;
  graph: Graph | null;
  onPickRoi: () => void;
  onClearRoi: () => void;
  onPlanned: (r: PlanResult) => void;
  onOpenChain: (id: string) => void;
}

function roiRect(roi: number[][] | undefined): number[] | null {
  if (!roi || roi.length !== 2) return null;
  const [[x0, y0], [x1, y1]] = roi;
  const x = Math.round(Math.min(x0, x1));
  const y = Math.round(Math.min(y0, y1));
  return [x, y, Math.max(1, Math.round(Math.abs(x1 - x0))), Math.max(1, Math.round(Math.abs(y1 - y0)))];
}

/** Ask the planner agent for candidate chains; it proposes the best two for the examiner to accept. */
export function PlannerPanel(props: Props) {
  const { evidence, roi, workflows } = props;
  const [goal, setGoal] = useState("");
  const [workflow, setWorkflow] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PlanResult | null>(null);
  const [blur, setBlur] = useState<{ length: number; angle: number; strength: number } | null>(null);
  const rect = roiRect(roi);
  const isVideo = evidence.media_type === "video";
  const frames = isVideo ? props.range ?? { start: props.frame, end: props.frame } : null;

  const plan = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.plan(props.caseId, { evidence_id: evidence.id, goal: goal || workflow || "general clarity", roi: rect, frames, workflow: workflow || null });
      setResult(r);
      props.onPlanned(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const estimate = async () => {
    setError(null);
    try {
      setBlur(await api.estimateBlur(evidence.id, { graph: { source: { frames: frames && { start: frames.start, end: frames.start } }, nodes: [] }, frame: 0, roi: rect }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <details className="planner">
      <summary>
        <b>Ask the planner</b> <span className="muted small">proposes chains for a goal; you accept or discard</span>
      </summary>
      <input placeholder="Goal, e.g. make the plate readable" value={goal} onChange={(e) => setGoal(e.target.value)} />
      <div className="row">
        <select value={workflow} onChange={(e) => setWorkflow(e.target.value)} title={workflows[workflow] ?? "Chosen from the goal"}>
          <option value="">Workflow from goal</option>
          {Object.keys(workflows).map((w) => (
            <option key={w} value={w}>
              {w.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <button className={props.picking ? "active" : ""} onClick={props.onPickRoi}>
          {props.picking ? "Click 2 corners…" : rect ? "Redo region" : "Pick region"}
        </button>
        {rect && <button onClick={props.onClearRoi}>Clear</button>}
      </div>
      <p className="muted small">
        {rect ? `Region ${rect[2]}×${rect[3]} at ${rect[0]}, ${rect[1]}.` : "No region: the whole frame is scored."}{" "}
        {isVideo && frames && (frames.end > frames.start ? `Frames ${frames.start}–${frames.end} (multi-frame methods).` : `Frame ${frames.start} only; set a range in a chain for multi-frame methods.`)}
      </p>
      <div className="row">
        <button className="primary" onClick={plan} disabled={busy}>
          {busy ? "Planning…" : "Plan"}
        </button>
        <button onClick={estimate} title="Cepstral estimate of linear motion blur in the region, for the deblur filters">
          Estimate motion blur
        </button>
      </div>
      {blur && (
        <p className="small">
          {blur.strength >= 4 ? (
            <>
              Motion blur about <b>{blur.length} px</b> at <b>{blur.angle}°</b> (strength {blur.strength}). Use these in a Deblur step with blur type “motion”.
            </>
          ) : (
            <>No clear linear motion blur found (strength {blur.strength}).</>
          )}
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {result && (
        <div className="plan-result small">
          <p>
            Workflow <b>{result.workflow.replace(/_/g, " ")}</b>: {result.workflow_description}
          </p>
          <ol>
            {result.candidates
              .slice()
              .sort((a, b) => (b.score ?? -99) - (a.score ?? -99))
              .map((c) => (
                <li key={c.label}>
                  {c.label} <span className="muted">{c.score === null ? `failed: ${c.error}` : `score ${c.score}`}</span>{" "}
                  {c.chain_id && (
                    <button className="link" onClick={() => props.onOpenChain(c.chain_id!)}>
                      open proposal
                    </button>
                  )}
                </li>
              ))}
          </ol>
          <p className="muted">{result.score_note}</p>
        </div>
      )}
    </details>
  );
}
