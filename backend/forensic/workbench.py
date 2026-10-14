"""Workbench: the single service layer used by the HTTP API and the MCP servers.

Humans (through the UI) and agents (through MCP) call the same methods, so every
action is validated and audited in exactly one place.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import BinaryIO

import numpy as np

from . import __version__, media
from .analysis import ANALYSES, AnalysisContext, get_analysis
from .audit import Actor, AuditLog
from .config import Settings, load_settings
from .db import Database, canonical_json, new_id, now_iso
from .filters import REGISTRY, FilterError
from .graph import Engine, frame_of, graph_hash, normalise, pending_proposals
from .store import EvidenceStore


class NotFound(LookupError):
    pass


class Workbench:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        self.audit = AuditLog(self.db)
        self.store = EvidenceStore(self.settings)
        self.engine = Engine(self._load_source)

    # Environment ----------------------------------------------------------

    def environment(self) -> dict:
        import cv2
        import PIL

        return {
            "app_version": __version__,
            "ffmpeg": media.ffmpeg_version(self.settings.ffmpeg),
            "opencv": cv2.__version__,
            "numpy": np.__version__,
            "pillow": PIL.__version__,
        }

    # Catalogues -------------------------------------------------------------

    @staticmethod
    def filters() -> list[dict]:
        return [f.schema() for f in REGISTRY.values()]

    @staticmethod
    def analyses() -> list[dict]:
        return [a.schema() for a in ANALYSES.values()]

    # Cases --------------------------------------------------------------------

    def create_case(self, name: str, examiner: str, actor: Actor, notes: str = "") -> dict:
        name, examiner = name.strip(), examiner.strip()
        if not name or not examiner:
            raise ValueError("case name and examiner are required")
        cid = new_id()
        self.db.execute(
            "INSERT INTO cases (id, name, examiner, notes, created_at) VALUES (?,?,?,?,?)",
            (cid, name, examiner, notes, now_iso()),
        )
        self.audit.append(actor, "case.create", case_id=cid, target=cid, params={"name": name, "examiner": examiner})
        return self.get_case(cid)

    def list_cases(self) -> list[dict]:
        return self.db.all(
            "SELECT c.*, (SELECT COUNT(*) FROM evidence e WHERE e.case_id = c.id) AS evidence_count "
            "FROM cases c ORDER BY created_at DESC"
        )

    def get_case(self, case_id: str) -> dict:
        row = self.db.one("SELECT * FROM cases WHERE id = ?", (case_id,))
        if not row:
            raise NotFound(f"case {case_id} not found")
        return row

    # Evidence -------------------------------------------------------------------

    def ingest(self, case_id: str, src: BinaryIO, filename: str, actor: Actor) -> dict:
        self.get_case(case_id)
        stored = self.store.ingest(src, filename)
        try:
            info = media.probe(stored["path"], self.settings)
        except media.MediaError as e:
            self.audit.append(
                actor, "evidence.rejected", case_id=case_id, target=filename,
                params={"reason": str(e)[:500], "sha256": stored["sha256"]},
            )
            raise
        eid = new_id()
        self.db.execute(
            "INSERT INTO evidence (id, case_id, filename, sha256, md5, size, media_type, stored_path,"
            " probe_json, ingested_by, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid, case_id, Path(filename).name, stored["sha256"], stored["md5"], stored["size"],
                info["media_type"], str(stored["path"]), json.dumps(info), actor.label, now_iso(),
            ),
        )
        self.audit.append(
            actor, "evidence.ingest", case_id=case_id, target=eid,
            params={"filename": Path(filename).name, "sha256": stored["sha256"], "md5": stored["md5"], "size": stored["size"]},
            output_hash=stored["sha256"],
        )
        return self.get_evidence(eid)

    def ingest_path(self, case_id: str, path: Path, actor: Actor) -> dict:
        with open(path, "rb") as f:
            return self.ingest(case_id, f, path.name, actor)

    def _evidence_row(self, evidence_id: str) -> dict:
        row = self.db.one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        if not row:
            raise NotFound(f"evidence {evidence_id} not found")
        return row

    def get_evidence(self, evidence_id: str) -> dict:
        row = self._evidence_row(evidence_id)
        row["probe"] = json.loads(row.pop("probe_json"))
        row.pop("stored_path")
        return row

    def evidence_path(self, evidence_id: str) -> Path:
        return Path(self._evidence_row(evidence_id)["stored_path"])

    def list_evidence(self, case_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM evidence WHERE case_id = ? ORDER BY ingested_at", (case_id,))
        for r in rows:
            r["probe"] = json.loads(r.pop("probe_json"))
            r.pop("stored_path")
        return rows

    def verify_evidence(self, evidence_id: str, actor: Actor) -> dict:
        row = self._evidence_row(evidence_id)
        res = self.store.verify(Path(row["stored_path"]), row["sha256"])
        res["expected_sha256"] = row["sha256"]
        self.audit.append(
            actor, "evidence.verify", case_id=row["case_id"], target=evidence_id,
            params={"ok": res["ok"], "read_only": res["read_only"]}, output_hash=res["sha256"],
        )
        return res

    def frame_table(self, evidence_id: str) -> list[dict]:
        row = self._evidence_row(evidence_id)
        if row["media_type"] != "video":
            return [{"n": 0, "type": "I", "key": True, "time": 0.0, "size": row["size"], "interlaced": False}]
        return media.frame_table(Path(row["stored_path"]), self.settings)

    def source_frame(self, evidence_id: str, n: int = 0) -> np.ndarray:
        data = self._load_source({"evidence_id": evidence_id, "frames": {"start": n, "end": n}})
        return frame_of(data)

    def _load_source(self, source: dict) -> np.ndarray:
        row = self._evidence_row(source["evidence_id"])
        if source.get("sha256") and source["sha256"] != row["sha256"]:
            raise FilterError("chain was built on a different file (SHA-256 mismatch)")
        path = Path(row["stored_path"])
        if row["media_type"] == "image":
            return media.decode_image(path)
        probe = json.loads(row["probe_json"])
        fr = source.get("frames") or {"start": 0, "end": 0}
        last = probe["frame_count"] - 1
        if fr["start"] > last:
            raise FilterError(f"frame {fr['start']} is beyond the last frame ({last})")
        end = min(fr["end"], last)
        if end - fr["start"] + 1 > 600:
            raise FilterError("frame ranges are limited to 600 frames in this version")
        stack = media.decode_frames(path, self.settings, fr["start"], end, probe["width"], probe["height"])
        return stack[0] if fr["start"] == end else stack

    # Chains ---------------------------------------------------------------------------

    def _normalise_for(self, evidence_id: str, graph: dict) -> dict:
        row = self._evidence_row(evidence_id)
        graph = dict(graph or {})
        src = dict(graph.get("source") or {})
        src["evidence_id"] = evidence_id
        src["sha256"] = row["sha256"]
        if row["media_type"] == "image":
            src["frames"] = None
        graph["source"] = src
        return normalise(graph)

    @staticmethod
    def _tag_agent_nodes(g: dict, actor: Actor, old: dict | None = None) -> dict:
        """Nodes an agent adds or changes are proposals until an examiner accepts them."""
        if actor.kind != "agent":
            return g
        before = {n["id"]: n for n in (old or {}).get("nodes", [])}
        for n in g["nodes"]:
            prev = before.get(n["id"])
            same = prev is not None and all(prev.get(k) == n.get(k) for k in ("filter", "version", "params", "enabled"))
            if not same:
                n["proposed_by"] = actor.label
                n.pop("accepted_by", None)
            elif prev.get("accepted_by"):
                n["accepted_by"] = prev["accepted_by"]
        return g

    def create_chain(self, case_id: str, evidence_id: str, name: str, actor: Actor, graph: dict | None = None) -> dict:
        ev = self._evidence_row(evidence_id)
        if ev["case_id"] != case_id:
            raise ValueError("evidence does not belong to this case")
        g = self._normalise_for(evidence_id, graph or {"source": {}, "nodes": []})
        g = self._tag_agent_nodes(g, actor)
        cid, ts = new_id(), now_iso()
        gh = graph_hash(g)
        self.db.execute(
            "INSERT INTO chains (id, case_id, evidence_id, name, graph_json, graph_hash, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (cid, case_id, evidence_id, name.strip() or "Chain", canonical_json(g), gh, ts, ts),
        )
        self.audit.append(actor, "chain.create", case_id=case_id, target=cid, params={"evidence_id": evidence_id, "graph_hash": gh, "graph": g})
        return self.get_chain(cid)

    def get_chain(self, chain_id: str) -> dict:
        row = self.db.one("SELECT * FROM chains WHERE id = ?", (chain_id,))
        if not row:
            raise NotFound(f"chain {chain_id} not found")
        row["graph"] = json.loads(row.pop("graph_json"))
        return row

    def list_chains(self, case_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM chains WHERE case_id = ? ORDER BY created_at", (case_id,))
        for r in rows:
            r["graph"] = json.loads(r.pop("graph_json"))
        return rows

    def update_chain(self, chain_id: str, graph: dict, actor: Actor, name: str | None = None) -> dict:
        old = self.get_chain(chain_id)
        g = self._normalise_for(old["evidence_id"], graph)
        g = self._tag_agent_nodes(g, actor, old["graph"])
        if actor.kind == "agent":
            # Agents cannot accept their own proposals by echoing an accepted_by field back.
            for n in g["nodes"]:
                if n.get("accepted_by") and not any(o["id"] == n["id"] and o.get("accepted_by") for o in old["graph"]["nodes"]):
                    n.pop("accepted_by")
        gh = graph_hash(g)
        if gh == old["graph_hash"] and (name is None or name == old["name"]):
            return old
        self.db.execute(
            "UPDATE chains SET graph_json = ?, graph_hash = ?, name = ?, output_hash = NULL, updated_at = ? WHERE id = ?",
            (canonical_json(g), gh, (name or old["name"]).strip(), now_iso(), chain_id),
        )
        self.audit.append(
            actor, "chain.update", case_id=old["case_id"], target=chain_id,
            params={"previous_graph_hash": old["graph_hash"], "graph_hash": gh, "changes": _diff_nodes(old["graph"], g), "graph": g},
        )
        return self.get_chain(chain_id)

    def accept_proposals(self, chain_id: str, actor: Actor, node_ids: list[str] | None = None) -> dict:
        """An examiner accepts agent-proposed steps, making them part of the evidential chain."""
        if actor.kind != "human":
            raise ValueError("only an examiner can accept proposed steps")
        ch = self.get_chain(chain_id)
        g = ch["graph"]
        accepted = []
        for n in g["nodes"]:
            if n.get("proposed_by") and not n.get("accepted_by") and (node_ids is None or n["id"] in node_ids):
                n["accepted_by"] = actor.label
                accepted.append(n["id"])
        if not accepted:
            return ch
        gh = graph_hash(g)
        self.db.execute(
            "UPDATE chains SET graph_json = ?, graph_hash = ?, output_hash = NULL, updated_at = ? WHERE id = ?",
            (canonical_json(g), gh, now_iso(), chain_id),
        )
        self.audit.append(actor, "chain.accept_proposals", case_id=ch["case_id"], target=chain_id,
                          params={"nodes": accepted, "previous_graph_hash": ch["graph_hash"], "graph_hash": gh})
        return self.get_chain(chain_id)

    def render_chain(self, chain_id: str, actor: Actor) -> dict:
        """Run the saved chain, store its output and record the result hashes."""
        ch = self.get_chain(chain_id)
        res = self.engine.run(ch["graph"])
        outputs = []
        frames = res.data if res.is_stack else res.data[None]
        for i, fr in enumerate(frames):
            digest, _ = self.store.put_derived(media.encode_png(fr), "png")
            outputs.append({"frame": i, "png": digest})
        self.db.execute("UPDATE chains SET output_hash = ? WHERE id = ?", (res.output_hash, chain_id))
        steps = [s.__dict__ | {"shape": list(s.shape)} for s in res.steps]
        self.audit.append(
            actor, "chain.render", case_id=ch["case_id"], target=chain_id,
            params={"graph_hash": res.graph_hash, "source_hash": res.source_hash, "steps": steps, "outputs": outputs},
            output_hash=res.output_hash,
        )
        return {
            "chain_id": chain_id,
            "pending_proposals": pending_proposals(normalise(ch["graph"])),
            "graph_hash": res.graph_hash,
            "source_hash": res.source_hash,
            "output_hash": res.output_hash,
            "shape": list(res.data.shape),
            "steps": steps,
            "outputs": outputs,
        }

    def verify_chain(self, chain_id: str, actor: Actor) -> dict:
        """Re-run from the original with no cache and compare to the recorded output hash."""
        ch = self.get_chain(chain_id)
        if not ch["output_hash"]:
            raise ValueError("render the chain before verifying it")
        fresh = Engine(self._load_source, cache_items=0).run(ch["graph"], use_cache=False)
        ok = fresh.output_hash == ch["output_hash"] and fresh.graph_hash == ch["graph_hash"]
        self.audit.append(
            actor, "chain.verify", case_id=ch["case_id"], target=chain_id,
            params={"reproduced": ok, "expected": ch["output_hash"], "graph_hash": fresh.graph_hash},
            output_hash=fresh.output_hash,
        )
        return {"reproduced": ok, "expected_hash": ch["output_hash"], "rerun_hash": fresh.output_hash, "graph_hash": fresh.graph_hash}

    def preview(self, graph: dict, evidence_id: str, upto: str | None = None, frame: int = 0) -> tuple[np.ndarray, dict]:
        """Live preview: not stored and not audited, so it is never evidential."""
        g = self._normalise_for(evidence_id, graph)
        res = self.engine.run(g, upto=upto)
        img = frame_of(res.data, frame)
        return img, {
            "output_hash": res.output_hash,
            "shape": list(res.data.shape),
            "steps": [s.__dict__ | {"shape": list(s.shape)} for s in res.steps],
        }

    # Analyses ------------------------------------------------------------------------

    def run_analysis(self, evidence_id: str, analysis_id: str, params: dict | None, actor: Actor) -> dict:
        row = self._evidence_row(evidence_id)
        a = get_analysis(analysis_id)
        if row["media_type"] not in a.applies_to:
            raise ValueError(f"{a.name} does not apply to {row['media_type']} evidence")
        p = a.validate(params)
        probe = json.loads(row["probe_json"])
        ctx = AnalysisContext(path=Path(row["stored_path"]), probe=probe, settings=self.settings)
        result = a.fn(ctx, p)
        maps = {}
        for name, img in result.maps.items():
            digest, _ = self.store.put_derived(media.encode_png(img), "png")
            maps[name] = digest
        payload = {"summary": result.summary, "findings": result.findings, "flags": result.flags, "maps": maps}
        out_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        aid = new_id()
        self.db.execute(
            "INSERT INTO analyses (id, case_id, evidence_id, name, version, params_json, result_json, output_hash, actor, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, row["case_id"], evidence_id, a.id, a.version, canonical_json(p), json.dumps(payload, default=str), out_hash, actor.label, now_iso()),
        )
        self.audit.append(
            actor, "analysis.run", case_id=row["case_id"], target=evidence_id,
            params={"analysis": a.id, "version": a.version, "params": p, "analysis_id": aid, "maps": maps},
            output_hash=out_hash,
        )
        return self.get_analysis_result(aid)

    def get_analysis_result(self, analysis_id: str) -> dict:
        row = self.db.one("SELECT * FROM analyses WHERE id = ?", (analysis_id,))
        if not row:
            raise NotFound(f"analysis {analysis_id} not found")
        row["params"] = json.loads(row.pop("params_json"))
        row["result"] = json.loads(row.pop("result_json"))
        return row

    def list_analysis_results(self, case_id: str, evidence_id: str | None = None) -> list[dict]:
        if evidence_id:
            ids = self.db.all("SELECT id FROM analyses WHERE evidence_id = ? ORDER BY created_at", (evidence_id,))
        else:
            ids = self.db.all("SELECT id FROM analyses WHERE case_id = ? ORDER BY created_at", (case_id,))
        return [self.get_analysis_result(r["id"]) for r in ids]

    # Audit ---------------------------------------------------------------------------------

    def audit_entries(self, case_id: str) -> list[dict]:
        return self.audit.entries(case_id)

    def note(self, case_id: str, text: str, actor: Actor) -> dict:
        self.get_case(case_id)
        return self.audit.append(actor, "case.note", case_id=case_id, params={"text": text[:5000]})


def _diff_nodes(old: dict, new: dict) -> list[str]:
    before = {n["id"]: n for n in old.get("nodes", [])}
    after = {n["id"]: n for n in new.get("nodes", [])}
    changes = []
    for nid, n in after.items():
        if nid not in before:
            changes.append(f"added {nid} ({n['filter']})")
        elif canonical_json(before[nid]) != canonical_json(n):
            changes.append(f"changed {nid} ({n['filter']})")
    for nid, n in before.items():
        if nid not in after:
            changes.append(f"removed {nid} ({n['filter']})")
    if [n["id"] for n in old.get("nodes", []) if n["id"] in after] != [n["id"] for n in new.get("nodes", []) if n["id"] in before]:
        changes.append("reordered nodes")
    if old.get("source") != new.get("source"):
        changes.append("changed source frames")
    return changes
