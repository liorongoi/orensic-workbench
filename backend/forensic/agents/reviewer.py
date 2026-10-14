"""Reviewer agent: an independent re-check of a case before it is reported.

It does what a second examiner would do with the case file, mechanically:
re-hashes every original, checks the audit chain, re-runs every rendered chain
from the originals with no cache, re-runs every analysis and measurement with
its recorded parameters and compares the result hashes, checks the hashes of the
learned models the case used, and flags steps a court will ask about:
generative (AI) steps, unjustified ones, unaccepted agent proposals and
results that did not reproduce. It changes nothing in the case; it only writes
its review, which is logged.
"""

from __future__ import annotations

import hashlib
import json

from .. import models, policy
from .. import measure as measure_mod
from ..analysis import get_analysis
from ..audit import Actor
from ..db import canonical_json
from ..graph import Engine, normalise, pending_proposals

VERSION = "1.0.0"
AGENT = Actor("reviewer-agent", "agent", f"reviewer-{VERSION}")
LEVELS = ("pass", "warn", "fail")


def _item(check: str, level: str, text: str, target: str | None = None, **extra) -> dict:
    return {"check": check, "level": level, "text": text, "target": target, **extra}


def review_case(wb, case_id: str, rerun_analyses: bool = True, actor: Actor | None = None) -> dict:
    actor = actor or AGENT
    wb.get_case(case_id)
    items: list[dict] = []

    # 1. Originals.
    for ev in wb.list_evidence(case_id):
        row = wb._evidence_row(ev["id"])
        res = wb.store.verify(wb.evidence_path(ev["id"]), row["sha256"])
        if not res["ok"]:
            items.append(_item("evidence.hash", "fail", f"{ev['filename']}: the stored original no longer matches its "
                               "ingest hash.", ev["id"], expected=row["sha256"], found=res["sha256"]))
        elif not res["read_only"]:
            items.append(_item("evidence.hash", "warn", f"{ev['filename']}: hash matches but the file is writable.", ev["id"]))
        else:
            items.append(_item("evidence.hash", "pass", f"{ev['filename']}: hash matches the ingest record.", ev["id"]))

    # 2. Audit log.
    au = wb.audit.verify()
    items.append(_item("audit.chain", "pass" if au["ok"] else "fail",
                       f"Audit log hash chain intact ({au['entries']} entries)." if au["ok"]
                       else f"Audit log hash chain broken at entry {au['broken_at']}."))

    # 3. Chains.
    used_models: dict[str, dict] = {}
    for ch in wb.list_chains(case_id):
        g = normalise(ch["graph"])
        name = ch["name"]
        pend = pending_proposals(g)
        if pend:
            items.append(_item("chain.proposals", "warn", f"Chain '{name}': {len(pend)} agent proposal(s) not accepted "
                               "by an examiner; they are excluded from its evidential use.", ch["id"], nodes=pend))
        for n in policy.generative_nodes(g):
            if policy.justified(n):
                items.append(_item("chain.generative", "warn", f"Chain '{name}': step {n['id']} ({n['filter']}) is "
                                   "generative; its output is illustrative only. Examiner's justification: "
                                   f"\"{n['justification'].strip()}\"", ch["id"], node=n["id"]))
            else:
                items.append(_item("chain.generative", "fail", f"Chain '{name}': step {n['id']} ({n['filter']}) is "
                                   "generative and has no justification; it must not be used as evidence.",
                                   ch["id"], node=n["id"]))
        agent_nodes = [n["id"] for n in g["nodes"] if n.get("enabled", True) and n.get("proposed_by") and n.get("accepted_by")]
        if agent_nodes:
            items.append(_item("chain.agent_steps", "pass", f"Chain '{name}': {len(agent_nodes)} step(s) proposed by an "
                               "agent were accepted by an examiner.", ch["id"], nodes=agent_nodes))
        for m in wb.chain_models(g):
            used_models[m["model"]] = m
        if not ch["output_hash"]:
            items.append(_item("chain.rerun", "warn", f"Chain '{name}' was never rendered, so there is no recorded "
                               "result to reproduce.", ch["id"]))
            continue
        try:
            a = Engine(wb._load_source, cache_items=0).run(ch["graph"], use_cache=False)
            b = Engine(wb._load_source, cache_items=0).run(ch["graph"], use_cache=False)
        except Exception as e:  # noqa: BLE001 - any failure to re-run is a finding
            items.append(_item("chain.rerun", "fail", f"Chain '{name}' could not be re-run: {e}", ch["id"]))
            continue
        if a.output_hash != b.output_hash:
            items.append(_item("chain.rerun", "fail", f"Chain '{name}' is not deterministic: two re-runs from the "
                               "original gave different pixels.", ch["id"], hashes=[a.output_hash, b.output_hash]))
        elif a.output_hash != ch["output_hash"]:
            items.append(_item("chain.rerun", "fail", f"Chain '{name}': re-run from the original does not match the "
                               "recorded output hash.", ch["id"], expected=ch["output_hash"], found=a.output_hash))
        else:
            items.append(_item("chain.rerun", "pass", f"Chain '{name}' re-ran twice from the original with no cache "
                               "and reproduced the recorded pixels.", ch["id"], output_hash=a.output_hash))

    # 4. Analyses.
    if rerun_analyses:
        for res in wb.list_analysis_results(case_id):
            label = f"{res['name']} on {wb.get_evidence(res['evidence_id'])['filename']}"
            try:
                an = get_analysis(res["name"])
            except ValueError:
                items.append(_item("analysis.rerun", "warn", f"{label}: this analysis is no longer installed.", res["id"]))
                continue
            if an.version != res["version"]:
                items.append(_item("analysis.rerun", "warn", f"{label}: recorded with version {res['version']}, "
                                   f"installed version is {an.version}; not compared.", res["id"]))
                continue
            try:
                _, h, _ = wb.compute_analysis(wb._evidence_row(res["evidence_id"]), an, res["params"])
            except Exception as e:  # noqa: BLE001
                items.append(_item("analysis.rerun", "fail", f"{label}: could not be re-run: {e}", res["id"]))
                continue
            ok = h == res["output_hash"]
            items.append(_item("analysis.rerun", "pass" if ok else "fail",
                               f"{label}: re-run reproduced the recorded result." if ok
                               else f"{label}: re-run gave a different result from the one recorded.",
                               res["id"], expected=res["output_hash"], found=h))
            if an.id == "synthetic.learned":
                items.append(_item("analysis.ai", "warn", f"{label}: AI-assisted result from a learned detector; read "
                                   "it with the detector's validation card.", res["id"]))

    # 5. Measurements.
    for m in wb.list_measurements(case_id):
        label = f"Measurement '{m['label'] or m['kind']}'"
        try:
            times = None
            if m["kind"] == "speed":
                times = {f["n"]: f["time"] for f in wb.frame_table(m["evidence_id"]) if f["time"] is not None}
            result = measure_mod.measure(m["kind"], m["inputs"], times)
            gh = wb.get_chain(m["chain_id"])["graph_hash"] if m["chain_id"] else None
            record = {"kind": m["kind"], "inputs": m["inputs"], "result": result, "chain_id": m["chain_id"],
                      "chain_graph_hash": gh}
            h = hashlib.sha256(canonical_json(record).encode()).hexdigest()
        except Exception as e:  # noqa: BLE001
            items.append(_item("measurement.rerun", "fail", f"{label}: could not be recomputed: {e}", m["id"]))
            continue
        ok = h == m["output_hash"]
        items.append(_item("measurement.rerun", "pass" if ok else "fail",
                           f"{label}: recomputed {result['value']:.3f} {result['unit']}, matching the record." if ok
                           else f"{label}: recomputation does not match the record.", m["id"]))

    # 6. Models.
    for mid, m in sorted(used_models.items()):
        try:
            info = models.get(mid)
            ok = models.verify(info)
        except models.ModelError as e:
            items.append(_item("model.hash", "fail", f"Model {mid}: {e}", mid))
            continue
        items.append(_item("model.hash", "pass" if ok else "fail",
                           f"Model {mid}: weights match the pinned hash." if ok
                           else f"Model {mid}: weights do not match the pinned hash.", mid, sha256=m["sha256"]))

    counts = {lvl: sum(1 for i in items if i["level"] == lvl) for lvl in LEVELS}
    level = "fail" if counts["fail"] else "warn" if counts["warn"] else "pass"
    line = {"pass": "Everything re-checked reproduced and no step needs explaining.",
            "warn": f"Reproducible, with {counts['warn']} point(s) the report must explain.",
            "fail": f"{counts['fail']} check(s) failed: resolve them before reporting."}[level]
    result = {"level": level, "line": line, "counts": counts, "items": items, "reviewer_version": VERSION,
              "rerun_analyses": rerun_analyses}
    return wb.record_review(case_id, result, actor)
