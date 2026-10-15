import { useEffect, useState } from "react";
import { api } from "../api";
import type { AuditEntry, Review } from "../types";

export function AuditPanel({ caseId, refreshKey, onChanged }: { caseId: string; refreshKey: number; onChanged?: () => void }) {
  const [data, setData] = useState<{ verification: { ok: boolean; entries: number; broken_at: number | null }; entries: AuditEntry[] } | null>(null);
  const [note, setNote] = useState("");
  const [tick, setTick] = useState(0);
  const [review, setReview] = useState<Review | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [showPassed, setShowPassed] = useState(false);

  useEffect(() => {
    api.audit(caseId).then(setData).catch(() => setData(null));
  }, [caseId, refreshKey, tick]);

  useEffect(() => {
    api.caseDetail(caseId).then((d) => setReview(d.review)).catch(() => setReview(null));
  }, [caseId]);

  const runReview = async () => {
    setReviewing(true);
    setReviewError(null);
    try {
      setReview(await api.review(caseId));
      setTick((t) => t + 1);
      onChanged?.();
    } catch (e) {
      setReviewError((e as Error).message);
    } finally {
      setReviewing(false);
    }
  };

  if (!data) return <p className="muted">Loading…</p>;
  return (
    <div className="audit">
      <p className={data.verification.ok ? "status ok" : "status bad"}>
        {data.verification.ok
          ? `✓ Log intact · ${data.verification.entries} entries`
          : `Hash chain BROKEN at entry ${data.verification.broken_at}. The log has been altered.`}
      </p>
      <div className={`review-box ${review?.level ?? ""}`}>
        <div className="row">
          <b title="The reviewer agent re-hashes the originals, checks this log, re-runs every rendered chain twice from the originals, re-runs analyses and measurements and compares hashes, checks model weights, and flags generative, unjustified or unaccepted steps. It changes nothing but its own logged review.">
            Independent review ⓘ
          </b>
          <span className="spacer" />
          <button className="primary" onClick={runReview} disabled={reviewing}>
            {reviewing ? "Reviewing…" : review ? "Review again" : "Run reviewer"}
          </button>
        </div>

        {reviewError && <p className="status bad">{reviewError}</p>}
        {review && (
          <>
            <p className="small">
              <b className={`lvl ${review.level}`}>{review.level.toUpperCase()}</b> · {review.result.line}{" "}
              <span className="muted">
                ({review.result.counts.pass} passed, {review.result.counts.warn} to explain, {review.result.counts.fail} failed ·{" "}
                {new Date(review.created_at).toLocaleString()})
              </span>
            </p>
            <ul className="review-items">
              {review.result.items
                .filter((i) => showPassed || i.level !== "pass")
                .map((i, k) => (
                  <li key={k} className="small">
                    <span className={`lvl ${i.level}`}>{i.level}</span>
                    <span>{i.text}</span>
                  </li>
                ))}
            </ul>
            <button className="link small" onClick={() => setShowPassed((v) => !v)}>
              {showPassed ? "Hide passed checks" : `Show ${review.result.counts.pass} passed checks`}
            </button>
          </>
        )}
      </div>
      <form
        className="note-form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!note.trim()) return;
          await api.addNote(caseId, note);
          setNote("");
          setTick((t) => t + 1);
        }}
      >
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Add a case note" />
        <button>Add</button>
      </form>
      <ol className="audit-list" reversed>
        {data.entries
          .slice()
          .reverse()
          .map((e) => (
            <li key={e.seq} className={e.actor_kind}>
              <div>
                <b>{e.action}</b> <span className="muted small">#{e.seq} · {new Date(e.ts).toLocaleString()}</span>
              </div>
              <div className="small">
                {e.actor}
                {e.actor_kind === "agent" && <span className="badge warn">agent</span>}
                {describe(e)}
              </div>
              {e.output_hash && (
                <div className="hash small" title={e.output_hash}>
                  {e.output_hash.slice(0, 12)}…
                  <button className="link small" onClick={() => navigator.clipboard?.writeText(e.output_hash!)} title="Copy hash">
                    copy
                  </button>
                </div>
              )}
            </li>
          ))}
      </ol>
    </div>
  );
}

function describe(e: AuditEntry): string {
  const p = e.params as Record<string, unknown>;
  switch (e.action) {
    case "evidence.ingest":
      return ` · ${p.filename} (${Number(p.size).toLocaleString()} bytes)`;
    case "chain.update":
      return ` · ${(p.changes as string[] | undefined)?.join(", ") ?? ""}`;
    case "chain.verify":
      return p.reproduced ? " · reproduced" : " · DID NOT REPRODUCE";
    case "analysis.run":
      return ` · ${p.analysis}`;
    case "case.note":
      return ` · ${p.text}`;
    case "evidence.remove":
      return ` · ${p.filename}${p.reason ? `: ${p.reason}` : ""}`;
    case "evidence.restore":
      return ` · ${p.filename}`;
    case "chain.remove":
      return ` · ${p.name}${p.reason ? `: ${p.reason}` : ""}`;
    case "evidence.verify":
      return p.ok ? " · hash matches" : " · HASH MISMATCH";
    case "approval.requested":
      return ` · ${p.reason}`;
    case "review.run":
      return ` · ${String(p.level).toUpperCase()}: ${p.line}`;
    case "chain.generative_justification":
      return ` · ${p.node}: ${p.justification}`;
    case "chain.export":
      return p.watermarked ? " · watermarked (illustrative only)" : "";
    default:
      return "";
  }
}
