import { useMemo, useState } from "react";
import type { Chain, ChainNode, FilterSchema, Graph, ParamSchema, StepInfo } from "../types";
import { urls } from "../api";
import { ParamForm } from "./ParamForm";

const MIN_JUSTIFICATION = 20;

interface Props {
  filters: FilterSchema[];
  chain: Chain;
  graph: Graph;
  dirty: boolean;
  isVideo: boolean;
  selected: string | null;
  steps: StepInfo[];
  picking: { nodeId: string; param: string } | null;
  status: { kind: "ok" | "bad" | "info"; text: string } | null;
  busy: boolean;
  onSelect: (id: string | null) => void;
  onChange: (g: Graph) => void;
  onPick: (nodeId: string, p: ParamSchema) => void;
  onSave: () => void;
  onRender: () => void;
  onVerify: () => void;
  onAccept: () => void;
  frame?: number;
}

function defaults(f: FilterSchema) {
  return Object.fromEntries(f.params.map((p) => [p.name, p.default]));
}

export function ChainEditor(props: Props) {
  const { filters, chain, graph, dirty, isVideo, selected, steps, picking, status, busy } = props;
  const [adding, setAdding] = useState(false);
  const byId = useMemo(() => Object.fromEntries(filters.map((f) => [f.id, f])), [filters]);
  const categories = useMemo(() => {
    const m = new Map<string, FilterSchema[]>();
    filters.forEach((f) => m.set(f.category, [...(m.get(f.category) ?? []), f]));
    return [...m.entries()];
  }, [filters]);
  const stepHash = Object.fromEntries(steps.map((s) => [s.node_id, s]));
  const pending = graph.nodes.filter((n) => n.enabled && n.proposed_by && !n.accepted_by);
  const generative = graph.nodes.filter((n) => n.enabled && byId[n.filter]?.generative);
  const unjustified = generative.filter((n) => (n.justification ?? "").trim().length < MIN_JUSTIFICATION);

  const update = (nodes: ChainNode[]) => props.onChange({ ...graph, nodes });
  const setNode = (id: string, patch: Partial<ChainNode>) =>
    update(graph.nodes.map((n) => (n.id === id ? { ...n, ...patch } : n)));

  const add = (f: FilterSchema) => {
    const ids = new Set(graph.nodes.map((n) => n.id));
    let k = graph.nodes.length + 1;
    while (ids.has(`n${k}`)) k++;
    const node: ChainNode = { id: `n${k}`, filter: f.id, params: defaults(f), enabled: true };
    const idx = selected ? graph.nodes.findIndex((n) => n.id === selected) + 1 : graph.nodes.length;
    const nodes = [...graph.nodes];
    nodes.splice(idx, 0, node);
    update(nodes);
    props.onSelect(node.id);
    setAdding(false);
  };

  const move = (id: string, d: number) => {
    const i = graph.nodes.findIndex((n) => n.id === id);
    const j = i + d;
    if (j < 0 || j >= graph.nodes.length) return;
    const nodes = [...graph.nodes];
    [nodes[i], nodes[j]] = [nodes[j], nodes[i]];
    update(nodes);
  };

  const sel = graph.nodes.find((n) => n.id === selected);
  const selFilter = sel ? byId[sel.filter] : null;

  return (
    <div className="chain-editor">
      <div className="chain-head">
        <div>
          <b>{chain.name}</b>
          {dirty && <span className="badge warn">unsaved</span>}
        </div>
        <div className="hash small" title="Hash of the saved chain document">
          chain {chain.graph_hash.slice(0, 12)}…{chain.output_hash ? ` · output ${chain.output_hash.slice(0, 12)}…` : " · not rendered"}
        </div>
      </div>

      {pending.length > 0 && (
        <div className="proposal-banner">
          {pending.length} step(s) proposed by an agent. They are excluded from findings until you accept them.
          <button className="primary" onClick={props.onAccept} disabled={dirty || busy}>
            Accept proposed steps
          </button>
        </div>
      )}

      {generative.length > 0 && (
        <div className="generative-banner">
          <b>Illustrative only.</b> This chain has {generative.length} generative AI step(s) that can add detail the camera never
          recorded. Results are watermarked and cannot be measured on.{" "}
          {unjustified.length > 0
            ? `Write a justification on ${unjustified.length === 1 ? "that step" : "each such step"} before it can be accepted or exported.`
            : "Each has a justification, which is logged and printed in the report."}
        </div>
      )}

      <ol className="nodes">
        <li className={`node source${selected === null ? " selected" : ""}`} onClick={() => props.onSelect(null)}>
          <span className="node-name">Source</span>
          <span className="muted small">
            {isVideo && graph.source.frames
              ? graph.source.frames.start === graph.source.frames.end
                ? `frame ${graph.source.frames.start}`
                : `frames ${graph.source.frames.start}–${graph.source.frames.end}`
              : "original image"}
          </span>
        </li>
        {graph.nodes.map((n, i) => {
          const f = byId[n.filter];
          const st = stepHash[n.id];
          return (
            <li
              key={n.id}
              className={`node${selected === n.id ? " selected" : ""}${n.enabled ? "" : " disabled"}${n.proposed_by && !n.accepted_by ? " proposed" : ""}`}
              onClick={() => props.onSelect(n.id)}
            >
              <input
                type="checkbox"
                checked={n.enabled}
                title="Enable / disable this step"
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setNode(n.id, { enabled: e.target.checked })}
              />
              <span className="node-name">
                {i + 1}. {f?.name ?? n.filter}
                {f?.input_kind === "stack" && <span className="badge">multi-frame</span>}
                {f?.ai && <span className="badge ai" title="Runs a learned model">AI</span>}
                {f?.generative && <span className="badge bad" title="Can add detail that was not recorded">generative</span>}
                {n.proposed_by && <span className={`badge ${n.accepted_by ? "" : "warn"}`}>{n.accepted_by ? "accepted" : "proposed"}</span>}
              </span>
              {st && <span className="hash small" title={`Output after this step: ${st.output_hash}`}>{st.shape.slice(-3, -1).reverse().join("×")}</span>}
              <span className="node-actions" onClick={(e) => e.stopPropagation()}>
                <button onClick={() => move(n.id, -1)} title="Move up">↑</button>
                <button onClick={() => move(n.id, 1)} title="Move down">↓</button>
                <button
                  onClick={() => {
                    update(graph.nodes.filter((x) => x.id !== n.id));
                    if (selected === n.id) props.onSelect(null);
                  }}
                  title="Remove"
                >
                  ✕
                </button>
              </span>
            </li>
          );
        })}
      </ol>

      <div className="add-filter">
        <button onClick={() => setAdding((a) => !a)}>{adding ? "Close" : "+ Add filter"}</button>
        {adding && (
          <div className="filter-menu">
            {categories.map(([cat, fs]) => (
              <div key={cat} className="filter-cat">
                <div className="filter-cat-name">{cat}</div>
                {fs.map((f) => (
                  <button
                    key={f.id}
                    className="filter-item"
                    title={f.description}
                    disabled={f.input_kind === "stack" && !isVideo}
                    onClick={() => add(f)}
                  >
                    {f.name}
                    {f.generative && <span className="badge bad">generative</span>}
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>

      {sel && selFilter && (
        <div className="node-detail">
          <h3>
            {selFilter.name} <span className="muted small">{selFilter.id} v{selFilter.version}</span>
          </h3>
          <p className="small">{selFilter.description}</p>
          <ParamForm
            params={selFilter.params}
            values={sel.params}
            onChange={(name, value) => setNode(sel.id, { params: { ...sel.params, [name]: value } })}
            onPick={(p) => props.onPick(sel.id, p)}
            picking={picking?.nodeId === sel.id ? picking.param : null}
          />
          <label className="param">
            <span className="param-label">Examiner note</span>
            <input type="text" value={sel.note ?? ""} placeholder="Why this step (printed in the report)" onChange={(e) => setNode(sel.id, { note: e.target.value || undefined })} />
          </label>
          {selFilter.generative && (
            <label className="param justification">
              <span className="param-label">Justification for using a generative step (required, logged)</span>
              <textarea
                rows={3}
                value={sel.justification ?? ""}
                placeholder="Why this illustration is needed and what it must not be used to show"
                onChange={(e) => setNode(sel.id, { justification: e.target.value || undefined })}
              />
              <span className="muted small">
                {(sel.justification ?? "").trim().length < MIN_JUSTIFICATION
                  ? `At least ${MIN_JUSTIFICATION} characters. Agents cannot write this.`
                  : "Recorded in the audit log when you save."}
              </span>
            </label>
          )}
          <p className="muted small reference">Reference: {selFilter.reference}</p>
          {sel.proposed_by && <p className="muted small">Proposed by {sel.proposed_by}{sel.accepted_by ? `, accepted by ${sel.accepted_by}` : ""}</p>}
        </div>
      )}

      <div className="chain-actions">
        <button onClick={props.onSave} disabled={!dirty || busy}>Save</button>
        <button className="primary" onClick={props.onRender} disabled={busy} title="Run from the original, store the lossless output and record its hash">
          Render result
        </button>
        <button onClick={props.onVerify} disabled={busy || dirty || !chain.output_hash} title="Re-run from the original and check the output is bit-identical">
          Verify reproducibility
        </button>
        <a
          className={`button${dirty || !chain.output_hash || unjustified.length || pending.length ? " disabled" : ""}`}
          href={urls.exportPng(chain.id, props.frame ?? 0)}
          target="_blank"
          rel="noreferrer"
          title={
            unjustified.length
              ? "Blocked: a generative step has no justification"
              : "Lossless PNG of the rendered result, re-computed and hash-checked; logged"
          }
        >
          Export PNG
        </a>
      </div>
      {status && <p className={`status ${status.kind}`}>{status.text}</p>}
    </div>
  );
}
