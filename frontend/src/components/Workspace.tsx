import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, fetchPreview, urls } from "../api";
import type {
  AnalysisRun,
  AnalysisSchema,
  CameraReference,
  Case,
  Catalogue,
  Chain,
  CodecInfo,
  CodecSettings,
  Evidence,
  FilterSchema,
  FrameInfo,
  Graph,
  Measurement,
  ParamSchema,
  StepInfo,
  Triage,
} from "../types";
import { AuditPanel } from "./AuditPanel";
import { AuthPanel } from "./AuthPanel";
import { ChainEditor } from "./ChainEditor";
import { ConvertPanel, RecorderTimingPanel } from "./ConvertPanel";
import { CODEC_VIEW_CODECS, CodecBar, describeBlock } from "./CodecBar";
import { FrameStrip } from "./FrameStrip";
import { MEASURE_FIELDS, MeasurePanel, type PointFields } from "./MeasurePanel";
import { PlannerPanel } from "./PlannerPanel";
import { Viewer, type Overlay } from "./Viewer";

interface Props {
  caseId: string;
  filters: FilterSchema[];
  analyses: AnalysisSchema[];
}

type Tab = "enhance" | "authenticate" | "measure" | "audit";
const TAB_LABEL: Record<Tab, string> = { enhance: "Enhance", authenticate: "Authenticate", measure: "Measure", audit: "Audit & review" };
type ExtPick = { field: string; count: number };

const PROPRIETARY = new Set([
  "dahua_dav", "hikvision", "hxvs", "xmeye", "hikvision_old", "mpeg_ps", "pcap",
  "annexb_h264", "annexb_hevc", "annexb_mpeg4", "annexb_mpeg2",
]);
type View = { kind: "chain" } | { kind: "map"; digest: string; label: string };

const sameGraph = (a: Graph, b: Graph) => JSON.stringify(a) === JSON.stringify(b);

export function Workspace({ caseId, filters, analyses }: Props) {
  const [kase, setCase] = useState<Case | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [chains, setChains] = useState<Chain[]>([]);
  const [runs, setRuns] = useState<AnalysisRun[]>([]);
  const [references, setReferences] = useState<CameraReference[]>([]);
  const [measurements, setMeasurements] = useState<Measurement[]>([]);
  const [triage, setTriage] = useState<Triage[]>([]);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [mpoints, setMpoints] = useState<PointFields>({});
  const [mkind, setMkind] = useState("height");
  const [measureOnChain, setMeasureOnChain] = useState(false);
  const [extPick, setExtPick] = useState<ExtPick | null>(null);
  const [triageBusy, setTriageBusy] = useState(false);
  const [evId, setEvId] = useState<string | null>(null);
  const [chainId, setChainId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Graph | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [frames, setFrames] = useState<FrameInfo[]>([]);
  const [frame, setFrame] = useState(0);
  const [tab, setTab] = useState<Tab>("enhance");
  const [view, setView] = useState<View>({ kind: "chain" });
  const [codecOpen, setCodecOpen] = useState(false);
  const [codec, setCodec] = useState<CodecSettings>({ layer: "type", mv: false, grid: false });
  const [codecInfo, setCodecInfo] = useState<{ info: CodecInfo | null; error: string | null }>({ info: null, error: null });
  const [preview, setPreview] = useState<{ url: string | null; steps: StepInfo[]; error: string | null; busy: boolean }>({ url: null, steps: [], error: null, busy: false });
  const [picking, setPicking] = useState<{ nodeId: string; param: string; count?: number } | null>(null);
  const [status, setStatus] = useState<{ kind: "ok" | "bad" | "info"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const [auditKey, setAuditKey] = useState(0);
  const [removed, setRemoved] = useState<Evidence[]>([]);
  const [removedChains, setRemovedChains] = useState<Chain[]>([]);
  const [showRemovedChains, setShowRemovedChains] = useState(false);
  const [showRemoved, setShowRemoved] = useState(false);
  const [confirm, setConfirm] = useState<{ kind: "evidence" | "chain"; id: string; reason: string } | null>(null);
  const [crop, setCrop] = useState<{ nodeId: string | null } | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [collapsed, setCollapsed] = useState<{ left: boolean; right: boolean }>(() => {
    try {
      return JSON.parse(localStorage.getItem("panels") ?? "") as { left: boolean; right: boolean };
    } catch {
      return { left: false, right: false };
    }
  });
  // Undo / redo over the draft graph of the open chain.
  const past = useRef<Graph[]>([]);
  const future = useRef<Graph[]>([]);
  const lastEdit = useRef(0);
  const [, setHistoryTick] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);
  // Until the examiner sets a range explicitly, a single-frame source follows the frame scrubber.
  const rangeTouched = useRef(false);

  const ev = evidence.find((e) => e.id === evId) ?? null;
  const chain = chains.find((c) => c.id === chainId) ?? null;
  const dirty = !!(chain && draft && !sameGraph(chain.graph, draft));
  const isVideo = ev?.media_type === "video";
  const range = draft?.source.frames ?? null;
  const codecOk = isVideo && CODEC_VIEW_CODECS.includes(ev?.probe.codec ?? "");
  const codecOn = codecOpen && codecOk && view.kind === "chain" && !crop;

  const reload = useCallback(async () => {
    const d = await api.caseDetail(caseId);
    setCase(d.case);
    setEvidence(d.evidence);
    setRemoved(d.removed_evidence ?? []);
    setRemovedChains(d.removed_chains ?? []);
    setChains(d.chains);
    setRuns(d.analyses);
    setReferences(d.camera_references);
    setMeasurements(d.measurements);
    setTriage(d.triage);
    return d;
  }, [caseId]);

  useEffect(() => {
    api.catalogue().then(setCatalogue).catch(() => setCatalogue(null));
  }, []);

  useEffect(() => {
    reload().then((d) => {
      if (d.evidence.length) selectEvidence(d.evidence[0].id, d.chains);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);

  const resetHistory = () => {
    past.current = [];
    future.current = [];
    setHistoryTick((t) => t + 1);
  };

  /** Open a chain in the editor, showing its last step. */
  const openChain = (c: Chain | null) => {
    setChainId(c?.id ?? null);
    setDraft(c ? c.graph : null);
    setSelected(c?.graph.nodes.length ? c.graph.nodes[c.graph.nodes.length - 1].id : null);
    rangeTouched.current = false;
    setPicking(null);
    setCrop(null);
    resetHistory();
  };

  /** Every examiner edit to the draft goes through here so it can be undone. */
  const edit = (g: Graph) => {
    if (!draft) return;
    const now = Date.now();
    // Slider drags fire many changes; keep one undo step per burst.
    if (now - lastEdit.current > 400 || !past.current.length) {
      past.current = [...past.current.slice(-99), draft];
      future.current = [];
    }
    lastEdit.current = now;
    setDraft(g);
    setHistoryTick((t) => t + 1);
  };

  const undo = () => {
    const prev = past.current.pop();
    if (!prev || !draft) return;
    future.current = [draft, ...future.current];
    lastEdit.current = 0;
    setDraft(prev);
    if (selected && !prev.nodes.some((n) => n.id === selected)) setSelected(prev.nodes.length ? prev.nodes[prev.nodes.length - 1].id : null);
    setHistoryTick((t) => t + 1);
  };

  const redo = () => {
    const next = future.current.shift();
    if (!next || !draft) return;
    past.current = [...past.current, draft];
    lastEdit.current = 0;
    setDraft(next);
    if (selected && !next.nodes.some((n) => n.id === selected)) setSelected(next.nodes.length ? next.nodes[next.nodes.length - 1].id : null);
    setHistoryTick((t) => t + 1);
  };

  const selectEvidence = (id: string, allChains = chains) => {
    setEvId(id);
    setView({ kind: "chain" });
    setPicking(null);
    setExtPick(null);
    setMpoints({});
    setStatus(null);
    setConfirm(null);
    const c = allChains.filter((x) => x.evidence_id === id);
    const first = c[c.length - 1] ?? null;
    openChain(first);
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
    const editing = picking?.nodeId ?? crop?.nodeId ?? null;
    if (!draft || (tab === "measure" && !measureOnChain)) {
      graph = { source: { frames: isVideo ? { start: frame, end: frame } : null }, nodes: [] };
    } else if (editing) {
      // Points and boxes are drawn on the image as it enters the step being edited.
      const idx = draft.nodes.findIndex((n) => n.id === editing);
      graph = { ...draft, nodes: draft.nodes.slice(0, Math.max(0, idx)) };
    } else if (tab === "enhance" && selected === null) {
      // The Source row shows the source.
      graph = { ...draft, nodes: [] };
    } else {
      graph = draft;
      // Like a history panel: the viewer follows the selected step.
      if (tab === "enhance" && selected && draft.nodes.some((n) => n.id === selected)) upto = selected;
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
  }, [ev?.id, draft, frame, picking, crop, view.kind, selected, tab, measureOnChain]); // eslint-disable-line react-hooks/exhaustive-deps

  const keys = useRef<(e: KeyboardEvent) => void>(() => {});
  keys.current = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      setPicking(null);
      setExtPick(null);
      setCrop(null);
      setConfirm(null);
      return;
    }
    const t = e.target as HTMLElement;
    if (t.closest("input, textarea, select, [contenteditable]")) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && (e.key === "z" || e.key === "Z") && !e.shiftKey) {
      e.preventDefault();
      undo();
    } else if (mod && (e.key === "y" || e.key === "Y" || ((e.key === "z" || e.key === "Z") && e.shiftKey))) {
      e.preventDefault();
      redo();
    } else if (!mod && (e.key === "c" || e.key === "C") && ev && ev.media_type !== "container") {
      startCrop(null);
    } else if (!mod && (e.key === "m" || e.key === "M") && codecOk) {
      setCodecOpen((o) => !o);
    } else if (!mod && isVideo && frames.length && (e.key === "j" || e.key === "J" || e.key === "l" || e.key === "L")) {
      const step = (e.key.toLowerCase() === "l" ? 1 : -1) * (e.shiftKey ? 10 : 1);
      seek(Math.min(frames.length - 1, Math.max(0, frame + step)));
    }
  };
  useEffect(() => {
    if (!codecOn || !ev) return;
    let alive = true;
    setCodecInfo({ info: null, error: null });
    api
      .codecInfo(ev.id, frame)
      .then((info) => alive && setCodecInfo({ info, error: null }))
      .catch((e) => alive && setCodecInfo({ info: null, error: (e as Error).message }));
    return () => {
      alive = false;
    };
  }, [codecOn, ev?.id, frame]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem("panels", JSON.stringify(collapsed));
    } catch {
      /* storage unavailable */
    }
  }, [collapsed]);

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

  const newChain = async (graph?: Graph, name?: string): Promise<Chain | null> => {
    if (!ev) return null;
    const n = chains.filter((c) => c.evidence_id === ev.id).length + 1;
    try {
      const c = await api.createChain(
        caseId,
        ev.id,
        name ?? `Enhancement ${n}`,
        graph ?? { source: { frames: isVideo ? { start: frame, end: frame } : null }, nodes: [] },
      );
      setChains((cs) => [...cs, c]);
      openChain(c);
      setAuditKey((k) => k + 1);
      return c;
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
      return null;
    }
  };

  const duplicateChain = async () => {
    if (!chain || !draft) return;
    await newChain(draft, `${chain.name} (copy)`);
  };

  const removeChain = async (id: string, reason: string) => {
    try {
      const gone = await api.removeChain(id, reason);
      const rest = chains.filter((c) => c.id !== id);
      setChains(rest);
      setRemovedChains((rc) => [...rc, gone]);
      const mine = rest.filter((c) => c.evidence_id === evId);
      openChain(mine[mine.length - 1] ?? null);
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    }
  };

  const restoreChain = async (id: string) => {
    try {
      const c = await api.restoreChain(id);
      setRemovedChains((rc) => rc.filter((x) => x.id !== id));
      setChains((cs) => [...cs, c].sort((a, b) => a.created_at.localeCompare(b.created_at)));
      openChain(c);
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    }
  };

  const removeEvidence = async (id: string, reason: string) => {
    try {
      await api.removeEvidence(id, reason);
      const d = await reload();
      if (id === evId || !d.evidence.some((e) => e.id === evId)) {
        if (d.evidence.length) selectEvidence(d.evidence[0].id, d.chains);
        else {
          setEvId(null);
          openChain(null);
        }
      }
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    }
  };

  const restoreEvidence = async (id: string) => {
    try {
      await api.restoreEvidence(id);
      const d = await reload();
      selectEvidence(id, d.chains);
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    }
  };

  const switchChain = (id: string) => {
    const c = chains.find((x) => x.id === id);
    if (!c) return;
    openChain(c);
    setStatus(null);
    setView({ kind: "chain" });
  };

  /** Crop tool: drag a box on the image. A new crop step goes after the selected step. */
  const startCrop = (nodeId: string | null) => {
    if (crop && crop.nodeId === nodeId) {
      setCrop(null);
      return;
    }
    setTab("enhance");
    setView({ kind: "chain" });
    setPicking(null);
    setExtPick(null);
    setCrop({ nodeId });
  };

  const applyCrop = async ([x, y, width, height]: [number, number, number, number]) => {
    const target = crop;
    setCrop(null);
    if (!target) return;
    const params = { x, y, width, height };
    if (target.nodeId && draft) {
      edit({ ...draft, nodes: draft.nodes.map((n) => (n.id === target.nodeId ? { ...n, params: { ...n.params, ...params } } : n)) });
      return;
    }
    const spec = filters.find((f) => f.id === "geometry.crop");
    let g = draft;
    if (!chain || !g) {
      const c = await newChain();
      if (!c) return;
      g = c.graph;
    }
    const ids = new Set(g.nodes.map((n) => n.id));
    let k = g.nodes.length + 1;
    while (ids.has(`n${k}`)) k++;
    const node = { id: `n${k}`, filter: "geometry.crop", version: spec?.version, params, enabled: true };
    // The box was drawn on the output of the selected step (the source if none is selected).
    const at = selected ? g.nodes.findIndex((n) => n.id === selected) + 1 : chain ? 0 : g.nodes.length;
    const nodes = [...g.nodes];
    nodes.splice(at, 0, node);
    if (chain && draft) edit({ ...g, nodes });
    else setDraft({ ...g, nodes });
    setSelected(node.id);
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
    setCrop(null);
    if (draft) edit({ ...draft, nodes: draft.nodes.map((n) => (n.id === nodeId ? { ...n, params: { ...n.params, [p.name]: [] } } : n)) });
  };

  const startExtPick = (field: string, count: number, reset: boolean) => {
    if (extPick?.field === field) {
      setExtPick(null);
      return;
    }
    setPicking(null);
    setView({ kind: "chain" });
    if (reset) setMpoints((m) => ({ ...m, [field]: [] }));
    setExtPick({ field, count });
  };

  const onPick = (x: number, y: number) => {
    if (extPick) {
      const pts = [...(mpoints[extPick.field] ?? []), [x, y]];
      setMpoints((m) => ({ ...m, [extPick.field]: pts }));
      if (pts.length >= extPick.count) setExtPick(null);
      return;
    }
    if (!picking || !draft) return;
    const node = draft.nodes.find((n) => n.id === picking.nodeId);
    if (!node) return;
    const pts = [...((node.params[picking.param] as number[][]) ?? []), [Math.round(x), Math.round(y)]];
    edit({ ...draft, nodes: draft.nodes.map((n) => (n.id === node.id ? { ...n, params: { ...n.params, [picking.param]: pts } } : n)) });
    if (picking.count && pts.length >= picking.count) setPicking(null);
  };

  const pickNode = picking && draft?.nodes.find((n) => n.id === picking.nodeId);
  const pickPoints = (pickNode?.params[picking!.param] as number[][] | undefined) ?? undefined;

  const evChains = useMemo(() => chains.filter((c) => c.evidence_id === evId), [chains, evId]);
  const evRemovedChains = removedChains.filter((c) => c.evidence_id === evId);
  const triageBy = useMemo(() => Object.fromEntries(triage.map((t) => [t.evidence_id, t])), [triage]);

  const overlays: Overlay[] = [];
  if (tab === "measure") {
    for (const f of MEASURE_FIELDS[mkind] ?? []) {
      const pts = mpoints[f.key] ?? [];
      overlays.push({ points: pts, color: f.color, mode: f.kind === "lines" ? "segments" : "points", label: f.kind === "lines" ? undefined : f.label });
    }
  } else if (tab === "authenticate") {
    for (const [k, pts] of Object.entries(mpoints)) {
      if (k.startsWith("auth:") && pts.length) overlays.push({ points: pts, color: "#ffcf3f", mode: "rect", label: k.split(":")[2] });
    }
  } else if (tab === "enhance" && mpoints.plan_roi?.length) {
    overlays.push({ points: mpoints.plan_roi, color: "#3fe08a", mode: "rect", label: "planner region" });
  }
  const extHint = extPick
    ? `Click on the image: ${extPick.field.replace(/_/g, " ")} (${mpoints[extPick.field]?.length ?? 0}/${extPick.count}). Esc to stop.`
    : null;

  const runTriage = async () => {
    setTriageBusy(true);
    try {
      const r = await api.triage(caseId);
      setStatus({ kind: "info", text: `Triage: ${r.counts.red ?? 0} red, ${r.counts.yellow ?? 0} yellow, ${r.counts.green ?? 0} green.` });
      await reload();
      setAuditKey((k) => k + 1);
    } catch (e) {
      setStatus({ kind: "bad", text: (e as Error).message });
    } finally {
      setTriageBusy(false);
    }
  };
  const sourceFrame = range ? range.start : frame;

  const setRange = (which: "start" | "end") => {
    if (!draft) return;
    rangeTouched.current = true;
    const cur = draft.source.frames ?? { start: frame, end: frame };
    let next = which === "start" ? { start: frame, end: Math.max(frame, cur.end) } : { start: Math.min(cur.start, frame), end: frame };
    if (next.end - next.start > 599) next = which === "start" ? { start: frame, end: frame + 599 } : { start: frame - 599, end: frame };
    edit({ ...draft, source: { ...draft.source, frames: next } });
  };

  const seek = (n: number) => {
    setFrame(n);
    if (draft && range && !rangeTouched.current && range.start === range.end && n !== range.start) {
      setDraft({ ...draft, source: { ...draft.source, frames: { start: n, end: n } } });
    }
  };

  const selIndex = draft && selected ? draft.nodes.findIndex((n) => n.id === selected) : -1;
  const selName = selIndex >= 0 ? filters.find((f) => f.id === draft!.nodes[selIndex].filter)?.name ?? draft!.nodes[selIndex].filter : null;
  const viewerLabel =
    view.kind === "map"
      ? view.label
      : crop
        ? "Crop: drag a box"
        : !draft || (tab === "measure" && !measureOnChain) || (tab === "enhance" && selIndex < 0)
          ? "Source"
          : tab === "enhance" && draft.nodes.length && selIndex < draft.nodes.length - 1
            ? `Step ${selIndex + 1} of ${draft.nodes.length} · ${selName}`
            : draft.nodes.length
              ? "Result"
              : "Source";

  if (!kase) return <div className="loading">Loading case…</div>;

  const kindTag = (e: Evidence) => (e.media_type === "video" ? "VID" : e.media_type === "container" ? "REC" : "IMG");
  const meta = (e: Evidence) =>
    e.media_type === "container"
      ? `${e.probe.format} · needs conversion`
      : `${e.probe.width}×${e.probe.height}` + (e.media_type === "video" ? ` · ${e.probe.frame_count} fr` : "") + (e.derived_from ? " · copy" : "");

  const confirmBox = (onYes: () => void, what: string) =>
    confirm && (
      <div className="confirm" onClick={(e) => e.stopPropagation()}>
        <div className="small">
          Remove {what} from the case? {confirm.kind === "evidence" && "The original file is kept and the removal is logged."}
        </div>
        <input
          autoFocus
          type="text"
          placeholder="Reason (optional, logged)"
          value={confirm.reason}
          onChange={(e) => setConfirm({ ...confirm, reason: e.target.value })}
          onKeyDown={(e) => e.key === "Enter" && onYes()}
        />
        <div className="row">
          <button className="danger" onClick={onYes}>Remove</button>
          <button onClick={() => setConfirm(null)}>Cancel</button>
        </div>
      </div>
    );

  return (
    <div className={`workspace${collapsed.left ? " no-left" : ""}${collapsed.right ? " no-right" : ""}`}>
      {collapsed.left ? (
        <button className="rail left-rail" onClick={() => setCollapsed((c) => ({ ...c, left: false }))} title="Show evidence">
          ›
        </button>
      ) : (
        <aside
          className={`left${dragOver ? " drag-over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            upload(e.dataTransfer.files);
          }}
        >
          <div className="panel-title">
            Evidence <span className="count">{evidence.length || ""}</span>
            <span className="spacer" />
            <button onClick={runTriage} className="small-btn" disabled={triageBusy || !evidence.length} title="Smart Report on every item: where to look first">
              {triageBusy ? "Triage…" : "Triage"}
            </button>
            <button onClick={() => fileInput.current?.click()} className="primary small-btn" title="Add images or video (or drop files here)">
              + Add
            </button>
            <button className="icon" onClick={() => setCollapsed((c) => ({ ...c, left: true }))} title="Hide panel">
              ‹
            </button>
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
          </div>
          {(uploading || !evidence.length || dragOver) && (
            <div className="dropzone" onClick={() => fileInput.current?.click()}>
              {uploading ?? "Drop images or video here"}
            </div>
          )}
          <ul className="evidence-list">
            {evidence.map((e) => (
              <li
                key={e.id}
                className={e.id === evId ? "selected" : ""}
                onClick={() => selectEvidence(e.id)}
                title={`${e.filename}\n${(e.size / 1024).toFixed(0)} KB · ingested ${new Date(e.ingested_at).toLocaleString()} by ${e.ingested_by}\nSHA-256 ${e.sha256}\nMD5 ${e.md5}`}
              >
                <div className="ev-row">
                  {triageBy[e.id] ? <span className={`light ${triageBy[e.id].level}`} title={triageBy[e.id].result.line} /> : <span className="light" />}
                  <span className={`kind ${e.media_type}`}>{kindTag(e)}</span>
                  <span className="ev-name">{e.filename}</span>
                  <span className="ev-meta">{meta(e)}</span>
                  <button
                    className="icon ev-del"
                    title="Remove from case"
                    onClick={(x) => {
                      x.stopPropagation();
                      setConfirm({ kind: "evidence", id: e.id, reason: "" });
                    }}
                  >
                    🗑
                  </button>
                </div>
                {confirm?.kind === "evidence" && confirm.id === e.id &&
                  confirmBox(() => {
                    removeEvidence(e.id, confirm.reason);
                    setConfirm(null);
                  }, `“${e.filename}”`)}
              </li>
            ))}
          </ul>
          {ev && (
            <div className="ev-actions">
              <button
                onClick={async () => {
                  const r = await api.verifyEvidence(ev.id);
                  setStatus(r.ok && r.read_only ? { kind: "ok", text: "Original verified: hash matches, read-only." } : { kind: "bad", text: "Original FAILED verification." });
                  setAuditKey((k) => k + 1);
                }}
                title="Re-hash the stored original and check it is read-only"
              >
                ✓ Verify
              </button>
              <a className="button" href={urls.original(ev.id)} download title="Download the original file">
                ⤓ Original
              </a>
            </div>
          )}
          {ev && catalogue && !ev.derived_from && (ev.media_type === "container" || PROPRIETARY.has(ev.probe.identified?.family ?? "") || ["h264", "hevc"].includes(ev.probe.format ?? "")) && (
            <ConvertPanel
              key={ev.id}
              evidence={ev}
              methods={catalogue.conversion_methods}
              onConverted={async (c) => {
                const d = await reload();
                selectEvidence(c.id, d.chains);
                setAuditKey((k) => k + 1);
              }}
            />
          )}
          {ev?.conversion?.timing && <RecorderTimingPanel key={`t-${ev.id}`} evidence={ev} />}
          {removed.length > 0 && (
            <div className="removed">
              <button className="link small" onClick={() => setShowRemoved((v) => !v)}>
                {showRemoved ? "Hide" : "Show"} removed ({removed.length})
              </button>
              {showRemoved && (
                <ul className="removed-list">
                  {removed.map((r) => (
                    <li key={r.id} title={`Removed by ${r.removed?.by} ${r.removed ? new Date(r.removed.at).toLocaleString() : ""}${r.removed?.reason ? `\n${r.removed.reason}` : ""}`}>
                      <span className="ev-name">{r.filename}</span>
                      <button className="small-btn" onClick={() => restoreEvidence(r.id)}>Restore</button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </aside>
      )}

      <main className="center">
        <Viewer
          src={
            codecOn && ev
              ? urls.codec(ev.id, frame, codec)
              : view.kind === "map"
                ? urls.derived(view.digest)
                : ev?.media_type === "container"
                  ? null
                  : preview.url
          }
          overlays={view.kind === "chain" && !crop && !codecOn ? overlays : undefined}
          compareSrc={
            codecOn && ev
              ? urls.frame(ev.id, frame)
              : ev && !crop && (view.kind === "map" || viewerLabel !== "Source")
                ? urls.frame(ev.id, sourceFrame)
                : null
          }
          compareLabel={codecOn ? "Frame" : undefined}
          label={codecOn ? `Macroblocks${codecInfo.info ? ` · ${codecInfo.info.type}-frame` : ""}` : viewerLabel}
          busy={view.kind === "chain" && !codecOn && preview.busy}
          error={codecOn ? codecInfo.error : view.kind === "chain" ? preview.error : null}
          describe={codecOn ? (x, y) => describeBlock(codecInfo.info, x, y) : undefined}
          points={picking ? pickPoints : undefined}
          pickHint={picking ? `Click to place points${picking.count ? ` (${pickPoints?.length ?? 0}/${picking.count})` : ""} · Esc to stop` : crop ? "Drag a box · Shift = square · Esc to cancel" : extHint}
          onPick={picking || extPick ? onPick : undefined}
          onRect={crop && preview.url && !preview.busy ? applyCrop : undefined}
          illustrative={view.kind === "chain" && !!draft && draft.nodes.some((n) => n.enabled && filters.find((f) => f.id === n.filter)?.generative)}
          tools={
            ev && ev.media_type !== "container" ? (
              <>
                {codecOk && (
                  <button className={codecOn ? "active" : ""} onClick={() => setCodecOpen((o) => !o)} title="Macroblocks, QP and motion vectors (M)">
                    ▦ MB
                  </button>
                )}
                <button className={crop ? "active" : ""} onClick={() => startCrop(null)} title="Crop: drag a box on the image (C)">
                  ⛶ Crop
                </button>
              </>
            ) : null
          }
        />
        {view.kind === "map" && (
          <div className="map-bar">
            Analysis map <button onClick={() => setView({ kind: "chain" })}>Back to image</button>
          </div>
        )}
        {codecOn && <CodecBar settings={codec} info={codecInfo.info} error={codecInfo.error} onChange={setCodec} onClose={() => setCodecOpen(false)} />}
        {isVideo && frames.length > 0 && (
          <FrameStrip
            frames={frames}
            current={frame}
            range={range}
            onSeek={seek}
            onSetStart={draft ? () => setRange("start") : undefined}
            onSetEnd={draft ? () => setRange("end") : undefined}
          />
        )}
      </main>

      {collapsed.right ? (
        <button className="rail right-rail" onClick={() => setCollapsed((c) => ({ ...c, right: false }))} title="Show tools">
          ‹
        </button>
      ) : (
      <aside className="right">
        <div className="tabs">
          {(["enhance", "authenticate", "measure", "audit"] as Tab[]).map((t) => (
            <button
              key={t}
              className={tab === t ? "active" : ""}
              onClick={() => {
                setTab(t);
                setExtPick(null);
                setCrop(null);
              }}
            >
              {TAB_LABEL[t]}
            </button>
          ))}
          <button className="icon tab-hide" onClick={() => setCollapsed((c) => ({ ...c, right: true }))} title="Hide panel">
            ›
          </button>
        </div>
        <div className="tab-body">
          {tab !== "audit" && ev?.media_type === "container" && (
            <p className="muted">Convert this recorder file first (left panel). The original stays untouched.</p>
          )}
          {tab === "enhance" && ev && ev.media_type !== "container" && (
            <>
              <div className="chain-picker">
                <select value={chainId ?? ""} onChange={(e) => switchChain(e.target.value)} disabled={!evChains.length} title="Enhancement chain">
                  {!evChains.length && <option value="">No chains yet</option>}
                  {evChains.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <button className="icon" onClick={() => newChain()} title="New chain">＋</button>
                <button className="icon" onClick={duplicateChain} disabled={!chain} title="Duplicate this chain (to compare a variation)">⧉</button>
                <button className="icon" onClick={() => chain && setConfirm({ kind: "chain", id: chain.id, reason: "" })} disabled={!chain} title="Remove this chain">
                  🗑
                </button>
              </div>
              {evRemovedChains.length > 0 && (
                <div className="removed-chains">
                  <button className="link small" onClick={() => setShowRemovedChains((v) => !v)}>
                    {showRemovedChains ? "Hide" : "Show"} removed chains ({evRemovedChains.length})
                  </button>
                  {showRemovedChains && (
                    <ul className="removed-list">
                      {evRemovedChains.map((c) => (
                        <li key={c.id} title={`Removed by ${c.removed?.by ?? "?"}${c.removed?.reason ? `\n${c.removed.reason}` : ""}`}>
                          <span className="ev-name">{c.name}</span>
                          <button className="small-btn" onClick={() => restoreChain(c.id)}>Restore</button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {chain && confirm?.kind === "chain" && confirm.id === chain.id &&
                confirmBox(() => {
                  removeChain(chain.id, confirm.reason);
                  setConfirm(null);
                }, `chain “${chain.name}”`)}
              {chain && draft ? (
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
                  onSelect={(id) => {
                    setSelected(id);
                    setCrop(null);
                  }}
                  onChange={edit}
                  onPick={startPick}
                  onDrawCrop={startCrop}
                  onStartPick={(nodeId, p) => {
                    setCrop(null);
                    setExtPick(null);
                    setView({ kind: "chain" });
                    setPicking({ nodeId, param: p.name, count: p.count ?? (p.name === "roi" ? 2 : undefined) });
                  }}
                  drawingCrop={!!crop?.nodeId && crop.nodeId === selected}
                  onSave={save}
                  onRender={render}
                  onVerify={verify}
                  onAccept={accept}
                  onUndo={undo}
                  onRedo={redo}
                  canUndo={past.current.length > 0}
                  canRedo={future.current.length > 0}
                  frame={Math.max(0, frame - (draft.source.frames?.start ?? frame))}
                />
              ) : (
                <div className="empty">
                  <button className="primary" onClick={() => newChain()}>Start enhancing</button>
                  <span className="muted small">or press C to crop</span>
                </div>
              )}
              {catalogue && (
                <PlannerPanel
                  key={ev.id}
                  caseId={caseId}
                  evidence={ev}
                  frame={frame}
                  range={draft?.source.frames ?? null}
                  roi={mpoints.plan_roi}
                  picking={extPick?.field === "plan_roi"}
                  workflows={catalogue.planner_workflows}
                  graph={draft}
                  onPickRoi={() => startExtPick("plan_roi", 2, true)}
                  onClearRoi={() => setMpoints((m) => ({ ...m, plan_roi: [] }))}
                  onPlanned={async () => {
                    await reload();
                    setAuditKey((k) => k + 1);
                  }}
                  onOpenChain={(id) => {
                    reload().then((d) => {
                      const c = d.chains.find((x) => x.id === id);
                      if (c) openChain(c);
                    });
                  }}
                />
              )}
            </>
          )}
          {tab === "authenticate" && ev && ev.media_type !== "container" && (
            <AuthPanel
              caseId={caseId}
              evidence={ev}
              allEvidence={evidence}
              references={references}
              triage={triageBy[ev.id] ?? null}
              onTriage={(t) => {
                setTriage((ts) => [...ts.filter((x) => x.evidence_id !== t.evidence_id), t]);
                reload();
                setAuditKey((k) => k + 1);
              }}
              onReference={(r) => {
                setReferences((rs) => [...rs, r]);
                setAuditKey((k) => k + 1);
              }}
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
              points={mpoints}
              pickField={extPick?.field ?? null}
              onStartPick={startExtPick}
              onPoints={(p) => setMpoints(p)}
            />
          )}
          {tab === "measure" && ev && ev.media_type !== "container" && catalogue && (
            <MeasurePanel
              evidence={ev}
              frame={frame}
              chain={chain}
              chainDirty={dirty}
              methods={catalogue.measurements}
              measurements={measurements}
              points={mpoints}
              kind={mkind}
              picking={extPick?.field ?? null}
              onChain={measureOnChain && !!chain && !dirty}
              onOnChain={setMeasureOnChain}
              onKind={(k) => {
                setMkind(k);
                setMpoints({});
                setExtPick(null);
              }}
              onPoints={(p) => setMpoints(p)}
              onStartPick={startExtPick}
              onDone={(m) => {
                setMeasurements((ms) => [...ms, m]);
                setAuditKey((k) => k + 1);
              }}
            />
          )}
          {tab === "audit" && <AuditPanel caseId={caseId} refreshKey={auditKey} onChanged={() => setAuditKey((k) => k + 1)} />}
          {!ev && tab !== "audit" && <p className="muted">Add evidence to begin.</p>}
          {tab !== "enhance" && status && <p className={`status ${status.kind}`}>{status.text}</p>}
        </div>
      </aside>
      )}
    </div>
  );
}
