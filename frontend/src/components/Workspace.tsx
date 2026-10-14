import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, fetchPreview, urls } from "../api";
import type { AnalysisRun, AnalysisSchema, Case, Chain, Evidence, FilterSchema, FrameInfo, Graph, ParamSchema, StepInfo } from "../types";
import { AuditPanel } from "./AuditPanel";
import { AuthPanel } from "./AuthPanel";
import { ChainEditor } from "./ChainEditor";
import { FrameStrip } from "./FrameStrip";
import { Viewer } from "./Viewer";

interface Props {
  caseId: string;
  filters: FilterSchema[];
  analyses: AnalysisSchema[];
}

type Tab = "enhance" | "authenticate" | "audit";
type View = { kind: "chain" } | { kind: "map"; digest: string; label: string };

const sameGraph = (a: Graph, b: Graph) => JSON.stringify(a) === JSON.stringify(b);

export function Workspace({ caseId, filters, analyses }: Props) {
  const [kase, setCase] = useState<Case | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [chains, setChains] = useState<Chain[]>([]);
  const [runs, setRuns] = useState<AnalysisRun[]>([]);
  const [evId, setEvId] = useState<string | null>(null);
  const [chainId, setChainId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Graph | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [frames, setFrames] = useState<FrameInfo[]>([]);
  const [frame, setFrame] = useState(0);
  const [tab, setTab] = useState<Tab>("enhance");
  const [view, setView] = useState<View>({ kind: "chain" });
  const [preview, setPreview] = useState<{ url: string | null; steps: StepInfo[]; error: string | null; busy: boolean }>({ url: null, steps: [], error: null, busy: false });
  const [picking, setPicking] = useState<{ nodeId: string; param: string; count?: number } | null>(null);
  const [status, setStatus] = useState<{ kind: "ok" | "bad" | "info"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const [auditKey, setAuditKey] = useState(0);
  const [showUpto, setShowUpto] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  // Until the examiner sets a range explicitly, a single-frame source follows the frame scrubber.
  const rangeTouched = useRef(false);

  const ev = evidence.find((e) => e.id === evId) ?? null;
  const chain = chains.find((c) => c.id === chainId) ?? null;
  const dirty = !!(chain && draft && !sameGraph(chain.graph, draft));
  const isVideo = ev?.media_type === "video";
  const range = draft?.source.frames ?? null;

  const reload = useCallback(async () => {
    const d = await api.caseDetail(caseId);
    setCase(d.case);
    setEvidence(d.evidence);
    setChains(d.chains);
    setRuns(d.analyses);
    return d;
  }, [caseId]);

  useEffect(() => {
    reload().then((d) => {
      if (d.evidence.length) selectEvidence(d.evidence[0].id, d.chains);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);

  const selectEvidence = (id: string, allChains = chains) => {
    setEvId(id);
    setView({ kind: "chain" });
    setPicking(null);
    setStatus(null);
    setSelected(null);
    const c = allChains.filter((x) => x.evidence_id === id);
    const first = c[c.length - 1] ?? null;
    setChainId(first?.id ?? null);
    setDraft(first ? first.graph : null);
    rangeTouched.current = false;
    setFrame(first?.graph.source.frames?.start ?? 0);
  };

  useEffect(() => {
    setFrames([]);
    if (!ev) return;
    if (ev.media_type === "video") api.frames(ev.id).then(setFrames).catch(() => setFrames([]));
  }, [ev?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live preview (server side, not stored, not audited).
  useEffect(() => {
    if (!ev || view.kind !== "chain") return;
    const ctrl = new AbortController();
    let graph: Graph;
    let upto: string | null = null;
    if (!draft) {
      graph = { source: { frames: isVideo ? { start: frame, end: frame } : null }, nodes: [] };
    } else if (picking) {
      // Points are picked on the image as it enters the step being edited.
      const idx = draft.nodes.findIndex((n) => n.id === picking.nodeId);
      graph = { ...draft, nodes: draft.nodes.slice(0, Math.max(0, idx)) };
    } else {
      graph = draft;
      if (showUpto && selected) upto = selected;
    }
    const offset = graph.source.frames ? Math.max(0, frame - graph.source.frames.start) : 0;
    setPreview((p) => ({ ...p, busy: true }));
    const t = setTimeout(() => {
      fetchPreview(ev.id, graph, upto, offset, ctrl.signal)
        .then(({ blob, info }) => {
          setPreview((p) => {
            if (p.url) URL.revokeObjectURL(p.url);
            return { url: URL.createObjectURL(blob), steps: info.steps ?? [], error: null, busy: false };
          });
        })
        .catch((e) => {
          if (e.name !== "AbortError") setPreview((p) => ({ ...p, error: e.message, busy: false }));
        });
    }, 180);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [ev?.id, draft, frame, picking, view.kind, selected, showUpto]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPicking(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const upload = async (files: FileList | null) => {
    if (!files) return;
    let last: Evidence | null = null;
    for (const f of Array.from(files)) {
      setUploading(`Hashing and storing ${f.name}…`);
      try {
        last = await api.upload(caseId, f);
      } catch (e) {
        setStatus({ kind: "bad", text: `${f.name}: ${(e as Error).message}` });
      }
    }
    setUploading(null);
    const d = await reload();
    if (last) selectEvidence(last.id, d.chains);
    setAuditKey((k) => k + 1);
  };

  const newChain = async () => {
    if (!ev) return;
    const n = chains.filter((c) => c.evidence_id === ev.id).length + 1;
    const c = await api.createChain(caseId, ev.id, `Enhancement ${n}`, {
      source: { frames: isVideo ? { start: frame, end: frame } : null },
      nodes: [],
    });
    setChains((cs) => [...cs, c]);
    setChainId(c.id);
    setDraft(c.graph);
    setSelected(null);
    rangeTouched.current = false;
    setAuditKey((k) => k + 1);
  };

  const switchChain = (id: string) => {
    const c = chains.find((x) => x.id === id);
    if (!c) return;
    setChainId(c.id);
    setDraft(c.graph);
    setSelected(null);
    rangeTouched.current = false;
    setPicking(null);
    setStatus(null);
    setView({ kind: "chain" });
  };

  const save = async (): Promise<Chain | null> => {
    if (!chain || !draft) return null;
    if (!dirty) return chain;
    try {
      const saved = await api.updateChain(chain.id, draft);
      setChains((cs) => cs.map((c) => (c.id === saved.id ? saved : c)));
      setDraft(saved.graph);
      setAuditKey((k) => k + 1);
      return saved;
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
      return null;
    }
  };

  const render = async () => {
    setBusy(true);
    setStatus({ kind: "info", text: "Rendering from the original…" });
    try {
      const saved = await save();
      if (!saved) return;
      const r = await api.render(saved.id);
      const fresh = { ...saved, output_hash: r.output_hash };
      setChains((cs) => cs.map((c) => (c.id === saved.id ? fresh : c)));
      setStatus({
        kind: r.pending_proposals.length ? "bad" : "ok",
        text: `Rendered. Output pixel hash ${r.output_hash}.` + (r.pending_proposals.length ? " Contains unaccepted agent proposals." : ""),
      });
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    if (!chain) return;
    setBusy(true);
    setStatus({ kind: "info", text: "Re-running from the original with no cache…" });
    try {
      const r = await api.verifyChain(chain.id);
      setStatus(
        r.reproduced
          ? { kind: "ok", text: `Reproduced bit for bit (${r.rerun_hash.slice(0, 16)}…).` }
          : { kind: "bad", text: `Did not reproduce: expected ${r.expected_hash.slice(0, 16)}…, got ${r.rerun_hash.slice(0, 16)}…` },
      );
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const accept = async () => {
    if (!chain) return;
    const c = await api.acceptProposals(chain.id);
    setChains((cs) => cs.map((x) => (x.id === c.id ? c : x)));
    setDraft(c.graph);
    setAuditKey((k) => k + 1);
  };

  const startPick = (nodeId: string, p: ParamSchema) => {
    if (picking?.nodeId === nodeId && picking.param === p.name) {
      setPicking(null);
      return;
    }
    setView({ kind: "chain" });
    setPicking({ nodeId, param: p.name, count: p.count ?? (p.name === "roi" ? 2 : undefined) });
    if (draft) setDraft({ ...draft, nodes: draft.nodes.map((n) => (n.id === nodeId ? { ...n, params: { ...n.params, [p.name]: [] } } : n)) });
  };

  const onPick = (x: number, y: number) => {
    if (!picking || !draft) return;
    const node = draft.nodes.find((n) => n.id === picking.nodeId);
    if (!node) return;
    const pts = [...((node.params[picking.param] as number[][]) ?? []), [Math.round(x), Math.round(y)]];
    setDraft({ ...draft, nodes: draft.nodes.map((n) => (n.id === node.id ? { ...n, params: { ...n.params, [picking.param]: pts } } : n)) });
    if (picking.count && pts.length >= picking.count) setPicking(null);
  };

  const pickNode = picking && draft?.nodes.find((n) => n.id === picking.nodeId);
  const pickPoints = (pickNode?.params[picking!.param] as number[][] | undefined) ?? undefined;

  const evChains = useMemo(() => chains.filter((c) => c.evidence_id === evId), [chains, evId]);
  const sourceFrame = range ? range.start : frame;

  const setRange = (which: "start" | "end") => {
    if (!draft) return;
    rangeTouched.current = true;
    const cur = draft.source.frames ?? { start: frame, end: frame };
    let next = which === "start" ? { start: frame, end: Math.max(frame, cur.end) } : { start: Math.min(cur.start, frame), end: frame };
    if (next.end - next.start > 599) next = which === "start" ? { start: frame, end: frame + 599 } : { start: frame - 599, end: frame };
    setDraft({ ...draft, source: { ...draft.source, frames: next } });
  };

  if (!kase) return <div className="loading">Loading case…</div>;

  return (
    <div className="workspace">
      <aside className="left">
        <div className="panel-title">
          Evidence <span className="spacer" />
          <button onClick={() => fileInput.current?.click()} className="primary small-btn">+ Add</button>
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
        </div>
        <div
          className="dropzone"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            upload(e.dataTransfer.files);
          }}
        >
          {uploading ?? "Drop images or video here. Originals are hashed and stored read-only."}
        </div>
        <ul className="evidence-list">
          {evidence.map((e) => (
            <li key={e.id} className={e.id === evId ? "selected" : ""} onClick={() => selectEvidence(e.id)}>
              <div className="ev-name">
                <span className={`kind ${e.media_type}`}>{e.media_type === "video" ? "VID" : "IMG"}</span> {e.filename}
              </div>
              <div className="muted small">
                {e.probe.width}×{e.probe.height}
                {e.media_type === "video" && ` · ${e.probe.frame_count} frames · ${e.probe.codec}`} · {(e.size / 1024).toFixed(0)} KB
              </div>
              <div className="hash small" title={`SHA-256 ${e.sha256}\nMD5 ${e.md5}`}>SHA-256 {e.sha256.slice(0, 16)}…</div>
            </li>
          ))}
        </ul>
        {ev && (
          <div className="ev-actions">
            <button
              onClick={async () => {
                const r = await api.verifyEvidence(ev.id);
                setStatus(r.ok && r.read_only ? { kind: "ok", text: "Original verified: hash matches and file is read-only." } : { kind: "bad", text: "Original FAILED verification." });
                setAuditKey((k) => k + 1);
              }}
            >
              Verify original
            </button>
            <a className="button" href={urls.original(ev.id)} download>
              Download original
            </a>
          </div>
        )}
      </aside>

      <main className="center">
        <Viewer
          src={view.kind === "map" ? urls.derived(view.digest) : preview.url}
          compareSrc={ev ? urls.frame(ev.id, sourceFrame) : null}
          label={view.kind === "map" ? view.label : draft ? (showUpto && selected ? `Result up to step ${selected}` : "Result (live preview)") : "Original"}
          busy={view.kind === "chain" && preview.busy}
          error={view.kind === "chain" ? preview.error : null}
          points={picking ? pickPoints : undefined}
          pickHint={picking ? `Click on the image to place points${picking.count ? ` (${pickPoints?.length ?? 0}/${picking.count})` : ""}. Esc to stop.` : null}
          onPick={picking ? onPick : undefined}
        />
        {view.kind === "map" && (
          <div className="map-bar">
            Showing analysis map. <button onClick={() => setView({ kind: "chain" })}>Back to image</button>
          </div>
        )}
        {isVideo && frames.length > 0 && (
          <FrameStrip
            frames={frames}
            current={frame}
            range={range}
            onSeek={(n) => {
              setFrame(n);
              if (draft && range && !rangeTouched.current && range.start === range.end && n !== range.start) {
                setDraft({ ...draft, source: { ...draft.source, frames: { start: n, end: n } } });
              }
            }}
            onSetStart={draft ? () => setRange("start") : undefined}
            onSetEnd={draft ? () => setRange("end") : undefined}
          />
        )}
      </main>

      <aside className="right">
        <div className="tabs">
          {(["enhance", "authenticate", "audit"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        <div className="tab-body">
          {tab === "enhance" && ev && (
            <>
              <div className="chain-picker">
                <select value={chainId ?? ""} onChange={(e) => switchChain(e.target.value)} disabled={!evChains.length}>
                  {!evChains.length && <option value="">No chains yet</option>}
                  {evChains.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <button onClick={newChain}>New chain</button>
              </div>
              {chain && draft ? (
                <>
                  <label className="small upto">
                    <input type="checkbox" checked={showUpto} onChange={(e) => setShowUpto(e.target.checked)} /> Show result up to the selected step
                  </label>
                  <ChainEditor
                    filters={filters}
                    chain={chain}
                    graph={draft}
                    dirty={dirty}
                    isVideo={isVideo}
                    selected={selected}
                    steps={preview.steps}
                    picking={picking}
                    status={status}
                    busy={busy}
                    onSelect={setSelected}
                    onChange={setDraft}
                    onPick={startPick}
                    onSave={save}
                    onRender={render}
                    onVerify={verify}
                    onAccept={accept}
                  />
                </>
              ) : (
                <p className="muted">Create a chain to start enhancing. Every step is recorded and can be re-run from the original.</p>
              )}
            </>
          )}
          {tab === "authenticate" && ev && (
            <AuthPanel
              evidence={ev}
              catalogue={analyses}
              runs={runs}
              activeMap={view.kind === "map" ? view.digest : null}
              onRun={(r) => {
                setRuns((rs) => [...rs, r]);
                setAuditKey((k) => k + 1);
                const first = Object.entries(r.result.maps)[0];
                if (first) setView({ kind: "map", digest: first[1], label: first[0] });
              }}
              onShowMap={(digest, label) => setView({ kind: "map", digest, label })}
            />
          )}
          {tab === "audit" && <AuditPanel caseId={caseId} refreshKey={auditKey} />}
          {!ev && tab !== "audit" && <p className="muted">Add evidence to begin.</p>}
          {tab !== "enhance" && status && <p className={`status ${status.kind}`}>{status.text}</p>}
        </div>
      </aside>
    </div>
  );
}
