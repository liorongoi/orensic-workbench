import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, urls } from "../api";
import { command, isDesktop, keyOf, onDesktopReady, openPage, openWindow, toggleFullscreen, typing, type Command } from "../desktop";
import { HELP_DOCS } from "../help";
import type { SystemInfo } from "../types";

interface Item {
  label: string;
  keys?: string;
  run?: () => void;
  disabled?: boolean;
  separator?: boolean;
}

interface Props {
  caseId: string | null;
  signedIn: boolean;
}

function download(href: string) {
  const a = document.createElement("a");
  a.href = href;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Keyboard shortcuts that need a modifier or a function key, handled for the whole window.
 * Single-key shortcuts (F, 1, B, T, C, M, J, L, +, -) belong to the viewer and the workspace. */
const GLOBAL_KEYS: { keys: string; match: (e: KeyboardEvent, k: string) => boolean; cmd: Command | "fullscreen" | "shortcuts" }[] = [
  { keys: "Ctrl+O", match: (e, k) => (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && k === "o", cmd: "add-evidence" },
  { keys: "Alt+1", match: (e, k) => e.altKey && !e.ctrlKey && k === "1", cmd: "toggle-left" },
  { keys: "Alt+2", match: (e, k) => e.altKey && !e.ctrlKey && k === "2", cmd: "toggle-right" },
  { keys: "Alt+3", match: (e, k) => e.altKey && !e.ctrlKey && k === "3", cmd: "focus-viewer" },
  { keys: "Ctrl+Shift+N", match: (e, k) => (e.ctrlKey || e.metaKey) && e.shiftKey && k === "n", cmd: "viewer-window" },
  { keys: "F11", match: (e) => e.key === "F11", cmd: "fullscreen" },
  { keys: "F1", match: (e) => e.key === "F1", cmd: "shortcuts" },
];

export const SHORTCUTS: [string, string][] = [
  ["Ctrl+O", "Add evidence (native Open dialog in the desktop app)"],
  ["Ctrl+Z / Ctrl+Y", "Undo / redo in the chain"],
  ["C", "Crop: drag a box on the image (Shift: square)"],
  ["G", "After a crop: show the full image, press again to go back to the cropped area"],
  ["F", "Fit the image to the viewer"],
  ["1", "Actual pixels: one image pixel per screen pixel"],
  ["+ / -", "Zoom in / out (or scroll)"],
  ["Hold B", "Show the original while held"],
  ["T", "Flip between the original and the result"],
  ["M", "Macroblocks, QP and motion vectors (video)"],
  ["J / L (Shift: 10)", "Previous / next frame (video)"],
  ["Alt+1 / Alt+2", "Show or hide the evidence / tools panel"],
  ["Alt+3", "Viewer only (hide both panels)"],
  ["Ctrl+Shift+N", "Viewer in a new window, for a second monitor"],
  ["F11", "Full screen"],
  ["Esc", "Cancel picking points, cropping or a dialog"],
  ["F1", "This list"],
];

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          {title}
          <span className="spacer" />
          <button className="icon" onClick={onClose} title="Close (Esc)">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function SystemDialog({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<SystemInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.system().then(setInfo).catch((e) => setError((e as Error).message));
  }, []);
  const gb = (mb: number) => `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB`;
  return (
    <Dialog title="This computer" onClose={onClose}>
      {error && <p className="error">{error}</p>}
      {!info && !error && <p className="muted">Reading…</p>}
      {info && (
        <table className="kv">
          <tbody>
            <tr><th>Graphics card</th><td>{info.gpu.name ? `${info.gpu.name}, ${gb(info.gpu.memory_mb)}` : "not identified"}</td></tr>
            <tr><th>Learned models run on</th><td>{info.models_on}</td></tr>
            <tr><th>Model jobs at once</th><td>{info.gpu.jobs}</td></tr>
            <tr><th>CPU threads</th><td>{info.cpu_threads} (classical filters, bit-exact on every machine)</td></tr>
            <tr><th>Memory</th><td>{gb(info.ram_mb)}</td></tr>
            <tr><th>Result cache</th><td>{gb(info.result_cache_mb)}</td></tr>
            <tr><th>Video frame cache</th><td>{gb(info.frame_cache_mb)}</td></tr>
            <tr><th>Largest frame range</th><td>{gb(info.range_mb)} of decoded frames</td></tr>
            <tr><th>Window</th><td>{isDesktop() ? "Lucida desktop app" : "web browser"}</td></tr>
          </tbody>
        </table>
      )}
      <p className="muted small">Settings: workbench.ini in the installation folder (threads, cache_mb, frame_cache_mb, ai_device, gpu_jobs).</p>
    </Dialog>
  );
}

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <Dialog title="Keyboard shortcuts" onClose={onClose}>
      <table className="kv">
        <tbody>
          {SHORTCUTS.map(([k, what]) => (
            <tr key={k}><th><kbd>{k}</kbd></th><td>{what}</td></tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">Letter shortcuts follow the physical key, so they work on any keyboard layout.</p>
    </Dialog>
  );
}

/** A desktop-style menu bar: File, Edit, View, Tools, Help. Click a title to open it; while one is
 * open, moving to another title opens that one; Esc or a click elsewhere closes it. */
export function MenuBar({ caseId, signedIn }: Props) {
  const [open, setOpen] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"system" | "shortcuts" | null>(null);
  const [, setDesktop] = useState(isDesktop());
  const bar = useRef<HTMLDivElement>(null);
  const inCase = signedIn && !!caseId;

  useEffect(() => onDesktopReady(() => setDesktop(true)), []);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!bar.current?.contains(e.target as Node)) setOpen(null);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(null);
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", esc);
    };
  }, [open]);

  const run = (c: Command | "fullscreen" | "shortcuts") => {
    if (c === "fullscreen") toggleFullscreen();
    else if (c === "shortcuts") setDialog("shortcuts");
    else command(c);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = keyOf(e);
      const hit = GLOBAL_KEYS.find((g) => g.match(e, k));
      if (!hit) return;
      if (hit.cmd !== "fullscreen" && hit.cmd !== "shortcuts" && !inCase) return;
      if (typing(e) && !(e.ctrlKey || e.metaKey || e.altKey || e.key.startsWith("F"))) return;
      e.preventDefault();
      run(hit.cmd);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const menus: Record<string, Item[]> = {
    File: [
      { label: "Cases…", run: () => (location.hash = "#/"), disabled: !signedIn },
      { label: "Add evidence…", keys: "Ctrl+O", run: () => command("add-evidence"), disabled: !inCase },
      { separator: true, label: "" },
      { label: "Open HTML report", run: () => caseId && openPage(urls.report(caseId, "html"), "Report"), disabled: !inCase },
      { label: "Open PDF report", run: () => caseId && openPage(urls.report(caseId, "pdf"), "Report (PDF)"), disabled: !inCase },
      { label: "Save JSON bundle…", run: () => caseId && download(urls.report(caseId, "json")), disabled: !inCase },
    ],
    Edit: [
      { label: "Undo", keys: "Ctrl+Z", run: () => command("undo"), disabled: !inCase },
      { label: "Redo", keys: "Ctrl+Y", run: () => command("redo"), disabled: !inCase },
      { separator: true, label: "" },
      { label: "Crop", keys: "C", run: () => command("crop"), disabled: !inCase },
      { label: "Full image / back to crop", keys: "G", run: () => command("full-image"), disabled: !inCase },
    ],
    View: [
      { label: "Fit", keys: "F", run: () => command("fit"), disabled: !inCase },
      { label: "Actual pixels", keys: "1", run: () => command("one-to-one"), disabled: !inCase },
      { label: "Zoom in", keys: "+", run: () => command("zoom-in"), disabled: !inCase },
      { label: "Zoom out", keys: "-", run: () => command("zoom-out"), disabled: !inCase },
      { separator: true, label: "" },
      { label: "Compare: swipe", run: () => command("compare-swipe"), disabled: !inCase },
      { label: "Compare: side by side", run: () => command("compare-side"), disabled: !inCase },
      { label: "Compare: flip", keys: "T", run: () => command("compare-flip"), disabled: !inCase },
      { separator: true, label: "" },
      { label: "Evidence panel", keys: "Alt+1", run: () => command("toggle-left"), disabled: !inCase },
      { label: "Tools panel", keys: "Alt+2", run: () => command("toggle-right"), disabled: !inCase },
      { label: "Viewer only", keys: "Alt+3", run: () => command("focus-viewer"), disabled: !inCase },
      { label: "Viewer in new window", keys: "Ctrl+Shift+N", run: () => command("viewer-window"), disabled: !inCase },
      { label: "Full screen", keys: "F11", run: toggleFullscreen },
    ],
    Tools: [
      { label: "Enhance", run: () => command("tab-enhance"), disabled: !inCase },
      { label: "Authenticate", run: () => command("tab-authenticate"), disabled: !inCase },
      { label: "Measure", run: () => command("tab-measure"), disabled: !inCase },
      { label: "Audit & review", run: () => command("tab-audit"), disabled: !inCase },
      { separator: true, label: "" },
      { label: "Triage all evidence", run: () => command("triage"), disabled: !inCase },
      { label: "This computer…", run: () => setDialog("system"), disabled: !signedIn },
    ],
    Help: [
      ...HELP_DOCS.map((d) => ({ label: d.title, run: () => openWindow(`#/help/${d.id}`, d.title) })),
      { separator: true, label: "" },
      { label: "Keyboard shortcuts", keys: "F1", run: () => setDialog("shortcuts") },
    ],
  };

  return (
    <>
      <div className="menubar" ref={bar} role="menubar">
        {Object.entries(menus).map(([title, items]) => (
          <div key={title} className={`mb-menu${open === title ? " open" : ""}`}>
            <button
              className="mb-title"
              role="menuitem"
              aria-haspopup="true"
              aria-expanded={open === title}
              onMouseDown={(e) => {
                e.preventDefault();
                setOpen((o) => (o === title ? null : title));
              }}
              onMouseEnter={() => open && open !== title && setOpen(title)}
            >
              {title}
            </button>
            {open === title && (
              <div className="mb-items" role="menu">
                {items.map((it, i) =>
                  it.separator ? (
                    <div key={i} className="mb-sep" />
                  ) : (
                    <button
                      key={i}
                      role="menuitem"
                      className="mb-item"
                      disabled={it.disabled}
                      onClick={() => {
                        setOpen(null);
                        it.run?.();
                      }}
                    >
                      <span>{it.label}</span>
                      {it.keys && <kbd>{it.keys}</kbd>}
                    </button>
                  ),
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      {dialog === "system" && <SystemDialog onClose={() => setDialog(null)} />}
      {dialog === "shortcuts" && <ShortcutsDialog onClose={() => setDialog(null)} />}
    </>
  );
}
