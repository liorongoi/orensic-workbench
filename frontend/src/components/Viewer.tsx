import { useCallback, useEffect, useRef, useState } from "react";

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
  illustrative?: boolean;
}

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

/** Pixel-exact viewer: nearest-neighbour above 100 %, wheel zoom around the cursor, drag to pan. */
export function Viewer({ src, overlays, compareSrc, compareLabel = "Original", label, busy, error, points, pickHint, onPick, illustrative }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [main, setMain] = useState<Loaded | null>(null);
  const [cmp, setCmp] = useState<Loaded | null>(null);
  const [showCompare, setShowCompare] = useState(false);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [cursor, setCursor] = useState<{ x: number; y: number; rgb: number[] | null } | null>(null);
  const drag = useRef<{ sx: number; sy: number; vx: number; vy: number; moved: boolean } | null>(null);
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

  const shown = showCompare && cmp ? cmp : main;

  const fit = useCallback(() => {
    const el = wrap.current;
    if (!el || !shown) return;
    const w = shown.img.naturalWidth;
    const h = shown.img.naturalHeight;
    const z = Math.min(el.clientWidth / w, el.clientHeight / h, 8) * 0.96;
    setView({ zoom: z, x: (el.clientWidth - w * z) / 2, y: (el.clientHeight - h * z) / 2 });
  }, [shown]);

  const oneToOne = () => {
    const el = wrap.current;
    if (!el || !shown) return;
    setView({ zoom: 1, x: (el.clientWidth - shown.img.naturalWidth) / 2, y: (el.clientHeight - shown.img.naturalHeight) / 2 });
  };

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
    cx.clearRect(0, 0, el.clientWidth, el.clientHeight);
    if (!shown) return;
    cx.imageSmoothingEnabled = view.zoom < 1;
    cx.imageSmoothingQuality = "high";
    cx.drawImage(shown.img, view.x, view.y, shown.img.naturalWidth * view.zoom, shown.img.naturalHeight * view.zoom);
    if (view.zoom >= 16) {
      cx.strokeStyle = "rgba(128,128,128,0.35)";
      cx.lineWidth = 1;
      const w = shown.img.naturalWidth;
      const h = shown.img.naturalHeight;
      cx.beginPath();
      for (let i = Math.max(0, Math.floor(-view.x / view.zoom)); i <= Math.min(w, (el.clientWidth - view.x) / view.zoom); i++) {
        const px = Math.round(view.x + i * view.zoom) + 0.5;
        cx.moveTo(px, Math.max(view.y, 0));
        cx.lineTo(px, Math.min(view.y + h * view.zoom, el.clientHeight));
      }
      for (let j = Math.max(0, Math.floor(-view.y / view.zoom)); j <= Math.min(h, (el.clientHeight - view.y) / view.zoom); j++) {
        const py = Math.round(view.y + j * view.zoom) + 0.5;
        cx.moveTo(Math.max(view.x, 0), py);
        cx.lineTo(Math.min(view.x + w * view.zoom, el.clientWidth), py);
      }
      cx.stroke();
    }
    if (points && points.length && !showCompare) {
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
    if (overlays && !showCompare) {
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
  }, [shown, view, points, showCompare, overlays]);

  useEffect(() => {
    const onResize = () => setView((v) => ({ ...v }));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement) return;
      if (e.key === "b" || e.key === "B") setShowCompare(true);
      if (e.key === "f" || e.key === "F") fit();
      if (e.key === "1") oneToOne();
    };
    const up = (e: KeyboardEvent) => {
      if (e.key === "b" || e.key === "B") setShowCompare(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  });

  const toImage = (e: React.MouseEvent | React.WheelEvent) => {
    const r = canvas.current!.getBoundingClientRect();
    const sx = e.clientX - r.left;
    const sy = e.clientY - r.top;
    return { sx, sy, x: (sx - view.x) / view.zoom, y: (sy - view.y) / view.zoom };
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
    if (drag.current) {
      const dx = e.clientX - drag.current.sx;
      const dy = e.clientY - drag.current.sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.current.moved = true;
      setView((v) => ({ ...v, x: drag.current!.vx + dx, y: drag.current!.vy + dy }));
    }
    if (!shown) return;
    const { x, y } = toImage(e);
    const ix = Math.floor(x);
    const iy = Math.floor(y);
    if (ix < 0 || iy < 0 || ix >= shown.img.naturalWidth || iy >= shown.img.naturalHeight) {
      setCursor(null);
      return;
    }
    let rgb: number[] | null = null;
    if (shown.data) {
      const o = (iy * shown.data.width + ix) * 4;
      rgb = [shown.data.data[o], shown.data.data[o + 1], shown.data.data[o + 2]];
    }
    setCursor({ x: ix, y: iy, rgb });
  };

  const onDown = (e: React.MouseEvent) => {
    drag.current = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
  };

  const onUp = (e: React.MouseEvent) => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && onPick && shown && !showCompare) {
      const { x, y } = toImage(e);
      if (x >= 0 && y >= 0 && x < shown.img.naturalWidth && y < shown.img.naturalHeight) {
        onPick(Math.round(x * 10) / 10, Math.round(y * 10) / 10);
      }
    }
  };

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <span className="viewer-label">{showCompare ? compareLabel : label}</span>
        {shown && (
          <span className="muted">
            {shown.img.naturalWidth}×{shown.img.naturalHeight} · {Math.round(view.zoom * 100)}%
          </span>
        )}
        <span className="spacer" />
        <button onClick={fit} title="Fit (F)">Fit</button>
        <button onClick={oneToOne} title="Actual pixels (1)">1:1</button>
        {compareSrc && (
          <button
            className={showCompare ? "active" : ""}
            onMouseDown={() => setShowCompare(true)}
            onMouseUp={() => setShowCompare(false)}
            onMouseLeave={() => setShowCompare(false)}
            title="Hold to show the original (or hold B)"
          >
            Hold: original
          </button>
        )}
      </div>
      <div
        ref={wrap}
        className={`viewer-canvas${onPick ? " picking" : ""}`}
        onWheel={onWheel}
        onMouseMove={onMove}
        onMouseDown={onDown}
        onMouseUp={onUp}
        onMouseLeave={() => {
          drag.current = null;
          setCursor(null);
        }}
      >
        <canvas ref={canvas} />
        {illustrative && !showCompare && src && (
          <div className="illustrative-overlay">ILLUSTRATIVE ONLY · AI-generated detail · not for measurement</div>
        )}
        {!src && !busy && !error && <div className="viewer-empty">Select evidence to view it</div>}
        {busy && (src ? <div className="viewer-updating">Updating preview…</div> : <div className="viewer-busy">Processing…</div>)}
        {error && <div className="viewer-error">{error}</div>}
        {pickHint && <div className="viewer-hint">{pickHint}</div>}
      </div>
      <div className="viewer-status">
        {cursor ? (
          <>
            x {cursor.x}, y {cursor.y}
            {cursor.rgb && (
              <>
                {" "}· R {cursor.rgb[0]} G {cursor.rgb[1]} B {cursor.rgb[2]}
                <span className="swatch" style={{ background: `rgb(${cursor.rgb.join(",")})` }} />
              </>
            )}
          </>
        ) : (
          <span className="muted">Scroll to zoom · drag to pan · hold B to compare with the original</span>
        )}
      </div>
    </div>
  );
}
