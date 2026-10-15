import { useEffect, useMemo, useRef, useState } from "react";
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
  onDrawCrop: (nodeId: string) => void;
  drawingCrop: boolean;
  onUndo: () => void;
  onRedo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  frame?: number;
}

const RECENT_KEY = "recentFilters";

function readRecent(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
  } catch {
    return [];
  }
}

function pushRecent(id: string) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify([id, ...readRecent().filter((x) => x !== id)].slice(0, 6)));
  } catch {
    /* storage unavailable */
  }
}

function defaults(f: FilterSchema) {
  return Object.fromEntries(f.params.map((p) => [p.name, p.default]));
}

export function ChainEditor(props: Props) {
  const { filters, chain, graph, dirty, isVideo, selected, steps, picking, status, busy } = props;
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const byId = useMemo(() => Object.fromEntries(filters.map((f) => [f.id, f])), [filters]);
  const categories = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = (f: FilterSchema) =>
      !q || f.name.toLowerCase().includes(q) || f.category.toLowerCase().includes(q) || f.description.toLowerCase().includes(q);
    const m = new Map<string, FilterSchema[]>();
    if (!q) {
      const recent = readRecent().map((id) => byId[id]).filter(Boolean);
      if (recent.length) m.set("Recent", recent);
    }
    filters.filter(match).forEach((f) => m.set(f.category, [...(m.get(f.category) ?? []), f]));
    return [...m.entries()];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, byId, query, adding]);
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
    pushRecent(f.id);
    setAdding(false);
    setQuery("");
  };

  const remove = (id: string) => {
    const i = graph.nodes.findIndex((x) => x.id === id);
    update(graph.nodes.filter((x) => x.id !== id));
    if (selected === id) props.onSelect(i > 0 ? graph.nodes[i - 1].id : null);
  };

  // Del removes the selected step, as in the History panel of desktop tools.
  const removeRef = useRef(remove);
  removeRef.current = remove;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable]")) return;
      if (e.key === "Delete" && selected) {
        e.preventDefault();
        removeRef.current(selected);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

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
        <span className={`state-dot ${chain.output_hash && !dirty ? "ok" : dirty ? "warn" : ""}`} />
        <span
          className="small muted"
          title={`Chain ${chain.graph_hash}\n${chain.output_hash ? `Rendered output ${chain.output_hash}` : "Not rendered yet"}`}
        >
          {dirty ? "Unsaved changes" : chain.output_hash ? "Rendered" : "Saved, not rendered"}
        </span>
        <span className="spacer" />
        <button className="icon" onClick={props.onUndo} disabled={!props.canUndo} title="Undo (Ctrl+Z)">↶</button>
        <button className="icon" onClick={props.onRedo} disabled={!props.canRedo} title="Redo (Ctrl+Y)">↷</button>
      </div>

      {pending.length > 0 && (
        <div className="proposal-banner" title="Agent-proposed steps are left out of findings until an examiner accepts them.">
          <span>{pending.length} proposed step(s)</span>
          <span className="spacer" />
          <button className="primary" onClick={props.onAccept} disabled={dirty || busy}>
            Accept
          </button>
        </div>
      )}

      {generative.length > 0 && (
        <div
          className="generative-banner"
          title="Generative AI steps can add detail the camera never recorded. Output is watermarked and cannot be measured on. Each step needs a written justification, which is logged and printed in the report."
        >
          <b>Illustrative only</b> · {generative.length} generative step(s)
          {unjustified.length > 0 && ` · ${unjustified.length} need a justification`}
        </div>
      )}

      <ol className="nodes">
        <li className={`node source${selected === null ? " selected" : ""}`} onClick={() => props.onSelect(null)} title="Click a step to see the image at that step">
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
                {f?.input_kind === "stack" && <span className="badge" title="Uses several frames">multi</span>}
                {f?.ai && <span className="badge ai" title="Runs a learned model">AI</span>}
                {f?.generative && <span className="badge bad" title="Can add detail that was not recorded">gen</span>}
                {n.proposed_by && !n.accepted_by && <span className="badge warn" title={`Proposed by ${n.proposed_by}`}>proposed</span>}
              </span>
              {st && <span className="hash small" title={`Output after this step: ${st.output_hash}`}>{st.shape.slice(-3, -1).reverse().join("×")}</span>}
              <span className="node-actions" onClick={(e) => e.stopPropagation()}>
                <button onClick={() => move(n.id, -1)} title="Move up">↑</button>
                <button onClick={() => move(n.id, 1)} title="Move down">↓</button>
                <button onClick={() => remove(n.id)} title="Remove step (Del)">✕</button>
              </span>
            </li>
          );
        })}
      </ol>

      <div className="add-filter">
        <button className={adding ? "" : "primary"} onClick={() => setAdding((a) => !a)}>{adding ? "Close" : "+ Add filter"}</button>
        {adding && (
          <div className="filter-menu">
            <input
              autoFocus
              type="text"
              placeholder="Search filters"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setAdding(false);
                if (e.key === "Enter") {
                  const first = categories.flatMap(([, fs]) => fs).find((f) => !(f.input_kind === "stack" && !isVideo));
                  if (first) add(first);
                }
              }}
            />
            {categories.length === 0 && <span className="muted small">No match</span>}
            {categories.map(([cat, fs]) => (
              <div key={cat} className="filter-cat">
                <div className="filter-cat-name">{cat}</div>
                {fs.map((f) => (
                  <button
                    key={f.id}
                    className="filter-item"
                    title={`${f.description}${f.input_kind === "stack" && !isVideo ? "\n(needs a video)" : ""}`}
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
          <details className="node-info">
            <summary>
              <b>{selFilter.name}</b> <span className="muted small">v{selFilter.version} ⓘ</span>
            </summary>
            <p className="small">{selFilter.description}</p>
            <p className="muted small">Reference: {selFilter.reference}</p>
            {sel.proposed_by && <p className="muted small">Proposed by {sel.proposed_by}{sel.accepted_by ? `, accepted by ${sel.accepted_by}` : ""}</p>}
          </details>
          {selFilter.id === "geometry.crop" && (
            <button className={props.drawingCrop ? "active" : ""} onClick={() => props.onDrawCrop(sel.id)} title="Drag a box on the image (Shift = square)">
              {props.drawingCrop ? "Drag on the image… (Esc)" : "Draw crop on image"}
            </button>
          )}
          <ParamForm
            params={selFilter.params}
            values={sel.params}
            onChange={(name, value) => setNode(sel.id, { params: { ...sel.params, [name]: value } })}
            onPick={(p) => props.onPick(sel.id, p)}
            picking={picking?.nodeId === sel.id ? picking.param : null}
          />
          <label className="param">
            <span className="param-label">Note</span>
            <input type="text" value={sel.note ?? ""} placeholder="Why this step (goes in the report)" onChange={(e) => setNode(sel.id, { note: e.target.value || undefined })} />
          </label>
          {selFilter.generative && (
            <label className="param justification">
              <span className="param-label">Justification (required)</span>
              <textarea
                rows={3}
                value={sel.justification ?? ""}
                placeholder="Why this illustration is needed and what it must not be used to show"
                onChange={(e) => setNode(sel.id, { justification: e.target.value || undefined })}
              />
              {(sel.justification ?? "").trim().length < MIN_JUSTIFICATION && (
                <span className="muted small">At least {MIN_JUSTIFICATION} characters</span>
              )}
            </label>
          )}
        </div>
      )}

      <div className="chain-actions">
        <button onClick={props.onSave} disabled={!dirty || busy}>Save</button>
        <button className="primary" onClick={props.onRender} disabled={busy} title="Run from the original, store the lossless output and record its hash">
          Render
        </button>
        <button onClick={props.onVerify} disabled={busy || dirty || !chain.output_hash} title="Re-run from the original and check the output is bit-identical">
          Verify
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
