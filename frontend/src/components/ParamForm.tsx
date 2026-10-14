import type { ParamSchema } from "../types";

interface Props {
  params: ParamSchema[];
  values: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
  onPick?: (param: ParamSchema) => void;
  picking?: string | null;
  disabled?: boolean;
}

export function ParamForm({ params, values, onChange, onPick, picking, disabled }: Props) {
  if (!params.length) return <p className="muted small">No parameters.</p>;
  return (
    <div className="params">
      {params.map((p) => {
        const v = values[p.name] ?? p.default;
        return (
          <label key={p.name} className="param" title={p.help}>
            <span className="param-label">{p.label}</span>
            {renderInput(p, v, onChange, onPick, picking === p.name, disabled)}
            {p.help && <span className="param-help">{p.help}</span>}
          </label>
        );
      })}
    </div>
  );
}

function renderInput(
  p: ParamSchema,
  v: unknown,
  onChange: Props["onChange"],
  onPick: Props["onPick"],
  isPicking: boolean,
  disabled?: boolean,
) {
  switch (p.type) {
    case "float":
    case "int": {
      const num = Number(v);
      const step = p.step ?? (p.type === "int" ? 1 : 0.01);
      const hasRange = p.min !== undefined && p.max !== undefined;
      const parse = (s: string) => (p.type === "int" ? parseInt(s, 10) : parseFloat(s));
      return (
        <span className="param-number">
          {hasRange && (
            <input
              type="range"
              min={p.min}
              max={p.max}
              step={step}
              value={num}
              disabled={disabled}
              onChange={(e) => onChange(p.name, parse(e.target.value))}
            />
          )}
          <input
            type="number"
            min={p.min}
            max={p.max}
            step={step}
            value={Number.isFinite(num) ? num : ""}
            disabled={disabled}
            onChange={(e) => {
              const n = parse(e.target.value);
              if (Number.isFinite(n)) onChange(p.name, n);
            }}
          />
        </span>
      );
    }
    case "bool":
      return <input type="checkbox" checked={Boolean(v)} disabled={disabled} onChange={(e) => onChange(p.name, e.target.checked)} />;
    case "choice":
      return (
        <select value={String(v)} disabled={disabled} onChange={(e) => onChange(p.name, e.target.value)}>
          {p.choices?.map((c) => (
            <option key={c} value={c}>
              {c.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      );
    case "points": {
      const pts = (v as number[][]) ?? [];
      return (
        <span className="param-points">
          <code>{pts.length ? pts.map(([x, y]) => `(${x}, ${y})`).join(" ") : "none"}</code>
          {onPick && (
            <button type="button" className={isPicking ? "active" : ""} disabled={disabled} onClick={(e) => { e.preventDefault(); onPick(p); }}>
              {isPicking ? "Picking… (Esc)" : "Pick on image"}
            </button>
          )}
          {pts.length > 0 && !p.count && (
            <button type="button" disabled={disabled} onClick={(e) => { e.preventDefault(); onChange(p.name, []); }}>
              Clear
            </button>
          )}
        </span>
      );
    }
    default:
      return <input type="text" value={String(v ?? "")} disabled={disabled} onChange={(e) => onChange(p.name, e.target.value)} />;
  }
}
