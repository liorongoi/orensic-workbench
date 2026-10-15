"""Report generator: machine-readable bundle, HTML and PDF.

The bundle is the source of truth; HTML and PDF are renderings of it. Every
chain is re-run from the original at report time and the report states whether
the result reproduced bit for bit.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json

from . import APP_NAME, __version__, media, policy
from .analysis import get_analysis
from .audit import Actor
from .db import canonical_json, now_iso
from .filters import get_filter
from .graph import frame_of
from .triage import RULES_VERSION, rules_table
from .validation import load_card
from .workbench import Workbench

STANDARDS = (
    "Prepared following SWGDE Best Practices for Image Authentication and for Forensic Video and Image "
    "Enhancement, and ENFSI Best Practice Manual for Forensic Image and Video Enhancement. "
    "Authentication results are expressed as 'consistent / inconsistent with', never as proof."
)


def build_bundle(wb: Workbench, case_id: str, actor: Actor, verify: bool = True) -> dict:
    case = wb.get_case(case_id)
    evidence = []
    for ev in wb.list_evidence(case_id):
        check = wb.verify_evidence(ev["id"], actor) if verify else None
        evidence.append({**ev, "integrity": check})
    chains = []
    for ch in wb.list_chains(case_id):
        steps = []
        for node in ch["graph"]["nodes"]:
            f = get_filter(node["filter"])
            steps.append({
                "id": node["id"],
                "filter": f.id,
                "name": f.name,
                "version": node["version"],
                "enabled": node["enabled"],
                "params": node["params"],
                "description": f.description,
                "reference": f.reference,
                "generative": f.generative,
                "ai": f.ai,
                "justification": node.get("justification") if f.generative else None,
                "proposed_by": node.get("proposed_by"),
                "accepted_by": node.get("accepted_by"),
                "note": node.get("note"),
            })
        pending = [n["id"] for n in ch["graph"]["nodes"] if n["enabled"] and n.get("proposed_by") and not n.get("accepted_by")]
        entry = {
            "pending_proposals": pending,
            "id": ch["id"],
            "name": ch["name"],
            "evidence_id": ch["evidence_id"],
            "source": ch["graph"]["source"],
            "graph_hash": ch["graph_hash"],
            "output_hash": ch["output_hash"],
            "graph": ch["graph"],
            "steps": steps,
            "policy": policy.status(ch["graph"]),
            "models": wb.chain_models(ch["graph"]),
        }
        if ch["output_hash"] and verify:
            entry["reproducibility"] = wb.verify_chain(ch["id"], actor)
        chains.append(entry)
    analyses = []
    for a in wb.list_analysis_results(case_id):
        spec = get_analysis(a["name"])
        analyses.append({
            "id": a["id"],
            "evidence_id": a["evidence_id"],
            "analysis": spec.id,
            "name": spec.name,
            "version": a["version"],
            "params": a["params"],
            "description": spec.description,
            "reference": spec.reference,
            "reading_guide": spec.reading_guide,
            "summary": a["result"]["summary"],
            "flags": a["result"]["flags"],
            "maps": a["result"]["maps"],
            "findings": a["result"]["findings"],
            "output_hash": a["output_hash"],
            "actor": a["actor"],
            "created_at": a["created_at"],
            "validation": card_summary(spec.id),
            "ai": spec.ai,
        })
    audit_check = wb.audit.verify()
    bundle = {
        "report_schema": 1,
        "generated_at": now_iso(),
        "generated_by": actor.label,
        "environment": wb.environment(),
        "standards": STANDARDS,
        "case": case,
        "evidence": evidence,
        "removed_evidence": wb.list_evidence(case_id, removed=True),
        "chains": chains,
        "analyses": analyses,
        "camera_references": wb.list_camera_references(case_id),
        "measurements": wb.list_measurements(case_id),
        "triage": wb.list_triage(case_id),
        "triage_rules": {"version": RULES_VERSION, "rules": rules_table()},
        "review": wb.latest_review(case_id),
        "audit": {"verification": audit_check, "entries": wb.audit_entries(case_id)},
    }
    digest = hashlib.sha256(canonical_json(bundle).encode()).hexdigest()
    wb.audit.append(actor, "report.generate", case_id=case_id, target=case_id, params={"verified": verify}, output_hash=digest)
    bundle["bundle_sha256"] = digest
    return bundle


def card_summary(detector_id: str) -> str | None:
    """One sentence from the detector card: what accuracy was measured, on what."""
    card = load_card(detector_id)
    if not card:
        return None
    res = card["results"]
    main = res.get("detection") or res.get("identification", {}).get("full_frame") or {}
    if "true_positive_rate" not in main:
        return None
    thr = main.get("threshold", res.get("identification", {}).get("threshold"))
    return (f"Validated {card['validated_on']} on {card['corpus']}: true-positive rate {main['true_positive_rate']:.0%}, "
            f"false-positive rate {main['false_positive_rate']:.1%}" + (f" at threshold {thr}" if thr is not None else "")
            + ". Known limits: " + " ".join(card["known_failure_modes"][:2]))


def conversion_text(ev: dict, names: dict) -> str | None:
    conv = ev.get("conversion")
    if not conv:
        return None
    src = names.get(ev.get("derived_from"), ev.get("derived_from"))
    text = (f"Working copy made from {src} (SHA-256 {conv.get('source_sha256')}) by {conv['method']}: {conv['description']}"
            + (f" Command: {conv['command']}." if conv.get("command") else "")
            + (_carve_text(conv) if conv.get("carve") else "")
            + (_parse_text(conv) if conv.get("timing") else "")
            + f" {conv.get('ffmpeg', '')}.")
    return text


def _carve_text(conv: dict) -> str:
    c = conv["carve"]
    kept = (f"{c['jpeg_frames_kept']} JPEG frames" if "jpeg_frames_kept" in c
            else f"{c['nal_units_kept']} units and dropped {c['nal_units_dropped']}")
    return f" Carving kept {kept}; frame times are synthetic at {conv['frame_rate']:g} fps."


def _parse_text(conv: dict) -> str:
    t = conv["timing"]
    text = (f" Parser {conv.get('parser')} read {t['frames']} {str(conv.get('codec', '')).upper()} frames"
            f" ({t['key_frames']} key frames) with timing from the {t['source']};"
            f" median frame interval {t['median_frame_interval_ms']} ms.")
    if t.get("recorder_clock_start"):
        text += f" Recorder clock {t['recorder_clock_start']} to {t['recorder_clock_end']} (precision {t['recorder_clock_precision']})."
    text += f" Timing gaps: {t['gap_count']}; timestamp anomalies: {t['timestamp_anomaly_count']}."
    for note in conv.get("notes") or []:
        text += f" Note: {note}."
    return text


def measurement_rows(bundle: dict) -> list[dict]:
    ev_by_id = {x["id"]: x for x in bundle["evidence"]}
    rows = []
    for m in bundle["measurements"]:
        r = m["result"]
        lo, hi = r["interval_95"]
        value = f"{r['value']:.3f} {r['unit']} (95 % interval {lo:.3f} to {hi:.3f})"
        if r.get("value_kmh") is not None:
            klo, khi = r["interval_95_kmh"]
            value += f" = {r['value_kmh']:.1f} km/h ({klo:.1f} to {khi:.1f})"
        rows.append({"label": m["label"] or m["kind"], "file": ev_by_id.get(m["evidence_id"], {}).get("filename", "?"),
                     "value": value, "method": f"{r['name']} v{r['version']}", "reference": r["reference"],
                     "assumptions": r["assumptions"], "on": f"output of chain {m['chain_id']}" if m["chain_id"] else "original frame",
                     "actor": m["actor"], "hash": m["output_hash"], "warning": r.get("warning")})
    return rows


def generative_banner(ch: dict) -> list[tuple[str, str]]:
    """(css class, text) lines that must appear above a chain with generative steps."""
    pol = ch.get("policy") or {}
    if not pol.get("illustrative_only"):
        return []
    lines = [("bad", "ILLUSTRATIVE ONLY: this chain contains a generative AI step that can add detail the camera never "
              "recorded. Its result is watermarked, was not measured on, and must not be read as showing what was recorded.")]
    if pol.get("unjustified_steps"):
        lines.append(("bad", "NOT JUSTIFIED: step(s) " + ", ".join(pol["unjustified_steps"]) + " have no examiner "
                      "justification; the result is excluded from evidential export."))
    else:
        lines.append(("muted", "The examiner recorded a justification for each generative step (below); the "
                      "watermarked result may be exported."))
    return lines


# Images ---------------------------------------------------------------------------


def _jpeg_b64(img) -> str:
    return base64.b64encode(media.encode_jpeg_preview(img, max_side=900, quality=88)).decode()


def _derived_png(wb: Workbench, digest: str):
    path = wb.store.derived_path(digest, "png")
    return media.decode_image(path) if path.exists() else None


def _before_after(wb: Workbench, chain: dict):
    src = chain["source"]
    fr = (src.get("frames") or {}).get("start", 0)
    try:
        before = wb.source_frame(chain["evidence_id"], fr)
        after = frame_of(wb.engine.run(chain["graph"]).data) if chain["output_hash"] else None
        if after is not None and policy.generative_nodes(chain["graph"]):
            after = policy.watermark(after)
    except Exception:  # noqa: BLE001 - a missing image must not stop the report
        return None, None
    return before, after


# HTML -------------------------------------------------------------------------------

CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d232b;max-width:960px;margin:32px auto;padding:0 20px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid #d5dbe3;padding-bottom:4px}
h3{font-size:15px;margin:20px 0 6px}code,.hash{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border:1px solid #d5dbe3;padding:4px 8px;text-align:left;vertical-align:top;font-size:13px}
th{background:#f2f4f7}.ai{color:#5b3fb5;font-size:12px}.ok{color:#11733b;font-weight:600}.bad{color:#b42318;font-weight:600}.muted{color:#5b6573}
.flag-warning{color:#9a5b00}.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}.pair img,.map img{max-width:100%;border:1px solid #d5dbe3}
figure{margin:0}figcaption{font-size:12px;color:#5b6573}
.brand{display:flex;align-items:center;gap:8px;margin:0 0 16px;color:#5b6573;font-size:13px}.brand svg{width:28px;height:28px}
.brand b{font:italic 400 20px/1 Georgia,"Times New Roman",serif;color:#0d201c}
"""

# The Lucida mark (brand/lucida-icon.svg), inlined so the HTML report stays a single file.
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true">'
    '<rect width="64" height="64" rx="14" fill="#0D201C"/><polygon points="12,12 52,12 12,52" fill="#8FE3C8"/>'
    '<polygon points="52,12 52,52 12,52" fill="none" stroke="#8FE3C8" stroke-width="2.5" stroke-linejoin="round"/>'
    '<path d="M22 42 A14 14 0 0 1 42 22" fill="none" stroke="#0D201C" stroke-width="3"/>'
    '<path d="M42 22 A14 14 0 0 1 22 42" fill="none" stroke="#8FE3C8" stroke-width="3"/>'
    '<circle cx="32" cy="32" r="3.5" fill="#0D201C" stroke="#8FE3C8" stroke-width="2"/></svg>'
)



def render_html(wb: Workbench, bundle: dict) -> str:
    e = html.escape
    case = bundle["case"]
    ev_by_id = {x["id"]: x for x in bundle["evidence"]}
    names = {x["id"]: x["filename"] for x in bundle["evidence"]}
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Report: {e(case['name'])}</title><style>{CSS}</style></head><body>"]
    out.append(f"<div class='brand'>{LOGO_SVG}<b>{APP_NAME}</b><span>{e(bundle['environment'].get('app_version', __version__))}</span></div>")
    out.append(f"<h1>Examination report: {e(case['name'])}</h1>")
    out.append(f"<p class='muted'>Examiner {e(case['examiner'])} · generated {e(bundle['generated_at'])} by {e(bundle['generated_by'])}</p>")
    out.append(f"<p>{e(bundle['standards'])}</p>")

    out.append("<h2>1. Evidence received</h2><table><tr><th>File</th><th>Type</th><th>SHA-256 / MD5</th><th>Integrity at report time</th></tr>")
    for ev in bundle["evidence"]:
        chk = ev.get("integrity")
        status = "not checked" if not chk else ("<span class='ok'>hash matches, stored read-only</span>" if chk["ok"] and chk["read_only"] else "<span class='bad'>FAILED</span>")
        dims = f"{ev['probe'].get('width')}x{ev['probe'].get('height')}"
        extra = f", {ev['probe'].get('frame_count')} frames, {e(str(ev['probe'].get('codec')))}" if ev["media_type"] == "video" else ""
        conv = conversion_text(ev, names)
        conv_html = f"<br><span class='muted'>{e(conv)}</span>" if conv else ""
        out.append(
            f"<tr><td>{e(ev['filename'])}<br><span class='muted'>{ev['size']:,} bytes · ingested {e(ev['ingested_at'])} by {e(ev['ingested_by'])}</span>{conv_html}</td>"
            f"<td>{e(ev['media_type'])} ({dims}{extra})</td><td class='hash'>{ev['sha256']}<br>{ev['md5']}</td><td>{status}</td></tr>"
        )
    out.append("</table>")
    if bundle.get("removed_evidence"):
        out.append("<p class='muted'>Removed from the case by an examiner (originals kept in the store; see audit trail): "
                   + "; ".join(f"{e(r['filename'])} (SHA-256 {r['sha256'][:16]}…, {e(r['removed']['by'])}"
                               + (f": {e(r['removed']['reason'])}" if r["removed"]["reason"] else "") + ")"
                               for r in bundle["removed_evidence"]) + "</p>")

    out.append("<h2>2. Enhancement</h2>")
    if not bundle["chains"]:
        out.append("<p class='muted'>No enhancement chains.</p>")
    for i, ch in enumerate(bundle["chains"], 1):
        ev = ev_by_id.get(ch["evidence_id"], {})
        fr = ch["source"].get("frames")
        frames = f", frames {fr['start']}–{fr['end']}" if fr else ""
        out.append(f"<h3>2.{i} {e(ch['name'])} <span class='muted'>on {e(ev.get('filename', '?'))}{frames}</span></h3>")
        if ch["pending_proposals"]:
            out.append("<p class='bad'>This chain contains agent-proposed steps an examiner has not accepted. It is shown for transparency and is not part of the findings.</p>")
        out.extend(f"<p class='{cls}'>{e(t)}</p>" for cls, t in generative_banner(ch))
        rep = ch.get("reproducibility")
        if rep:
            out.append(f"<p>Re-run from the original at report time: " + ("<span class='ok'>reproduced bit for bit</span>" if rep["reproduced"] else "<span class='bad'>DID NOT REPRODUCE</span>") + "</p>")
        out.append(f"<p class='hash'>Chain hash {ch['graph_hash']}<br>Output pixel hash {ch['output_hash'] or 'not rendered'}</p>")
        out.append("<table><tr><th>#</th><th>Step</th><th>Parameters</th><th>What it does / reference</th></tr>")
        for n, st in enumerate(ch["steps"], 1):
            params = "<br>".join(f"{e(k)} = {e(json.dumps(v))}" for k, v in st["params"].items()) or "–"
            tag = "" if st["enabled"] else " <span class='muted'>(disabled)</span>"
            if st.get("proposed_by"):
                status = f"accepted by {e(st['accepted_by'])}" if st.get("accepted_by") else "<span class='bad'>NOT ACCEPTED</span>"
                tag += f" <span class='muted'>(proposed by {e(st['proposed_by'])}; {status})</span>"
            note = f"<br><em>Examiner note: {e(st['note'])}</em>" if st.get("note") else ""
            if st.get("ai"):
                tag += " <b class='ai'>AI-assisted</b>" + (" <b class='bad'>generative</b>" if st["generative"] else "")
            if st.get("generative"):
                note += (f"<br><em>Examiner's justification: {e(st['justification'])}</em>" if st.get("justification")
                         else "<br><span class='bad'>No justification recorded.</span>")
            for m in ch["models"]:
                if m["node"] == st["id"]:
                    note += f"<br><span class='muted'>Model {e(m['model'])} · weights SHA-256 <span class='hash'>{m['sha256']}</span> · {e(m['licence'])}</span>"
            out.append(f"<tr><td>{n}</td><td>{e(st['name'])}<br><span class='muted'>{e(st['filter'])} v{e(st['version'])}</span>{tag}</td><td>{params}</td><td>{e(st['description'])}<br><span class='muted'>{e(st['reference'])}</span>{note}</td></tr>")
        out.append("</table>")
        before, after = _before_after(wb, ch)
        if before is not None:
            out.append("<div class='pair'>")
            out.append(f"<figure><img src='data:image/jpeg;base64,{_jpeg_b64(before)}'><figcaption>Original (decoded, unprocessed)</figcaption></figure>")
            if after is not None:
                cap = ("Result, ILLUSTRATIVE ONLY: contains AI-generated detail and is watermarked" if ch["policy"]["illustrative_only"]
                       else "Result (preview; the evidential output is the lossless PNG with the hash above)")
                out.append(f"<figure><img src='data:image/jpeg;base64,{_jpeg_b64(after)}'><figcaption>{cap}</figcaption></figure>")
            out.append("</div>")

    out.append("<h2>3. Authentication analyses</h2>")
    if not bundle["analyses"]:
        out.append("<p class='muted'>No analyses run.</p>")
    for i, a in enumerate(bundle["analyses"], 1):
        ev = ev_by_id.get(a["evidence_id"], {})
        ai = " <b class='ai'>AI-assisted</b>" if a.get("ai") else ""
        out.append(f"<h3>3.{i} {e(a['name'])}{ai} <span class='muted'>on {e(ev.get('filename', '?'))}</span></h3>")
        out.append(f"<p><strong>{e(a['summary'])}</strong></p>")
        if a["flags"]:
            out.append("<ul>" + "".join(f"<li class='flag-{e(f['level'])}'>{e(f['text'])}</li>" for f in a["flags"]) + "</ul>")
        params = ", ".join(f"{k} = {json.dumps(v)}" for k, v in a["params"].items()) or "defaults"
        out.append(f"<p class='muted'>{e(a['analysis'])} v{e(a['version'])} · {e(params)} · run by {e(a['actor'])}<br>{e(a['description'])}<br>Reference: {e(a['reference'])}</p>")
        if a["reading_guide"]:
            out.append(f"<p><em>How to read this: {e(a['reading_guide'])}</em></p>")
        if a.get("validation"):
            out.append(f"<p class='muted'>{e(a['validation'])}</p>")
        for name, digest in a["maps"].items():
            img = _derived_png(wb, digest)
            if img is not None:
                out.append(f"<figure class='map'><img src='data:image/jpeg;base64,{_jpeg_b64(img)}'><figcaption>{e(name)} · <span class='hash'>{digest}</span></figcaption></figure>")

    if bundle["camera_references"]:
        out.append("<h3>Camera references (PRNU)</h3><table><tr><th>Name</th><th>Built from</th><th>Size</th><th>Fingerprint SHA-256</th></tr>")
        for r in bundle["camera_references"]:
            src = ", ".join(e(names.get(x, x)) for x in r["evidence"])
            out.append(f"<tr><td>{e(r['name'])}<br><span class='muted'>{e(r['camera'])} · by {e(r['actor'])}</span></td><td>{src}</td>"
                       f"<td>{r['width']}x{r['height']}</td><td class='hash'>{r['fingerprint_sha256']}</td></tr>")
        out.append("</table>")

    out.append("<h2>4. Measurements</h2>")
    rows = measurement_rows(bundle)
    if not rows:
        out.append("<p class='muted'>No measurements.</p>")
    for i, m in enumerate(rows, 1):
        out.append(f"<h3>4.{i} {e(m['label'])} <span class='muted'>on {e(m['file'])}, {e(m['on'])}</span></h3>")
        out.append(f"<p><strong>{e(m['value'])}</strong></p>")
        if m["warning"]:
            out.append(f"<p class='bad'>{e(m['warning'])}</p>")
        out.append("<p>Assumptions:</p><ul>" + "".join(f"<li>{e(a)}</li>" for a in m["assumptions"]) + "</ul>")
        out.append(f"<p class='muted'>{e(m['method'])} · by {e(m['actor'])} · Reference: {e(m['reference'])}<br>"
                   f"Interval from Monte Carlo propagation of point and survey uncertainty (seeded). <span class='hash'>{m['hash']}</span></p>")

    out.append("<h2>5. Triage (Smart Report)</h2>")
    if not bundle["triage"]:
        out.append("<p class='muted'>Triage not run.</p>")
    else:
        out.append("<p>Triage sorts items by where to look first. A colour is not a finding: green means no check in the battery "
                   f"found an inconsistency, not that the item is authentic. Rules v{e(bundle['triage_rules']['version'])} are listed below.</p>")
        out.append("<table><tr><th>File</th><th>Level</th><th>Why</th><th>By</th></tr>")
        for t in bundle["triage"]:
            out.append(f"<tr><td>{e(names.get(t['evidence_id'], '?'))}</td><td><b>{e(t['level'].upper())}</b></td>"
                       f"<td>{e(t['result']['line'])}</td><td>{e(t['actor'])}</td></tr>")
        out.append("</table><details><summary>Triage rules</summary><table><tr><th>Code</th><th>Level</th><th>Reason</th></tr>"
                   + "".join(f"<tr><td>{e(r['code'])}</td><td>{e(r['level'])}</td><td>{e(r['reason'])}</td></tr>" for r in bundle["triage_rules"]["rules"])
                   + "</table></details>")

    out.append("<h2>6. Independent review</h2>")
    rv = bundle.get("review")
    if not rv:
        out.append("<p class='muted'>The reviewer agent has not been run on this case.</p>")
    else:
        r = rv["result"]
        cls = {"pass": "ok", "warn": "flag-warning", "fail": "bad"}[rv["level"]]
        out.append(f"<p><b class='{cls}'>{e(rv['level'].upper())}</b>: {e(r['line'])} <span class='muted'>Reviewer v{e(r['reviewer_version'])}, "
                   f"run {e(rv['created_at'])} by {e(rv['actor'])}; it re-ran the case from the originals and compared hashes.</span></p>")
        out.append("<table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>")
        for it in r["items"]:
            c = {"pass": "ok", "warn": "flag-warning", "fail": "bad"}[it["level"]]
            out.append(f"<tr><td>{e(it['check'])}</td><td class='{c}'>{e(it['level'])}</td><td>{e(it['text'])}</td></tr>")
        out.append("</table>")

    out.append("<h2>7. Audit trail</h2>")
    av = bundle["audit"]["verification"]
    out.append("<p>Audit log hash chain: " + (f"<span class='ok'>intact</span> ({av['entries']} entries in the log)" if av["ok"] else f"<span class='bad'>BROKEN at entry {av['broken_at']}</span>") + "</p>")
    out.append("<table><tr><th>#</th><th>Time (UTC)</th><th>Who</th><th>Action</th><th>Output hash</th></tr>")
    for en in bundle["audit"]["entries"]:
        who = f"{en['actor']}" + (" (agent)" if en["actor_kind"] == "agent" else "")
        out.append(f"<tr><td>{en['seq']}</td><td>{e(en['ts'])}</td><td>{e(who)}</td><td>{e(en['action'])}</td><td class='hash'>{(en['output_hash'] or '')[:16]}</td></tr>")
    out.append("</table>")

    env = bundle["environment"]
    out.append("<h2>8. Software environment</h2><table>" + "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in env.items()) + "</table>")
    out.append(f"<p class='hash'>Report bundle SHA-256 {bundle['bundle_sha256']}</p></body></html>")
    return "".join(out)


# PDF ----------------------------------------------------------------------------------


def render_pdf(wb: Workbench, bundle: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#5b6573"))
    mono = ParagraphStyle("mono", parent=small, fontName="Courier")
    h1, h2, h3 = ss["Title"], ss["Heading2"], ss["Heading3"]
    e = html.escape
    width = A4[0] - 30 * mm

    def table(rows, cols):
        t = Table([[Paragraph(str(c), body if r else small) for c in row] for r, row in enumerate(rows)], colWidths=cols, repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dbe3")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f4f7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    def image(img, max_w):
        data = media.encode_jpeg_preview(img, max_side=1200, quality=88)
        h, w = img.shape[:2]
        scale = min(max_w / w, 90 * mm / h)
        return RLImage(io.BytesIO(data), width=w * scale, height=h * scale)

    case = bundle["case"]
    ev_by_id = {x["id"]: x for x in bundle["evidence"]}
    names = {x["id"]: x["filename"] for x in bundle["evidence"]}
    s = [Paragraph(f"Examination report: {e(case['name'])}", h1),
         Paragraph(f"Examiner {e(case['examiner'])} · generated {e(bundle['generated_at'])}", small), Spacer(1, 4),
         Paragraph(e(bundle["standards"]), body)]

    s.append(Paragraph("1. Evidence received", h2))
    rows = [["File", "Type", "SHA-256 / MD5", "Integrity"]]
    for ev in bundle["evidence"]:
        chk = ev.get("integrity")
        status = "not checked" if not chk else ("hash matches, read-only" if chk["ok"] and chk["read_only"] else "<b>FAILED</b>")
        conv = conversion_text(ev, names)
        conv_txt = f"<br/><font size='6.5' color='#5b6573'>{e(conv)}</font>" if conv else ""
        rows.append([f"{e(ev['filename'])}<br/>{ev['size']:,} bytes{conv_txt}", e(ev["media_type"]), f"<font name='Courier' size='6.5'>{ev['sha256']}<br/>{ev['md5']}</font>", status])
    s.append(table(rows, [0.25 * width, 0.12 * width, 0.45 * width, 0.18 * width]))
    if bundle.get("removed_evidence"):
        s.append(Paragraph("Removed from the case by an examiner (originals kept in the store; see audit trail): "
                           + "; ".join(f"{e(r['filename'])} (SHA-256 {r['sha256'][:16]}…, {e(r['removed']['by'])}"
                                       + (f": {e(r['removed']['reason'])}" if r["removed"]["reason"] else "") + ")"
                                       for r in bundle["removed_evidence"]), small))

    s.append(Paragraph("2. Enhancement", h2))
    for i, ch in enumerate(bundle["chains"], 1):
        ev = ev_by_id.get(ch["evidence_id"], {})
        s.append(Paragraph(f"2.{i} {e(ch['name'])} — {e(ev.get('filename', '?'))}", h3))
        if ch["pending_proposals"]:
            s.append(Paragraph("<b>Contains agent-proposed steps not accepted by an examiner; not part of the findings.</b>", body))
        for cls, text in generative_banner(ch):
            s.append(Paragraph(f"<b>{e(text)}</b>" if cls == "bad" else e(text), body if cls == "bad" else small))
        rep = ch.get("reproducibility")
        if rep:
            s.append(Paragraph("Re-run from the original at report time: " + ("<b>reproduced bit for bit</b>" if rep["reproduced"] else "<b>DID NOT REPRODUCE</b>"), body))
        s.append(Paragraph(f"Chain hash {ch['graph_hash']}<br/>Output pixel hash {ch['output_hash'] or 'not rendered'}", mono))
        rows = [["#", "Step", "Parameters", "Description and reference"]]
        for n, st in enumerate(ch["steps"], 1):
            params = "<br/>".join(f"{e(k)} = {e(json.dumps(v))}" for k, v in st["params"].items()) or "–"
            tag = ""
            if st.get("ai"):
                tag = "<br/><font size='7' color='#5b3fb5'><b>AI-assisted" + (", generative" if st["generative"] else "") + "</b></font>"
            extra = ""
            if st.get("generative"):
                extra += (f"<br/><i>Examiner's justification: {e(st['justification'])}</i>" if st.get("justification")
                          else "<br/><b>No justification recorded.</b>")
            for m in ch["models"]:
                if m["node"] == st["id"]:
                    extra += f"<br/><font size='6.5' color='#5b6573'>Model {e(m['model'])} · weights SHA-256 <font name='Courier'>{m['sha256']}</font></font>"
            rows.append([str(n), f"{e(st['name'])}<br/><font size='7'>{e(st['filter'])} v{e(st['version'])}</font>{tag}", params,
                         f"{e(st['description'])}<br/><font size='7' color='#5b6573'>{e(st['reference'])}</font>{extra}"])
        s.append(table(rows, [0.05 * width, 0.2 * width, 0.25 * width, 0.5 * width]))
        before, after = _before_after(wb, ch)
        if before is not None and after is not None:
            s.append(Spacer(1, 4))
            pair = Table([[image(before, width / 2 - 6), image(after, width / 2 - 6)], [Paragraph("Original", small), Paragraph("Result, ILLUSTRATIVE ONLY (watermarked)" if ch["policy"]["illustrative_only"] else "Result", small)]], colWidths=[width / 2, width / 2])
            s.append(pair)

    s.append(Paragraph("3. Authentication analyses", h2))
    for i, a in enumerate(bundle["analyses"], 1):
        ev = ev_by_id.get(a["evidence_id"], {})
        s.append(Paragraph(f"3.{i} {e(a['name'])}" + (" (AI-assisted)" if a.get("ai") else "") + f" — {e(ev.get('filename', '?'))}", h3))
        s.append(Paragraph(f"<b>{e(a['summary'])}</b>", body))
        for f in a["flags"]:
            s.append(Paragraph(("<b>Warning:</b> " if f["level"] == "warning" else "• ") + e(f["text"]), body))
        s.append(Paragraph(f"{e(a['analysis'])} v{e(a['version'])} · {e(a['reference'])}", small))
        if a["reading_guide"]:
            s.append(Paragraph(f"<i>How to read this: {e(a['reading_guide'])}</i>", small))
        if a.get("validation"):
            s.append(Paragraph(e(a["validation"]), small))
        for name, digest in a["maps"].items():
            img = _derived_png(wb, digest)
            if img is not None:
                s.append(image(img, width * 0.7))
                s.append(Paragraph(f"{e(name)} · {digest}", mono))

    if bundle["camera_references"]:
        s.append(Paragraph("Camera references (PRNU)", h3))
        rows = [["Name", "Built from", "Size", "Fingerprint SHA-256"]]
        for r in bundle["camera_references"]:
            rows.append([f"{e(r['name'])}<br/>{e(r['camera'])}", ", ".join(e(names.get(x, x)) for x in r["evidence"]),
                         f"{r['width']}x{r['height']}", f"<font name='Courier' size='6.5'>{r['fingerprint_sha256']}</font>"])
        s.append(table(rows, [0.2 * width, 0.35 * width, 0.12 * width, 0.33 * width]))

    s.append(Paragraph("4. Measurements", h2))
    mrows = measurement_rows(bundle)
    if not mrows:
        s.append(Paragraph("No measurements.", small))
    for i, m in enumerate(mrows, 1):
        s.append(Paragraph(f"4.{i} {e(m['label'])} — {e(m['file'])}, {e(m['on'])}", h3))
        s.append(Paragraph(f"<b>{e(m['value'])}</b>", body))
        if m["warning"]:
            s.append(Paragraph(f"<b>{e(m['warning'])}</b>", body))
        for a in m["assumptions"]:
            s.append(Paragraph(f"• {e(a)}", small))
        s.append(Paragraph(f"{e(m['method'])} · {e(m['reference'])} · {m['hash']}", small))

    s.append(Paragraph("5. Triage (Smart Report)", h2))
    if bundle["triage"]:
        s.append(Paragraph("A colour is where to look first, not a finding: green means no check in the battery found an inconsistency.", small))
        rows = [["File", "Level", "Why"]]
        for t in bundle["triage"]:
            rows.append([e(names.get(t["evidence_id"], "?")), e(t["level"].upper()), e(t["result"]["line"])])
        s.append(table(rows, [0.25 * width, 0.1 * width, 0.65 * width]))
    else:
        s.append(Paragraph("Triage not run.", small))

    s.append(Paragraph("6. Independent review", h2))
    rv = bundle.get("review")
    if not rv:
        s.append(Paragraph("The reviewer agent has not been run on this case.", small))
    else:
        r = rv["result"]
        s.append(Paragraph(f"<b>{e(rv['level'].upper())}</b>: {e(r['line'])}", body))
        s.append(Paragraph(f"Reviewer v{e(r['reviewer_version'])}, run {e(rv['created_at'])}; re-ran the case from the originals and compared hashes.", small))
        s.append(table([["Check", "Result", "Detail"]] + [[e(it["check"]), e(it["level"]), e(it["text"])] for it in r["items"]],
                       [0.2 * width, 0.1 * width, 0.7 * width]))

    s.append(Paragraph("7. Audit trail", h2))
    av = bundle["audit"]["verification"]
    s.append(Paragraph("Audit log hash chain: " + ("intact" if av["ok"] else f"BROKEN at entry {av['broken_at']}"), body))
    rows = [["#", "Time (UTC)", "Who", "Action", "Output hash"]]
    for en in bundle["audit"]["entries"]:
        rows.append([str(en["seq"]), e(en["ts"]), e(en["actor"]) + (" (agent)" if en["actor_kind"] == "agent" else ""), e(en["action"]), f"<font name='Courier' size='6.5'>{(en['output_hash'] or '')[:16]}</font>"])
    s.append(table(rows, [0.06 * width, 0.27 * width, 0.22 * width, 0.2 * width, 0.25 * width]))

    s.append(Paragraph("8. Software environment", h2))
    s.append(table([["Component", "Version"]] + [[e(k), e(str(v))] for k, v in bundle["environment"].items()], [0.3 * width, 0.7 * width]))
    s.append(Spacer(1, 6))
    s.append(Paragraph(f"Report bundle SHA-256 {bundle['bundle_sha256']}", mono))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Report: {case['name']}", author=case["examiner"], creator=f"{APP_NAME} {__version__}")
    doc.build(s, onFirstPage=_footer(case), onLaterPages=_footer(case))
    return buf.getvalue()


def _footer(case):
    from reportlab.lib.units import mm

    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(15 * mm, 8 * mm, f"{APP_NAME} {__version__} · case {case['name']} · examiner {case['examiner']}")
        canvas.drawRightString(doc.pagesize[0] - 15 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    return draw
