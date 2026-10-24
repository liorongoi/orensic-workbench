import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, fetchPreview, urls } from "../api";
import { cropBoxOnSource, cropPosition, framingGraph, isBoxParam } from "../crop";
import { bridge, keyOf, onCommand, openWindow, type Command, type OfferedFile } from "../desktop";
import type {
  AnalysisRun,
  AnalysisSchema,
  CameraReference,
  Case,
  Catalogue,
  Chain,
  ChainNode,
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
import { BatchPanel, CasePanel, IntelPanel, VideoPanel } from "./PlatformPanels";
import { CODEC_VIEW_CODECS, CodecBar, describeBlock } from "./CodecBar";
import { FrameStrip } from "./FrameStrip";
import { MEASURE_FIELDS, MeasurePanel, type PointFields } from "./MeasurePanel";
import { PlannerPanel } from "./PlannerPanel";
import { Viewer, type Overlay } from "./Viewer";

interface Props {
  caseId: string;
  filters: FilterSchema[];
  analyses: AnalysisSchema[];
  /** Evidence to open first (a second viewer window opens on the same item). */
  evidence?: string | null;
  /** A window with only the viewer: both panels start hidden and the layout is not saved. */
  viewerOnly?: boolean;
}

type Widths = { left: number; right: number };
const DEFAULT_WIDTHS: Widths = { left: 260, right: 360 };
const clampWidths = (w: Widths): Widths => ({
  left: Math.round(Math.min(640, Math.max(180, w.left))),
  right: Math.round(Math.min(760, Math.max(280, w.right))),
});

type Tab = "enhance" | "authenticate" | "measure" | "video" | "intel" | "case" | "audit";
const TABS: Tab[] = ["enhance", "authenticate", "measure", "video", "intel", "case", "audit"];
const TAB_LABEL: Record<Tab, string> = {
  enhance: "Enhance", authenticate: "Authenticate", measure: "Measure", video: "Video & sync", intel: "Detect & redact",
  case: "Case", audit: "Audit & review",
};
type ExtPick = { field: string; count: number };

const PROPRIETARY = new Set([
  "dahua_dav", "hikvision", "hxvs", "xmeye", "hikvision_old", "mpeg_ps", "pcap",
  "annexb_h264", "annexb_hevc", "annexb_mpeg4", "annexb_mpeg2",
]);
type View = { kind: "chain" } | { kind: "map"; digest: string; label: string };

const sameGraph = (a: Graph, b: Graph) => JSON.stringify(a) === JSON.stringify(b);

export function Workspace({ caseId, filters, analyses, evidence: initialEvidence, viewerOnly }: Props) {
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
  // Crops made by marking a region for a filter, by that filter's step id, so marking again replaces them.
  const markedCrops = useRef<Record<string, string>>({});
  useEffect(() => {
    markedCrops.current = {}; // step ids repeat between chains
  }, [evId, chainId]);
  // After a crop the viewer shows only the cropped area; this shows the whole image until clicked again.
  const [fullImage, setFullImage] = useState(false);
  // The cropped area of the source, untouched: what the result is compared with once the chain has a crop.
  const [framingUrl, setFramingUrl] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [collapsed, setCollapsed] = useState<{ left: boolean; right: boolean }>(() => {
    if (viewerOnly) return { left: true, right: true };
    try {
      return JSON.parse(localStorage.getItem("panels") ?? "") as { left: boolean; right: boolean };
    } catch {
      return { left: false, right: false };
    }
  });
  // Where the tools (algorithm) panel sits. Left is the default: examiners work from the
  // filter and analysis lists while the evidence list stays out of the way on the right.
  const [toolsLeft, setToolsLeft] = useState<boolean>(() => {
    try {
      return localStorage.getItem("toolsSide") !== "right";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("toolsSide", toolsLeft ? "left" : "right");
    } catch {
      /* storage unavailable: the choice lasts for this window only */
    }
  }, [toolsLeft]);
  // Panel widths, dragged on the splitters and kept between sessions.
  const [widths, setWidths] = useState<Widths>(() => {
    try {
      return clampWidths({ ...DEFAULT_WIDTHS, ...JSON.parse(localStorage.getItem("panelWidths") ?? "{}") });
    } catch {
      return DEFAULT_WIDTHS;
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
  const framing = useMemo(() => framingGraph(filters, draft), [filters, draft]);
  const framingKey = framing ? JSON.stringify(framing) : "";
  const showFull = fullImage && !!framing && view.kind === "chain";

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
      const first = d.evidence.find((e) => e.id === initialEvidence) ?? d.evidence[0];
      if (first) selectEvidence(first.id, d.chains);
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

  const previewReq = useRef<{
    want: { evId: string; graph: Graph; upto: string | null; offset: number } | null;
    inFlight: boolean;
    ctrl: AbortController | null;
    evId: string | null;
  }>({ want: null, inFlight: false, ctrl: null, evId: null });
  const pumpPreview = () => {
    const r = previewReq.current;
    if (r.inFlight || !r.want) return;
    const job = r.want;
    r.want = null;
    r.inFlight = true;
    const ctrl = new AbortController();
    r.ctrl = ctrl;
    fetchPreview(job.evId, job.graph, job.upto, job.offset, ctrl.signal)
      .then(({ blob, info }) => {
        if (job.evId !== r.evId) return;
        setPreview((p) => {
          if (p.url) URL.revokeObjectURL(p.url);
          return { url: URL.createObjectURL(blob), steps: info.steps ?? [], error: null, busy: r.want !== null };
        });
      })
      .catch((e) => {
        if (e.name !== "AbortError" && job.evId === r.evId) setPreview((p) => ({ ...p, error: e.message, busy: r.want !== null }));
      })
      .finally(() => {
        r.inFlight = false;
        r.ctrl = null;
        pumpPreview();
      });
  };

  // Switching evidence drops the request (declared before the preview effect so it runs first) for the previous one.
  useEffect(() => {
    previewReq.current.evId = ev?.id ?? null;
    return () => {
      const r = previewReq.current;
      r.want = null;
      r.ctrl?.abort();
    };
  }, [ev?.id]);

  // Live preview (server side, not stored, not audited).
  useEffect(() => {
    if (!ev || view.kind !== "chain") return;
    let graph: Graph;
    let upto: string | null = null;
    const editing = picking?.nodeId ?? crop?.nodeId ?? null;
    if (!draft || (tab === "measure" && !measureOnChain)) {
      graph = { source: { frames: isVideo ? { start: frame, end: frame } : null }, nodes: [] };
    } else if (showFull && !editing) {
      // "Full image": the whole source frame, with the crop box drawn on it.
      graph = { ...draft, nodes: [] };
    } else if (editing) {
      // Points and boxes are drawn on the image as it enters the step being edited.
      const idx = draft.nodes.findIndex((n) => n.id === editing);
      graph = { ...draft, nodes: draft.nodes.slice(0, Math.max(0, idx)) };
    } else if (tab === "enhance" && selected === null) {
      // The Source row shows the source; once cropped, only the cropped area of it.
      graph = framing ?? { ...draft, nodes: [] };
    } else {
      graph = draft;
      // Like a history panel: the viewer follows the selected step.
      if (tab === "enhance" && selected && draft.nodes.some((n) => n.id === selected)) upto = selected;
    }
    const offset = graph.source.frames ? Math.max(0, frame - graph.source.frames.start) : 0;
    // Latest wins: while a slider moves, one request is in flight and the newest settings wait
    // behind it, so the image follows the slider instead of waiting until it is let go.
    previewReq.current.want = { evId: ev.id, graph, upto, offset };
    setPreview((p) => (p.busy ? p : { ...p, busy: true }));
    pumpPreview();
  }, [ev?.id, draft, frame, picking, crop, view.kind, selected, tab, measureOnChain, showFull, framingKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // The comparison image for a cropped chain: the same area of the source with no enhancement.
  useEffect(() => {
    if (!ev || !framing) {
      setFramingUrl(null);
      return;
    }
    const ctrl = new AbortController();
    let url: string | null = null;
    const offset = framing.source.frames ? Math.max(0, frame - framing.source.frames.start) : 0;
    const t = setTimeout(() => {
      fetchPreview(ev.id, framing, null, offset, ctrl.signal)
        .then(({ blob }) => {
          url = URL.createObjectURL(blob);
          setFramingUrl(url);
        })
        .catch(() => undefined);
    }, 200);
    return () => {
      clearTimeout(t);
      ctrl.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [ev?.id, framingKey, frame]); // eslint-disable-line react-hooks/exhaustive-deps

  // A new piece of evidence, or a chain without a crop, starts on the cropped view again.
  useEffect(() => setFullImage(false), [ev?.id, chainId, !!framing]); // eslint-disable-line react-hooks/exhaustive-deps

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
    const k = keyOf(e);
    if (e.altKey) return;
    if (mod && k === "z" && !e.shiftKey) {
      e.preventDefault();
      undo();
    } else if (mod && (k === "y" || (k === "z" && e.shiftKey))) {
      e.preventDefault();
      redo();
    } else if (!mod && k === "c" && ev && ev.media_type !== "container") {
      startCrop(null);
    } else if (!mod && k === "g" && framing) {
      setFullImage((f) => !f);
    } else if (!mod && k === "m" && codecOk) {
      setCodecOpen((o) => !o);
    } else if (!mod && isVideo && frames.length && (k === "j" || k === "l")) {
      const step = (k === "l" ? 1 : -1) * (e.shiftKey ? 10 : 1);
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

  // Menu bar commands and window-wide shortcuts (see MenuBar).
  const commands = useRef<(c: Command) => void>(() => {});
  commands.current = (c: Command) => {
    const showTab = (t: Tab) => {
      setTab(t);
      setExtPick(null);
      setCrop(null);
      setCollapsed((x) => ({ ...x, right: false }));
    };
    switch (c) {
      case "add-evidence":
        chooseEvidence();
        break;
      case "undo":
        undo();
        break;
      case "redo":
        redo();
        break;
      case "full-image":
        if (framing) setFullImage((f) => !f);
        break;
      case "crop":
        if (ev && ev.media_type !== "container") startCrop(null);
        break;
      case "triage":
        if (evidence.length && !triageBusy) runTriage();
        break;
      case "toggle-left":
        setCollapsed((x) => ({ ...x, left: !x.left }));
        break;
      case "toggle-right":
        setCollapsed((x) => ({ ...x, right: !x.right }));
        break;
      case "focus-viewer":
        setCollapsed((x) => (x.left && x.right ? { left: false, right: false } : { left: true, right: true }));
        break;
      case "viewer-window":
        openWindow(`#/case/${caseId}?ev=${evId ?? ""}&layout=viewer`, `${kase?.name ?? "Lucida"} · viewer`);
        break;
      case "tab-enhance":
        showTab("enhance");
        break;
      case "tab-authenticate":
        showTab("authenticate");
        break;
      case "tab-measure":
        showTab("measure");
        break;
      case "tab-audit":
        showTab("audit");
        break;
    }
  };
  useEffect(() => onCommand((c) => commands.current(c)), []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (viewerOnly) return;
    try {
      localStorage.setItem("panels", JSON.stringify(collapsed));
    } catch {
      /* storage unavailable */
    }
  }, [collapsed, viewerOnly]);

  useEffect(() => {
    try {
      localStorage.setItem("panelWidths", JSON.stringify(widths));
    } catch {
      /* storage unavailable */
    }
  }, [widths]);

  /** Drag a splitter: the left one sets the evidence panel's width, the right one the tools panel's. */
  const startResize = (side: "left" | "right") => (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    const x0 = e.clientX;
    const w0 = widths;
    const move = (m: PointerEvent) => {
      // With the tools panel on the left the two panels swap sides, so dragging works the other way.
      const dx = (m.clientX - x0) * (toolsLeft ? -1 : 1);
      setWidths(clampWidths(side === "left" ? { ...w0, left: w0.left + dx } : { ...w0, right: w0.right - dx }));
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };

  /** Add evidence one file at a time; `send` stores one file and returns its evidence record. */
  const addEvidence = async (items: { name: string; send: () => Promise<Evidence> }[]) => {
    if (!items.length) return;
    let last: Evidence | null = null;
    for (const it of items) {
      setUploading(`Hashing and storing ${it.name}…`);
      try {
        last = await it.send();
      } catch (e) {
        setStatus({ kind: "bad", text: `${it.name}: ${(e as Error).message}` });
      }
    }
    setUploading(null);
    const d = await reload();
    if (last) selectEvidence(last.id, d.chains);
    setAuditKey((k) => k + 1);
  };

  const upload = (files: FileList | null) =>
    addEvidence(Array.from(files ?? []).map((f) => ({ name: f.name, send: () => api.upload(caseId, f) })));

  /** Add evidence: the native Open dialog in the desktop app (files are read from their folder,
   * not uploaded), the browser's file picker otherwise. */
  const chooseEvidence = async () => {
    const b = bridge();
    if (!b) {
      fileInput.current?.click();
      return;
    }
    let offered: OfferedFile[] = [];
    try {
      offered = await b.open_evidence();
    } catch (e) {
      setStatus({ kind: "bad", text: `Open dialog: ${(e as Error).message}` });
    }
    await addEvidence(offered.map((f) => ({ name: f.name, send: () => api.ingestLocal(caseId, f.token) })));
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

  /** Crop tool: drag a box on the image. The crop goes as early in the chain as it can, so every enhancement
   * runs on the cropped area, and the viewer then shows only that area (G or "Full image" to see the rest). */
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
    setFullImage(false);
    if (target.nodeId && draft) {
      // The box was drawn on this step's input; move the crop as early as it can go so every
      // enhancement after it works on the cropped area only.
      const i = draft.nodes.findIndex((n) => n.id === target.nodeId);
      if (i < 0) return;
      const node = { ...draft.nodes[i], params: { ...draft.nodes[i].params, ...params } };
      const rest = draft.nodes.filter((_, j) => j !== i);
      rest.splice(cropPosition(filters, rest, i), 0, node);
      edit({ ...draft, nodes: rest });
      setSelected(rest[rest.length - 1].id);
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
    // It goes as early as it can, so the enhancements run on the cropped area only.
    const drawnOn = selected ? g.nodes.findIndex((n) => n.id === selected) + 1 : chain ? 0 : g.nodes.length;
    const at = cropPosition(filters, g.nodes, drawnOn);
    const nodes = [...g.nodes];
    nodes.splice(at, 0, node);
    if (chain && draft) edit({ ...g, nodes });
    else setDraft({ ...g, nodes });
    // Show the whole chain's result on the cropped area; new filters then go at the end.
    setSelected(nodes[nodes.length - 1].id);
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
    setFullImage(false);
    if (!draft) return;
    // Marking a region again starts from the image without the crop the last marking made.
    const madeCrop = p.name === "roi" ? markedCropOf(draft, nodeId) : undefined;
    delete markedCrops.current[nodeId];
    edit({
      ...draft,
      nodes: draft.nodes
        .filter((n) => n.id !== madeCrop)
        .map((n) => (n.id === nodeId ? { ...n, params: { ...n.params, [p.name]: [] } } : n)),
    });
  };

  /** The crop that marking a region for this step added, if it is still in the chain: the one remembered for it,
   * else (after a reload) the crop next to it with its note. Never another crop. */
  const markedCropOf = (g: Graph, nodeId: string): string | undefined => {
    const idx = g.nodes.findIndex((n) => n.id === nodeId);
    if (idx < 0) return undefined;
    const name = filters.find((f) => f.id === g.nodes[idx].filter)?.name ?? g.nodes[idx].filter;
    const isMarked = (n: ChainNode | undefined) => !!n && n.filter === "geometry.crop" && n.note === `Area marked for ${name}`;
    const known = g.nodes.find((n) => n.id === markedCrops.current[nodeId]);
    if (known) return isMarked(known) ? known.id : undefined;
    if (g.nodes[idx].filter === "stack.stabilise") return isMarked(g.nodes[idx + 1]) ? g.nodes[idx + 1].id : undefined;
    for (let i = idx - 1; i >= 0; i--) {
      if (g.nodes[i].filter === "geometry.crop") return isMarked(g.nodes[i]) ? g.nodes[i].id : undefined;
    }
    return undefined;
  };

  /** A box marked on the image for a filter (a region, a light streak). A region of interest also crops the
   * image to it, so the filter and everything after it work on that area only; "Full image" shows the rest. */
  const applyBox = ([x, y, w, h]: [number, number, number, number]) => {
    const target = picking;
    setPicking(null);
    if (!target || !draft) return;
    const idx = draft.nodes.findIndex((n) => n.id === target.nodeId);
    if (idx < 0) return;
    const node = draft.nodes[idx];
    const box = [
      [x, y],
      [x + w - 1, y + h - 1],
    ];
    if (target.param !== "roi") {
      edit({ ...draft, nodes: draft.nodes.map((n) => (n.id === node.id ? { ...n, params: { ...n.params, [target.param]: box } } : n)) });
      return;
    }
    const spec = filters.find((f) => f.id === "geometry.crop");
    const name = filters.find((f) => f.id === node.filter)?.name ?? node.filter;
    const ids = new Set(draft.nodes.map((n) => n.id));
    let k = draft.nodes.length + 1;
    while (ids.has(`n${k}`)) k++;
    const cropNode = {
      id: `n${k}`,
      filter: "geometry.crop",
      version: spec?.version,
      params: { x, y, width: w, height: h },
      enabled: true,
      note: `Area marked for ${name}`,
    };
    let nodes;
    if (node.filter === "stack.stabilise") {
      // Stabilising needs the whole frame to move; it tracks the marked area, then the result is cropped to it.
      nodes = draft.nodes.map((n) => (n.id === node.id ? { ...n, params: { ...n.params, roi: box } } : n));
      nodes.splice(idx + 1, 0, cropNode);
    } else {
      // The filter then works on the marked area only: its region is the whole cropped image.
      const others = draft.nodes.map((n) => (n.id === node.id ? { ...n, params: { ...n.params, roi: [] } } : n));
      nodes = [...others];
      nodes.splice(cropPosition(filters, others, idx), 0, cropNode);
    }
    markedCrops.current[node.id] = cropNode.id;
    setFullImage(false);
    edit({ ...draft, nodes });
    setSelected(node.filter === "stack.stabilise" ? cropNode.id : node.id);
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
    const prev = (node.params[picking.param] as number[][]) ?? [];
    // A full set (e.g. four corners) starts again rather than growing past what the filter takes.
    const pts = [...(picking.count && prev.length >= picking.count ? [] : prev), [Math.round(x), Math.round(y)]];
    edit({ ...draft, nodes: draft.nodes.map((n) => (n.id === node.id ? { ...n, params: { ...n.params, [picking.param]: pts } } : n)) });
    if (picking.count && pts.length >= picking.count) setPicking(null);
  };

  const pickNode = picking && draft?.nodes.find((n) => n.id === picking.nodeId);
  const pickPoints = (pickNode?.params[picking!.param] as number[][] | undefined) ?? undefined;
  const pickSpec = pickNode ? filters.find((f) => f.id === pickNode.filter)?.params.find((p) => p.name === picking!.param) : undefined;
  const pickingBox = isBoxParam(pickSpec);

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
      // geometric checks are drawn as the clicked pairs (shadow to object, object to reflection, line points)
      if (k.startsWith("auth:") && pts.length) overlays.push({ points: pts, color: "#ffcf3f", mode: k.startsWith("auth:geometry.") ? "segments" : "rect", label: k.split(":")[2] });
    }
  } else if (showFull && cropBoxOnSource(filters, draft)) {
    overlays.push({ points: cropBoxOnSource(filters, draft)!, color: "#ffcf3f", mode: "rect", label: "crop" });
  } else if (tab === "enhance" && !picking && draft && selected && draft.nodes.some((x) => x.id === selected)) {
    // Boxes marked for the selected step (e.g. a light streak), drawn where they were marked.
    const n = draft.nodes.find((x) => x.id === selected)!;
    for (const p of filters.find((f) => f.id === n.filter)?.params ?? []) {
      const v = n.params[p.name] as number[][] | undefined;
      if (isBoxParam(p) && v?.length === 2) overlays.push({ points: v, color: "#ffcf3f", mode: "rect", label: p.label.replace(/\s*\(.*$/, "") });
    }
    if (mpoints.plan_roi?.length) overlays.push({ points: mpoints.plan_roi, color: "#3fe08a", mode: "rect", label: "planner region" });
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
        : showFull
          ? "Full image"
        : tab === "enhance" && selIndex < 0 && framing
          ? "Source · cropped"
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
    <div
      className={`workspace${toolsLeft ? " tools-left" : ""}${collapsed.left ? " no-left" : ""}${collapsed.right ? " no-right" : ""}`}
      style={{ "--left-w": `${widths.left}px`, "--right-w": `${widths.right}px` } as React.CSSProperties}
    >
      {!collapsed.left && (
        <div className="splitter split-left" onPointerDown={startResize("left")} onDoubleClick={() => setWidths((w) => ({ ...w, left: DEFAULT_WIDTHS.left }))} title="Drag to resize · double-click to reset" />
      )}
      {!collapsed.right && (
        <div className="splitter split-right" onPointerDown={startResize("right")} onDoubleClick={() => setWidths((w) => ({ ...w, right: DEFAULT_WIDTHS.right }))} title="Drag to resize · double-click to reset" />
      )}
      {collapsed.left ? (
        <button className="rail left-rail" onClick={() => setCollapsed((c) => ({ ...c, left: false }))} title="Show evidence">
          {toolsLeft ? "‹" : "›"}
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
            <button onClick={chooseEvidence} className="primary small-btn" title="Add images or video (Ctrl+O, or drop files here)">
              + Add
            </button>
            <button className="icon" onClick={() => setCollapsed((c) => ({ ...c, left: true }))} title="Hide panel">
              {toolsLeft ? "›" : "‹"}
            </button>
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
          </div>
          {(uploading || !evidence.length || dragOver) && (
            <div className="dropzone" onClick={chooseEvidence}>
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
              : ev && !crop && !showFull && framing && view.kind === "chain"
                ? viewerLabel === "Source · cropped" ? null : framingUrl
                : ev && !crop && !showFull && (view.kind === "map" || (viewerLabel !== "Source" && viewerLabel !== "Source · cropped"))
                  ? urls.frame(ev.id, sourceFrame)
                  : null
          }
          compareLabel={codecOn ? "Frame" : framing && view.kind === "chain" ? "Original (same area)" : undefined}
          label={codecOn ? `Macroblocks${codecInfo.info ? ` · ${codecInfo.info.type}-frame` : ""}` : viewerLabel}
          busy={view.kind === "chain" && !codecOn && preview.busy}
          busyLabel={view.kind === "chain" && tab === "enhance" && selName ? `Applying ${selName}` : undefined}
          error={codecOn ? codecInfo.error : view.kind === "chain" ? preview.error : null}
          describe={codecOn ? (x, y) => describeBlock(codecInfo.info, x, y) : undefined}
          points={picking && !pickingBox ? pickPoints : undefined}
          pointsClosed={picking?.param === "points"}
          pickHint={
            picking
              ? pickingBox
                ? "Drag a box around the area · Esc to cancel"
                : `Click to place points${picking.count ? ` (${pickPoints?.length ?? 0}/${picking.count})` : ""} · Esc to stop`
              : crop
                ? "Drag a box · Shift = square · Esc to cancel"
                : extHint
          }
          onPick={(picking && !pickingBox) || extPick ? onPick : undefined}
          onRect={crop && preview.url && !preview.busy ? applyCrop : pickingBox && preview.url && !preview.busy ? applyBox : undefined}
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
                {framing && view.kind === "chain" && !crop && (
                  <button className={showFull ? "active" : ""} onClick={() => setFullImage((f) => !f)} title="Show the whole image, then click again to go back to the cropped area (G)">
                    {showFull ? "↩ Back to crop" : "⤢ Full image"}
                  </button>
                )}
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
          {toolsLeft ? "›" : "‹"}
        </button>
      ) : (
      <aside className="right">
        <div className="tabs">
          {TABS.map((t) => (
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
          <button className="icon tab-swap" onClick={() => setToolsLeft((v) => !v)}
            title={toolsLeft ? "Move the tools panel to the right" : "Move the tools panel to the left"}>
            ⇄
          </button>
          <button className="icon tab-hide" onClick={() => setCollapsed((c) => ({ ...c, right: true }))} title="Hide panel">
            {toolsLeft ? "‹" : "›"}
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
                  onPlanned={async (r) => {
                    const d = await reload();
                    setAuditKey((k) => k + 1);
                    // Open the best proposal straight away, so its Accept button is in front of the examiner.
                    const best = r.candidates
                      .filter((c) => c.chain_id && c.score !== null)
                      .sort((a, b) => (b.score ?? -99) - (a.score ?? -99))[0];
                    const c = best && d.chains.find((x) => x.id === best.chain_id);
                    if (c) openChain(c);
                  }}
                  onUseChain={async (id) => {
                    try {
                      await api.acceptProposals(id);
                      const d = await reload();
                      const c = d.chains.find((x) => x.id === id);
                      if (c) openChain(c);
                      setAuditKey((k) => k + 1);
                      setStatus({ kind: "ok", text: "Planner chain accepted. Its steps are now yours to adjust, render and report." });
                    } catch (e) {
                      setStatus({ kind: "bad", text: (e as Error).message });
                    }
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
          {(tab === "video" || tab === "intel" || tab === "case") && (() => {
            const common = {
              caseId, ev: ev ?? null, evidence, frame,
              onEvidence: async (c: Evidence) => {
                const d = await reload();
                selectEvidence(c.id, d.chains);
                setAuditKey((k) => k + 1);
              },
              onChanged: () => setAuditKey((k) => k + 1),
            };
            if (tab === "video") return ev && ev.media_type !== "container" ? <VideoPanel key={ev.id} {...common} /> : null;
            if (tab === "intel") return ev && ev.media_type !== "container" ? <IntelPanel key={ev.id} {...common} /> : null;
            return (
              <>
                <CasePanel {...common} />
                <BatchPanel caseId={caseId} />
              </>
            );
          })()}
          {tab === "audit" && <AuditPanel caseId={caseId} refreshKey={auditKey} onChanged={() => setAuditKey((k) => k + 1)} />}
          {!ev && tab !== "audit" && tab !== "case" && <p className="muted">Add evidence to begin.</p>}
          {tab !== "enhance" && status && <p className={`status ${status.kind}`}>{status.text}</p>}
        </div>
      </aside>
      )}
    </div>
  );
}
