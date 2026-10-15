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
  const ident = evidence.probe.identified;
  const [method, setMethod] = useState(
    ident?.parser ? "parse" : evidence.media_type === "container" && !ident?.family ? "carve" : "remux",
  );
  const [fps, setFps] = useState(25);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="convert-panel">
      <div className="panel-title">Recorder export</div>
      <p className="small">
        {ident?.label ? <>Identified as <b>{ident.label}</b></> : "Container not identified"}
        {ident?.elementary_codec && <> · carries {ident.elementary_codec.toUpperCase()} video</>}.
        {evidence.media_type === "container" && " It cannot be opened directly; convert it to work on it."}
        {ident?.parser && " Lucida reads this format itself, with the recorder's own frame timing (parse)."}
      </p>
      <select value={method} onChange={(e) => setMethod(e.target.value)}>
        {Object.keys(methods)
          .filter((m) => m !== "parse" || ident?.parser)
          .map((m) => (
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

/** What the recorder's own timing says about a working copy made by parse: clock span, gaps and anomalies. */
export function RecorderTimingPanel({ evidence }: { evidence: Evidence }) {
  const t = evidence.conversion?.timing;
  if (!t) return null;
  return (
    <div className="convert-panel">
      <div className="panel-title">Recorder timing</div>
      <p className="small">
        {t.frames} frames ({t.key_frames} key) over {t.duration_s} s, {t.measured_frame_rate ?? "?"} fps measured
        {t.recorder_frame_rate ? <> ({t.recorder_frame_rate} fps written by the recorder)</> : null}.
      </p>
      {t.recorder_clock_start && (
        <p className="small">
          Recorder clock {t.recorder_clock_start.replace("T", " ")} to {t.recorder_clock_end?.replace("T", " ")}.
        </p>
      )}
      <p className={t.gap_count || t.timestamp_anomaly_count ? "small warn" : "small muted"}>
        {t.gap_count} gap{t.gap_count === 1 ? "" : "s"} longer than 1.5 frame intervals
        {t.gaps.length > 0 && <> (first at frame {t.gaps[0].frame}: {t.gaps[0].gap_ms} ms)</>}; {t.timestamp_anomaly_count} timestamp
        anomal{t.timestamp_anomaly_count === 1 ? "y" : "ies"}.
      </p>
      {(evidence.conversion?.notes ?? []).map((n) => (
        <p key={n} className="small muted">
          {n}
        </p>
      ))}
      <p className="muted small">Timing source: {t.source}.</p>
    </div>
  );
}
