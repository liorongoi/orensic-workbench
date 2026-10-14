"""Triage agent: runs the Smart Report over a batch and writes one line per item on why."""

from __future__ import annotations

from ..audit import Actor
from ..triage import RULES_VERSION

AGENT = Actor("triage-agent", "agent", f"rules-{RULES_VERSION}")


def triage_case(wb, case_id: str, evidence_ids: list[str] | None = None, actor: Actor | None = None) -> dict:
    """Sort every (or the selected) evidence item into green / yellow / red, with reasons."""
    actor = actor or AGENT
    items = wb.list_evidence(case_id)
    if evidence_ids is not None:
        wanted = set(evidence_ids)
        items = [e for e in items if e["id"] in wanted]
    results = []
    for ev in items:
        t = wb.smart_report(ev["id"], actor)
        results.append({"evidence_id": ev["id"], "filename": ev["filename"], "level": t["level"], "line": t["result"]["line"]})
    order = {"red": 0, "yellow": 1, "green": 2}
    results.sort(key=lambda r: order[r["level"]])
    counts = {lvl: sum(1 for r in results if r["level"] == lvl) for lvl in ("red", "yellow", "green")}
    if results:
        lines = [f"{r['level'].upper()}: {r['filename']}: {r['line']}" for r in results]
        wb.note(case_id, f"[agent opinion] Triage of {len(results)} item(s) "
                f"({counts['red']} red, {counts['yellow']} yellow, {counts['green']} green):\n" + "\n".join(lines), actor)
    return {"counts": counts, "items": results, "rules_version": RULES_VERSION}
