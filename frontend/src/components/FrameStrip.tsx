import { useEffect, useRef } from "react";
import type { FrameInfo } from "../types";

const COLORS: Record<string, string> = { I: "#e5484d", P: "#3e9bff", B: "#46a758" };

interface Props {
  frames: FrameInfo[];
  current: number;
  range: { start: number; end: number } | null;
  onSeek: (n: number) => void;
  onSetStart?: () => void;
  onSetEnd?: () => void;
}

/** Frame scrubber with the bitstream's I/P/B frame types drawn underneath. */
export function FrameStrip({ frames, current, range, onSeek, onSetStart, onSetEnd }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const n = frames.length;

  useEffect(() => {
    const c = canvas.current;
    if (!c || !n) return;
    const w = c.clientWidth;
    const dpr = window.devicePixelRatio || 1;
    c.width = w * dpr;
    c.height = 14 * dpr;
    const cx = c.getContext("2d")!;
    cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx.clearRect(0, 0, w, 14);
    if (range) {
      cx.fillStyle = "rgba(255,207,63,0.25)";
      cx.fillRect((range.start / n) * w, 0, ((range.end - range.start + 1) / n) * w, 14);
    }
    frames.forEach((f, i) => {
      cx.fillStyle = COLORS[f.type] ?? "#888";
      const x = (i / n) * w;
      const h = f.type === "I" ? 14 : f.type === "P" ? 9 : 6;
      cx.fillRect(x, 14 - h, Math.max(1, w / n - 0.5), h);
    });
    cx.fillStyle = "#fff";
    cx.fillRect((current / n) * w, 0, Math.max(1.5, w / n), 14);
  }, [frames, current, range, n]);

  if (!n) return null;
  const f = frames[current];
  return (
    <div className="frame-strip">
      <div className="frame-row">
        <button onClick={() => onSeek(Math.max(0, current - 1))} title="Previous frame (J, Shift+J = 10)">◀</button>
        <input type="range" min={0} max={n - 1} value={current} onChange={(e) => onSeek(parseInt(e.target.value, 10))} />
        <button onClick={() => onSeek(Math.min(n - 1, current + 1))} title="Next frame (L, Shift+L = 10)">▶</button>
        <span className="frame-info">
          Frame {current} / {n - 1}
          {f && (
            <>
              {" "}· <b style={{ color: COLORS[f.type] }}>{f.type}</b>
              {f.time !== null && ` · ${f.time.toFixed(3)} s`}
              {f.recorder_time && ` · recorder ${f.recorder_time.replace("T", " ")}`}
              {f.size !== null && ` · ${f.size.toLocaleString()} B`}
            </>
          )}
        </span>
        {onSetStart && <button onClick={onSetStart} title="Start the chain's frame range here">[ In</button>}
        {onSetEnd && <button onClick={onSetEnd} title="End the chain's frame range here">Out ]</button>}
        {range && range.end > range.start && (
          <span className="muted small" title="Frame range used by the chain">
            {range.start}–{range.end} ({range.end - range.start + 1})
          </span>
        )}
      </div>
      <canvas
        ref={canvas}
        className="frame-types"
        title="Frame types: red I, blue P, green B. Click to jump."
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          onSeek(Math.min(n - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * n))));
        }}
      />
    </div>
  );
}
