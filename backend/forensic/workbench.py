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

from . import APP_NAME, __version__, formats, media, models, policy, prnu
from . import measure as measure_mod
from .analysis import ANALYSES, AnalysisContext, get_analysis
from .audit import Actor, AuditLog
from .config import Settings, load_settings
from .db import Database, canonical_json, new_id, now_iso
from .filters import REGISTRY, FilterError
from .graph import Engine, frame_of, graph_hash, normalise, pending_proposals
from .store import EvidenceStore, hash_bytes


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
            "application": APP_NAME,
            "app_version": __version__,
            "ffmpeg": media.ffmpeg_version(self.settings.ffmpeg),
            "opencv": cv2.__version__,
            "numpy": np.__version__,
            "pillow": PIL.__version__,
            "pywavelets": __import__("pywt").__version__,
            "onnxruntime": __import__("onnxruntime").__version__,
            "c2pa": _c2pa_version(),
            "models": ", ".join(f"{m['id']} {m['sha256'][:12]}" for m in models.manifest()),
            "network": "none used at run time (all models bundled or installed locally)",
        }

    # Catalogues -------------------------------------------------------------

    @staticmethod
    def filters() -> list[dict]:
        return [f.schema() for f in REGISTRY.values()]

    @staticmethod
    def model_catalogue() -> list[dict]:
        checks = {v["id"]: v for v in models.verify_all()}
        return [models.public(m) | {"verified": checks[m["id"]]["ok"]} for m in models.manifest()]

    @staticmethod
    def analyses() -> list[dict]:
        from .validation import load_card

        return [a.schema() | {"card": load_card(a.id)} for a in ANALYSES.values()]

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
            "SELECT c.*, (SELECT COUNT(*) FROM evidence e WHERE e.case_id = c.id AND e.removed_json IS NULL) AS evidence_count "
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
        with open(stored["path"], "rb") as f:
            identified = formats.sniff(f.read(1 << 16))
        try:
            info = media.probe(stored["path"], self.settings)
        except media.MediaError as e:
            if not (identified["family"] or identified["elementary_codec"]):
                self.audit.append(
                    actor, "evidence.rejected", case_id=case_id, target=filename,
                    params={"reason": str(e)[:500], "sha256": stored["sha256"]},
                )
                raise
            # A recorder export FFmpeg cannot open directly: keep it so it can be converted.
            info = {"media_type": "container", "format": identified["label"] or "unknown container",
                    "width": 0, "height": 0, "frame_count": 0, "probe_error": str(e)[-500:]}
        if info["media_type"] == "video" or info["media_type"] == "container":
            info["identified"] = identified
        return self._insert_evidence(case_id, filename, stored, info, actor)

    def _insert_evidence(self, case_id: str, filename: str, stored: dict, info: dict, actor: Actor,
                         derived_from: str | None = None, conversion: dict | None = None) -> dict:
        eid = new_id()
        self.db.execute(
            "INSERT INTO evidence (id, case_id, filename, sha256, md5, size, media_type, stored_path,"
            " probe_json, ingested_by, ingested_at, derived_from, conversion_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid, case_id, Path(filename).name, stored["sha256"], stored["md5"], stored["size"],
                info["media_type"], str(stored["path"]), json.dumps(info), actor.label, now_iso(),
                derived_from, json.dumps(conversion) if conversion else None,
            ),
        )
        params = {"filename": Path(filename).name, "sha256": stored["sha256"], "md5": stored["md5"], "size": stored["size"],
                  "media_type": info["media_type"]}
        if derived_from:
            params.update(derived_from=derived_from, conversion=conversion)
        self.audit.append(actor, "evidence.convert" if derived_from else "evidence.ingest", case_id=case_id,
                          target=eid, params=params, output_hash=stored["sha256"])
        return self.get_evidence(eid)

    def convert_evidence(self, evidence_id: str, method: str, actor: Actor, frame_rate: float = 25.0,
                         codec: str | None = None) -> dict:
        """Convert a recorder export into a standard container as a new, linked evidence item.

        The original is only read. The working copy records its source hash, the method and
        the exact command, and is itself hashed and stored write-once like any evidence.
        """
        import tempfile

        row = self._evidence_row(evidence_id)
        src = Path(row["stored_path"])
        if method == "carve" and not codec:
            codec = formats.guess_codec(src.read_bytes())
        ext = formats.output_extension(method, codec)
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            dest = Path(tmp) / f"converted.{ext}"
            record = formats.convert(src, dest, method, self.settings, frame_rate=frame_rate, codec=codec)
            record["source_sha256"] = row["sha256"]
            info = media.probe(dest, self.settings)
            if method == "carve":
                info["frame_rate"] = f"{frame_rate:g}"
                info["timestamps"] = "synthetic"
            with open(dest, "rb") as f:
                stored = self.store.ingest(f, f"converted.{ext}")
        name = f"{Path(row['filename']).stem} ({method}).{ext}"
        record["output_sha256"] = stored["sha256"]
        return self._insert_evidence(row["case_id"], name, stored, info, actor, derived_from=evidence_id, conversion=record)

    def ingest_path(self, case_id: str, path: Path, actor: Actor) -> dict:
        with open(path, "rb") as f:
            return self.ingest(case_id, f, path.name, actor)

    def _evidence_row(self, evidence_id: str) -> dict:
        row = self.db.one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        if not row:
            raise NotFound(f"evidence {evidence_id} not found")
        return row

    def get_evidence(self, evidence_id: str) -> dict:
        return _public_evidence(self._evidence_row(evidence_id))

    def evidence_path(self, evidence_id: str) -> Path:
        return Path(self._evidence_row(evidence_id)["stored_path"])

    def list_evidence(self, case_id: str, removed: bool = False) -> list[dict]:
        """Evidence in the case; `removed=True` lists the items an examiner removed instead."""
        cond = "IS NOT NULL" if removed else "IS NULL"
        rows = self.db.all(f"SELECT * FROM evidence WHERE case_id = ? AND removed_json {cond} ORDER BY ingested_at", (case_id,))
        return [_public_evidence(r) for r in rows]

    def remove_evidence(self, evidence_id: str, actor: Actor, reason: str = "") -> dict:
        """Take an item (and its working copies and chains) out of the case.

        Nothing is deleted: the original stays in the write-once store with its hash, the
        rows stay in the database, and the removal is an audit entry that can be undone.
        """
        if actor.kind != "human":
            raise ValueError("only an examiner can remove evidence")
        row = self._evidence_row(evidence_id)
        if row["removed_json"]:
            return self.get_evidence(evidence_id)
        mark = canonical_json({"by": actor.name, "at": now_iso(), "reason": reason.strip()[:500]})
        ids = [evidence_id]
        for r in self.db.all("SELECT id FROM evidence WHERE derived_from = ? AND removed_json IS NULL", (evidence_id,)):
            ids.append(r["id"])
        chains = []
        for eid in ids:
            self.db.execute("UPDATE evidence SET removed_json = ? WHERE id = ?", (mark, eid))
            chains += [r["id"] for r in self.db.all("SELECT id FROM chains WHERE evidence_id = ? AND removed_json IS NULL", (eid,))]
        self.audit.append(actor, "evidence.remove", case_id=row["case_id"], target=evidence_id,
                          params={"filename": row["filename"], "sha256": row["sha256"], "reason": reason.strip()[:500],
                                  "working_copies": ids[1:], "chains_hidden": chains, "original_kept": True},
                          output_hash=row["sha256"])
        return self.get_evidence(evidence_id)

    def restore_evidence(self, evidence_id: str, actor: Actor) -> dict:
        if actor.kind != "human":
            raise ValueError("only an examiner can restore evidence")
        row = self._evidence_row(evidence_id)
        if not row["removed_json"]:
            return self.get_evidence(evidence_id)
        mark = row["removed_json"]
        ids = [evidence_id] + [r["id"] for r in self.db.all(
            "SELECT id FROM evidence WHERE derived_from = ? AND removed_json = ?", (evidence_id, mark))]
        for eid in ids:
            self.db.execute("UPDATE evidence SET removed_json = NULL WHERE id = ?", (eid,))
        self.audit.append(actor, "evidence.restore", case_id=row["case_id"], target=evidence_id,
                          params={"filename": row["filename"], "sha256": row["sha256"], "working_copies": ids[1:]},
                          output_hash=row["sha256"])
        return self.get_evidence(evidence_id)


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
        if row["media_type"] == "container":
            return []
        if row["media_type"] != "video":
            return [{"n": 0, "type": "I", "key": True, "time": 0.0, "size": row["size"], "interlaced": False}]
        table = media.frame_table(Path(row["stored_path"]), self.settings)
        conv = json.loads(row["conversion_json"]) if row.get("conversion_json") else None
        if conv and conv.get("method") == "carve":
            # A carved elementary stream has no timestamps of its own.
            for f in table:
                f["time"] = round(f["n"] / conv["frame_rate"], 6)
                f["synthetic_time"] = True
        return table

    def source_frame(self, evidence_id: str, n: int = 0) -> np.ndarray:
        data = self._load_source({"evidence_id": evidence_id, "frames": {"start": n, "end": n}})
        return frame_of(data)

    def _load_source(self, source: dict) -> np.ndarray:
        row = self._evidence_row(source["evidence_id"])
        if source.get("sha256") and source["sha256"] != row["sha256"]:
            raise FilterError("chain was built on a different file (SHA-256 mismatch)")
        path = Path(row["stored_path"])
        if row["media_type"] == "container":
            raise FilterError("this recorder export must be converted to a standard container first")
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
        """Nodes an agent adds or changes are proposals until an examiner accepts them.

        Agents can never write the justification that lets a generative step into evidential output.
        """
        if actor.kind != "agent":
            return g
        before = {n["id"]: n for n in (old or {}).get("nodes", [])}
        for n in g["nodes"]:
            prev_j = (before.get(n["id"]) or {}).get("justification")
            if n.get("justification") != prev_j:
                if prev_j:
                    n["justification"] = prev_j
                else:
                    n.pop("justification", None)
            prev = before.get(n["id"])
            same = prev is not None and all(prev.get(k) == n.get(k) for k in ("filter", "version", "params", "enabled"))
            if not same:
                n["proposed_by"] = actor.label
                n.pop("accepted_by", None)
            elif prev.get("accepted_by"):
                n["accepted_by"] = prev["accepted_by"]
        return g

    @staticmethod
    def _check_agent_keeps_examiner_steps(old: dict, new: dict) -> None:
        """Agents may propose new or changed steps, but only an examiner removes, disables or
        reorders a step the examiner wrote or accepted: those changes cannot be held as proposals."""
        owned = [n for n in old["nodes"] if not (n.get("proposed_by") and not n.get("accepted_by"))]
        new_by_id = {n["id"]: n for n in new["nodes"]}
        removed = [n["id"] for n in owned if n["id"] not in new_by_id]
        if removed:
            raise ValueError(f"only an examiner can remove step(s) {', '.join(removed)}")
        disabled = [n["id"] for n in owned if n.get("enabled", True) and not new_by_id[n["id"]].get("enabled", True)]
        if disabled:
            raise ValueError(f"only an examiner can disable step(s) {', '.join(disabled)}")
        owned_ids = {n["id"] for n in owned}
        if [n["id"] for n in new["nodes"] if n["id"] in owned_ids] != [n["id"] for n in owned]:
            raise ValueError("only an examiner can reorder the examiner's steps")

    def create_chain(self, case_id: str, evidence_id: str, name: str, actor: Actor, graph: dict | None = None) -> dict:
        ev = self._evidence_row(evidence_id)
        if ev["case_id"] != case_id:
            raise ValueError("evidence does not belong to this case")
        if ev["removed_json"]:
            raise ValueError("that evidence item was removed from the case; restore it first")
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
        removed = row.pop("removed_json", None)
        row["removed"] = json.loads(removed) if removed else None
        return row

    def list_chains(self, case_id: str) -> list[dict]:
        """Chains still in the case: not removed themselves, and on evidence that is not removed."""
        rows = self.db.all(
            "SELECT c.* FROM chains c JOIN evidence e ON e.id = c.evidence_id"
            " WHERE c.case_id = ? AND c.removed_json IS NULL AND e.removed_json IS NULL ORDER BY c.created_at",
            (case_id,),
        )
        for r in rows:
            r["graph"] = json.loads(r.pop("graph_json"))
            r["removed"] = json.loads(r.pop("removed_json")) if r["removed_json"] else None
        return rows

    def remove_chain(self, chain_id: str, actor: Actor, reason: str = "") -> dict:
        """Take an enhancement chain out of the case. Its rows and audit history are kept."""
        if actor.kind != "human":
            raise ValueError("only an examiner can remove a chain")
        ch = self.get_chain(chain_id)
        if not ch["removed"]:
            mark = canonical_json({"by": actor.name, "at": now_iso(), "reason": reason.strip()[:500]})
            self.db.execute("UPDATE chains SET removed_json = ? WHERE id = ?", (mark, chain_id))
            self.audit.append(actor, "chain.remove", case_id=ch["case_id"], target=chain_id,
                              params={"name": ch["name"], "evidence_id": ch["evidence_id"], "graph_hash": ch["graph_hash"],
                                      "output_hash": ch["output_hash"], "reason": reason.strip()[:500]})
        return self.get_chain(chain_id)

    def update_chain(self, chain_id: str, graph: dict, actor: Actor, name: str | None = None) -> dict:
        old = self.get_chain(chain_id)
        g = self._normalise_for(old["evidence_id"], graph)
        if actor.kind == "agent":
            self._check_agent_keeps_examiner_steps(old["graph"], g)
        g = self._tag_agent_nodes(g, actor, old["graph"])
        if actor.kind == "agent":
            # Agents cannot accept their own proposals by echoing an accepted_by field back.
            for n in g["nodes"]:
                if n.get("accepted_by") and not any(o["id"] == n["id"] and o.get("accepted_by") for o in old["graph"]["nodes"]):
                    n.pop("accepted_by")
        gh = graph_hash(g)
        if gh == old["graph_hash"] and (name is None or name == old["name"]):
            return old
        old_j = {n["id"]: n.get("justification") for n in old["graph"]["nodes"]}
        justified = [{"node": n["id"], "filter": n["filter"], "justification": n["justification"]}
                     for n in g["nodes"] if n.get("justification") and n["justification"] != old_j.get(n["id"])]
        self.db.execute(
            "UPDATE chains SET graph_json = ?, graph_hash = ?, name = ?, output_hash = NULL, updated_at = ? WHERE id = ?",
            (canonical_json(g), gh, (name or old["name"]).strip(), now_iso(), chain_id),
        )
        self.audit.append(
            actor, "chain.update", case_id=old["case_id"], target=chain_id,
            params={"previous_graph_hash": old["graph_hash"], "graph_hash": gh, "changes": _diff_nodes(old["graph"], g), "graph": g},
        )
        for j in justified:
            self.audit.append(actor, "chain.generative_justification", case_id=old["case_id"], target=chain_id,
                              params=j | {"graph_hash": gh})
        return self.get_chain(chain_id)

    def accept_proposals(self, chain_id: str, actor: Actor, node_ids: list[str] | None = None) -> dict:
        """An examiner accepts agent-proposed steps, making them part of the evidential chain."""
        if actor.kind != "human":
            raise ValueError("only an examiner can accept proposed steps")
        ch = self.get_chain(chain_id)
        g = ch["graph"]
        blocked = [n["id"] for n in g["nodes"] if n.get("proposed_by") and not n.get("accepted_by")
                   and (node_ids is None or n["id"] in node_ids) and n["id"] in policy.unjustified(g)]
        if blocked:
            raise ValueError(f"step(s) {', '.join(blocked)} run a generative AI filter: write a justification on each "
                             f"(at least {policy.MIN_JUSTIFICATION} characters) before accepting it")
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
        pol = policy.status(ch["graph"])
        outputs = []
        frames = res.data if res.is_stack else res.data[None]
        for i, fr in enumerate(frames):
            # Generative results are stored watermarked; the pixel hash is of the unmarked result.
            digest, _ = self.store.put_derived(media.encode_png(policy.watermark(fr) if pol["illustrative_only"] else fr), "png")
            outputs.append({"frame": i, "png": digest, "watermarked": pol["illustrative_only"]})
        self.db.execute("UPDATE chains SET output_hash = ? WHERE id = ?", (res.output_hash, chain_id))
        steps = [s.__dict__ | {"shape": list(s.shape)} for s in res.steps]
        used = self.chain_models(ch["graph"])
        self.audit.append(
            actor, "chain.render", case_id=ch["case_id"], target=chain_id,
            params={"graph_hash": res.graph_hash, "source_hash": res.source_hash, "steps": steps, "outputs": outputs,
                    "models": used, "policy": pol},
            output_hash=res.output_hash,
        )
        return {
            "chain_id": chain_id,
            "policy": pol,
            "models": used,
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

    @staticmethod
    def chain_models(graph: dict) -> list[dict]:
        """The learned models an enabled step of the chain runs, with their pinned weights hashes."""
        from .filters import get_filter

        used = []
        for n in graph["nodes"]:
            if not n.get("enabled", True):
                continue
            f = get_filter(n["filter"])
            for mid in f.model_ids(n["params"]):
                m = models.get(mid)
                used.append({"node": n["id"], "model": mid, "sha256": m["sha256"], "licence": m["licence"],
                             "source": m["source"], "generative": bool(m.get("generative"))})
        return used

    def export_chain(self, chain_id: str, actor: Actor, frame: int = 0) -> tuple[bytes, dict]:
        """The rendered result as a lossless PNG for disclosure.

        Generative results are watermarked, and blocked unless every generative step carries
        a written justification. The output is re-computed and must match the recorded hash.
        """
        ch = self.get_chain(chain_id)
        if not ch["output_hash"]:
            raise ValueError("render the chain before exporting it")
        if pending_proposals(normalise(ch["graph"])):
            raise ValueError("the chain contains agent-proposed steps nobody has accepted")
        pol = policy.status(ch["graph"])
        if not pol["evidential_export"]:
            raise PermissionError("this chain contains a generative AI step without a written justification; "
                                  "its result is illustrative only and cannot be exported as evidence")
        res = self.engine.run(ch["graph"])
        if res.output_hash != ch["output_hash"]:
            raise ValueError("re-computed result does not match the recorded output hash; verify the chain")
        img = frame_of(res.data, frame)
        if pol["illustrative_only"]:
            img = policy.watermark(img)
        png = media.encode_png(img)
        digest = hashlib.sha256(png).hexdigest()
        info = {"chain_id": chain_id, "frame": frame, "output_hash": res.output_hash, "png_sha256": digest,
                "watermarked": pol["illustrative_only"], "justifications": [
                    {"node": n["id"], "justification": n.get("justification")} for n in policy.generative_nodes(ch["graph"])]}
        self.audit.append(actor, "chain.export", case_id=ch["case_id"], target=chain_id, params=info, output_hash=digest)
        return png, info

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
        payload, out_hash, map_png = self.compute_analysis(row, a, p)
        for data in map_png:
            self.store.put_derived(data, "png")
        maps = payload["maps"]
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

    def compute_analysis(self, row: dict, a, p: dict) -> tuple[dict, str, list[bytes]]:
        """Run an analysis without storing or logging anything: the payload, its hash and the map PNGs."""
        if a.unavailable and (why := a.unavailable()):
            raise ValueError(why)
        probe = json.loads(row["probe_json"])
        ctx = AnalysisContext(path=Path(row["stored_path"]), probe=probe, settings=self.settings,
                              resolve=lambda kind, rid: self._resolve(row["case_id"], kind, rid))
        result = a.fn(ctx, p)
        maps, pngs = {}, []
        for name, img in result.maps.items():
            data = media.encode_png(img)
            maps[name] = hash_bytes(data)
            pngs.append(data)
        payload = {"summary": result.summary, "findings": result.findings, "flags": result.flags, "maps": maps}
        return payload, hashlib.sha256(canonical_json(payload).encode()).hexdigest(), pngs

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
            ids = self.db.all(
                "SELECT a.id FROM analyses a JOIN evidence e ON e.id = a.evidence_id"
                " WHERE a.case_id = ? AND e.removed_json IS NULL ORDER BY a.created_at", (case_id,))
        return [self.get_analysis_result(r["id"]) for r in ids]

    def _resolve(self, case_id: str, kind: str, rid: str):
        if kind == "prnu_reference":
            ref = self.get_camera_reference(rid)
            if ref["case_id"] != case_id:
                raise ValueError("that camera reference belongs to another case")
            return ref, self.load_fingerprint(ref)
        raise ValueError(f"unknown resource kind {kind}")

    # Camera references (PRNU) ----------------------------------------------------------------

    def create_camera_reference(self, case_id: str, name: str, evidence_ids: list[str], actor: Actor,
                                camera: str = "") -> dict:
        """Estimate a PRNU fingerprint from known images of one camera and store it with its provenance."""
        self.get_case(case_id)
        if not name.strip():
            raise ValueError("give the camera reference a name")
        if len(set(evidence_ids)) < 2:
            raise ValueError("select at least two images from the camera (20 or more flat, bright images is best)")
        rows = [self._evidence_row(e) for e in dict.fromkeys(evidence_ids)]
        for r in rows:
            if r["case_id"] != case_id or r["media_type"] != "image":
                raise ValueError("camera references are built from still images in this case")
        K, stats = prnu.estimate_fingerprint([media.decode_image(Path(r["stored_path"])) for r in rows])
        digest, _ = self.store.put_derived(prnu.to_bytes(K), "npy")
        rid = new_id()
        ids = [r["id"] for r in rows]
        stats["source_sha256"] = [r["sha256"] for r in rows]
        self.db.execute(
            "INSERT INTO camera_references (id, case_id, name, camera, evidence_json, width, height, fingerprint_sha256,"
            " stats_json, version, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, case_id, name.strip(), camera.strip(), json.dumps(ids), stats["width"], stats["height"], digest,
             json.dumps(stats), "1.0.0", actor.label, now_iso()),
        )
        self.audit.append(actor, "prnu.reference", case_id=case_id, target=rid,
                          params={"name": name.strip(), "camera": camera.strip(), "evidence": ids, "images": len(ids)},
                          output_hash=digest)
        return self.get_camera_reference(rid)

    def get_camera_reference(self, ref_id: str) -> dict:
        row = self.db.one("SELECT * FROM camera_references WHERE id = ?", (ref_id,))
        if not row:
            raise NotFound(f"camera reference {ref_id} not found")
        row["evidence"] = json.loads(row.pop("evidence_json"))
        row["stats"] = json.loads(row.pop("stats_json"))
        return row

    def list_camera_references(self, case_id: str) -> list[dict]:
        ids = self.db.all("SELECT id FROM camera_references WHERE case_id = ? ORDER BY created_at", (case_id,))
        return [self.get_camera_reference(r["id"]) for r in ids]

    def load_fingerprint(self, ref: dict) -> np.ndarray:
        path = self.store.derived_path(ref["fingerprint_sha256"], "npy")
        data = path.read_bytes()
        if hash_bytes(data) != ref["fingerprint_sha256"]:
            raise ValueError("stored camera fingerprint does not match its recorded hash")
        return prnu.from_bytes(data)

    # Measurements -------------------------------------------------------------------------------

    def measure(self, evidence_id: str, kind: str, inputs: dict, actor: Actor, label: str = "",
                chain_id: str | None = None, frame: int = 0) -> dict:
        """Run a measurement on clicked points and record inputs, result and hash.

        Points are in the pixel coordinates of the image they were clicked on: the
        original frame, or the output of `chain_id` if given (e.g. after lens correction).
        """
        row = self._evidence_row(evidence_id)
        if chain_id:
            ch = self.get_chain(chain_id)
            if ch["evidence_id"] != evidence_id:
                raise ValueError("that chain belongs to another evidence item")
            if policy.generative_nodes(ch["graph"]):
                raise ValueError("this chain contains a generative AI step: its detail may not have been recorded, "
                                 "so it cannot be measured on. Measure on the original or a classical chain.")
        times = None
        if kind == "speed":
            if row["media_type"] != "video":
                raise ValueError("speed needs a video")
            times = {f["n"]: f["time"] for f in self.frame_table(evidence_id) if f["time"] is not None}
        result = measure_mod.measure(kind, inputs, times)
        record = {"kind": kind, "inputs": inputs, "result": result, "chain_id": chain_id,
                  "chain_graph_hash": self.get_chain(chain_id)["graph_hash"] if chain_id else None}
        out_hash = hashlib.sha256(canonical_json(record).encode()).hexdigest()
        mid = new_id()
        self.db.execute(
            "INSERT INTO measurements (id, case_id, evidence_id, chain_id, kind, label, frame, inputs_json, result_json,"
            " output_hash, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, row["case_id"], evidence_id, chain_id, kind, label.strip()[:200], int(frame), canonical_json(inputs),
             json.dumps(result), out_hash, actor.label, now_iso()),
        )
        self.audit.append(actor, "measurement.run", case_id=row["case_id"], target=evidence_id,
                          params={"measurement_id": mid, "kind": kind, "label": label, "chain_id": chain_id,
                                  "value": result["value"], "unit": result["unit"], "interval_95": result["interval_95"]},
                          output_hash=out_hash)
        return self.get_measurement(mid)

    def get_measurement(self, mid: str) -> dict:
        row = self.db.one("SELECT * FROM measurements WHERE id = ?", (mid,))
        if not row:
            raise NotFound(f"measurement {mid} not found")
        row["inputs"] = json.loads(row.pop("inputs_json"))
        row["result"] = json.loads(row.pop("result_json"))
        return row

    def list_measurements(self, case_id: str) -> list[dict]:
        ids = self.db.all(
            "SELECT m.id FROM measurements m JOIN evidence e ON e.id = m.evidence_id"
            " WHERE m.case_id = ? AND e.removed_json IS NULL ORDER BY m.created_at", (case_id,))
        return [self.get_measurement(r["id"]) for r in ids]

    # Triage (Smart Report) ----------------------------------------------------------------------

    def smart_report(self, evidence_id: str, actor: Actor) -> dict:
        """Run the triage battery on one item and record a traffic-light reading with its reasons."""
        from .analysis.video import VPF_CODECS
        from .triage import BATTERY, assess

        row = self._evidence_row(evidence_id)
        runs = []
        for aid, params in BATTERY.get(row["media_type"], []):
            if aid.startswith("tamper.") and row["media_type"] == "image" and not formats_is_jpeg(Path(row["stored_path"])):
                continue
            if aid == "video.double_encoding" and json.loads(row["probe_json"]).get("codec") not in VPF_CODECS:
                continue
            runs.append(self.run_analysis(evidence_id, aid, params, actor))
        result = assess(row["media_type"], runs)
        out_hash = hashlib.sha256(canonical_json(result).encode()).hexdigest()
        tid = new_id()
        self.db.execute(
            "INSERT INTO triage (id, case_id, evidence_id, level, result_json, output_hash, actor, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (tid, row["case_id"], evidence_id, result["level"], json.dumps(result), out_hash, actor.label, now_iso()),
        )
        self.audit.append(actor, "triage.smart_report", case_id=row["case_id"], target=evidence_id,
                          params={"triage_id": tid, "level": result["level"], "line": result["line"]}, output_hash=out_hash)
        return self.latest_triage(evidence_id)

    def latest_triage(self, evidence_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM triage WHERE evidence_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (evidence_id,))
        if row:
            row["result"] = json.loads(row.pop("result_json"))
        return row

    def list_triage(self, case_id: str) -> list[dict]:
        out = []
        for ev in self.db.all("SELECT id FROM evidence WHERE case_id = ? AND removed_json IS NULL ORDER BY ingested_at", (case_id,)):
            t = self.latest_triage(ev["id"])
            if t:
                out.append(t)
        return out

    # Reviews ----------------------------------------------------------------------------------

    def review_case(self, case_id: str, actor: Actor | None = None, rerun_analyses: bool = True) -> dict:
        from .agents.reviewer import review_case

        return review_case(self, case_id, rerun_analyses=rerun_analyses, actor=actor)

    def record_review(self, case_id: str, result: dict, actor: Actor) -> dict:
        out_hash = hashlib.sha256(canonical_json(result).encode()).hexdigest()
        rid = new_id()
        self.db.execute(
            "INSERT INTO reviews (id, case_id, level, result_json, output_hash, actor, created_at) VALUES (?,?,?,?,?,?,?)",
            (rid, case_id, result["level"], json.dumps(result), out_hash, actor.label, now_iso()),
        )
        self.audit.append(actor, "review.run", case_id=case_id, target=rid,
                          params={"review_id": rid, "level": result["level"], "counts": result["counts"],
                                  "line": result["line"]}, output_hash=out_hash)
        return self.latest_review(case_id)

    def latest_review(self, case_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM reviews WHERE case_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (case_id,))
        if row:
            row["result"] = json.loads(row.pop("result_json"))
        return row

    # Audit ---------------------------------------------------------------------------------

    def audit_entries(self, case_id: str) -> list[dict]:
        return self.audit.entries(case_id)

    def note(self, case_id: str, text: str, actor: Actor) -> dict:
        self.get_case(case_id)
        return self.audit.append(actor, "case.note", case_id=case_id, params={"text": text[:5000]})


def formats_is_jpeg(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xff\xd8\xff"


def _public_evidence(row: dict) -> dict:
    row["probe"] = json.loads(row.pop("probe_json"))
    conv = row.pop("conversion_json", None)
    row["conversion"] = json.loads(conv) if conv else None
    row.pop("stored_path")
    removed = row.pop("removed_json", None)
    row["removed"] = json.loads(removed) if removed else None
    return row


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


def _c2pa_version() -> str:
    try:
        from importlib.metadata import version

        return version("c2pa-python")
    except Exception:  # noqa: BLE001 - optional at run time
        return "not installed"
