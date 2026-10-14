"""Append-only, hash-chained audit log.

Every entry stores the hash of the entry before it, so deleting or editing a row
breaks the chain and `verify()` reports where.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .db import Database, canonical_json, now_iso

GENESIS = "0" * 64


@dataclass(frozen=True)
class Actor:
    name: str
    kind: str = "human"  # "human" or "agent"
    model: str | None = None  # agent model version, when kind == "agent"

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}" + (f" ({self.model})" if self.model else "")


def _entry_hash(fields: dict) -> str:
    return hashlib.sha256(canonical_json(fields).encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def append(
        self,
        actor: Actor,
        action: str,
        *,
        case_id: str | None = None,
        target: str | None = None,
        params: dict[str, Any] | None = None,
        output_hash: str | None = None,
    ) -> dict:
        params = dict(params or {})
        if actor.model:
            params.setdefault("agent_model", actor.model)
        with self.db.transaction():
            last = self.db.one("SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1")
            prev = last["entry_hash"] if last else GENESIS
            fields = {
                "case_id": case_id,
                "ts": now_iso(),
                "actor": actor.name,
                "actor_kind": actor.kind,
                "action": action,
                "target": target,
                "params": params,
                "output_hash": output_hash,
                "prev_hash": prev,
            }
            h = _entry_hash(fields)
            cur = self.db.execute(
                "INSERT INTO audit (case_id, ts, actor, actor_kind, action, target, params_json,"
                " output_hash, prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    fields["ts"],
                    actor.name,
                    actor.kind,
                    action,
                    target,
                    canonical_json(params),
                    output_hash,
                    prev,
                    h,
                ),
            )
            return {"seq": cur.lastrowid, **fields, "entry_hash": h}

    def entries(self, case_id: str | None = None) -> list[dict]:
        if case_id is None:
            rows = self.db.all("SELECT * FROM audit ORDER BY seq")
        else:
            rows = self.db.all("SELECT * FROM audit WHERE case_id = ? ORDER BY seq", (case_id,))
        for r in rows:
            r["params"] = json.loads(r.pop("params_json"))
        return rows

    def verify(self) -> dict:
        """Recompute the whole chain. Returns {"ok": bool, "entries": n, "broken_at": seq|None}."""
        prev = GENESIS
        rows = self.entries()
        for r in rows:
            fields = {
                "case_id": r["case_id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "actor_kind": r["actor_kind"],
                "action": r["action"],
                "target": r["target"],
                "params": r["params"],
                "output_hash": r["output_hash"],
                "prev_hash": r["prev_hash"],
            }
            if r["prev_hash"] != prev or _entry_hash(fields) != r["entry_hash"]:
                return {"ok": False, "entries": len(rows), "broken_at": r["seq"]}
            prev = r["entry_hash"]
        return {"ok": True, "entries": len(rows), "broken_at": None, "head": prev}
