"""Still-image traffic report (HTML and PDF): case, cameras and their settings, method and models,
per-image results with the image and its boxes, training history, charts asked for in words, and
limitations. Built fresh from the database at request time; the request itself is audited."""

from __future__ import annotations

import base64
import html
import io

from .. import APP_NAME, __version__
from ..audit import Actor
from ..db import now_iso
from . import METHOD_NOTES, VERSION, detectors
from .charts import chart

PALETTE = ("#2f6db5", "#e08a1e", "#3a9a5b", "#c44e52", "#8172b2", "#7f7f7f", "#b5a22f", "#1fa3b5")
MAX_PICTURES = 60

LIMITATIONS = [
    "Detections are proposals from automatic detectors. Only images with status 'approved' were checked by an examiner, "
    "and only for the vehicles visible to them in the regions shown.",
    "The learned detector (YOLOX, trained on COCO photographs) was not trained on traffic-camera imagery; small, distant, "
    "occluded, blurred and night-time vehicles are missed more often. Tiling helps distant vehicles but not unlit ones.",
    "At night a vehicle is usually seen only by its lamps; a pair of lamps is counted as one vehicle. Two vehicles close "
    "together, or one with a lamp out, can be counted wrongly.",
    "Background lights are learned from the camera's other night images and from false alarms the examiner deleted; a "
    "new light (for example a lit window) can be reported as a vehicle until it is learned.",
    "Weather indicators (haze/fog, snow) are image statistics, not observations of the weather.",
    "Training metrics are measured on the examiner's own reviewed images and are not a validation of the method.",
]


def build(wb, case_id: str, actor: Actor, charts: list[str] | None = None) -> dict:
    T = wb.traffic
    case = wb.get_case(case_id)
    images = T.images(case_id, actor)
    cams = T.cameras(case_id)
    det_rows = {im["evidence_id"]: T.detections(im["evidence_id"]) for im in images}
    runs = {}
    for im in images:
        if im["run_id"]:
            r = T.get_run(im["run_id"])
            runs[im["evidence_id"]] = {k: r[k] for k in ("id", "output_hash", "created_at", "profile_id", "models", "scene")}
    charts = charts or ["detections by hour and lighting as stacked", "images by status as pie"]
    chart_data = [chart(wb, case_id, q, actor) for q in charts if q.strip()]
    profiles = [p for c in cams for p in T.profiles(c["camera_id"])]
    wb.audit.append(actor, "traffic.report", case_id=case_id, target=case_id, params={"images": len(images), "charts": charts})
    return {"case": case, "generated_at": now_iso(), "generated_by": actor.name, "software": f"{APP_NAME} {__version__}, traffic {VERSION}",
            "cameras": cams, "images": images, "detections": det_rows, "runs": runs, "profiles": profiles,
            "charts": chart_data, "members": detectors.MEMBER_INFO, "method": METHOD_NOTES, "limitations": LIMITATIONS,
            "totals": {"images": len(images), "detections": sum(im["detection_count"] for im in images),
                       "approved": sum(1 for im in images if im["status"] == "approved"),
                       "pending": sum(1 for im in images if im["status"] == "pending")}}


# HTML -----------------------------------------------------------------------------------------------


def svg_chart(c: dict, width: int = 640, height: int = 280) -> str:
    xs, series = c["x"], c["series"]
    if not xs:
        return "<p class=muted>No data for this chart.</p>"
    if c["spec"]["type"] == "pie":
        total = sum(series[0]["values"]) or 1
        import math

        cx, cy, r, a0, parts, legend = 140, height / 2, 110, -math.pi / 2, [], []
        for i, (x, v) in enumerate(zip(xs, series[0]["values"])):
            a1 = a0 + 2 * math.pi * v / total
            large = 1 if a1 - a0 > math.pi else 0
            col = PALETTE[i % len(PALETTE)]
            if v == total:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}"/>')
            elif v:
                parts.append(f'<path d="M{cx},{cy} L{cx + r * math.cos(a0):.1f},{cy + r * math.sin(a0):.1f} A{r},{r} 0 {large} 1 '
                             f'{cx + r * math.cos(a1):.1f},{cy + r * math.sin(a1):.1f} Z" fill="{col}"/>')
            legend.append(f'<rect x="300" y="{30 + i * 20}" width="12" height="12" fill="{col}"/>'
                          f'<text x="318" y="{40 + i * 20}" font-size="12">{html.escape(x)}: {v}</text>')
            a0 = a1
        return f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img">{"".join(parts)}{"".join(legend)}</svg>'
    stacked = c["spec"]["type"] == "stacked"
    line = c["spec"]["type"] == "line"
    left, bottom, top, right = 44, 40, 14, 150
    pw, ph = width - left - right, height - top - bottom
    totals = [sum(s["values"][i] for s in series) for i in range(len(xs))] if stacked else \
        [max(s["values"][i] for s in series) for i in range(len(xs))]
    tick = max(1, -(-max(1, max(totals)) // 4))
    vmax = tick * 4
    out = [f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#888"/>',
           f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + ph}" stroke="#888"/>']
    for k in range(5):
        v = vmax * k / 4
        y = top + ph - ph * k / 4
        out.append(f'<text x="{left - 4}" y="{y + 4:.1f}" font-size="10" text-anchor="end">{v:.0f}</text>'
                   f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" stroke="#eee"/>')
    n, m = len(xs), len(series)
    gw = pw / n
    step = max(1, n // 16)
    for i, x in enumerate(xs):
        if i % step == 0:
            out.append(f'<text x="{left + gw * (i + 0.5):.1f}" y="{top + ph + 14}" font-size="10" text-anchor="middle">{html.escape(x)}</text>')
    if line:
        for j, s in enumerate(series):
            pts = " ".join(f"{left + gw * (i + 0.5):.1f},{top + ph - ph * v / vmax:.1f}" for i, v in enumerate(s["values"]))
            out.append(f'<polyline points="{pts}" fill="none" stroke="{PALETTE[j % len(PALETTE)]}" stroke-width="2"/>')
    else:
        for i in range(n):
            base = 0.0
            for j, s in enumerate(series):
                v = s["values"][i]
                col = PALETTE[j % len(PALETTE)]
                if stacked:
                    h = ph * v / vmax
                    out.append(f'<rect x="{left + gw * i + gw * 0.15:.1f}" y="{top + ph - base - h:.1f}" width="{gw * 0.7:.1f}" '
                               f'height="{h:.1f}" fill="{col}"/>')
                    base += h
                else:
                    bw = gw * 0.8 / m
                    h = ph * v / vmax
                    out.append(f'<rect x="{left + gw * i + gw * 0.1 + bw * j:.1f}" y="{top + ph - h:.1f}" width="{bw:.1f}" '
                               f'height="{h:.1f}" fill="{col}"/>')
    for j, s in enumerate(series):
        out.append(f'<rect x="{left + pw + 12}" y="{top + j * 18}" width="12" height="12" fill="{PALETTE[j % len(PALETTE)]}"/>'
                   f'<text x="{left + pw + 30}" y="{top + 10 + j * 18}" font-size="11">{html.escape(s["name"])}</text>')
    out.append(f'<text x="{left + pw / 2}" y="{height - 6}" font-size="11" text-anchor="middle">{html.escape(c["x_label"])}</text>')
    return f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" aria-label="{html.escape(c["title"])}">{"".join(out)}</svg>'


def render_html(wb, case_id: str, actor: Actor, charts: list[str] | None = None) -> str:
    b = build(wb, case_id, actor, charts)
    e = html.escape
    c = b["case"]
    cams = {x["camera_id"]: x for x in b["cameras"]}
    parts = [f"<!doctype html><html><head><meta charset=utf-8><title>Traffic report: {e(c['name'])}</title><style>{CSS}</style></head><body>",
             f"<h1>Still-image traffic report</h1><p class=muted>{e(b['software'])} · generated {e(b['generated_at'])} by {e(b['generated_by'])}</p>",
             "<h2>Case</h2><table>",
             *(f"<tr><th>{k}</th><td>{e(str(c.get(f) or ''))}</td></tr>" for k, f in
               (("Name", "name"), ("Case number", "case_number"), ("Examiner", "examiner"), ("Legal authority", "legal_authority"),
                ("Description", "description"))),
             "</table>",
             f"<h2>Summary</h2><p>{b['totals']['images']} images, {b['totals']['detections']} vehicles counted, "
             f"{b['totals']['approved']} images approved by an examiner, {b['totals']['pending']} not yet processed.</p>",
             "<h2>Cameras</h2><table><tr><th>Camera</th><th>UTC offset (h)</th><th>Regions</th><th>Detector profile</th>"
             "<th>Backgrounds learned</th><th>Images by status</th></tr>"]
    for cam in b["cameras"]:
        p = cam["profile"]
        regs = ", ".join(f"{r['kind']}{(' ' + r['label']) if r['label'] else ''}" for r in cam["regions"]) or "whole image"
        parts.append(f"<tr><td>{e(cam['label'])}</td><td>{cam['utc_offset_hours']:+g}</td><td>{e(regs)}</td>"
                     f"<td>{'v' + str(p['version']) + ' (' + p['sha256'][:12] + ')' if p else 'default'}</td>"
                     f"<td>{', '.join(x['lighting'] + ' (' + str(x['images']) + ')' for x in cam['backgrounds']) or 'none'}</td>"
                     f"<td>{e(', '.join(f'{k} {v}' for k, v in sorted(cam['counts'].items())))}</td></tr>")
    parts.append("</table>")
    for ch in b["charts"]:
        parts.append(f"<h2>{e(ch['title'])}</h2><p class=muted>Asked for: “{e(ch['query'])}”. {e(ch['local_time_note'])}. "
                     f"{e(' '.join(ch['notes']))}</p>{svg_chart(ch)}")
    parts.append("<h2>Images</h2><table><tr><th>Image</th><th>Local time</th><th>Time source</th><th>Lighting</th>"
                 "<th>Vehicles</th><th>Status</th><th>SHA-256</th></tr>")
    for im in b["images"]:
        st = im["status"] + (f" by {im['approved_by']}" if im["approved_by"] else "")
        parts.append(f"<tr><td>{e(im['image_name'])}</td><td>{e(im['local_time'] or 'unknown')}</td><td>{e(im['time_source'])}</td>"
                     f"<td>{e(im['lighting'] or '')}{' (set by examiner)' if im['lighting_overridden'] else ''}</td>"
                     f"<td>{im['detection_count']}</td><td>{e(st)}</td><td class=hash>{e(im['sha256'] or '')[:16]}…</td></tr>")
    parts.append("</table><h2>Images with detections</h2><p class=muted>Orange boxes were proposed by the detectors, "
                 "yellow boxes were added by the examiner; green outlines are scan areas and red outlines masks. "
                 "These pictures are illustrations made from the evidence, not evidence.</p>")
    for im in b["images"][:MAX_PICTURES]:
        if im["status"] == "pending":
            continue
        png = wb.traffic.overlay(im["evidence_id"], 900)
        run = b["runs"].get(im["evidence_id"])
        dets = b["detections"][im["evidence_id"]]
        rows = "".join(f"<tr><td>{d['id']}</td><td>{e(d['cls'])}</td><td>{'' if d['score'] is None else f'{d["score"]:.2f}'}</td>"
                       f"<td>{d['origin']}{' (edited)' if d['edited'] else ''}</td><td>{e(', '.join(f'{k} {v:.2f}' for k, v in d['members'].items()))}</td></tr>"
                       for d in dets)
        parts.append(f"<div class=img><h3>{e(im['image_name'])} · {e(im['local_time'] or '')} · {im['detection_count']} vehicles · "
                     f"{e(im['status'])}</h3><img src='data:image/png;base64,{base64.b64encode(png).decode()}' alt=''>"
                     f"<table><tr><th>#</th><th>Class</th><th>Score</th><th>Origin</th><th>Members</th></tr>{rows}</table>"
                     + (f"<p class=muted>Run {run['id']} · output hash {run['output_hash'][:16]}… · camera "
                        f"{e(cams.get(im['camera_id'], {}).get('label', ''))}</p>" if run else "") + "</div>")
    if len(b["images"]) > MAX_PICTURES:
        parts.append(f"<p class=muted>Pictures shown for the first {MAX_PICTURES} images; all images are in the table and the Excel export.</p>")
    parts.append("<h2>Method</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in b["method"]) + "</ul><table><tr><th>Member</th><th>Version</th><th>Reference</th></tr>")
    for k, v in b["members"].items():
        parts.append(f"<tr><td>{e(v['label'])}</td><td>{v['version']}</td><td>{e(v['reference'])}</td></tr>")
    parts.append("</table>")
    models = {m["model"]: m for r in b["runs"].values() for m in r["models"]}
    if models:
        parts.append("<table><tr><th>Model</th><th>Weights SHA-256</th><th>Licence</th><th>Device</th></tr>" + "".join(
            f"<tr><td>{e(m['model'])}</td><td class=hash>{m['sha256']}</td><td>{e(m['licence'])}</td><td>{e(m['device'])}</td></tr>"
            for m in models.values()) + "</table>")
    if b["profiles"]:
        parts.append("<h2>Training from examiner corrections</h2><table><tr><th>Camera</th><th>Version</th><th>Decision</th><th>Method</th>"
                     "<th>Reviewed images</th><th>F1 before</th><th>F1 after (CV)</th><th>Unrecoverable misses</th></tr>")
        for p in b["profiles"]:
            m, t = p["metrics"], p["training"]
            parts.append(f"<tr><td>{e(cams.get(p['camera_id'], {}).get('label', ''))}</td><td>v{p['version']}</td><td>{p['status']}</td>"
                         f"<td>{e(t['method'])}</td><td>{len(t['images'])}</td><td>{m['baseline']['f1']:.3f}</td>"
                         f"<td>{m['cross_validated']['f1']:.3f}</td><td>{t['unrecoverable_misses']}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>Limitations</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in b["limitations"]) + "</ul></body></html>")
    return "".join(parts)


CSS = ("body{font:14px/1.45 system-ui,sans-serif;margin:24px auto;max-width:1000px;color:#1d232b;padding:0 16px}"
       "h1{font-size:24px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #d5dbe3;padding-bottom:4px}h3{font-size:14px}"
       "table{border-collapse:collapse;margin:8px 0;width:100%}th,td{border:1px solid #d5dbe3;padding:4px 6px;text-align:left;"
       "vertical-align:top;font-size:12px}th{background:#f1f4f8}.muted{color:#5b6573;font-size:12px}.hash{font-family:monospace;"
       "font-size:11px;word-break:break-all}.img img{max-width:100%;border:1px solid #d5dbe3}.img{page-break-inside:avoid}"
       "svg{max-width:100%;height:auto}")


# PDF ------------------------------------------------------------------------------------------------------


def render_pdf(wb, case_id: str, actor: Actor, charts: list[str] | None = None) -> bytes:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    b = build(wb, case_id, actor, charts)
    ss = getSampleStyleSheet()
    small = ss["BodyText"].clone("small", fontSize=8, leading=10)
    story = [Paragraph("Still-image traffic report", ss["Title"]),
             Paragraph(html.escape(f"{b['software']} · generated {b['generated_at']} by {b['generated_by']}"), small),
             Paragraph(html.escape(f"Case: {b['case']['name']}  {b['case'].get('case_number') or ''} · examiner {b['case']['examiner']}"), ss["BodyText"]),
             Paragraph(f"{b['totals']['images']} images, {b['totals']['detections']} vehicles counted, {b['totals']['approved']} approved, "
                       f"{b['totals']['pending']} not processed.", ss["BodyText"]), Spacer(1, 4 * mm)]
    ts = TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                     ("FONTSIZE", (0, 0), (-1, -1), 7), ("VALIGN", (0, 0), (-1, -1), "TOP")])
    for ch in b["charts"]:
        story.append(Paragraph(html.escape(ch["title"]), ss["Heading3"]))
        story.append(Paragraph(html.escape(f"Asked for: “{ch['query']}”. {ch['local_time_note']}. {' '.join(ch['notes'])}"), small))
        if ch["x"]:
            d = Drawing(170 * mm, 70 * mm)
            bc = VerticalBarChart()
            bc.x, bc.y, bc.width, bc.height = 10 * mm, 10 * mm, 120 * mm, 55 * mm
            bc.data = [s["values"] for s in ch["series"]]
            bc.categoryAxis.categoryNames = ch["x"]
            bc.categoryAxis.labels.fontSize = 6
            bc.valueAxis.labels.fontSize = 6
            bc.valueAxis.valueMin = 0
            if ch["spec"]["type"] == "stacked":
                bc.categoryAxis.style = "stacked"
            for i in range(len(ch["series"])):
                bc.bars[i].fillColor = colors.HexColor(PALETTE[i % len(PALETTE)])
            lg = Legend()
            lg.x, lg.y, lg.fontSize = 135 * mm, 60 * mm, 7
            lg.colorNamePairs = [(colors.HexColor(PALETTE[i % len(PALETTE)]), s["name"]) for i, s in enumerate(ch["series"])]
            d.add(bc)
            d.add(lg)
            story.append(d)
    story.append(Paragraph("Images", ss["Heading2"]))
    rows = [["Image", "Local time", "Lighting", "Vehicles", "Status", "SHA-256"]]
    for im in b["images"]:
        rows.append([Paragraph(html.escape(im["image_name"]), small), im["local_time"] or "unknown", im["lighting"] or "",
                     str(im["detection_count"]), im["status"] + (f" ({im['approved_by']})" if im["approved_by"] else ""),
                     (im["sha256"] or "")[:16]])
    t = Table(rows, repeatRows=1, colWidths=[45 * mm, 35 * mm, 18 * mm, 15 * mm, 30 * mm, 32 * mm])
    t.setStyle(ts)
    story.append(t)
    story.append(Paragraph("Images with detections (illustrations, not evidence)", ss["Heading2"]))
    for im in b["images"][:MAX_PICTURES]:
        if im["status"] == "pending":
            continue
        png = wb.traffic.overlay(im["evidence_id"], 1000)
        img = RLImage(io.BytesIO(png))
        scale = min(170 * mm / img.imageWidth, 110 * mm / img.imageHeight)
        img.drawWidth, img.drawHeight = img.imageWidth * scale, img.imageHeight * scale
        story += [Paragraph(html.escape(f"{im['image_name']} · {im['local_time'] or ''} · {im['detection_count']} vehicles · {im['status']}"),
                            ss["Heading4"]), img, Spacer(1, 3 * mm)]
    story.append(Paragraph("Method", ss["Heading2"]))
    story += [Paragraph(html.escape(x), small) for x in b["method"]]
    story += [Paragraph(html.escape(f"{v['label']} {v['version']}: {v['reference']}"), small) for v in b["members"].values()]
    if b["profiles"]:
        story.append(Paragraph("Training from examiner corrections", ss["Heading2"]))
        rows = [["Version", "Decision", "Images", "F1 before", "F1 after (CV)", "Unrecoverable misses"]]
        for p in b["profiles"]:
            rows.append([f"v{p['version']}", p["status"], str(len(p["training"]["images"])), f"{p['metrics']['baseline']['f1']:.3f}",
                         f"{p['metrics']['cross_validated']['f1']:.3f}", str(p["training"]["unrecoverable_misses"])])
        t = Table(rows)
        t.setStyle(ts)
        story.append(t)
    story.append(Paragraph("Limitations", ss["Heading2"]))
    story += [Paragraph("• " + html.escape(x), small) for x in b["limitations"]]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, title="Traffic report", leftMargin=15 * mm, rightMargin=15 * mm).build(story)
    return buf.getvalue()
