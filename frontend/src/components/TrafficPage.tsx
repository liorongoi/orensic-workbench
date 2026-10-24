import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, urls } from "../api";
import { openPage } from "../desktop";
import {
  traffic,
  VEHICLE_CLASSES,
  type Box,
  type Camera,
  type Chart,
  type Detection,
  type ImageDetail,
  type Notice,
  type Point,
  type Region,
  type TrafficImage,
} from "../traffic";

type Tool = "select" | "add" | "region";
type Drag =
  | { kind: "move"; id: number; start: Point; orig: Box; box: Box }
  | { kind: "resize"; id: number; corner: number; orig: Box; box: Box }
  | { kind: "draw"; start: Point; box: Box };

const STATUS_LABEL: Record<string, string> = { pending: "Not run", detected: "Detected", edited: "Edited", approved: "Approved" };
const CHART_EXAMPLES = [
  "detections by hour and lighting as stacked",
  "images by status as pie",
  "detections by date and camera as line",
  "false alarms by camera",
];
const PALETTE = ["#3e9bff", "#ffa94d", "#46c27a", "#ff6b6b", "#b197fc", "#8b95a3", "#ffd43b", "#3bc9db"];

function fmtTime(t: string | null) {
  return t ? t.replace("T", " ").slice(0, 19) : "unknown";
}

function norm(b: Box): Box {
  const [x, y, w, h] = b;
  return [w < 0 ? x + w : x, h < 0 ? y + h : y, Math.abs(w), Math.abs(h)].map((v) => Math.round(v * 10) / 10) as Box;
}

export function TrafficPage({ caseId }: { caseId: string }) {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [images, setImages] = useState<TrafficImage[]>([]);
  const [camId, setCamId] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<ImageDetail | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [msg, setMsg] = useState<{ kind: "ok" | "bad" | "info"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [following, setFollowing] = useState<string | null>(null);

  const camera = cameras.find((c) => c.camera_id === camId) ?? cameras[0];

  const loadLists = useCallback(async () => {
    // Images first: listing them registers new evidence and creates the first camera.
    const ims = await traffic.images(caseId);
    const cs = await traffic.cameras(caseId);
    setCameras(cs);
    setImages(ims);
    setCamId((cur) => (cur && cs.some((c) => c.camera_id === cur) ? cur : cs[0]?.camera_id ?? ""));
  }, [caseId]);

  const loadDetail = useCallback(async (id: string | null) => {
    if (!id) return setDetail(null);
    setDetail(await traffic.image(id));
  }, []);

  useEffect(() => {
    loadLists().catch((e) => setMsg({ kind: "bad", text: (e as Error).message }));
  }, [loadLists]);

  useEffect(() => {
    loadDetail(sel).catch((e) => setMsg({ kind: "bad", text: (e as Error).message }));
  }, [sel, loadDetail]);

  // Training and batch results arrive as notifications: show them and refresh what they changed.
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const n = await traffic.notifications(caseId);
        if (!alive) return;
        setNotices((old) => {
          if (n.some((x) => !old.some((o) => o.id === x.id))) {
            loadLists();
            loadDetail(sel);
          }
          return n;
        });
      } catch {
        /* offline for a moment */
      }
    };
    poll();
    const t = setInterval(poll, 4000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [caseId, sel, loadLists, loadDetail]);

  const run = async (label: string, f: () => Promise<unknown>, after?: (r: unknown) => string | void) => {
    setBusy(true);
    setMsg({ kind: "info", text: `${label}…` });
    try {
      const r = await f();
      const job = r as { id?: string; kind?: string; status?: string };
      if (job && job.kind && job.id && job.status) setFollowing(job.id);
      const text = after?.(r);
      setMsg(text ? { kind: "ok", text } : null);
      await loadLists();
      await loadDetail(sel);
      return r;
    } catch (e) {
      setMsg({ kind: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  // Follow a queued job: refresh the list while it runs, so results appear image by image.
  useEffect(() => {
    if (!following) return;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const j = await api.job(following);
        if (!alive) return;
        await loadLists();
        if (["done", "failed", "cancelled"].includes(j.status)) {
          setFollowing(null);
          setMsg(j.status !== "done" ? { kind: "bad", text: `${j.label}: ${j.error ?? j.status}` } : null);
          loadDetail(sel);
        }
      } catch {
        /* try again */
      }
    }, 1500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [following, loadLists, loadDetail, sel]);

  const shown = useMemo(
    () => images.filter((i) => (!camId || i.camera_id === camId) && (!statusFilter || i.status === statusFilter)),
    [images, camId, statusFilter],
  );

  const step = (d: number) => {
    if (!shown.length) return;
    const i = shown.findIndex((x) => x.evidence_id === sel);
    const n = shown[Math.min(shown.length - 1, Math.max(0, (i < 0 ? -1 : i) + d))];
    setSel(n.evidence_id);
  };

  const trainingNote = (r: unknown) => {
    const j = (r as { training_job?: { id: string } | null })?.training_job;
    return j ? "Enough corrections collected: training started. You will be told here when it finishes." : undefined;
  };

  return (
    <div className="traffic">
      <aside className="traffic-side">
        <CameraPanel
          caseId={caseId}
          cameras={cameras}
          camera={camera}
          onPick={setCamId}
          busy={busy}
          run={run}
        />
        <div className="traffic-list-head">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Filter by status">
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <span className="muted small">{shown.length} images</span>
        </div>
        <div className="traffic-list">
          <table className="table compact">
            <thead>
              <tr><th>Image</th><th>Local time</th><th>Light</th><th>#</th><th>Status</th></tr>
            </thead>
            <tbody>
              {shown.map((im) => (
                <tr key={im.evidence_id} className={im.evidence_id === sel ? "sel" : ""} onClick={() => setSel(im.evidence_id)}>
                  <td title={im.image_name} className="ellipsis">{im.image_name}</td>
                  <td className="small" title={`${im.time_source}; UTC ${fmtTime(im.capture_utc)}`}>{fmtTime(im.local_time).slice(5, 16)}</td>
                  <td className="small">{im.lighting ?? ""}</td>
                  <td>{im.status === "pending" ? "" : im.detection_count}</td>
                  <td><span className={`pill ${im.status}`}>{STATUS_LABEL[im.status]}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!images.length && <p className="muted">Add still images to the case (Case list → open the case → Add evidence), then come back here.</p>}
        </div>
      </aside>
      <main className="traffic-main">
        {notices.length > 0 && (
          <div className="notices">
            {notices.slice(0, 3).map((n) => (
              <div key={n.id} className={`notice ${n.kind}`}>
                <span>{n.message}</span>
                <button className="link" onClick={() => traffic.readNotifications(caseId, [n.id]).then(() => setNotices((o) => o.filter((x) => x.id !== n.id)))}>
                  Dismiss
                </button>
              </div>
            ))}
          </div>
        )}
        {msg && <p className={`status ${msg.kind}`}>{msg.text}</p>}
        {detail ? (
          <Review
            key={detail.evidence_id}
            detail={detail}
            camera={cameras.find((c) => c.camera_id === detail.camera_id)}
            cameras={cameras}
            busy={busy}
            run={run}
            trainingNote={trainingNote}
            onStep={step}
          />
        ) : (
          <p className="muted pad">Choose an image on the left to review its detections.</p>
        )}
        <Charts caseId={caseId} />
      </main>
    </div>
  );
}

// Camera settings, background, training, batch -------------------------------------------------------------

function CameraPanel({ caseId, cameras, camera, onPick, busy, run }: {
  caseId: string;
  cameras: Camera[];
  camera?: Camera;
  onPick: (id: string) => void;
  busy: boolean;
  run: (label: string, f: () => Promise<unknown>, after?: (r: unknown) => string | void) => Promise<unknown>;
}) {
  const [offset, setOffset] = useState("0");
  const [every, setEvery] = useState("20");
  useEffect(() => {
    if (camera) {
      setOffset(String(camera.utc_offset_hours));
      setEvery(String(camera.settings.auto_train_after));
    }
  }, [camera?.camera_id, camera?.utc_offset_hours, camera?.settings.auto_train_after]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!camera) return <div className="traffic-cam"><p className="muted">No images yet.</p></div>;
  const s = camera.settings;
  const set = (body: Parameters<typeof traffic.setCamera>[1]) => run("Saving camera settings", () => traffic.setCamera(camera.camera_id, body), () => "Saved.");
  const p = camera.profile;
  return (
    <div className="traffic-cam">
      <div className="row">
        <select value={camera.camera_id} onChange={(e) => onPick(e.target.value)} aria-label="Camera">
          {cameras.map((c) => (
            <option key={c.camera_id} value={c.camera_id}>{c.label}</option>
          ))}
        </select>
        <button
          title="Add a camera"
          onClick={() => {
            const label = prompt("Camera name");
            if (label) run("Adding camera", () => traffic.createCamera(caseId, label, Number(offset) || 0));
          }}
        >+</button>
      </div>
      <label className="row small">
        Local time = UTC +
        <input type="number" step="0.5" value={offset} onChange={(e) => setOffset(e.target.value)} style={{ width: 64 }} />
        h
        <button disabled={busy || Number(offset) === camera.utc_offset_hours} onClick={() => set({ utc_offset_hours: Number(offset) })}>Set</button>
      </label>
      <details>
        <summary className="small">Detection settings</summary>
        <label className="row small">Detector
          <select value={s.model} onChange={(e) => set({ settings: { model: e.target.value } })}>
            <option value="detect.yolox_s">YOLOX-S (accurate)</option>
            <option value="detect.yolox_tiny">YOLOX-Tiny (faster)</option>
            <option value="detect.yolox_nano">YOLOX-Nano (fastest)</option>
          </select>
        </label>
        <label className="row small">Pre-processing
          <select value={s.preprocess} onChange={(e) => set({ settings: { preprocess: e.target.value as "auto" } })}>
            <option value="auto">Automatic (night: contrast, fog: dehaze)</option>
            <option value="none">None</option>
            <option value="clahe">Local contrast (CLAHE)</option>
            <option value="dehaze">Dehaze</option>
          </select>
        </label>
        {(["yolox", "lights", "change"] as const).map((m) => (
          <label key={m} className="row small">
            <input type="checkbox" checked={s.members[m]} onChange={(e) => set({ settings: { members: { ...s.members, [m]: e.target.checked } } })} />
            {{ yolox: "Learned detector (with tiles for distant vehicles)", lights: "Vehicle lamps at night", change: "Change against learned background" }[m]}
          </label>
        ))}
        <label className="row small">Train after
          <input type="number" min={1} value={every} onChange={(e) => setEvery(e.target.value)} style={{ width: 56 }} />
          corrections
          <button disabled={busy || Number(every) === s.auto_train_after} onClick={() => set({ settings: { auto_train_after: Number(every) } })}>Set</button>
        </label>
      </details>
      <div className="small muted">
        Regions: {camera.regions.length ? camera.regions.map((r) => r.kind === "include" ? "scan area" : "mask").join(", ") : "whole image"} ·
        backgrounds: {camera.backgrounds.length ? camera.backgrounds.map((b) => `${b.lighting} (${b.images})`).join(", ") : "none"} ·
        scenery lights taught: {camera.static_lights.length}
      </div>
      <div className="small muted">
        Profile: {p ? `v${p.version}, F1 ${p.metrics.cross_validated.f1.toFixed(2)} on ${p.training.images.length} reviewed images` : "default (not trained)"} ·
        {" "}{camera.pending_feedback ?? 0} new corrections
      </div>
      <div className="row wrap">
        <button className="primary" disabled={busy}
          onClick={() => run("Queueing detection", () => traffic.detectBatch(caseId, { only: "pending", camera_id: camera.camera_id }),
            () => "Detection is running on images not run yet; the list fills in as it goes (see Jobs).")}>
          Detect new images
        </button>
        <button disabled={busy} title="Images never edited or approved"
          onClick={() => run("Queueing re-run", () => traffic.detectBatch(caseId, { only: "unedited", camera_id: camera.camera_id }),
            () => "Re-running unedited images; edited and approved images are not touched.")}>
          Re-run unedited
        </button>
        <button disabled={busy} title="Learn the empty scene and its scenery lights from this camera's images"
          onClick={() => run("Learning background", () => traffic.learnBackground(camera.camera_id), (r) => {
            const x = r as { learned: { lighting: string; images: number }[]; note: string };
            return x.learned.length ? `Learned: ${x.learned.map((l) => `${l.lighting} from ${l.images} images`).join(", ")}.` : `Nothing learned: ${x.note}.`;
          })}>
          Learn background
        </button>
        <button disabled={busy} onClick={() => run("Queueing training", () => traffic.train(camera.camera_id),
          () => "Training started. You will be told here when it finishes.")}>
          Train now
        </button>
      </div>
      <div className="row wrap small">
        <a className="button" href={traffic.exportUrl(caseId, "xlsx")}>Excel</a>
        <a className="button" href={traffic.exportUrl(caseId, "csv")}>CSV</a>
        <a className="button" href={traffic.exportUrl(caseId, "json")}>JSON</a>
        <a className="button" href={traffic.datasetUrl(caseId)} title="Reviewed images and boxes in YOLO format, for detector fine-tuning">Training set</a>
      </div>
    </div>
  );
}

// Review canvas ---------------------------------------------------------------------------------------------------

function Review({ detail, camera, cameras, busy, run, trainingNote, onStep }: {
  detail: ImageDetail;
  camera?: Camera;
  cameras: Camera[];
  busy: boolean;
  run: (label: string, f: () => Promise<unknown>, after?: (r: unknown) => string | void) => Promise<unknown>;
  trainingNote: (r: unknown) => string | undefined;
  onStep: (d: number) => void;
}) {
  const [tool, setTool] = useState<Tool>("select");
  const [cls, setCls] = useState("car");
  const [selId, setSelId] = useState<number | null>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [poly, setPoly] = useState<Point[]>([]);
  const [regionKind, setRegionKind] = useState<"include" | "exclude">("exclude");
  const [scope, setScope] = useState<"camera" | "image">("camera");
  const [zoom, setZoom] = useState(0); // 0 = fit
  const [showBelow, setShowBelow] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const W = detail.width || 1;
  const H = detail.height || 1;
  const lw = Math.max(1, W / 600);
  const dets = detail.detections;
  const selected = dets.find((d) => d.id === selId) ?? null;

  const pt = (e: { clientX: number; clientY: number }): Point => {
    const svg = svgRef.current!;
    const p = svg.createSVGPoint();
    p.x = e.clientX;
    p.y = e.clientY;
    const q = p.matrixTransform(svg.getScreenCTM()!.inverse());
    return [Math.min(W, Math.max(0, q.x)), Math.min(H, Math.max(0, q.y))];
  };

  const saveRegions = (regions: Region[]) => {
    if (scope === "camera" && camera) return run("Saving regions", () => traffic.setCamera(camera.camera_id, { regions }), () => "Regions saved for the camera. Re-run detection to apply them.");
    return run("Saving regions", () => traffic.setImage(detail.evidence_id, { regions }), () => "Regions saved for this image. Re-run detection to apply them.");
  };
  const currentRegions = scope === "camera" ? camera?.regions ?? [] : detail.regions ?? [];
  const closePoly = () => {
    if (poly.length >= 3) saveRegions([...currentRegions, { kind: regionKind, label: "", points: poly }]);
    setPoly([]);
  };

  const onDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const p = pt(e);
    if (tool === "region") {
      setPoly((ps) => [...ps, [Math.round(p[0]), Math.round(p[1])]]);
      return;
    }
    if (tool === "add") {
      (e.target as Element).setPointerCapture?.(e.pointerId);
      setDrag({ kind: "draw", start: p, box: [p[0], p[1], 0, 0] });
      return;
    }
    setSelId(null);
  };
  const onBoxDown = (e: React.PointerEvent, d: Detection) => {
    if (tool !== "select") return;
    e.stopPropagation();
    setSelId(d.id);
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag({ kind: "move", id: d.id, start: pt(e), orig: d.box, box: d.box });
  };
  const onHandleDown = (e: React.PointerEvent, d: Detection, corner: number) => {
    e.stopPropagation();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag({ kind: "resize", id: d.id, corner, orig: d.box, box: d.box });
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag) return;
    const p = pt(e);
    if (drag.kind === "draw") setDrag({ ...drag, box: [drag.start[0], drag.start[1], p[0] - drag.start[0], p[1] - drag.start[1]] });
    else if (drag.kind === "move") {
      const [x, y, w, h] = drag.orig;
      setDrag({ ...drag, box: [x + p[0] - drag.start[0], y + p[1] - drag.start[1], w, h] });
    } else {
      const [x, y, w, h] = drag.orig;
      let [x0, y0, x1, y1] = [x, y, x + w, y + h];
      if (drag.corner === 0 || drag.corner === 3) x0 = p[0]; else x1 = p[0];
      if (drag.corner === 0 || drag.corner === 1) y0 = p[1]; else y1 = p[1];
      setDrag({ ...drag, box: [x0, y0, x1 - x0, y1 - y0] });
    }
  };
  const onUp = () => {
    if (!drag) return;
    const b = norm(drag.box);
    setDrag(null);
    if (drag.kind === "draw") {
      if (b[2] >= 3 && b[3] >= 3) run("Adding missed vehicle", () => traffic.add(detail.evidence_id, b, cls), trainingNote);
    } else {
      const o = drag.orig;
      if (Math.abs(b[0] - o[0]) + Math.abs(b[1] - o[1]) + Math.abs(b[2] - o[2]) + Math.abs(b[3] - o[3]) > 1)
        run("Saving box", () => traffic.update(drag.id, { box: b }), trainingNote);
    }
  };

  const falseAlarm = (d: Detection) => run("Marking false alarm", () => traffic.falseAlarm(d.id), (r) => {
    const x = r as Detection;
    return (x.learned_static_light ? "Marked as a false alarm; its lights are now known scenery lights for this camera. " : "Marked as a false alarm (kept for training). ") + (trainingNote(r) ?? "");
  });
  const approve = () => run("Approving", () => traffic.approve(detail.evidence_id, detail.status !== "approved"), (r) => trainingNote(r) ?? "");

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA") return;
      if ((e.key === "Delete" || e.key === "Backspace") && selected) { e.preventDefault(); falseAlarm(selected); }
      else if (e.key === "Escape") { setPoly([]); setDrag(null); setSelId(null); }
      else if (e.key === "Enter" && tool === "region") closePoly();
      else if (e.key === "v") setTool("select");
      else if (e.key === "b") setTool("add");
      else if (e.key === "r") setTool("region");
      else if (e.key === "a" && !busy) approve();
      else if (e.key === "n" || e.key === "ArrowDown") onStep(1);
      else if (e.key === "p" || e.key === "ArrowUp") onStep(-1);
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  });

  const shownBox = (d: Detection): Box => (drag && drag.kind !== "draw" && drag.id === d.id ? norm(drag.box) : d.box);
  const below = showBelow && detail.run ? detail.run.objects.filter((o) => !o.kept) : [];
  const indicators = detail.scene.indicators ?? [];

  return (
    <div className="review">
      <div className="review-head">
        <strong className="ellipsis">{detail.image_name}</strong>
        <span className="muted small">
          {fmtTime(detail.local_time)} (UTC{detail.utc_offset_hours >= 0 ? "+" : ""}{detail.utc_offset_hours}, {detail.time_source})
        </span>
        <span className="spacer" />
        <span className={`pill ${detail.status}`}>{STATUS_LABEL[detail.status]}{detail.approved_by ? ` by ${detail.approved_by}` : ""}</span>
      </div>
      <div className="row wrap toolbar">
        <div className="seg">
          <button className={tool === "select" ? "active" : ""} onClick={() => setTool("select")} title="Select, move, resize (V)">Select</button>
          <button className={tool === "add" ? "active" : ""} onClick={() => setTool("add")} title="Draw a box around a missed vehicle (B)">Add missed</button>
          <button className={tool === "region" ? "active" : ""} onClick={() => setTool("region")} title="Click points, Enter to close (R)">Draw region</button>
        </div>
        {tool === "add" && (
          <select value={cls} onChange={(e) => setCls(e.target.value)} aria-label="Class for new boxes">
            {VEHICLE_CLASSES.map((c) => <option key={c}>{c}</option>)}
          </select>
        )}
        {tool === "region" && (
          <>
            <select value={regionKind} onChange={(e) => setRegionKind(e.target.value as "include")}>
              <option value="exclude">Mask (never detect here)</option>
              <option value="include">Scan area (detect only here)</option>
            </select>
            <select value={scope} onChange={(e) => setScope(e.target.value as "camera")}>
              <option value="camera">for the camera</option>
              <option value="image">for this image only</option>
            </select>
            <button disabled={poly.length < 3} onClick={closePoly}>Close polygon ({poly.length})</button>
            <button disabled={!currentRegions.length} onClick={() => saveRegions([])}>Clear {scope} regions</button>
            {scope === "image" && detail.regions && (
              <button onClick={() => run("Using camera regions", () => traffic.setImage(detail.evidence_id, { clear_regions: true }))}>Use camera regions</button>
            )}
          </>
        )}
        <span className="spacer" />
        <button onClick={() => setZoom(0)} className={zoom === 0 ? "active" : ""}>Fit</button>
        <button onClick={() => setZoom(1)} className={zoom === 1 ? "active" : ""}>1:1</button>
        <button onClick={() => setZoom((z) => (z || 0.5) * 1.5)}>+</button>
        <button onClick={() => setZoom((z) => Math.max(0.1, (z || 0.5) / 1.5))}>−</button>
      </div>
      <div className="review-canvas">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          style={zoom ? { width: W * zoom, height: H * zoom } : { width: "100%", height: "auto", maxHeight: "62vh" }}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          className={`tool-${tool}`}
        >
          <image href={urls.frame(detail.evidence_id, 0)} width={W} height={H} style={{ imageRendering: zoom >= 2 ? "pixelated" : "auto" }} />
          {detail.regions_effective.map((r, i) => (
            <polygon key={i} points={r.points.map((p) => p.join(",")).join(" ")} className={`region ${r.kind}`} strokeWidth={lw} />
          ))}
          {poly.length > 0 && <polyline points={poly.map((p) => p.join(",")).join(" ")} className={`region draft ${regionKind}`} strokeWidth={lw} />}
          {below.map((o) => (
            <rect key={`b${o.idx}`} x={o.box[0]} y={o.box[1]} width={o.box[2]} height={o.box[3]} className="det below" strokeWidth={lw}>
              <title>{`${o.cls} ${o.score.toFixed(2)} (below threshold${o.in_region ? "" : ", outside regions"})`}</title>
            </rect>
          ))}
          {dets.map((d) => {
            const [x, y, w, h] = shownBox(d);
            const on = d.id === selId;
            return (
              <g key={d.id}>
                <rect x={x} y={y} width={w} height={h} className={`det ${d.origin} ${on ? "on" : ""}`} strokeWidth={lw * (on ? 2 : 1.3)}
                  onPointerDown={(e) => onBoxDown(e, d)} />
                <text x={x} y={Math.max(10 * lw, y - 3 * lw)} fontSize={11 * lw} className="det-label">
                  {d.cls}{d.score != null ? ` ${d.score.toFixed(2)}` : " (added)"}
                </text>
                {on && tool === "select" && [[x, y], [x + w, y], [x + w, y + h], [x, y + h]].map(([hx, hy], i) => (
                  <rect key={i} x={hx - 4 * lw} y={hy - 4 * lw} width={8 * lw} height={8 * lw} className="handle" onPointerDown={(e) => onHandleDown(e, d, i)} />
                ))}
              </g>
            );
          })}
          {drag?.kind === "draw" && (() => {
            const b = norm(drag.box);
            return <rect x={b[0]} y={b[1]} width={b[2]} height={b[3]} className="det manual" strokeWidth={lw * 1.3} strokeDasharray={`${4 * lw}`} />;
          })()}
        </svg>
      </div>
      <div className="row wrap">
        <button className="primary" disabled={busy || detail.status === "pending"} onClick={approve} title="A">
          {detail.status === "approved" ? "Re-open" : "Approve"}
        </button>
        <button disabled={busy} onClick={() => onStep(-1)} title="P">‹ Previous</button>
        <button disabled={busy} onClick={() => onStep(1)} title="N">Next ›</button>
        {selected && (
          <>
            <select value={selected.cls} onChange={(e) => run("Changing class", () => traffic.update(selected.id, { cls: e.target.value }), trainingNote)}>
              {VEHICLE_CLASSES.map((c) => <option key={c}>{c}</option>)}
            </select>
            <button disabled={busy} onClick={() => falseAlarm(selected)} title="Delete">False alarm</button>
          </>
        )}
        <span className="spacer" />
        <button disabled={busy} onClick={() => {
          const reviewed = detail.status === "edited" || detail.status === "approved";
          let reason = "";
          if (reviewed) {
            reason = prompt("This image was reviewed. Re-running replaces the reviewed result. Reason:") ?? "";
            if (!reason.trim()) return;
          }
          run("Detecting", () => traffic.detect(detail.evidence_id, reviewed, reason), (r) => `${(r as { detections: number }).detections} vehicles detected.`);
        }}>{detail.status === "pending" ? "Detect" : "Re-run"}</button>
        <label className="small row"><input type="checkbox" checked={showBelow} onChange={(e) => setShowBelow(e.target.checked)} />Show below-threshold</label>
      </div>
      <div className="review-info small">
        <span>
          Lighting <select value={detail.lighting_overridden ? detail.lighting ?? "" : ""}
            onChange={(e) => run("Setting lighting", () => traffic.setImage(detail.evidence_id, { lighting: e.target.value }))}>
            <option value="">automatic ({detail.lighting_auto ?? "not measured"})</option>
            {["day", "twilight", "night", "night_ir"].map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </span>
        {indicators.length > 0 && <span className="muted"> · indicators: {indicators.join(", ")}</span>}
        <span> · camera <select value={detail.camera_id} onChange={(e) => run("Moving to camera", () => traffic.setImage(detail.evidence_id, { camera_id: e.target.value }))}>
          {cameras.map((c) => <option key={c.camera_id} value={c.camera_id}>{c.label}</option>)}
        </select></span>
        <span> · <button className="link" onClick={() => {
          const t = prompt("Capture time, local (YYYY-MM-DD HH:MM:SS)", fmtTime(detail.local_time) === "unknown" ? "" : fmtTime(detail.local_time));
          if (t) run("Setting time", () => traffic.setImage(detail.evidence_id, { time: t, time_is_local: true }));
        }}>set time</button></span>
        {detail.run && (
          <div className="muted run-line">
            Run {detail.run.id} · {detail.run.models.map((m) => `${m.model} ${m.sha256.slice(0, 10)} (${m.device})`).join(", ") || "no learned model"} ·
            profile {detail.run.profile_id ?? "default"} · output {detail.run.output_hash.slice(0, 12)} ·
            {" "}members: {Object.entries(detail.run.members).map(([k, v]) => `${k} ${String(v.candidates ?? 0)}`).join(", ")}
          </div>
        )}
      </div>
    </div>
  );
}

// Charts asked for in words --------------------------------------------------------------------------------------

function Charts({ caseId }: { caseId: string }) {
  const [q, setQ] = useState(CHART_EXAMPLES[0]);
  const [charts, setCharts] = useState<Chart[]>([]);
  const [err, setErr] = useState("");
  const add = async (text: string) => {
    setErr("");
    try {
      const c = await traffic.chart(caseId, text);
      setCharts((cs) => [c, ...cs.filter((x) => x.query !== c.query)].slice(0, 4));
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const queries = charts.map((c) => c.query);
  return (
    <section className="traffic-charts">
      <h3>Charts</h3>
      <form className="row" onSubmit={(e) => { e.preventDefault(); add(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} list="chart-examples" style={{ flex: 1 }}
          placeholder="e.g. detections by hour and lighting as stacked for night" aria-label="Describe a chart" />
        <datalist id="chart-examples">{CHART_EXAMPLES.map((x) => <option key={x} value={x} />)}</datalist>
        <button type="submit" className="primary">Draw</button>
        <button type="button" onClick={() => openPage(traffic.reportUrl(caseId, "html", queries), "Traffic report")}>Report</button>
        <button type="button" onClick={() => openPage(traffic.reportUrl(caseId, "pdf", queries), "Traffic report (PDF)")}>PDF</button>
      </form>
      <p className="muted small">Say what to count (detections, images, false alarms, missed, corrections), “by” one or two of hour, date,
        weekday, camera, lighting, status, class, and optionally “as bar / line / pie / stacked”, “for night”, “for camera …”,
        “from 2024-05-01”. The charts drawn here go into the report.</p>
      {err && <p className="status bad">{err}</p>}
      <div className="chart-grid">
        {charts.map((c) => (
          <figure key={c.query} className="chart">
            <figcaption>
              <strong>{c.title}</strong>
              <button className="link" onClick={() => setCharts((cs) => cs.filter((x) => x !== c))}>remove</button>
              <div className="muted small">“{c.query}” · {c.local_time_note}{c.notes.length ? " · " + c.notes.join(" · ") : ""}</div>
            </figcaption>
            <ChartSvg c={c} />
          </figure>
        ))}
      </div>
    </section>
  );
}

function ChartSvg({ c }: { c: Chart }) {
  const width = 560, height = 260;
  if (!c.x.length) return <p className="muted">No data.</p>;
  if (c.spec.type === "pie") {
    const vals = c.series[0].values;
    const total = vals.reduce((a, b) => a + b, 0) || 1;
    let a0 = -Math.PI / 2;
    const cx = 120, cy = height / 2, r = 100;
    return (
      <svg viewBox={`0 0 ${width} ${height}`} className="chart-svg" role="img" aria-label={c.title}>
        {vals.map((v, i) => {
          const a1 = a0 + (2 * Math.PI * v) / total;
          const large = a1 - a0 > Math.PI ? 1 : 0;
          const d = v === total ? undefined : `M${cx},${cy} L${cx + r * Math.cos(a0)},${cy + r * Math.sin(a0)} A${r},${r} 0 ${large} 1 ${cx + r * Math.cos(a1)},${cy + r * Math.sin(a1)} Z`;
          a0 = a1;
          return v === 0 ? null : d ? <path key={i} d={d} fill={PALETTE[i % PALETTE.length]} /> : <circle key={i} cx={cx} cy={cy} r={r} fill={PALETTE[i % PALETTE.length]} />;
        })}
        {c.x.map((x, i) => (
          <g key={x}>
            <rect x={260} y={20 + i * 20} width={12} height={12} fill={PALETTE[i % PALETTE.length]} />
            <text x={278} y={30 + i * 20} className="chart-text">{x}: {vals[i]}</text>
          </g>
        ))}
      </svg>
    );
  }
  const left = 40, top = 10, right = 130, bottom = 34;
  const pw = width - left - right, ph = height - top - bottom;
  const n = c.x.length;
  const stacked = c.spec.type === "stacked";
  const tot = c.x.map((_, i) => (stacked ? c.series.reduce((a, s) => a + s.values[i], 0) : Math.max(...c.series.map((s) => s.values[i]))));
  const tick = Math.max(1, Math.ceil(Math.max(1, ...tot) / 4));
  const vmax = tick * 4;
  const gw = pw / n;
  const y = (v: number) => top + ph - (ph * v) / vmax;
  const every = Math.max(1, Math.ceil(n / 14));
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="chart-svg" role="img" aria-label={c.title}>
      {[0, 0.25, 0.5, 0.75, 1].map((f) => (
        <g key={f}>
          <line x1={left} x2={left + pw} y1={y(vmax * f)} y2={y(vmax * f)} className="grid" />
          <text x={left - 4} y={y(vmax * f) + 4} textAnchor="end" className="chart-text">{Math.round(vmax * f)}</text>
        </g>
      ))}
      {c.x.map((x, i) => i % every === 0 && (
        <text key={x} x={left + gw * (i + 0.5)} y={top + ph + 14} textAnchor="middle" className="chart-text">{x}</text>
      ))}
      <text x={left + pw / 2} y={height - 4} textAnchor="middle" className="chart-text">{c.x_label}</text>
      {c.spec.type === "line"
        ? c.series.map((s, j) => (
          <polyline key={s.name} fill="none" stroke={PALETTE[j % PALETTE.length]} strokeWidth={2}
            points={s.values.map((v, i) => `${left + gw * (i + 0.5)},${y(v)}`).join(" ")} />
        ))
        : c.x.map((_, i) => {
          let base = 0;
          return c.series.map((s, j) => {
            const v = s.values[i];
            const col = PALETTE[j % PALETTE.length];
            if (stacked) {
              const h = (ph * v) / vmax;
              const el = <rect key={`${i}-${j}`} x={left + gw * i + gw * 0.15} y={top + ph - base - h} width={gw * 0.7} height={h} fill={col}><title>{`${c.x[i]} · ${s.name}: ${v}`}</title></rect>;
              base += h;
              return el;
            }
            const bw = (gw * 0.8) / c.series.length;
            return <rect key={`${i}-${j}`} x={left + gw * i + gw * 0.1 + bw * j} y={y(v)} width={bw} height={top + ph - y(v)} fill={col}><title>{`${c.x[i]} · ${s.name}: ${v}`}</title></rect>;
          });
        })}
      {c.series.map((s, j) => (
        <g key={s.name}>
          <rect x={left + pw + 12} y={top + j * 18} width={12} height={12} fill={PALETTE[j % PALETTE.length]} />
          <text x={left + pw + 30} y={top + 10 + j * 18} className="chart-text">{s.name}</text>
        </g>
      ))}
    </svg>
  );
}
