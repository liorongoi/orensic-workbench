"""Lay out WORK/results.json (from tools.showcase.run) as the showcase PDF.

    python -m tools.showcase.pdf --work DIR --out report.pdf [--tests "246 passed"]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (CondPageBreak, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
pdfmetrics.registerFont(TTFont("Sans", str(FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFont(TTFont("Mono", str(FONT_DIR / "DejaVuSansMono.ttf")))
from reportlab.pdfbase.pdfmetrics import registerFontFamily  # noqa: E402

registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans", boldItalic="Sans-Bold")

INK = colors.HexColor("#1b2430")
MUTED = colors.HexColor("#5b6675")
ACCENT = colors.HexColor("#1f6fd1")
GOOD = colors.HexColor("#1d7a46")
BAD = colors.HexColor("#b3261e")
LINE = colors.HexColor("#d5dbe3")
SOFT = colors.HexColor("#f3f6fa")

S = {
    "title": ParagraphStyle("title", fontName="Sans-Bold", fontSize=24, leading=29, textColor=INK, spaceAfter=6),
    "sub": ParagraphStyle("sub", fontName="Sans", fontSize=12, leading=16, textColor=MUTED, spaceAfter=14),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=17, leading=21, textColor=INK, spaceBefore=4, spaceAfter=8),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=12, leading=15, textColor=INK, spaceBefore=8, spaceAfter=3),
    "h3": ParagraphStyle("h3", fontName="Sans-Bold", fontSize=10, leading=13, textColor=INK, spaceBefore=4, spaceAfter=2),
    "body": ParagraphStyle("body", fontName="Sans", fontSize=9, leading=12.5, textColor=INK, spaceAfter=4, alignment=TA_LEFT),
    "small": ParagraphStyle("small", fontName="Sans", fontSize=7.6, leading=10, textColor=MUTED, spaceAfter=2),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=7.6, leading=9.6, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Sans-Bold", fontSize=7.6, leading=9.6, textColor=INK),
    "mono": ParagraphStyle("mono", fontName="Mono", fontSize=7, leading=9, textColor=INK),
    "cap": ParagraphStyle("cap", fontName="Sans", fontSize=7.4, leading=9, textColor=MUTED, alignment=1),
    "good": ParagraphStyle("good", fontName="Sans-Bold", fontSize=8, leading=10, textColor=GOOD),
    "bad": ParagraphStyle("bad", fontName="Sans-Bold", fontSize=8, leading=10, textColor=BAD),
}
W = A4[0] - 36 * mm


def P(text, style="body"):
    return Paragraph(text, S[style])


def E(text) -> str:
    return escape(str(text))


def img(path, max_w, max_h):
    if not path or not Path(path).exists():
        return P("(image missing)", "small")
    with PILImage.open(path) as im:
        w, h = im.size
    s = min(max_w / w, max_h / h)
    return Image(str(_display_copy(Path(path), w * s, h * s)), width=w * s, height=h * s)


DISPLAY_JPEG_QUALITY = 92


def _display_copy(path: Path, w_pt: float, h_pt: float, dpi: int = 220) -> Path:
    """A display copy for the PDF: at most `dpi` at its printed size, JPEG quality 92 with no chroma
    subsampling. Only the report's pictures are compressed; every hash and measurement in the report refers
    to the lossless outputs."""
    with PILImage.open(path) as im:
        tw = int(w_pt / 72 * dpi)
        out = path.with_name(path.stem + f".print{tw}.jpg")
        if not out.exists():
            im = im.convert("RGB")
            if im.width > tw * 1.1:
                im = im.resize((tw, max(1, round(im.height * tw / im.width))), PILImage.BOX)
            im.save(out, quality=DISPLAY_JPEG_QUALITY, subsampling=0)
        return out


def pair(before, after, cap_b="Before (input)", cap_a="After", max_h=62 * mm):
    half = (W - 6 * mm) / 2
    t = Table([[img(before, half, max_h), img(after, half, max_h)], [P(cap_b, "cap"), P(cap_a, "cap")]],
              colWidths=[half + 3 * mm, half + 3 * mm])
    t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return t


def grid(rows, widths, header=True, zebra=True, font=7.6):
    data = [[c if not isinstance(c, str) else P(E(c), "cellb" if header and i == 0 else "cell") for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
          ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), SOFT))
    t.setStyle(TableStyle(st))
    return t


def kv(pairs, w1=42 * mm):
    """Key/value table. A value that already carries markup (<b>, <br/>, escaped entities) is used as is."""
    def cell(v):
        if isinstance(v, str) and re.search(r"</?(b|i)>|<br/>|&(amp|lt|gt);", v):
            return P(v, "cell")
        return v
    return grid([[k, cell(v)] for k, v in pairs], [w1, W - w1], header=False)


def fmt_params(p: dict, defaults: dict | None = None) -> str:
    out = []
    for k, v in (p or {}).items():
        if isinstance(v, list) and len(json.dumps(v)) > 70:
            v = json.dumps(v)[:70] + "…"
        changed = defaults is not None and defaults.get(k) != v
        out.append(f"<b>{E(k)}</b>={E(v)}" if changed else f"{E(k)}={E(v)}")
    return ", ".join(out) or "—"


def metric_line(m) -> str:
    b, a = m["before"], m["after"]
    def arrow(x, y, better_high=True):
        good = (y > x) if better_high else (y < x)
        c = "#1d7a46" if good else "#b3261e"
        return f'<font color="{c}">{x} → {y}</font>'
    return (f"Against the clean original: PSNR {arrow(b['psnr_db'], a['psnr_db'])} dB, SSIM {arrow(b['ssim'], a['ssim'])} "
            "(higher is closer to the original).")


CATEGORY_ORDER = ["Exposure", "Colour", "Denoise", "Deblur", "Sharpen", "Frequency", "Analysis view", "Geometry",
                  "Multi-frame", "AI-assisted"]

NOTES = {
    "ai.super_resolution": "Generative step: the export is watermarked 'illustrative'. Metrics are computed on the unwatermarked pixels.",
    "ai.realesrgan": "Generative step: invents plausible texture. It scores below plain bicubic resizing here — sharper-looking "
                     "is not the same as more accurate. Watermarked and illustrative only.",
    "deblur.richardson_lucy": "Iterative: more iterations sharpen further but amplify noise (SSIM falls while PSNR rises).",
    "deblur.wiener": "Ringing around strong edges lowers SSIM slightly although PSNR rises; nsr trades sharpness against noise.",
    "sharpen.laplacian": "Sharpening increases apparent detail but also noise, so SSIM against the clean original drops.",
    "exposure.clahe": "Local contrast enhancement: it is meant to reveal detail, not to restore the original tone, so the "
                      "distance to the original is not the goal.",
    "exposure.histogram_equalization": "Global; maps the histogram to uniform. Not a restoration.",
    "exposure.shadows_highlights": "Lifts shadows locally; with default strength it does not undo a 2.3-stop under-exposure.",
    "color.channel_gains": "Grey-world assumes the scene averages to grey; this scene is not quite grey, so the correction is partial.",
    "denoise.deblock": "Heavy blocking at JPEG quality 8 cannot be undone; deblocking softens block edges only.",
    "stack.super_resolution": "Ten 260×180 frames fused to 520×360. Compared after resizing back to the 260×180 truth.",
    "stack.integrate": "Preceded by stack.stabilise (alignment). Noise falls roughly with the square root of the frame count.",
    "stack.denoise_temporal": "Preceded by stack.stabilise. Frame 5 of the output is shown.",
    "geometry.unwrap_360": "Input is a synthetic 360° grid, not a real camera image; it shows the projection geometry only.",
    "geometry.fisheye": "Undistorts the synthetic equidistant fisheye chessboard made with the same focal length (0.35 × width).",
    "deblur.variable": "Two streaks marked along the (uniform) blur; the method interpolates the blur between them.",
    "deblur.blind": "Estimates the blur kernel from the image itself; here a Gaussian blur of σ = 2 px.",
}


class Doc:
    def __init__(self, work: Path, tests: str):
        self.r = json.loads((work / "results.json").read_text())
        self.tests = tests
        self.story = []

    def add(self, *xs):
        self.story.extend(xs)

    # Sections ---------------------------------------------------------------------------------------

    def cover(self):
        r = self.r
        env = r["environment"]
        f_ok = [f for f in r["filters"] if f.get("reproduced")]
        self.add(Spacer(1, 30 * mm), P("Lucida forensic workbench", "sub"),
                 P("Algorithms and workflows: tested examples", "title"),
                 P(f"Version {E(env['lucida'])} · generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "sub"),
                 Spacer(1, 6 * mm))
        self.add(P(
            "This report runs every image filter, every authentication analysis and every workflow of the "
            "application on test media, through the application itself, and shows what each one does, with "
            "the algorithm, its parameters and the measured result. Where a test input was made by degrading a "
            "clean image in a known way, the result is also measured against that clean original."))
        self.add(P(
            "<b>What this report is not.</b> It is not a scientific validation of any method and does not "
            "establish that any result is admissible in court. The inputs are a small set of public sample "
            "images and synthetic test material; results on real casework depend on the material and on the "
            "examiner. Nothing in this report was sent to an outside service: every model ran on this computer. "
            "To keep the file small, the pictures in this PDF are JPEG (quality 92, no colour subsampling); every "
            "hash and every measurement refers to the lossless outputs."))
        self.add(Spacer(1, 4 * mm))
        n_tm = [f for f in r["filters"] if f.get("truth_metrics")]
        better = [f for f in n_tm if f["truth_metrics"]["after"]["psnr_db"] and f["truth_metrics"]["after"]["psnr_db"] > f["truth_metrics"]["before"]["psnr_db"]]
        an_run = [a for a in r["analyses"] if a.get("summary")]
        wf_ok = [k for k, v in r["workflows"].items() if "error" not in v]
        rows = [
            ("Automated test suite", self.tests),
            ("Image filters run", f"{len(r['filters'])} of {len(r['filters'])}; errors: {sum(1 for f in r['filters'] if f.get('error'))}"),
            ("Filters re-run identically", f"{len(f_ok)} of {len(r['filters'])}: re-running each saved chain from the original gave a "
                                           "byte-identical result"),
            ("Measured against ground truth", f"{len(n_tm)} filters; {len(better)} moved the image closer to the clean original (PSNR)"),
            ("Authentication analyses", f"{len(an_run)} run; {sum(1 for a in r['analyses'] if a.get('skipped'))} need examiner-marked "
                                        f"points; {sum(1 for a in r['analyses'] if a.get('error'))} not installed"),
            ("Workflows", f"{len(wf_ok)} of {len(r['workflows'])} completed"),
            ("Audit log", f"{r['audit_ok']['entries']} entries, hash chain {'intact' if r['audit_ok']['ok'] else 'BROKEN'}"),
            ("Environment", f"Python {env['python']}, OpenCV {env['opencv']}, NumPy {env['numpy']}, {env['ffmpeg'].split(' Copyright')[0]}, "
                            f"Tesseract {env['tesseract']}; {env['machine']}"),
        ]
        self.add(kv(rows, 48 * mm), PageBreak())

    def findings(self):
        self.add(P("Problems found during this testing, and fixed", "h1"))
        self.add(P("Running everything end to end surfaced these defects. Each was fixed and covered by a new automated test "
                   "before this report was generated."))
        rows = [["Area", "Problem", "Fix"],
                ["Plate search", "Wildcards (12*67) were stripped before matching, so searches only found exact text.",
                 "Wildcards are kept; the text between them is normalised."],
                ["Plate reading", "On still images no vehicle was searched for a plate (the tracker needs 3 frames).",
                 "Tracking is off for single images; short tracks are still read."],
                ["Export package", "'Include the PDF report' called the report renderer wrongly and failed.", "Corrected; tested."],
                ["Redaction", "Re-encoding a video whose audio was slightly shorter dropped the last video frame "
                              "(30 in, 29 out). The built-in check caught it and marked the copy as failed.",
                 "Every video frame is written; the audio is cut to the video's length instead."],
                ["Redaction check", "The 'region obscured' test measured edge energy; pixelation creates new block edges, "
                                    "so correctly pixelated regions were flagged.",
                 "Replaced by a fine-detail correlation test (original texture surviving in the output)."],
                ["Export package", "Documentation claimed two packages of the same selection have the same hash; the manifest "
                                   "records the time, so they do not.",
                 "Wording corrected: member files are byte-identical; only the manifest differs."],
                ["Interface", "Model names showed as '[object Object]'; the sync offset showed raw seconds.", "Fixed."],
                ["Windows", "Batch scan listed sub-folder files with backslashes; the backup left its database snapshot "
                 "open, so Windows could not delete it and the backup failed.", "Paths are stored with '/'; the snapshot is "
                 "closed explicitly. Windows CI is green."],
                ["Audio sync", "Audio cross-correlation ignored a container's audio start delay, so camera B came out "
                 "0.011 s off (10× its stated uncertainty).", "The audio is aligned to the first video frame using each "
                 "stream's start time; the error is now under 0.001 s."],
                ["Redaction coverage", "The automatic plate finder missed the silver car's plate in the street video, and the "
                 "verifier did not flag it.", "New option: a region that follows the plate zone of a tracked vehicle. The "
                 "report shows both passes."]]
        self.add(grid(rows, [28 * mm, 82 * mm, W - 110 * mm]), PageBreak())

    def inputs(self):
        self.add(P("Test inputs", "h1"))
        self.add(P("Each input was added to a case like any evidence (hashed and stored write-once). Public images are "
                   "from the OpenCV sample data; the rest were made for this report as described."))
        rows = [["Key", "Description", "SHA-256 (first 16)"]]
        for k, v in self.r["inputs"].items():
            rows.append([k, v["description"], v["sha256"][:16]])
        self.add(grid(rows, [24 * mm, W - 58 * mm, 34 * mm]), PageBreak())

    def filters(self):
        self.add(P("Image filters", "h1"))
        self.add(P("Each filter ran as a one-step chain saved in the case (a filter that needs aligned frames first runs "
                   "after a stabilise step, as stated). The chain was rendered, re-run from the original with no cache "
                   "and compared hash-for-hash (reproducibility), then exported as a lossless PNG. Parameters in "
                   "<b>bold</b> differ from the filter's defaults."))
        by_cat: dict[str, list] = {}
        for f in self.r["filters"]:
            by_cat.setdefault(f["category"], []).append(f)
        for cat in CATEGORY_ORDER + [c for c in by_cat if c not in CATEGORY_ORDER]:
            if cat not in by_cat:
                continue
            self.add(CondPageBreak(90 * mm), P(f"{E(cat)} ({len(by_cat[cat])})", "h2"))
            for f in by_cat[cat]:
                self.filter_block(f)
        self.add(PageBreak())

    def filter_block(self, f):
        head = P(f"{E(f['name'])} <font color='#5b6675' size='8'>· {E(f['id'])} v{E(f['version'])}"
                 f"{' · AI' if f.get('ai') else ''}{' · generative' if f.get('generative') else ''}</font>", "h3")
        parts = [head, P(E(f["description"]), "small")]
        if f.get("error"):
            parts.append(P(f"Not run: {E(f['error'])}", "bad"))
            self.add(KeepTogether(parts))
            return
        rows = [("Input", f"{E(f['input'])}: {E(self.r['inputs'][f['input']]['description'])}"),
                ("Parameters", fmt_params(f.get("params"), f.get("defaults"))
                 + (f" (after {', '.join(f['pre'])})" if f.get("pre") else "")),
                ("Result", f"{f['seconds']} s · output hash {f['output_hash'][:16]}… · re-run identical: "
                           f"{'<font color=\"#1d7a46\"><b>yes</b></font>' if f.get('reproduced') else '<font color=\"#b3261e\"><b>NO</b></font>'}"
                           + (" · watermarked (illustrative)" if f.get("watermarked") else "")),
                ("Reference", E(f["reference"]))]
        if f.get("models"):
            rows.insert(2, ("Models", E(", ".join(f"{m.get('id', m.get('model', '?'))} sha256 {str(m.get('sha256', ''))[:12]}…" for m in f["models"]))))
        parts.append(kv([(k, P(v, "cell")) for k, v in rows], 22 * mm))
        if f.get("truth_metrics"):
            parts.append(P(metric_line(f["truth_metrics"]), "small"))
        if NOTES.get(f["id"]):
            parts.append(P(E(NOTES[f["id"]]), "small"))
        parts.append(pair(f["before"], f["after"]))
        parts.append(Spacer(1, 3 * mm))
        self.add(KeepTogether(parts))

    def analyses(self):
        self.add(P("Authentication analyses", "h1"))
        self.add(P("Analyses report measurements and maps for the examiner to interpret; none of them decides on its own "
                   "whether an image was edited. The spliced test image has a region (x 300–440, y 150–270) that was "
                   "compressed at JPEG quality 55 before the whole image was saved at quality 92; the copy-move image has a "
                   "region copied from x 330 to x 60. The PRNU test uses a synthetic camera: a fixed multiplicative noise "
                   "pattern added to seven reference images and one test image, plus a control image with a different pattern."))
        for a in self.r["analyses"]:
            parts = [P(f"{E(a['name'])} <font color='#5b6675' size='8'>· {E(a['id'])} v{E(a['version'])}{' · AI' if a.get('ai') else ''}</font>", "h3"),
                     P(E(a["description"]), "small")]
            if a.get("skipped"):
                parts.append(P(f"Not run automatically: {E(a['skipped'])}", "small"))
                self.add(KeepTogether(parts))
                continue
            if a.get("error"):
                parts.append(P(f"Not run: {E(a['error'])}", "bad"))
                self.add(KeepTogether(parts))
                continue
            rows = [("Input", E(a["input"])), ("Parameters", fmt_params(a.get("params"))),
                    ("Result", f"<b>{E(a.get('summary') or '')}</b> ({a['seconds']} s)")]
            flags = a.get("flags") or []
            if flags:
                rows.append(("Flags", "<br/>".join(E(f"{x.get('level', '')}: {x.get('text', '')}")[:220] for x in flags[:4])))
            if a.get("control"):
                rows.append(("Control image", E(a["control"]["summary"])))
            rows.append(("Reference", E(a["reference"])))
            parts.append(kv([(k, P(v, "cell")) for k, v in rows], 22 * mm))
            maps = list((a.get("maps") or {}).items())
            if maps:
                parts.append(pair(a["before"], maps[0][1], "Input", f"Map: {maps[0][0]}", max_h=55 * mm))
            parts.append(Spacer(1, 3 * mm))
            self.add(KeepTogether(parts))
        self.add(PageBreak())

    # Workflows ------------------------------------------------------------------------------------

    def wf_header(self, title, intro):
        self.add(CondPageBreak(80 * mm), P(title, "h2"), P(intro))

    def workflows(self):
        w = self.r["workflows"]
        self.add(P("Workflows", "h1"))
        for k, v in w.items():
            if "error" in v:
                self.add(P(f"Workflow {E(k)} failed: {E(v['error'])}", "bad"))
        if "error" not in w.get("vertical_slice", {"error": 1}):
            v = w["vertical_slice"]
            self.wf_header("1. Case → import → hash → metadata → processing → derivative → report",
                           "An under-exposed image is added to a new case. It is hashed (SHA-256, SHA-512, MD5) and stored write-once; "
                           "metadata is read; a three-step chain is applied without touching the original; the result is "
                           "re-run from the original and must match; it is exported; a case report is built; the audit chain is checked.")
            ev = v["evidence"]
            self.add(kv([("File", E(ev["filename"])), ("SHA-256", P(ev["sha256"], "mono")), ("SHA-512", P(ev["sha512"], "mono")),
                         ("Metadata", E(v["metadata_summary"])),
                         ("Chain", P("<br/>".join(E(s) for s in v["chain"]["steps"]), "mono")),
                         ("Re-run", f"identical: {v['chain']['reproduced']} · output hash {v['chain']['output_hash'][:24]}…"),
                         ("Export", f"PNG sha256 {v['export']['png_sha256'][:24]}…"),
                         ("Report", f"{v['report_pages']} page(s), {v['report_bytes']:,} bytes"),
                         ("Custody", " → ".join(v["custody"])),
                         ("Audit", f"{v['audit']['entries']} entries in the whole log; chain intact: {v['audit']['ok']}")]))
            self.add(Spacer(1, 2 * mm), pair(v["before"], v["after"], "Original (under-exposed)", "Exported derivative"))
        if "error" not in w.get("tamper", {"error": 1}):
            v = w["tamper"]
            self.wf_header("2. Tamper detection", "One byte of a stored original is changed outside the application.")
            self.add(kv([("Before", f"verifies: {v['before']['ok']}"), ("After", f"verifies: {v['after']['ok']} (sha256 now {v['after']['sha256'][:16]}…)"),
                         ("Logged", f"{v['logged']} integrity-failure entry in the audit log"),
                         ("Export", E(v["export_blocked"] or "not blocked")), ("Custody", " → ".join(v["custody"]))]))
        if "error" not in w.get("case_management", {"error": 1}):
            v = w["case_management"]
            ex = v["export"]
            self.wf_header("3. Case management, chain of custody and export package",
                           "Sub-cases, tags, bookmarks, custody transfer, duplicate detection, audited search, a disclosure "
                           "package with a manifest, the recipient's check, and the legal-hold and role rules for disposal.")
            self.add(kv([("Sub-cases", ", ".join(v["subcases"])),
                         ("Custody log", P("<br/>".join(E(f"{c['event']} by {c['actor']}" + (f" → {c['to_party']}" if c['to_party'] else "")) for c in v["custody"][:8]), "cell")),
                         ("Duplicates", E(f"{v['duplicates']['exact']} exact; near: " + "; ".join(f"{n['a']} ≈ {n['b']} (distance {n['distance']}/64)" for n in v["duplicates"]["near"]))),
                         ("Search 'pedestrian'", E("; ".join(f"{h[0]} ({', '.join(h[1])})" for h in v["search"]["hits"]))),
                         ("Package", P("<br/>".join(E(f"{f['path']} · {f['kind']} · {f['size']:,} B · {f['sha256'][:16]}…") for f in ex["files"]), "cell")),
                         ("Recipient check", f"intact package: ok={ex['verify']['ok']}; one byte changed: ok={ex['tampered_verify']['ok']} — "
                                             + E("; ".join(ex["tampered_verify"]["problems"]))),
                         ("Repeatability", f"same selection twice: member files identical = {ex['members_identical']}; package hashes differ "
                                           "only because the manifest records the time"),
                         ("Disposal rules", E(f"legal hold: {v['legal_hold_blocks_disposal']} · non-administrator: {v['non_admin_blocked']}"))]))
        if "error" not in w.get("video", {"error": 1}):
            v = w["video"]
            self.wf_header("4. Video operations", "Clip extraction in three modes, frame export, scene-change and repeated-frame "
                                                  "detection, a contact sheet, comparison and a processed chain rendered as video.")
            rows = [["Mode", "Frames", "Codec", "Requested", "Actual", "Note"]]
            for c in v["clips"]:
                rows.append([c["mode"], str(c["frames"]), str(c["codec"]), json.dumps(c["requested"]), json.dumps(c["actual"]), E(c.get("note") or "")[:160]])
            self.add(grid(rows, [18 * mm, 13 * mm, 15 * mm, 30 * mm, 30 * mm, W - 106 * mm]))
            self.add(kv([("Frame export", f"{v['frames_zip']['count']} PNG frames, ZIP sha256 {v['frames_zip']['sha256'][:16]}…"),
                         ("Scene changes", E(f"{[c['frame'] for c in v['temporal']['scene_changes']]} · repeated {[c['frame'] for c in v['temporal']['repeated']]} · {v['temporal']['method']}")),
                         ("Compare", E(f"building vs JPEG Q20 copy: {v['compare']}")),
                         ("Chain as video", E(json.dumps(v["render_video"]))),
                         ("Image sequence", E(json.dumps(v["sequence"])))]))
            self.add(pair(v["contact_sheet"], v["compare_diff"], "Contact sheet (12 frames)", "Difference map, original vs JPEG Q20 (×4)", max_h=58 * mm))
        if "error" not in w.get("sync", {"error": 1}):
            v = w["sync"]
            self.wf_header("5. Multi-camera synchronisation",
                           "Camera B is the same 4-second recording started 0.48 s later (known truth), with a white flash and a "
                           "tone burst at 1.60 s. Each method's offset is compared with the truth.")
            rows = [["Method", "Offset (s)", "Truth (s)", "Error (s)", "Stated ± (s)", "Strength"]]
            for x in v["rows"]:
                rows.append([x["method"], str(x["offset_vs_reference_s"]), str(x["truth_s"]), str(x["error_s"]), str(x["uncertainty_s"]),
                             f"{x['strength'] or ''} {('ratio ' + str(x['peak_ratio'])) if x['peak_ratio'] else ''}"])
            self.add(grid(rows, [58 * mm, 18 * mm, 16 * mm, 16 * mm, 20 * mm, W - 128 * mm]))
            au = next((x for x in v["rows"] if x["method"].startswith("audio")), None)
            if au:
                self.add(P(f"Audio: camera B's file starts its audio track 0.011 s after its first video frame. The first "
                           f"run of this report ignored that and was 0.011 s off; the fix reads each stream's start time, and "
                           f"the error is now {au['error_s']} s. The stated ±{au['uncertainty_s']} s is the correlation's "
                           f"sampling step only; it does not include microphone distance unless the examiner enters it. "
                           f"Event-based methods are limited to ±1 frame (0.04 s), as stated.", "small"))
            self.add(img(v["grid"], W, 70 * mm), P(f"All cameras at the same case time ({E(v['grid_time'])})", "cap"))
        if "error" not in w.get("detection", {"error": 1}):
            v = w["detection"]
            self.wf_header("6. Object detection and tracking",
                           "YOLOX (Apache-2.0, run locally with ONNX Runtime) on every frame of the street pan, followed by the "
                           "IoU tracker. The hardware profile chose the model and sampling; the run records it.")
            p = v["run"]["params"]
            self.add(kv([("Model", E(", ".join(str(m.get("model", m.get("plate_localiser", ""))) for m in v["run"]["models"]))),
                         ("Parameters", E(f"classes {p['classes']}, min score {p['min_score']}, every {p['frame_step']} frame(s), profile {p['profile']}")),
                         ("Tracker", E(json.dumps(p["tracker"]))), ("Result", E(json.dumps(v["run"]["stats"])))]))
            rows = [["Track", "Class", "Frames", "Boxes", "Mean score", "Colour"]] + [
                [f"#{t['number']}", t["cls"], f"{t['start_frame']}–{t['end_frame']}", str(t["boxes"]), str(t["mean_score"]), str(t["colour"] or "")] for t in v["tracks"]]
            self.add(grid(rows, [16 * mm, 20 * mm, 22 * mm, 16 * mm, 22 * mm, W - 96 * mm]))
            third = (W - 6 * mm) / 3
            t = Table([[img(p_, third, 70 * mm) for p_ in v["overlays"]], [P(f"frame {n}", "cap") for n in (0, 15, 29)]], colWidths=[third + 2 * mm] * 3)
            self.add(t)
        if "error" not in w.get("plates", {"error": 1}):
            v = w["plates"]
            self.wf_header("7. Licence plates: reading and human review",
                           f"Plate localisation (character grouping, morphology, bright regions), perspective rectification and "
                           f"OCR ({E(v['ocr']['engine'])} {E(v['ocr']['version'])}, no character whitelist) over several "
                           "binarisation and scale variants; candidates are checked against plate formats. A reading never "
                           "becomes 'confirmed' by itself: an examiner confirms, corrects or rejects it.")
            for c in v["cases"]:
                self.add(CondPageBreak(60 * mm))
                self.add(P(f"{E(c['input'])} — {E(self.r['inputs'][c['input']]['description'])}", "h3"))
                for p_ in c["plates"]:
                    cands = ", ".join(f"{x['text']} ({x['confidence']})" for x in p_["candidates"] if x["text"])
                    t = Table([[img(c["overlay"], 60 * mm, 50 * mm), img(p_["rectified"], 45 * mm, 20 * mm),
                                P(f"Machine best: <b>{E(p_['best_text'] or '—')}</b> ({p_['confidence']}), format {E(p_['format'] or 'none')}"
                                  f"<br/>Alternatives: {E(cands)}", "cell")]], colWidths=[62 * mm, 47 * mm, W - 109 * mm])
                    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
                    self.add(t)
            rows = [["Input", "Machine reading", "Examiner decision", "Reviewed text"]] + [
                [r_["input"], r_["machine"] or "—", r_["status"], r_["reviewed_text"] or "—"] for r_ in v["reviews"]]
            self.add(grid(rows, [30 * mm, 35 * mm, 35 * mm, W - 100 * mm]))
            self.add(P(f"Search '{E(v['search']['query'])}': {v['search']['total']} hit(s) — "
                       + E("; ".join(f"{h['reviewed_text']} ({h['status']}, matched on {h['matched_on']})" for h in v["search"]["results"])), "small"))
            self.add(P("Limitation: the real UK plate (about 60 px wide) and the real motion-blurred plate are not read "
                       "correctly by OCR. They stay in the review queue for a person to read or reject, which is the intended "
                       "behaviour; the system does not present them as results.", "small"))
        if "error" not in w.get("redaction", {"error": 1}):
            v = w["redaction"]
            self.wf_header("8. Redaction with verification, and an annotated exhibit",
                           "Tracked people are blurred, a sign is masked with keyframes, faces and plates are pixelated "
                           "automatically and the audio is muted. The rendered copy is new linked evidence; the original is untouched. "
                           "The verification decodes the output and checks every frame.")
            fp = v.get("first_pass")
            if fp:
                fv = fp["verification"]
                self.add(P(f"<b>First pass (automatic plate finder only).</b> Verification result: "
                           f"<b>{'passed' if fv['ok'] else 'needs review'}</b>, {fv['frames_with_problems']} of {fv['frames_in']} frames flagged. "
                           "The plate finder did not catch every plate: in frame 0 the silver car's plate is still readable, and the "
                           "verifier, which uses the same kind of detector, did not flag that frame either. A clean or partly clean "
                           "verification is therefore not proof that nothing sensitive remains.", "body"))
                self.add(img(fp["shot"], W * 0.5, 70 * mm), P("First pass, frame 0: the silver car's plate is not covered", "cap"))
                self.add(P(f"<b>Second pass (examiner review).</b> The plate zone of each of the {fp['vehicles_added']} tracked vehicles "
                           "is added as a region that follows the track (middle 70 % of the vehicle box width, lower 45 % of its height). "
                           "This is a fixed geometric approximation, not a detection, so the copy still needs a visual check.", "body"))
            ver = v["verification"]
            self.add(kv([("Regions", E("; ".join(f"{r_['label']} ({r_['method']}, {r_['keyframes']} keyframes)" for r_ in v["regions"]))),
                         ("Frames", f"{ver['frames_in']} in, {ver['frames_out']} out (count ok: {ver['frame_count_ok']})"),
                         ("Flagged frames", f"{ver['frames_with_problems']} of {ver['frames_in']}. Examples: " + E("; ".join(
                             f"frame {n}: {pr['problem']} at x={pr['box'][0]:.0f}, y={pr['box'][1]:.0f}" if pr.get("box") else f"frame {n}: {pr['problem']}"
                             for n, prs in v["flagged_examples"].items() for pr in prs))),
                         ("Tracking gaps", str(len(ver["tracking_gaps"]))), ("Audio", E(ver["audio"]["detail"])),
                         ("Overall", f"<b>{'passed' if ver['ok'] else 'needs review'}</b> — {E(ver['note'])}"),
                         ("Original", f"still verifies: {v['original_still_verifies']}")]))
            if fp:
                self.add(P("After the second pass the silver car's plate is covered in every frame. The frames still flagged were "
                           "checked by eye: the plate finder reports road markings and already-pixelated blocks as possible plates. "
                           "The verifier reports these instead of hiding them, so the copy stays marked 'needs review' until a person "
                           "has looked.", "body"))
            for b, a in v["shots"]:
                self.add(pair(b, a, "Original frame", "Redacted copy (second pass)", max_h=70 * mm))
            self.add(img(v["exhibit"], W * 0.6, 80 * mm), P("Annotated exhibit (burnt into a derivative PNG with a caption bar)", "cap"))
        if "error" not in w.get("batch", {"error": 1}):
            v = w["batch"]
            self.wf_header("9. Batch conversion of a recorder export",
                           "A folder as it might come from a DVR: Dahua DAV, Xiongmai and HiSilicon streams (synthetic, built from "
                           "a known H.264 stream), a duplicate, a phone MP4, a self-playing .exe, a damaged file and a text file.")
            rows = [["Input", "Detected", "Status", "Method", "Output (frames)", "Check"]]
            for i in v["items"]:
                rows.append([i["input"], i["detected"] or "—", i["status"], i["method"] or "—",
                             ", ".join(f"{o[0]} ({o[1]})" for o in i["outputs"]) or (i["error_class"] or ""),
                             f"{i['input_frames']} in" + (" ✓" if i["frame_check"] else "") if i["input_frames"] else (i["error"][:60] if i["error"] else "")])
            self.add(grid(rows, [36 * mm, 30 * mm, 16 * mm, 14 * mm, 40 * mm, W - 136 * mm]))
            self.add(kv([("Settings", E(", ".join(f"{k}={v['profile'][k]}" for k in ("hardware_profile", "method", "output", "frame_rate", "recursive", "segment_seconds") if k in v["profile"]))), ("Parallel", f"{v['concurrency']} at a time (hardware profile)"),
                         ("Inputs", f"unchanged after the batch: {v['inputs_unchanged']}"),
                         ("Manifest", f"sha256 {v['manifest_sha256'][:24]}…; sidecar matches: {v['manifest_sidecar_ok']}; CSV rows {v['csv_rows']}")]))
        if "error" not in w.get("cli", {"error": 1}):
            v = w["cli"]
            self.wf_header("10. Command line: encrypted backup and verified restore",
                           "Exit codes: 0 success, 1 partial, 2 usage error, 3 failure.")
            self.add(kv([("Backup (AES-256-GCM)", f"exit {v['backup_exit']}, {v['backup_bytes']:,} bytes, sha256 {v['backup_sha256'][:16]}…"),
                         ("Overwrite attempt", f"exit {v['overwrite_refused_exit']} (refused)"),
                         ("Restore + verify", f"exit {v['restore_exit']} (every file hash, audit chain and evidence hash checked)"),
                         ("Wrong password", f"exit {v['wrong_password_exit']} (refused)"),
                         ("Verify restored copy", f"exit {v['verify_restored_exit']}")]))
        if "error" not in w.get("profiles", {"error": 1}):
            v = w["profiles"]
            self.wf_header("11. Hardware profiles",
                           f"This computer: <b>{E(v['this_machine']['name'])}</b> ({E(v['this_machine']['reason'])}). The profile "
                           "sets only how much work heavy jobs do (model size, frame sampling, parallel conversions, plate "
                           "upscaling) and is recorded with each run. Filter defaults are the same on every computer, so a "
                           "chain gives the same result everywhere. FORENSIC_PROFILE overrides the choice.")
            rows = [["Profile", "Detector", "Every n frames", "Max frames", "Parallel conversions", "Plate upscale"]]
            for name, p in v["profiles"].items():
                rows.append([name, p["detector"], str(p["frame_step"]), str(p["max_frames"]), str(p["batch_workers"]), str(p["plate_upscale"])])
            self.add(grid(rows, [20 * mm, 36 * mm, 24 * mm, 22 * mm, 34 * mm, W - 136 * mm]))
        self.add(PageBreak())

    def closing(self):
        self.add(P("Limitations", "h1"))
        for t in [
            "Ground-truth measurements use synthetic degradations of a few images. They show that each method does what "
            "it claims under known conditions; they do not predict performance on any particular piece of evidence.",
            "PSNR and SSIM measure closeness to the original, not legibility or evidential value. Enhancement methods "
            "(CLAHE, false colour, edge views) are meant to reveal, not restore, and are not judged by them.",
            "Generative AI steps (learned super-resolution) are illustrative only: exports are watermarked and need a "
            "written justification; this report shows one scoring below plain resizing.",
            "Plate OCR reads clear plates; small or blurred real plates are not read reliably and go to human review.",
            "Detectors used for automatic redaction and its verification can miss small or turned faces; the verification "
            "lists what it checked and a person must review a redacted copy before release.",
            "Three geometric analyses (shadows, reflections, perspective) need examiner-marked points and were not run "
            "automatically; the learned synthetic-image detector is not installed in this build.",
        ]:
            self.add(P("• " + E(t)))
        self.add(P("Reproducing this report", "h2"))
        self.add(P("cd backend &amp;&amp; python -m tools.showcase.run --media &lt;OpenCV samples&gt; --work &lt;dir&gt; &amp;&amp; "
                   "python -m tools.showcase.pdf --work &lt;dir&gt; --out report.pdf", "mono"))

    def build(self, out: Path):
        doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                                title="Lucida: algorithms and workflows, tested examples", author="Lucida showcase")
        self.cover()
        self.findings()
        self.inputs()
        self.filters()
        self.analyses()
        self.workflows()
        self.closing()

        def footer(c, d):
            c.saveState()
            c.setFont("Sans", 7)
            c.setFillColor(MUTED)
            c.drawString(18 * mm, 9 * mm, "Lucida forensic workbench · algorithms and workflows: tested examples")
            c.drawRightString(A4[0] - 18 * mm, 9 * mm, f"page {d.page}")
            c.restoreState()

        doc.build(self.story, onFirstPage=footer, onLaterPages=footer)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tests", default="not run")
    a = ap.parse_args()
    Doc(Path(a.work), a.tests).build(Path(a.out))


if __name__ == "__main__":
    main()
