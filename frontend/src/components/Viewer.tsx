import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

export interface Overlay {
  points: number[][];
  mode: "segments" | "points" | "rect";
  color: string;
  label?: string;
}

interface Props {
  src: string | null;
  overlays?: Overlay[];
  compareSrc?: string | null;
  compareLabel?: string;
  label?: string;
  busy?: boolean;
  error?: string | null;
  points?: number[][];
  pickHint?: string | null;
  onPick?: (x: number, y: number) => void;
  /** When set, dragging on the image draws a rectangle (Shift = square) instead of panning. */
  onRect?: (rect: [number, number, number, number]) => void;
  illustrative?: boolean;
  /** Extra toolbar buttons, shown left of Fit / 1:1. */
  tools?: ReactNode;
  /** Extra readout for the pixel under the cursor (e.g. its macroblock), shown in the status bar. */
  describe?: (x: number, y: number) => string | null;
}

type CompareMode = "off" | "swipe" | "side" | "flip";

interface Loaded {
  img: HTMLImageElement;
  data: ImageData | null;
}

function load(src: string): Promise<Loaded> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      let data: ImageData | null = null;
      try {
        const c = document.createElement("canvas");
        c.width = img.naturalWidth;
        c.height = img.naturalHeight;
        const cx = c.getContext("2d", { willReadFrequently: true })!;
        cx.drawImage(img, 0, 0);
        data = cx.getImageData(0, 0, c.width, c.height);
      } catch {
        data = null;
      }
      resolve({ img, data });
    };
    img.onerror = () => reject(new Error("image failed to load"));
    img.src = src;
  });
}

/** Where the original sits in the enhanced image's pixel space, as [x, y, w, h]. Same aspect ratio (e.g. after
 * upscaling): stretched over it exactly. Otherwise (e.g. after a crop): scaled to the same height and centred. */
function placeOriginal(m: HTMLImageElement, c: HTMLImageElement): [number, number, number, number] {
  const w = m.naturalWidth;
  const h = m.naturalHeight;
  const cw = c.naturalWidth;
  const ch = c.naturalHeight;
  if (Math.abs(w / h - cw / ch) <= 0.02 * (w / h)) return [0, 0, w, h];
  const s = h / ch;
  return [(w - cw * s) / 2, 0, cw * s, h];
}

/** Pixel-exact viewer: nearest-neighbour above 100 %, wheel zoom around the cursor, drag to pan.
 * Compare modes put the original next to the result: swipe, side by side (linked zoom and pan) or flip. */
export function Viewer({ src, overlays, compareSrc, compareLabel = "Original", label, busy, error, points, pickHint, onPick, onRect, illustrative, tools, describe }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [main, setMain] = useState<Loaded | null>(null);
  const [cmp, setCmp] = useState<Loaded | null>(null);
  const [holdB, setHoldB] = useState(false);
  const [mode, setMode] = useState<CompareMode>("off");
  const [flipped, setFlipped] = useState(false);
  const [split, setSplit] = useState(0.5);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [cursor, setCursor] = useState<{ x: number; y: number; rgb: number[] | null; original?: boolean } | null>(null);
  const drag = useRef<{ sx: number; sy: number; vx: number; vy: number; moved: boolean } | null>(null);
  const [box, setBox] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const lastSize = useRef("");

  useEffect(() => {
    let alive = true;
    if (!src) {
      setMain(null);
      return;
    }
    load(src).then((l) => alive && setMain(l)).catch(() => alive && setMain(null));
    return () => {
      alive = false;
    };
  }, [src]);

  useEffect(() => {
    let alive = true;
    if (!compareSrc) {
      setCmp(null);
      return;
    }
    load(compareSrc).then((l) => alive && setCmp(l)).catch(() => alive && setCmp(null));
    return () => {
      alive = false;
    };
  }, [compareSrc]);

  // The result image sets the geometry; the original is mapped onto it so both line up.
  const shown = main;
  const canCompare = !!main && !!cmp;
  const active: CompareMode = canCompare ? mode : "off";
  const side = active === "side";
  const showOriginal = canCompare && (holdB || (active === "flip" && flipped));
  const orig = canCompare ? placeOriginal(main!.img, cmp!.img) : null;

  const paneWidth = () => (wrap.current ? wrap.current.clientWidth / (side ? 2 : 1) : 0);

  const fit = useCallback(() => {
    const el = wrap.current;
    if (!el || !shown) return;
    const pw = el.clientWidth / (side ? 2 : 1);
    const w = shown.img.naturalWidth;
    const h = shown.img.naturalHeight;
    const z = Math.min(pw / w, el.clientHeight / h, 8) * 0.96;
    setView({ zoom: z, x: (pw - w * z) / 2, y: (el.clientHeight - h * z) / 2 });
  }, [shown, side]);

  const oneToOne = () => {
    const el = wrap.current;
    if (!el || !shown) return;
    setView({ zoom: 1, x: (paneWidth() - shown.img.naturalWidth) / 2, y: (el.clientHeight - shown.img.naturalHeight) / 2 });
  };

  const pickMode = (m: CompareMode) => {
    setMode((cur) => (cur === m ? "off" : m));
    setFlipped(m === "flip");
  };

  // Entering or leaving side by side halves or doubles the pane, so re-fit.
  const lastSide = useRef(side);
  useEffect(() => {
    if (lastSide.current !== side) {
      lastSide.current = side;
      fit();
    }
  }, [side, fit]);

  // Re-fit only when the image size changes, so editing parameters keeps the current zoom.
  useEffect(() => {
    if (!main) return;
    const key = `${main.img.naturalWidth}x${main.img.naturalHeight}`;
    if (key !== lastSize.current) {
      lastSize.current = key;
      fit();
    }
  }, [main, fit]);

  useEffect(() => {
    const c = canvas.current;
    const el = wrap.current;
    if (!c || !el) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = el.clientWidth * dpr;
    c.height = el.clientHeight * dpr;
    const cx = c.getContext("2d")!;
    cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = el.clientWidth;
    const H = el.clientHeight;
    cx.clearRect(0, 0, W, H);
    if (!shown) return;
    cx.imageSmoothingEnabled = view.zoom < 1;
    cx.imageSmoothingQuality = "high";
    const paint = (original: boolean) => {
      const [ox, oy, ow, oh] = original && orig ? orig : [0, 0, shown.img.naturalWidth, shown.img.naturalHeight];
      cx.drawImage(original && cmp ? cmp.img : shown.img, view.x + ox * view.zoom, view.y + oy * view.zoom, ow * view.zoom, oh * view.zoom);
    };
    const clip = (x: number, w: number) => {
      cx.beginPath();
      cx.rect(x, 0, w, H);
      cx.clip();
    };
    const grid = () => {
      if (view.zoom >= 16) {
        cx.strokeStyle = "rgba(128,128,128,0.35)";
        cx.lineWidth = 1;
        const w = shown.img.naturalWidth;
        const h = shown.img.naturalHeight;
        cx.beginPath();
        for (let i = Math.max(0, Math.floor(-view.x / view.zoom)); i <= Math.min(w, (PW - view.x) / view.zoom); i++) {
          const px = Math.round(view.x + i * view.zoom) + 0.5;
          cx.moveTo(px, Math.max(view.y, 0));
          cx.lineTo(px, Math.min(view.y + h * view.zoom, H));
        }
        for (let j = Math.max(0, Math.floor(-view.y / view.zoom)); j <= Math.min(h, (H - view.y) / view.zoom); j++) {
          const py = Math.round(view.y + j * view.zoom) + 0.5;
          cx.moveTo(Math.max(view.x, 0), py);
          cx.lineTo(Math.min(view.x + w * view.zoom, PW), py);
        }
        cx.stroke();
      }
    };
    const marks = () => {
      if (points && points.length) {
        cx.strokeStyle = "#ffcf3f";
        cx.fillStyle = "#ffcf3f";
        cx.lineWidth = 1.5;
        cx.beginPath();
        points.forEach(([x, y], i) => {
          const sx = view.x + (x + 0.5) * view.zoom;
          const sy = view.y + (y + 0.5) * view.zoom;
          if (i === 0) cx.moveTo(sx, sy);
          else cx.lineTo(sx, sy);
        });
        if (points.length > 2) cx.closePath();
        cx.stroke();
        points.forEach(([x, y], i) => {
          const sx = view.x + (x + 0.5) * view.zoom;
          const sy = view.y + (y + 0.5) * view.zoom;
          cx.beginPath();
          cx.arc(sx, sy, 4, 0, Math.PI * 2);
          cx.fill();
          cx.fillText(String(i + 1), sx + 6, sy - 6);
        });
      }
      if (overlays) {
        const at = ([x, y]: number[]) => [view.x + (x + 0.5) * view.zoom, view.y + (y + 0.5) * view.zoom];
        cx.lineWidth = 2;
        cx.font = "12px system-ui, sans-serif";
        for (const o of overlays) {
          if (!o.points.length) continue;
          cx.strokeStyle = o.color;
          cx.fillStyle = o.color;
          if (o.mode === "segments") {
            for (let i = 0; i + 1 < o.points.length; i += 2) {
              const [ax, ay] = at(o.points[i]);
              const [bx, by] = at(o.points[i + 1]);
              cx.beginPath();
              cx.moveTo(ax, ay);
              cx.lineTo(bx, by);
              cx.stroke();
            }
          } else if (o.mode === "rect") {
            for (let i = 0; i + 1 < o.points.length; i += 2) {
              const [ax, ay] = at(o.points[i]);
              const [bx, by] = at(o.points[i + 1]);
              cx.strokeRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
            }
          }
          o.points.forEach((p, i) => {
            const [sx, sy] = at(p);
            cx.beginPath();
            cx.arc(sx, sy, 3.5, 0, Math.PI * 2);
            cx.fill();
            if (o.label && (o.mode === "points" || i === 0)) cx.fillText(o.label, sx + 6, sy - 6);
          });
        }
      }
      if (box) {
        const r = boxRect(box, shown.img.naturalWidth, shown.img.naturalHeight);
        if (r) {
          const [bx, by, bw, bh] = r;
          cx.fillStyle = "rgba(0,0,0,0.45)";
          const X = view.x + bx * view.zoom;
          const Y = view.y + by * view.zoom;
          const W = bw * view.zoom;
          const H = bh * view.zoom;
          const iw = shown.img.naturalWidth * view.zoom;
          const ih = shown.img.naturalHeight * view.zoom;
          cx.fillRect(view.x, view.y, iw, Y - view.y);
          cx.fillRect(view.x, Y + H, iw, view.y + ih - Y - H);
          cx.fillRect(view.x, Y, X - view.x, H);
          cx.fillRect(X + W, Y, view.x + iw - X - W, H);
          cx.setLineDash([5, 4]);
          cx.strokeStyle = "#ffcf3f";
          cx.lineWidth = 1.5;
          cx.strokeRect(X + 0.5, Y + 0.5, W, H);
          cx.setLineDash([]);
          cx.fillStyle = "#ffcf3f";
          cx.font = "12px system-ui, sans-serif";
          cx.fillText(`${bw}×${bh}`, X + 4, Y - 6 > 10 ? Y - 6 : Y + 14);
        }
      }
    };
    let PW = W;
    if (side) {
      PW = W / 2;
      cx.save();
      clip(0, PW);
      paint(true);
      grid();
      cx.restore();
      cx.save();
      clip(PW, PW);
      cx.translate(PW, 0);
      paint(showOriginal);
      grid();
      if (!showOriginal) marks();
      cx.restore();
      cx.fillStyle = "#0f1216";
      cx.fillRect(PW - 1, 0, 2, H);
    } else if (active === "swipe" && !showOriginal) {
      const sx = W * split;
      paint(false);
      cx.save();
      clip(0, sx);
      paint(true);
      cx.restore();
      grid();
      cx.save();
      clip(sx, W - sx);
      marks();
      cx.restore();
    } else {
      paint(showOriginal);
      grid();
      if (!showOriginal) marks();
    }
  }, [shown, cmp, orig?.join(), side, active, split, showOriginal, view, points, overlays, box]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onResize = () => setView((v) => ({ ...v }));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement) return;
      if (e.key === "b" || e.key === "B") setHoldB(true);
      if ((e.key === "t" || e.key === "T") && !e.ctrlKey && !e.metaKey && canCompare) {
        if (mode === "flip") setFlipped((f) => !f);
        else {
          setMode("flip");
          setFlipped(true);
        }
      }
      if (e.key === "f" || e.key === "F") fit();
      if (e.key === "1") oneToOne();
    };
    const up = (e: KeyboardEvent) => {
      if (e.key === "b" || e.key === "B") setHoldB(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  });

  /** Pointer position in pane and image coordinates; `original` is true over a part showing the original. */
  const toImage = (e: React.MouseEvent | React.WheelEvent) => {
    const r = canvas.current!.getBoundingClientRect();
    let sx = e.clientX - r.left;
    const sy = e.clientY - r.top;
    let original = showOriginal;
    if (side) {
      const pw = r.width / 2;
      if (sx >= pw) sx -= pw;
      else original = true;
    } else if (active === "swipe" && sx < r.width * split) original = true;
    return { sx, sy, x: (sx - view.x) / view.zoom, y: (sy - view.y) / view.zoom, original };
  };

  const onWheel = (e: React.WheelEvent) => {
    if (!shown) return;
    const { sx, sy } = toImage(e);
    const factor = Math.exp(-e.deltaY * 0.0015);
    setView((v) => {
      const z = Math.min(64, Math.max(0.02, v.zoom * factor));
      return { zoom: z, x: sx - ((sx - v.x) / v.zoom) * z, y: sy - ((sy - v.y) / v.zoom) * z };
    });
  };

  const onMove = (e: React.MouseEvent) => {
    if (box && shown) {
      const { x, y } = toImage(e);
      let x1 = x;
      let y1 = y;
      if (e.shiftKey) {
        const side = Math.max(Math.abs(x - box.x0), Math.abs(y - box.y0));
        x1 = box.x0 + Math.sign(x - box.x0 || 1) * side;
        y1 = box.y0 + Math.sign(y - box.y0 || 1) * side;
      }
      setBox({ ...box, x1, y1 });
    } else if (drag.current) {
      const dx = e.clientX - drag.current.sx;
      const dy = e.clientY - drag.current.sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.current.moved = true;
      setView((v) => ({ ...v, x: drag.current!.vx + dx, y: drag.current!.vy + dy }));
    }
    if (!shown) return;
    const { x, y, original } = toImage(e);
    // Over the original, read its own pixel under the pointer.
    const src = original && cmp && orig ? cmp : shown;
    const px = src === cmp && orig ? ((x - orig[0]) * cmp.img.naturalWidth) / orig[2] : x;
    const py = src === cmp && orig ? ((y - orig[1]) * cmp.img.naturalHeight) / orig[3] : y;
    const ix = Math.floor(px);
    const iy = Math.floor(py);
    if (ix < 0 || iy < 0 || ix >= src.img.naturalWidth || iy >= src.img.naturalHeight) {
      setCursor(null);
      return;
    }
    let rgb: number[] | null = null;
    if (src.data) {
      const o = (iy * src.data.width + ix) * 4;
      rgb = [src.data.data[o], src.data.data[o + 1], src.data.data[o + 2]];
    }
    setCursor({ x: ix, y: iy, rgb, original: src === cmp });
  };

  const onDown = (e: React.MouseEvent) => {
    if (onRect && shown && e.button === 0 && !toImage(e).original) {
      const { x, y } = toImage(e);
      setBox({ x0: x, y0: y, x1: x, y1: y });
      return;
    }
    drag.current = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
  };

  const onUp = (e: React.MouseEvent) => {
    if (box) {
      const r = shown ? boxRect(box, shown.img.naturalWidth, shown.img.naturalHeight) : null;
      setBox(null);
      if (r && r[2] >= 2 && r[3] >= 2) onRect?.(r);
      return;
    }
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && onPick && shown) {
      const { x, y, original } = toImage(e);
      if (!original && x >= 0 && y >= 0 && x < shown.img.naturalWidth && y < shown.img.naturalHeight) {
        onPick(Math.round(x * 10) / 10, Math.round(y * 10) / 10);
      }
    }
  };

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <span className="viewer-label">{showOriginal && !side ? compareLabel : label}</span>
        {shown && (
          <span className="muted">
            {showOriginal && !side && cmp ? `${cmp.img.naturalWidth}×${cmp.img.naturalHeight}` : `${shown.img.naturalWidth}×${shown.img.naturalHeight}`} ·{" "}
            {Math.round(view.zoom * 100)}%
          </span>
        )}
        <span className="spacer" />
        {tools}
        <button onClick={fit} title="Fit (F)">Fit</button>
        <button onClick={oneToOne} title="Actual pixels (1)">1:1</button>
        {compareSrc && (
          <span className="seg" title={`Compare with the ${compareLabel.toLowerCase()} · hold B to peek`}>
            <button className={active === "swipe" ? "active" : ""} disabled={!canCompare} onClick={() => pickMode("swipe")} title="Swipe: drag the divider">
              Swipe
            </button>
            <button className={side ? "active" : ""} disabled={!canCompare} onClick={() => pickMode("side")} title="Side by side: zoom and pan together">
              Side by side
            </button>
            <button className={active === "flip" ? "active" : ""} disabled={!canCompare} onClick={() => pickMode("flip")} title="Flip between the two (T)">
              Flip
            </button>
          </span>
        )}
      </div>
      <div
        ref={wrap}
        className={`viewer-canvas${onPick || onRect ? " picking" : ""}`}
        onWheel={onWheel}
        onMouseMove={onMove}
        onMouseDown={onDown}
        onMouseUp={onUp}
        onMouseLeave={() => {
          drag.current = null;
          setBox(null);
          setCursor(null);
        }}
      >
        <canvas ref={canvas} />
        {active === "swipe" && !showOriginal && (
          <>
            <div
              className="compare-split"
              style={{ left: `${split * 100}%` }}
              onMouseDown={(e) => e.stopPropagation()}
              onPointerDown={(e) => {
                e.stopPropagation();
                e.currentTarget.setPointerCapture(e.pointerId);
              }}
              onPointerMove={(e) => {
                if (!e.currentTarget.hasPointerCapture(e.pointerId) || !wrap.current) return;
                const r = wrap.current.getBoundingClientRect();
                setSplit(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)));
              }}
              title="Drag to compare"
            >
              <span className="compare-knob">⇔</span>
            </div>
            <span className="compare-tag" style={{ left: 8 }}>{compareLabel}</span>
            <span className="compare-tag" style={{ right: 8 }}>{label}</span>
          </>
        )}
        {side && (
          <>
            <span className="compare-tag" style={{ left: 8 }}>{compareLabel}</span>
            <span className="compare-tag" style={{ left: "calc(50% + 8px)" }}>{showOriginal ? compareLabel : label}</span>
          </>
        )}
        {active === "flip" && (
          <button className="compare-flip" onClick={() => setFlipped((f) => !f)} onMouseDown={(e) => e.stopPropagation()} title="Flip (T)">
            <b className={flipped ? "" : "dim"}>{compareLabel}</b> ⇄ <b className={flipped ? "dim" : ""}>{label}</b>
          </button>
        )}
        {illustrative && !showOriginal && src && (
          <div className="illustrative-overlay">ILLUSTRATIVE ONLY · AI-generated detail · not for measurement</div>
        )}
        {!src && !busy && !error && <div className="viewer-empty">No image</div>}
        {busy && (src ? <div className="viewer-updating">Updating…</div> : <div className="viewer-busy">Processing…</div>)}
        {error && <div className="viewer-error">{error}</div>}
        {pickHint && <div className="viewer-hint">{pickHint}</div>}
      </div>
      <div className="viewer-status">
        {cursor ? (
          <>
            {cursor.original && `${compareLabel} · `}x {cursor.x}, y {cursor.y}
            {cursor.rgb && (
              <>
                {" "}· R {cursor.rgb[0]} G {cursor.rgb[1]} B {cursor.rgb[2]}
                <span className="swatch" style={{ background: `rgb(${cursor.rgb.join(",")})` }} />
              </>
            )}
            {describe && !cursor.original && (() => {
              const d = describe(cursor.x, cursor.y);
              return d ? ` · ${d}` : null;
            })()}
          </>
        ) : (
          <span className="muted" title="Scroll: zoom · Drag: pan · F: fit · 1: actual pixels · Hold B: original · T: flip · M: macroblocks (video) · Ctrl+Z / Ctrl+Y: undo / redo">
            ? shortcuts
          </span>
        )}
      </div>
    </div>
  );
}

/** Integer pixel rectangle [x, y, w, h] from a drag, clamped to the image. */
function boxRect(b: { x0: number; y0: number; x1: number; y1: number }, w: number, h: number): [number, number, number, number] | null {
  const x0 = Math.max(0, Math.min(w, Math.round(Math.min(b.x0, b.x1))));
  const y0 = Math.max(0, Math.min(h, Math.round(Math.min(b.y0, b.y1))));
  const x1 = Math.max(0, Math.min(w, Math.round(Math.max(b.x0, b.x1))));
  const y1 = Math.max(0, Math.min(h, Math.round(Math.max(b.y0, b.y1))));
  if (x1 <= x0 || y1 <= y0) return null;
  return [x0, y0, x1 - x0, y1 - y0];
}
