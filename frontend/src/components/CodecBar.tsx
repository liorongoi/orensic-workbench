import type { CodecInfo, CodecLayer, CodecSettings } from "../types";

export const CODEC_VIEW_CODECS = ["h264", "mpeg2video", "mpeg1video", "mpeg4", "h263", "h263p", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3"];

const KIND_LETTER: Record<string, string> = { i: "intra", f: "fwd", b: "bwd", B: "bi", d: "direct", s: "skip", o: "other" };
const MV_FWD = "#ffec78";
const MV_BWD = "#5ae6ff";

interface Props {
  settings: CodecSettings;
  info: CodecInfo | null;
  error: string | null;
  onChange: (s: CodecSettings) => void;
  onClose: () => void;
}

/** Controls and legend for the macroblock view: block classes or QP, motion vectors, grid. */
export function CodecBar({ settings, info, error, onChange, onClose }: Props) {
  const set = (p: Partial<CodecSettings>) => onChange({ ...settings, ...p });
  const layers: [CodecLayer, string, string][] = [
    ["type", "Type", "Colour each 16×16 block by how it was coded"],
    ["qp", "QP", "Quantiser heat map: blue = low QP (more detail), red = high"],
    ["none", "Off", "No block colours"],
  ];
  return (
    <div className="codec-bar">
      <span className="seg">
        {layers.map(([id, label, title]) => (
          <button key={id} className={settings.layer === id ? "active" : ""} onClick={() => set({ layer: id })} title={title}>
            {label}
          </button>
        ))}
      </span>
      <button className={settings.mv ? "active" : ""} onClick={() => set({ mv: !settings.mv })} title="Motion vectors: yellow forward, cyan backward">
        MV
      </button>
      <button className={settings.grid ? "active" : ""} onClick={() => set({ grid: !settings.grid })} title="16×16 macroblock grid">
        Grid
      </button>
      {error ? (
        <span className="bad small">{error}</span>
      ) : info ? (
        <span className="codec-legend">
          {settings.layer === "type" &&
            info.legend
              .filter((k) => info.counts[k.id] > 0)
              .map((k) => (
                <span key={k.id} title={`${info.counts[k.id]} of ${info.cols * info.rows} blocks`}>
                  <i style={{ background: k.color }} />
                  {k.label} {Math.round(info.share[k.id] * 100)}%
                </span>
              ))}
          {settings.layer === "qp" && (
            <span title={`Scale ${info.qp.scale[0]}–${info.qp.scale[1]}`}>
              <i className="qp-ramp" /> QP {info.qp.min}–{info.qp.max} · mean {info.qp.mean}
            </span>
          )}
          {settings.mv && (
            <span>
              <i style={{ background: MV_FWD }} />
              Fwd <i style={{ background: MV_BWD }} />
              Bwd
            </span>
          )}
          {settings.layer !== "qp" && <span className="muted">QP {info.qp.mean}</span>}
        </span>
      ) : (
        <span className="muted small">Reading blocks…</span>
      )}
      <span className="spacer" />
      <button className="icon" onClick={onClose} title="Close the macroblock view (M)">
        ✕
      </button>
    </div>
  );
}

/** "MB 3,5 · Intra · QP 27" for the block under the cursor. */
export function describeBlock(info: CodecInfo | null, x: number, y: number): string | null {
  if (!info) return null;
  const c = Math.floor(x / info.mb_size);
  const r = Math.floor(y / info.mb_size);
  if (r < 0 || c < 0 || r >= info.rows || c >= info.cols) return null;
  const kind = info.legend.find((k) => k.id === KIND_LETTER[info.kinds[r][c]]);
  return `MB ${c},${r} · ${kind?.label ?? "?"} · QP ${info.qp_grid[r][c]}`;
}
