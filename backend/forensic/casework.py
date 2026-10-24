"""Evidence management: case details, chain of custody, tags, bookmarks, duplicates, search,
legal holds, retention and controlled disposal, export packages with manifests, chain branches
and presets.

Every change is written to the hash-chained audit log; custody events are also kept in their
own table so a custody report can be printed per item without reading the whole log.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import APP_NAME, __version__
from .audit import Actor
from .db import canonical_json, new_id, now_iso

if TYPE_CHECKING:  # pragma: no cover
    from .workbench import Workbench

CUSTODY_EVENTS = ("acquired", "derived", "transferred", "accessed", "verified", "exported", "returned",
                  "released", "disposed", "note")
CASE_STATUSES = ("open", "active", "suspended", "closed", "archived")
BOOKMARK_KINDS = ("bookmark", "event", "range", "note")
MAX_TAG = 60
PHASH_VERSION = "phash-dct-1.0.0"


def _clean_tag(tag: str) -> str:
    t = " ".join(str(tag).strip().lower().split())[:MAX_TAG]
    if not t or not re.fullmatch(r"[\w .:/#@+-]+", t):
        raise ValueError(f"invalid tag {tag!r}: letters, digits, spaces and . : / # @ + - only")
    return t


def phash(gray: np.ndarray) -> str:
    """64-bit DCT perceptual hash (Zauner 2010): 32x32 grey, 2-D DCT, top-left 8x8 compared with
    their median (DC term excluded from the median). Robust to rescaling and recompression;
    not to crops or rotations."""
    small = cv2.resize(gray.astype(np.float32), (32, 32), interpolation=cv2.INTER_AREA)
    d = cv2.dct(small)[:8, :8]
    med = np.median(d.flatten()[1:])
    bits = (d > med).flatten()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class Casework:
    def __init__(self, wb: "Workbench"):
        self.wb = wb
        self.db = wb.db

    # Case details -------------------------------------------------------------------------

    def update_case(self, case_id: str, fields: dict, actor: Actor) -> dict:
        case = self.wb.get_case(case_id)
        allowed = {"name": 300, "case_number": 120, "legal_authority": 2000, "description": 5000, "notes": 5000,
                   "status": 20, "examiner": 200}
        changes = {}
        for k, v in (fields or {}).items():
            if k not in allowed:
                raise ValueError(f"unknown case field {k}")
            if v is None:
                continue
            v = str(v).strip()[: allowed[k]]
            if k == "status" and v not in CASE_STATUSES:
                raise ValueError(f"status must be one of {', '.join(CASE_STATUSES)}")
            if k in ("name", "examiner") and not v:
                raise ValueError(f"{k} cannot be empty")
            if v != (case.get(k) or ""):
                changes[k] = {"from": case.get(k), "to": v}
        if not changes:
            return case
        if case.get("disposed"):
            raise ValueError("a disposed case cannot be edited")
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.db.execute(f"UPDATE cases SET {sets} WHERE id = ?", (*[c["to"] for c in changes.values()], case_id))
        self.wb.audit.append(actor, "case.update", case_id=case_id, target=case_id, params={"changes": changes})
        return self.wb.get_case(case_id)

    def subcases(self, case_id: str) -> list[dict]:
        return [c for c in self.wb.list_cases() if c.get("parent_id") == case_id]

    # Chain of custody -------------------------------------------------------------------------

    def custody_event(self, case_id: str, evidence_id: str | None, event: str, actor: Actor, *, from_party: str = "",
                      to_party: str = "", location: str = "", notes: str = "", details: dict | None = None) -> dict:
        if event not in CUSTODY_EVENTS:
            raise ValueError(f"custody event must be one of {', '.join(CUSTODY_EVENTS)}")
        row = {"id": new_id(), "case_id": case_id, "evidence_id": evidence_id, "ts": now_iso(), "event": event,
               "actor": actor.label, "from_party": from_party.strip()[:300], "to_party": to_party.strip()[:300],
               "location": location.strip()[:300], "notes": notes.strip()[:2000],
               "details_json": canonical_json(details or {})}
        self.db.execute(
            "INSERT INTO custody (id, case_id, evidence_id, ts, event, actor, from_party, to_party, location, notes,"
            " details_json) VALUES (:id,:case_id,:evidence_id,:ts,:event,:actor,:from_party,:to_party,:location,:notes,"
            ":details_json)", row)
        return row

    def record_custody(self, evidence_id: str, event: str, actor: Actor, **kw) -> dict:
        """An examiner records a manual custody event (transfer, return, release...). It is also audited."""
        ev = self.wb.get_evidence(evidence_id)
        if event in ("acquired", "derived", "verified", "exported", "disposed"):
            raise ValueError(f"'{event}' events are recorded automatically by the action itself")
        row = self.custody_event(ev["case_id"], evidence_id, event, actor, **kw)
        self.wb.audit.append(actor, "custody.record", case_id=ev["case_id"], target=evidence_id,
                             params={k: row[k] for k in ("event", "from_party", "to_party", "location", "notes")})
        return self._public_custody(row)

    @staticmethod
    def _public_custody(r: dict) -> dict:
        r = dict(r)
        r["details"] = json.loads(r.pop("details_json") or "{}")
        return r

    def custody(self, case_id: str, evidence_id: str | None = None) -> list[dict]:
        if evidence_id:
            rows = self.db.all("SELECT * FROM custody WHERE evidence_id = ? ORDER BY ts, rowid", (evidence_id,))
        else:
            rows = self.db.all("SELECT * FROM custody WHERE case_id = ? ORDER BY ts, rowid", (case_id,))
        return [self._public_custody(r) for r in rows]

    def note_access(self, evidence_id: str, actor: Actor, what: str) -> None:
        """Downloads of an original are custody 'accessed' events."""
        ev = self.wb.get_evidence(evidence_id)
        self.custody_event(ev["case_id"], evidence_id, "accessed", actor, notes=what)
        self.wb.audit.append(actor, "evidence.access", case_id=ev["case_id"], target=evidence_id, params={"what": what},
                             output_hash=ev["sha256"])

    # Tags ---------------------------------------------------------------------------------------

    def set_tags(self, evidence_id: str, tags: list[str], actor: Actor) -> list[str]:
        ev = self.wb.get_evidence(evidence_id)
        new = sorted({_clean_tag(t) for t in tags if str(t).strip()})
        old = self.tags(evidence_id)
        if new == old:
            return old
        with self.db.transaction():
            self.db.execute("DELETE FROM evidence_tags WHERE evidence_id = ?", (evidence_id,))
            for t in new:
                self.db.execute("INSERT INTO evidence_tags (evidence_id, tag) VALUES (?,?)", (evidence_id, t))
        self.wb.audit.append(actor, "evidence.tags", case_id=ev["case_id"], target=evidence_id,
                             params={"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new))})
        return new

    def tags(self, evidence_id: str) -> list[str]:
        return [r["tag"] for r in self.db.all("SELECT tag FROM evidence_tags WHERE evidence_id = ? ORDER BY tag",
                                              (evidence_id,))]

    def case_tags(self, case_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.db.all("SELECT t.evidence_id, t.tag FROM evidence_tags t JOIN evidence e ON e.id = t.evidence_id"
                             " WHERE e.case_id = ? ORDER BY t.tag", (case_id,)):
            out.setdefault(r["evidence_id"], []).append(r["tag"])
        return out

    # Bookmarks ------------------------------------------------------------------------------------

    def add_bookmark(self, evidence_id: str, frame: int, label: str, actor: Actor, *, end_frame: int | None = None,
                     note: str = "", kind: str = "bookmark") -> dict:
        ev = self.wb.get_evidence(evidence_id)
        label = label.strip()[:200]
        if not label:
            raise ValueError("give the bookmark a label")
        if kind not in BOOKMARK_KINDS:
            raise ValueError(f"kind must be one of {', '.join(BOOKMARK_KINDS)}")
        last = max(0, int(ev["probe"].get("frame_count") or 1) - 1)
        if not 0 <= frame <= last or (end_frame is not None and not frame <= end_frame <= last):
            raise ValueError(f"frames must be within 0-{last}")
        bid = new_id()
        self.db.execute("INSERT INTO bookmarks (id, case_id, evidence_id, frame, end_frame, label, note, kind, actor,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (bid, ev["case_id"], evidence_id, int(frame), end_frame, label, note.strip()[:2000], kind,
                         actor.label, now_iso()))
        self.wb.audit.append(actor, "bookmark.add", case_id=ev["case_id"], target=evidence_id,
                             params={"bookmark_id": bid, "frame": frame, "end_frame": end_frame, "label": label,
                                     "kind": kind})
        return self.get_bookmark(bid)

    def get_bookmark(self, bid: str) -> dict:
        row = self.db.one("SELECT * FROM bookmarks WHERE id = ?", (bid,))
        if not row:
            from .workbench import NotFound

            raise NotFound(f"bookmark {bid} not found")
        return self._with_time(row)

    def _with_time(self, row: dict) -> dict:
        times = self._frame_times(row["evidence_id"])
        row["time"] = times.get(row["frame"])
        row["end_time"] = times.get(row["end_frame"]) if row.get("end_frame") is not None else None
        return row

    def _frame_times(self, evidence_id: str) -> dict[int, float]:
        ev = self.wb.get_evidence(evidence_id)
        if ev["media_type"] != "video":
            return {0: 0.0}
        return {f["n"]: f["time"] for f in self.wb.frame_table(evidence_id) if f["time"] is not None}

    def delete_bookmark(self, bid: str, actor: Actor) -> None:
        b = self.get_bookmark(bid)
        self.db.execute("DELETE FROM bookmarks WHERE id = ?", (bid,))
        self.wb.audit.append(actor, "bookmark.delete", case_id=b["case_id"], target=b["evidence_id"],
                             params={"bookmark_id": bid, "label": b["label"], "frame": b["frame"]})

    def bookmarks(self, case_id: str, evidence_id: str | None = None) -> list[dict]:
        if evidence_id:
            rows = self.db.all("SELECT * FROM bookmarks WHERE evidence_id = ? ORDER BY frame", (evidence_id,))
        else:
            rows = self.db.all("SELECT * FROM bookmarks WHERE case_id = ? ORDER BY evidence_id, frame", (case_id,))
        cache: dict[str, dict] = {}
        for r in rows:
            if r["evidence_id"] not in cache:
                cache[r["evidence_id"]] = self._frame_times(r["evidence_id"])
            t = cache[r["evidence_id"]]
            r["time"] = t.get(r["frame"])
            r["end_time"] = t.get(r["end_frame"]) if r.get("end_frame") is not None else None
        return rows

    # Duplicates ------------------------------------------------------------------------------------

    def perceptual_hash(self, evidence_id: str) -> str | None:
        row = self.wb._evidence_row(evidence_id)
        if row.get("phash"):
            return row["phash"]
        if row["media_type"] == "container":
            return None
        if row["media_type"] == "image":
            img = self.wb.source_frame(evidence_id, 0)
        else:
            probe = json.loads(row["probe_json"])
            img = self.wb.source_frame(evidence_id, max(0, int(probe["frame_count"]) // 2))
        gray = 0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]
        h = phash(gray)
        self.db.execute("UPDATE evidence SET phash = ? WHERE id = ?", (h, evidence_id))
        return h

    def duplicates(self, case_id: str, max_distance: int = 8, actor: Actor | None = None) -> dict:
        """Exact duplicates (same SHA-256) and near duplicates (perceptual hash within
        `max_distance` of 64 bits; the middle frame stands for a video). Near-duplicate pairs are
        leads for the examiner to compare, not proof that two items show the same content."""
        items = self.wb.list_evidence(case_id)
        by_sha: dict[str, list[dict]] = {}
        for ev in items:
            by_sha.setdefault(ev["sha256"], []).append(ev)
        exact = [[{"id": e["id"], "filename": e["filename"]} for e in grp] for grp in by_sha.values() if len(grp) > 1]
        hashes = []
        for ev in items:
            try:
                h = self.perceptual_hash(ev["id"])
            except Exception:  # noqa: BLE001 - undecodable item: reported, not fatal
                h = None
            if h:
                hashes.append((ev, h))
        near = []
        for i in range(len(hashes)):
            for j in range(i + 1, len(hashes)):
                a, ha = hashes[i]
                b, hb = hashes[j]
                if a["sha256"] == b["sha256"]:
                    continue
                d = hamming(ha, hb)
                if d <= max_distance:
                    near.append({"a": {"id": a["id"], "filename": a["filename"]},
                                 "b": {"id": b["id"], "filename": b["filename"]},
                                 "distance": d, "similarity": round(1 - d / 64, 3)})
        near.sort(key=lambda p: p["distance"])
        out = {"method": PHASH_VERSION, "max_distance": max_distance, "exact": exact, "near": near,
               "hashed": len(hashes), "items": len(items),
               "limitations": "Perceptual hashes survive rescaling and recompression but not crops, rotations or "
                              "heavy edits; a video is represented by its middle frame only."}
        if actor:
            self.wb.audit.append(actor, "case.duplicates", case_id=case_id, target=case_id,
                                 params={"exact_groups": len(exact), "near_pairs": len(near), "max_distance": max_distance})
        return out

    # Search ------------------------------------------------------------------------------------------

    def search(self, query: str, actor: Actor, *, case_ids: list[str] | None = None, media_type: str | None = None,
               tag: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        """Full-text search over case details, filenames, hashes, tags, bookmarks, recorder and
        metadata fields. `case_ids` limits the search to the cases the user may see; every search is
        audited (searches over people and plates are sensitive)."""
        q = query.strip()
        if len(q) < 2 and not tag:
            raise ValueError("search for at least two characters")
        like = f"%{q.lower()}%"
        scope = ""
        params: list = []
        if case_ids is not None:
            if not case_ids:
                return {"query": q, "total": 0, "results": []}
            scope = f" AND e.case_id IN ({','.join('?' * len(case_ids))})"
            params += case_ids
        conds = ["(lower(e.filename) LIKE ? OR lower(e.sha256) LIKE ? OR lower(e.probe_json) LIKE ? OR "
                 "lower(c.name) LIKE ? OR lower(c.case_number) LIKE ? OR lower(c.description) LIKE ? OR "
                 "EXISTS (SELECT 1 FROM evidence_tags t WHERE t.evidence_id = e.id AND t.tag LIKE ?) OR "
                 "EXISTS (SELECT 1 FROM bookmarks b WHERE b.evidence_id = e.id AND (lower(b.label) LIKE ? OR lower(b.note) LIKE ?)) OR "
                 "EXISTS (SELECT 1 FROM analyses a WHERE a.evidence_id = e.id AND a.name = 'file.metadata' AND lower(a.result_json) LIKE ?))"]
        qp = [like] * 10 if q else ["%"] * 10
        if media_type:
            conds.append("e.media_type = ?")
            qp.append(media_type)
        if tag:
            conds.append("EXISTS (SELECT 1 FROM evidence_tags t2 WHERE t2.evidence_id = e.id AND t2.tag = ?)")
            qp.append(_clean_tag(tag))
        where = " AND ".join(conds) + " AND e.removed_json IS NULL" + scope
        sql = (f"SELECT e.id, e.case_id, e.filename, e.media_type, e.sha256, e.size, e.ingested_at, c.name AS case_name,"
               f" c.case_number FROM evidence e JOIN cases c ON c.id = e.case_id WHERE {where}"
               " ORDER BY e.ingested_at DESC")
        rows = self.db.all(sql, tuple(qp + params))
        total = len(rows)
        page = rows[offset: offset + min(max(limit, 1), 500)]
        for r in page:
            r["tags"] = self.tags(r["id"])
            r["matched"] = self._why(r, q.lower())
        self.wb.audit.append(actor, "search.evidence", params={"query": q[:200], "media_type": media_type, "tag": tag,
                                                                 "results": total})
        return {"query": q, "total": total, "offset": offset, "results": page}

    def _why(self, r: dict, q: str) -> list[str]:
        why = []
        if q and q in r["filename"].lower():
            why.append("filename")
        if q and q in r["sha256"]:
            why.append("hash")
        if q and (q in (r["case_name"] or "").lower() or q in (r["case_number"] or "").lower()):
            why.append("case")
        if q and any(q in t for t in r["tags"]):
            why.append("tag")
        return why or ["metadata / bookmarks"]

    # Legal hold, retention, disposal --------------------------------------------------------------------

    def set_legal_hold(self, case_id: str, on: bool, reason: str, actor: Actor) -> dict:
        self.wb.get_case(case_id)
        if on and not reason.strip():
            raise ValueError("give the authority or reason for the legal hold")
        val = canonical_json({"by": actor.name, "at": now_iso(), "reason": reason.strip()[:1000]}) if on else None
        self.db.execute("UPDATE cases SET legal_hold_json = ? WHERE id = ?", (val, case_id))
        self.wb.audit.append(actor, "case.legal_hold", case_id=case_id, target=case_id,
                             params={"on": on, "reason": reason.strip()[:1000]})
        return self.wb.get_case(case_id)

    def set_retention(self, case_id: str, until: str | None, actor: Actor) -> dict:
        self.wb.get_case(case_id)
        if until:
            try:
                datetime.fromisoformat(until)
            except ValueError as e:
                raise ValueError("retention date must be an ISO date (YYYY-MM-DD)") from e
        self.db.execute("UPDATE cases SET retention_until = ? WHERE id = ?", (until, case_id))
        self.wb.audit.append(actor, "case.retention", case_id=case_id, target=case_id, params={"until": until})
        return self.wb.get_case(case_id)

    def dispose_case(self, case_id: str, actor: Actor, *, reason: str, confirm: str, role: str) -> dict:
        """Controlled deletion. Only an administrator, only without a legal hold, only after the
        retention date, and only with the case number (or name) typed as confirmation.

        Originals that no other case uses are deleted from the store; everything else (rows, audit
        log, custody, hashes) is kept so the disposal itself stays provable.
        """
        case = self.wb.get_case(case_id)
        if role != "admin":
            raise PermissionError("only an administrator can dispose of a case")
        if case.get("disposed"):
            raise ValueError("this case has already been disposed of")
        if case.get("legal_hold"):
            raise ValueError("the case is under legal hold; lift the hold first")
        if not case.get("retention_until"):
            raise ValueError("set a retention date first; disposal is only possible after it")
        if datetime.fromisoformat(case["retention_until"]).date() > datetime.now(timezone.utc).date():
            raise ValueError(f"retention runs until {case['retention_until']}")
        if confirm.strip() not in {case["case_number"], case["name"]} or not confirm.strip():
            raise ValueError("type the case number (or name) to confirm")
        if not reason.strip():
            raise ValueError("give the reason and authority for disposal")
        for sub in self.subcases(case_id):
            if not sub.get("disposed"):
                raise ValueError("dispose of the sub-cases first")
        deleted, kept = [], []
        rows = self.db.all("SELECT id, sha256, stored_path FROM evidence WHERE case_id = ?", (case_id,))
        for r in rows:
            others = self.db.one("SELECT COUNT(*) AS n FROM evidence e JOIN cases c ON c.id = e.case_id"
                                 " WHERE e.sha256 = ? AND e.case_id != ? AND c.disposed_json IS NULL",
                                 (r["sha256"], case_id))["n"]
            p = Path(r["stored_path"])
            if others or not p.exists():
                kept.append(r["sha256"])
                continue
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            p.unlink()
            deleted.append(r["sha256"])
            self.custody_event(case_id, r["id"], "disposed", actor, notes=reason.strip()[:500],
                               details={"sha256": r["sha256"]})
        mark = {"by": actor.name, "at": now_iso(), "reason": reason.strip()[:1000], "originals_deleted": deleted,
                "originals_kept_shared": kept}
        self.db.execute("UPDATE cases SET disposed_json = ?, status = 'archived' WHERE id = ?", (canonical_json(mark), case_id))
        self.wb.audit.append(actor, "case.dispose", case_id=case_id, target=case_id, params=mark)
        return self.wb.get_case(case_id)

    def verify_case(self, case_id: str, actor: Actor) -> dict:
        results = []
        for ev in self.wb.list_evidence(case_id):
            r = self.wb.verify_evidence(ev["id"], actor)
            results.append({"evidence_id": ev["id"], "filename": ev["filename"], "ok": r["ok"], "sha256": r["sha256"]})
        return {"ok": all(r["ok"] for r in results), "checked": len(results),
                "failed": [r for r in results if not r["ok"]], "results": results}

    # Export packages ----------------------------------------------------------------------------------

    def export_package(self, case_id: str, actor: Actor, *, evidence_ids: list[str] | None = None,
                       chain_ids: list[str] | None = None, include_report: bool = False, purpose: str = "",
                       recipient: str = "") -> tuple[bytes, dict]:
        """A ZIP for delivery: chosen originals (verified first), rendered chain outputs and a
        manifest listing every file with SHA-256 and SHA-512, its source and the operation that made it.

        The ZIP is built deterministically (fixed timestamps, sorted entries) so the same selection
        gives the same package hash."""
        case = self.wb.get_case(case_id)
        ev_ids = evidence_ids if evidence_ids is not None else [e["id"] for e in self.wb.list_evidence(case_id)]
        files: list[tuple[str, bytes, dict]] = []
        for eid in ev_ids:
            ev = self.wb.get_evidence(eid)
            if ev["case_id"] != case_id:
                raise ValueError(f"evidence {eid} belongs to another case")
            check = self.wb.verify_evidence(eid, actor)
            if not check["ok"]:
                raise ValueError(f"{ev['filename']} failed integrity verification; it cannot be exported")
            data = self.wb.evidence_path(eid).read_bytes()
            kind = "derived evidence" if ev.get("derived_from") else "original evidence"
            files.append((f"evidence/{eid}_{_safe(ev['filename'])}", data,
                          {"kind": kind, "evidence_id": eid, "filename": ev["filename"],
                           "acquired": ev["ingested_at"], "derived_from": ev.get("derived_from"),
                           "operation": (ev.get("conversion") or {}).get("method")}))
        for cid in chain_ids or []:
            ch = self.wb.get_chain(cid)
            if ch["case_id"] != case_id:
                raise ValueError(f"chain {cid} belongs to another case")
            png, info = self.wb.export_chain(cid, actor)
            files.append((f"derived/{cid}_{_safe(ch['name'])}.png", png,
                          {"kind": "processed derivative", "chain_id": cid, "evidence_id": ch["evidence_id"],
                           "graph_hash": ch["graph_hash"], "output_pixel_hash": ch["output_hash"],
                           "steps": [f"{n['filter']}@{n['version']}" for n in ch["graph"]["nodes"] if n["enabled"]]}))
        if include_report:
            from .report import build_bundle, render_pdf

            bundle = build_bundle(self.wb, case_id, actor, verify=False)
            files.append(("report/report.pdf", render_pdf(bundle), {"kind": "report"}))
        entries = []
        for name, data, meta in sorted(files, key=lambda f: f[0]):
            entries.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "sha512": hashlib.sha512(data).hexdigest(), **meta})
        manifest = {
            "manifest_version": 1,
            "application": f"{APP_NAME} {__version__}",
            "case": {"id": case_id, "name": case["name"], "case_number": case.get("case_number", "")},
            "purpose": purpose.strip()[:1000], "recipient": recipient.strip()[:300],
            "created_by": actor.name, "created_at": now_iso(),
            "files": entries,
            "note": "Original evidence files are bit-for-bit copies of the stored originals. Derived files are "
                    "products of the listed operations and are not originals.",
        }
        mjson = json.dumps(manifest, indent=2, sort_keys=True).encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data, _ in sorted(files, key=lambda f: f[0]):
                z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
            z.writestr(zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0)), mjson)
            z.writestr(zipfile.ZipInfo("manifest.json.sha256", date_time=(1980, 1, 1, 0, 0, 0)),
                       (hashlib.sha256(mjson).hexdigest() + "  manifest.json\n").encode())
        pkg = buf.getvalue()
        digest = hashlib.sha256(pkg).hexdigest()
        self.wb.store.put_derived(pkg, "zip")
        xid = new_id()
        self.db.execute("INSERT INTO exports (id, case_id, purpose, recipient, manifest_json, package_sha256, actor,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (xid, case_id, manifest["purpose"], manifest["recipient"], canonical_json(manifest), digest,
                         actor.label, now_iso()))
        for e in entries:
            if e.get("evidence_id") and e["kind"].endswith("evidence"):
                self.custody_event(case_id, e["evidence_id"], "exported", actor, to_party=recipient,
                                   notes=purpose, details={"export_id": xid, "package_sha256": digest})
        self.wb.audit.append(actor, "export.package", case_id=case_id, target=xid,
                             params={"files": [{"path": e["path"], "sha256": e["sha256"]} for e in entries],
                                     "purpose": manifest["purpose"], "recipient": manifest["recipient"]},
                             output_hash=digest)
        return pkg, {"export_id": xid, "package_sha256": digest, "manifest": manifest}

    def exports(self, case_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM exports WHERE case_id = ? ORDER BY created_at", (case_id,))
        for r in rows:
            r["manifest"] = json.loads(r.pop("manifest_json"))
        return rows

    def export_bytes(self, export_id: str) -> bytes:
        row = self.db.one("SELECT package_sha256 FROM exports WHERE id = ?", (export_id,))
        if not row:
            from .workbench import NotFound

            raise NotFound(f"export {export_id} not found")
        data = self.wb.store.derived_path(row["package_sha256"], "zip").read_bytes()
        if hashlib.sha256(data).hexdigest() != row["package_sha256"]:
            raise ValueError("stored export package does not match its recorded hash")
        return data

    @staticmethod
    def verify_package(data: bytes) -> dict:
        """Check a delivered package against its own manifest (the recipient can run this too)."""
        problems = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            mjson = z.read("manifest.json")
            want = z.read("manifest.json.sha256").decode().split()[0]
            if hashlib.sha256(mjson).hexdigest() != want:
                problems.append("manifest.json does not match manifest.json.sha256")
            manifest = json.loads(mjson)
            names = set(z.namelist()) - {"manifest.json", "manifest.json.sha256"}
            listed = {f["path"] for f in manifest["files"]}
            for extra in sorted(names - listed):
                problems.append(f"{extra} is in the package but not in the manifest")
            for f in manifest["files"]:
                if f["path"] not in names:
                    problems.append(f"{f['path']} is listed but missing")
                    continue
                d = z.read(f["path"])
                if hashlib.sha256(d).hexdigest() != f["sha256"] or hashlib.sha512(d).hexdigest() != f["sha512"]:
                    problems.append(f"{f['path']} does not match its manifest hashes")
        return {"ok": not problems, "files": len(manifest["files"]), "problems": problems}

    # Branches and presets -----------------------------------------------------------------------------

    def fork_chain(self, chain_id: str, actor: Actor, *, upto: str | None = None, name: str = "") -> dict:
        """A new branch from the same original: the steps of `chain_id` up to node `upto`. The parent
        chain is untouched, so earlier outputs are never overwritten."""
        ch = self.wb.get_chain(chain_id)
        nodes = []
        for n in ch["graph"]["nodes"]:
            nodes.append(n)
            if upto and n["id"] == upto:
                break
        else:
            if upto:
                raise ValueError(f"step {upto} is not in that chain")
        g = {"source": ch["graph"]["source"], "nodes": nodes}
        new = self.wb.create_chain(ch["case_id"], ch["evidence_id"], name or f"{ch['name']} (branch)", actor, g)
        self.db.execute("UPDATE chains SET parent_chain_id = ? WHERE id = ?", (chain_id, new["id"]))
        self.wb.audit.append(actor, "chain.fork", case_id=ch["case_id"], target=new["id"],
                             params={"parent_chain_id": chain_id, "upto": upto, "graph_hash": new["graph_hash"]})
        return self.wb.get_chain(new["id"])

    def save_preset(self, chain_id: str, name: str, actor: Actor, description: str = "") -> dict:
        ch = self.wb.get_chain(chain_id)
        if not name.strip():
            raise ValueError("name the preset")
        nodes = [{k: n[k] for k in ("id", "filter", "version", "params", "enabled")} for n in ch["graph"]["nodes"]]
        if any(n["params"].get("points") or n["params"].get("roi") for n in nodes):
            description = (description + " (contains clicked points or regions tied to the original image; "
                           "check them on each new item)").strip()
        pid = new_id()
        self.db.execute("INSERT INTO presets (id, name, description, nodes_json, actor, created_at) VALUES (?,?,?,?,?,?)",
                        (pid, name.strip()[:200], description[:2000], canonical_json(nodes), actor.label, now_iso()))
        self.wb.audit.append(actor, "preset.save", case_id=ch["case_id"], target=pid,
                             params={"name": name.strip(), "from_chain": chain_id, "nodes": nodes})
        return self.get_preset(pid)

    def get_preset(self, pid: str) -> dict:
        row = self.db.one("SELECT * FROM presets WHERE id = ?", (pid,))
        if not row:
            from .workbench import NotFound

            raise NotFound(f"preset {pid} not found")
        row["nodes"] = json.loads(row.pop("nodes_json"))
        return row

    def presets(self) -> list[dict]:
        return [self.get_preset(r["id"]) for r in self.db.all("SELECT id FROM presets ORDER BY created_at")]

    def apply_preset(self, preset_id: str, case_id: str, evidence_id: str, actor: Actor, name: str = "") -> dict:
        p = self.get_preset(preset_id)
        ch = self.wb.create_chain(case_id, evidence_id, name or p["name"], actor, {"source": {}, "nodes": p["nodes"]})
        self.wb.audit.append(actor, "preset.apply", case_id=case_id, target=ch["id"],
                             params={"preset_id": preset_id, "evidence_id": evidence_id})
        return ch


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name)[:80]
