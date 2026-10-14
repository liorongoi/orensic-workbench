import { useState } from "react";
import { api } from "../api";
import type { Evidence } from "../types";

interface Props {
  evidence: Evidence;
  methods: Record<string, string>;
  onConverted: (ev: Evidence) => void;
}

/** Turn a recorder export into a standard working copy; the original stays untouched. */
export function ConvertPanel({ evidence, methods, onConverted }: Props) {
  const [method, setMethod] = useState(evidence.media_type === "container" && !evidence.probe.identified?.family ? "carve" : "remux");
  const [fps, setFps] = useState(25);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ident = evidence.probe.identified;
  return (
    <div className="convert-panel">
      <div className="panel-title">Recorder export</div>
      <p className="small">
        {ident?.label ? <>Identified as <b>{ident.label}</b></> : "Container not identified"}
        {ident?.elementary_codec && <> · carries {ident.elementary_codec.toUpperCase()} video</>}.
        {evidence.media_type === "container" && " It cannot be opened directly; convert it to work on it."}
      </p>
      <select value={method} onChange={(e) => setMethod(e.target.value)}>
        {Object.keys(methods).map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <p className="muted small">{methods[method]}</p>
      {method === "carve" && (
        <label className="small">
          Frame rate <input type="number" min={1} max={240} step={0.001} value={fps} onChange={(e) => setFps(parseFloat(e.target.value))} />
        </label>
      )}
      <button
        className="primary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            onConverted(await api.convert(evidence.id, method, fps));
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Converting…" : "Make working copy"}
      </button>
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
