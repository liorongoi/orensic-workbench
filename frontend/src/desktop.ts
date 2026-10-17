/** The desktop app's native side (python -m forensic.desktop), with browser fallbacks, and the
 * command bus the menu bar and keyboard shortcuts use to reach the workspace and the viewer. */

export interface OfferedFile {
  token: string;
  name: string;
  size: number;
}

interface Bridge {
  open_evidence(): Promise<OfferedFile[]>;
  open_window(route: string, title: string): Promise<boolean>;
  open_page(path: string, title: string): Promise<boolean>;
  toggle_fullscreen(): Promise<boolean>;
}

declare global {
  interface Window {
    pywebview?: { api?: Bridge };
  }
}

export function bridge(): Bridge | null {
  const api = window.pywebview?.api;
  return api && typeof api.open_evidence === "function" ? api : null;
}

export const isDesktop = () => bridge() !== null;

/** pywebview injects its API shortly after the page loads; call `cb` once it is there. */
export function onDesktopReady(cb: () => void): () => void {
  if (isDesktop()) {
    cb();
    return () => {};
  }
  window.addEventListener("pywebviewready", cb);
  return () => window.removeEventListener("pywebviewready", cb);
}

/** Another Lucida window at `route` (the part from '#'), for example the viewer on a second monitor. */
export function openWindow(route: string, title = "Lucida") {
  const b = bridge();
  if (b) b.open_window(route, title);
  else window.open(location.pathname + route, "_blank", "noopener");
}

/** A page served by Lucida (an HTML or PDF report) in its own window. */
export function openPage(path: string, title = "Lucida") {
  const b = bridge();
  if (b) b.open_page(path, title);
  else window.open(path, "_blank", "noopener");
}

export function toggleFullscreen() {
  const b = bridge();
  if (b) {
    b.toggle_fullscreen();
    return;
  }
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.();
}

/** In the desktop window, links that would open a browser tab open a Lucida window instead
 * (signed in like this one); links to other sites are not followed. */
export function installLinkHandler() {
  document.addEventListener("click", (e) => {
    if (!isDesktop() || e.defaultPrevented) return;
    const a = (e.target as HTMLElement | null)?.closest?.("a[target=_blank]") as HTMLAnchorElement | null;
    if (!a || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    e.preventDefault();
    if (url.origin !== location.origin) return;
    const title = a.textContent?.trim() || "Lucida";
    if (url.pathname === location.pathname && url.hash) openWindow(url.hash, title);
    else openPage(url.pathname + url.search, title);
  });
}

// Commands -------------------------------------------------------------------------------------

export type Command =
  | "add-evidence"
  | "undo"
  | "redo"
  | "crop"
  | "triage"
  | "fit"
  | "one-to-one"
  | "zoom-in"
  | "zoom-out"
  | "compare-swipe"
  | "compare-side"
  | "compare-flip"
  | "toggle-left"
  | "toggle-right"
  | "focus-viewer"
  | "viewer-window"
  | "tab-enhance"
  | "tab-authenticate"
  | "tab-measure"
  | "tab-audit"
  | "frame-prev"
  | "frame-next";

const COMMAND = "lucida-command";

export function command(c: Command) {
  window.dispatchEvent(new CustomEvent<Command>(COMMAND, { detail: c }));
}

export function onCommand(handler: (c: Command) => void): () => void {
  const fn = (e: Event) => handler((e as CustomEvent<Command>).detail);
  window.addEventListener(COMMAND, fn);
  return () => window.removeEventListener(COMMAND, fn);
}

/** The key pressed, independent of the keyboard layout: Ctrl+Z is "z" on a Hebrew or Russian
 * layout too. Letters and digits come from the physical key; everything else from `key`. */
export function keyOf(e: KeyboardEvent): string {
  if (/^Key[A-Z]$/.test(e.code)) return e.code.slice(3).toLowerCase();
  if (/^Digit\d$/.test(e.code)) return e.code.slice(5);
  if (/^Numpad\d$/.test(e.code)) return e.code.slice(6);
  if (e.code === "NumpadAdd") return "+";
  if (e.code === "NumpadSubtract") return "-";
  return e.key.length === 1 ? e.key.toLowerCase() : e.key;
}

/** True when the key press is typing into a field, so single-key shortcuts must not fire. */
export function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return !!t?.closest?.("input, textarea, select, [contenteditable]");
}
