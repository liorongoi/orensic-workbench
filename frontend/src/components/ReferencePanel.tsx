import { useState } from "react";
import { api } from "../api";
import type { CameraReference, Evidence } from "../types";

interface Props {
  caseId: string;
  evidence: Evidence[];
  references: CameraReference[];
  onCreated: (r: CameraReference) => void;
}

/** Build PRNU camera references from known images of one camera. */
export function ReferencePanel({ caseId, evidence, references, onCreated }: Props) {
  const images = evidence.filter((e) => e.media_type === "image");
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [camera, setCamera] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const names = Object.fromEntries(evidence.map((e) => [e.id, e.filename]));
  return (
    <div className="references">
      <div className="detector-head">
        <b>Camera references</b>
        <span className="spacer" />
        <button onClick={() => setOpen(!open)}>{open ? "Cancel" : "New reference"}</button>
      </div>
      {references.length === 0 && !open && <p className="muted small">None yet. Build one from 20 or more flat, bright images known to come from the camera.</p>}
      <ul className="small ref-list">
        {references.map((r) => (
          <li key={r.id} title={r.evidence.map((x) => names[x] ?? x).join(", ")}>
            <b>{r.name}</b> {r.camera && <span className="muted">({r.camera})</span>} · {r.stats.images} images · {r.width}×{r.height}
          </li>
        ))}
      </ul>
      {open && (
        <div className="ref-form">
          <input placeholder="Reference name" value={name} onChange={(e) => setName(e.target.value)} />
          <input placeholder="Camera make and model (optional)" value={camera} onChange={(e) => setCamera(e.target.value)} />
          <div className="ref-images small">
            {images.map((e) => (
              <label key={e.id}>
                <input
                  type="checkbox"
                  checked={picked.includes(e.id)}
                  onChange={(ev) => setPicked((p) => (ev.target.checked ? [...p, e.id] : p.filter((x) => x !== e.id)))}
                />{" "}
                {e.filename} <span className="muted">{e.probe.width}×{e.probe.height}</span>
              </label>
            ))}
          </div>
          <button
            className="primary"
            disabled={busy || picked.length < 2 || !name.trim()}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                onCreated(await api.createReference(caseId, name, camera, picked));
                setOpen(false);
                setPicked([]);
                setName("");
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Estimating fingerprint…" : `Build from ${picked.length} images`}
          </button>
          {error && <p className="error small">{error}</p>}
        </div>
      )}
    </div>
  );
}
