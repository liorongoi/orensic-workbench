import { useState } from "react";
import { api } from "../api";
import type { Chain, Evidence, MeasureMethod, Measurement } from "../types";

export type PointFields = Record<string, number[][]>;

interface FieldSpec {
  key: string;
  label: string;
  kind: "lines" | "point" | "plane";
  color: string;
  help?: string;
}

export const MEASURE_FIELDS: Record<string, FieldSpec[]> = {
  height: [
    { key: "vertical", label: "Vertical lines", kind: "lines", color: "#ff5c5c", help: "Edges that are vertical in the scene: door frames, poles, building corners" },
    { key: "horizontal_a", label: "Ground lines, direction 1", kind: "lines", color: "#3fb6ff", help: "Two lines parallel on the ground, e.g. kerb edges" },
    { key: "horizontal_b", label: "Ground lines, direction 2", kind: "lines", color: "#3fe08a", help: "Two lines parallel on the ground in another direction, e.g. paving joints" },
    { key: "ref_base", label: "Reference base", kind: "point", color: "#ffcf3f" },
    { key: "ref_top", label: "Reference top", kind: "point", color: "#ffcf3f" },
    { key: "target_base", label: "Target base", kind: "point", color: "#ff8af0" },
    { key: "target_top", label: "Target top", kind: "point", color: "#ff8af0" },
  ],
  distance: [
    { key: "plane_image_points", label: "Surveyed ground points", kind: "plane", color: "#3fb6ff", help: "At least four points on the ground whose positions you measured at the scene" },
    { key: "point_a", label: "First point", kind: "point", color: "#ffcf3f" },
    { key: "point_b", label: "Second point", kind: "point", color: "#ff8af0" },
  ],
  camera_height: [
    { key: "control_image_points", label: "Surveyed 3D points", kind: "plane", color: "#3fb6ff", help: "At least six points whose X, Y, Z you measured at the scene (Z up, ground at 0), spread over the image and in depth" },
    { key: "target_base", label: "Target base (on the ground)", kind: "point", color: "#ffcf3f" },
    { key: "target_top", label: "Target top", kind: "point", color: "#ff8af0" },
  ],
  speed: [
    { key: "plane_image_points", label: "Surveyed ground points", kind: "plane", color: "#3fb6ff", help: "At least four points on the ground whose positions you measured at the scene" },
    { key: "point_a", label: "Contact point in frame A", kind: "point", color: "#ffcf3f", help: "Pick while frame A is shown" },
    { key: "point_b", label: "Same point in frame B", kind: "point", color: "#ff8af0", help: "Pick while frame B is shown" },
  ],
};

interface Props {
  evidence: Evidence;
  frame: number;
  chain: Chain | null;
  chainDirty: boolean;
  methods: Record<string, MeasureMethod>;
  measurements: Measurement[];
  points: PointFields;
  kind: string;
  picking: string | null;
  onChain: boolean;
  onOnChain: (v: boolean) => void;
  onKind: (k: string) => void;
  onPoints: (p: PointFields) => void;
  onStartPick: (field: string, count: number, reset: boolean) => void;
  onDone: (m: Measurement) => void;
}

const fmt = (v: number, d = 3) => (Number.isFinite(v) ? v.toFixed(d) : "?");

export function MeasurePanel(props: Props) {
  const { evidence, frame, chain, chainDirty, methods, points, kind, picking, onChain } = props;
  const isVideo = evidence.media_type === "video";
  const kinds = Object.keys(methods).filter((k) => k !== "speed" || isVideo);
  const [refHeight, setRefHeight] = useState(1.8);
  const [refSigma, setRefSigma] = useState(0.01);
  const [sigma, setSigma] = useState(1.0);
  const [worldSigma, setWorldSigma] = useState(0.05);
  const [world, setWorld] = useState<string[][]>([]);
  const [frames, setFrames] = useState<{ a: number | null; b: number | null }>({ a: null, b: null });
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const spec = methods[kind];
  const fields = MEASURE_FIELDS[kind] ?? [];
  const is3d = kind === "camera_height";
  const planeKey = is3d ? "control_image_points" : "plane_image_points";
  const nPlane = points[planeKey]?.length ?? 0;
  const dims = is3d ? 3 : 2;
  const minPlane = is3d ? 6 : 4;

  const pick = (f: FieldSpec, more = false) => {
    const cur = points[f.key]?.length ?? 0;
    if (f.kind === "point") props.onStartPick(f.key, 1, true);
    else if (f.kind === "lines") props.onStartPick(f.key, more ? cur + 2 : 4, !more);
    else props.onStartPick(f.key, more ? cur + 1 : minPlane, !more);
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const inputs: Record<string, unknown> = { point_sigma: sigma };
      for (const f of fields) inputs[f.key] = f.kind === "point" ? points[f.key]?.[0] : points[f.key];
      if (kind === "height") Object.assign(inputs, { ref_height: refHeight, ref_height_sigma: refSigma });
      if (kind !== "height") {
        const rows = Array.from({ length: nPlane }, (_, i) => Array.from({ length: dims }, (_, j) => parseFloat(world[i]?.[j] ?? "")));
        inputs[is3d ? "control_world_points" : "plane_world_points"] = rows;
        inputs.world_sigma = worldSigma;
      }
      if (is3d) inputs.image_size = [evidence.probe.width, evidence.probe.height];
      if (kind === "speed") Object.assign(inputs, { frame_a: frames.a, frame_b: frames.b });
      const m = await api.measure(evidence.id, { kind, inputs, label, chain_id: onChain && chain ? chain.id : null, frame });
      props.onDone(m);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const mine = props.measurements.filter((m) => m.evidence_id === evidence.id).slice().reverse();

  return (
    <div className="measure-panel">
      <div className="chain-picker">
        <select value={kind} onChange={(e) => props.onKind(e.target.value)}>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {methods[k].name}
            </option>
          ))}
        </select>
      </div>
      {spec && <p className="small">{spec.description}</p>}
      {chain && (
        <label className="small upto">
          <input type="checkbox" checked={onChain} disabled={chainDirty} onChange={(e) => props.onOnChain(e.target.checked)} />
          Measure on the result of “{chain.name}” {chainDirty && "(save the chain first)"}
        </label>
      )}
      <p className="muted small">
        Click points on the {onChain ? "enhanced result" : "original"} in the viewer. Correct lens distortion first if the camera has a wide lens.
      </p>
      <div className="measure-fields">
        {fields.map((f) => {
          const n = points[f.key]?.length ?? 0;
          const active = picking === f.key;
          return (
            <div key={f.key} className="measure-field" title={f.help}>
              <span className="swatch" style={{ background: f.color }} />
              <span className="grow">
                {f.label}
                <span className="muted small"> · {f.kind === "point" ? (n ? "set" : "not set") : f.kind === "lines" ? `${Math.floor(n / 2)} lines` : `${n} points`}</span>
              </span>
              <button className={active ? "active" : ""} onClick={() => pick(f)}>
                {active ? "Picking…" : n ? "Redo" : "Pick"}
              </button>
              {f.kind !== "point" && n >= (f.kind === "lines" ? 4 : minPlane) && <button onClick={() => pick(f, true)}>+1</button>}
            </div>
          );
        })}
      </div>

      {kind === "height" && (
        <div className="params">
          <label className="param">
            <span className="param-label">Reference height (m)</span>
            <input type="number" step={0.01} value={refHeight} onChange={(e) => setRefHeight(parseFloat(e.target.value))} />
          </label>
          <label className="param">
            <span className="param-label">Reference height uncertainty (m)</span>
            <input type="number" step={0.005} value={refSigma} onChange={(e) => setRefSigma(parseFloat(e.target.value))} />
          </label>
        </div>
      )}
      {kind !== "height" && nPlane > 0 && (
        <table className="kv world">
          <tbody>
            <tr>
              <th>#</th>
              <th>{is3d ? "X (m)" : "Ground x (m)"}</th>
              <th>{is3d ? "Y (m)" : "Ground y (m)"}</th>
              {is3d && <th>Z up (m)</th>}
            </tr>
            {Array.from({ length: nPlane }, (_, i) => (
              <tr key={i}>
                <td>{i + 1}</td>
                {Array.from({ length: dims }, (_, j) => j).map((j) => (
                  <td key={j}>
                    <input
                      type="number"
                      step={0.01}
                      value={world[i]?.[j] ?? ""}
                      onChange={(e) =>
                        setWorld((w) => {
                          const next = Array.from({ length: nPlane }, (_, k) => [...(w[k] ?? Array(dims).fill(""))]);
                          next[i][j] = e.target.value;
                          return next;
                        })
                      }
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {kind === "speed" && (
        <div className="frame-pick small">
          Frame A: <b>{frames.a ?? "–"}</b> <button onClick={() => setFrames((f) => ({ ...f, a: frame }))}>Use current ({frame})</button> · Frame B:{" "}
          <b>{frames.b ?? "–"}</b> <button onClick={() => setFrames((f) => ({ ...f, b: frame }))}>Use current ({frame})</button>
        </div>
      )}
      <div className="params">
        <label className="param">
          <span className="param-label">Clicking uncertainty (px)</span>
          <input type="number" step={0.1} min={0} max={20} value={sigma} onChange={(e) => setSigma(parseFloat(e.target.value))} />
        </label>
        {kind !== "height" && (
          <label className="param">
            <span className="param-label">Survey uncertainty (m)</span>
            <input type="number" step={0.01} min={0} value={worldSigma} onChange={(e) => setWorldSigma(parseFloat(e.target.value))} />
          </label>
        )}
        <label className="param">
          <span className="param-label">Label</span>
          <input value={label} placeholder="e.g. person at door" onChange={(e) => setLabel(e.target.value)} />
        </label>
      </div>
      <div className="row">
        <button className="primary" onClick={run} disabled={busy}>
          {busy ? "Computing…" : "Measure"}
        </button>
        <button onClick={() => props.onPoints({})}>Clear points</button>
      </div>
      {error && <p className="error">{error}</p>}
      {spec && (
        <details className="small">
          <summary>Assumptions and reference</summary>
          <ul>
            {spec.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
          <p className="muted">{spec.reference}</p>
        </details>
      )}

      <h3>Results</h3>
      {mine.length === 0 && <p className="muted small">No measurements on this item yet.</p>}
      {mine.map((m) => (
        <div key={m.id} className="result measure-result">
          <b>{m.label || m.result.name}</b>
          <div>
            {fmt(m.result.value)} {m.result.unit}{" "}
            <span className="muted">
              (95 % {fmt(m.result.interval_95[0])} to {fmt(m.result.interval_95[1])})
            </span>
            {m.result.value_kmh !== undefined && m.result.interval_95_kmh && (
              <>
                {" "}= {fmt(m.result.value_kmh, 1)} km/h <span className="muted">({fmt(m.result.interval_95_kmh[0], 1)} to {fmt(m.result.interval_95_kmh[1], 1)})</span>
              </>
            )}
          </div>
          {m.result.camera && (
            <div className="muted small">
              Camera: focal length {fmt(m.result.camera.focal_length_px, 0)} px, position ({m.result.camera.position_m.map((v) => fmt(v, 2)).join(", ")}) m,
              control-point fit {fmt(m.result.camera.reprojection_rms_px, 2)} px RMS
            </div>
          )}
          {m.result.fit_warning && <p className="error small">{m.result.fit_warning}</p>}
          {m.result.warning && <p className="error small">{m.result.warning}</p>}
          <p className="muted small">
            {m.chain_id ? "on enhanced result" : "on original"} · {m.actor} · <span className="hash">{m.output_hash.slice(0, 16)}…</span>
          </p>
        </div>
      ))}
    </div>
  );
}
