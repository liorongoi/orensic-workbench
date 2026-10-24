"""Optional sign-in for shared lab installations (FORENSIC_AUTH=accounts).

By default Lucida has no sign-in: one examiner works on their own computer (see accounts.py).
A lab that runs one server for several people sets FORENSIC_AUTH=accounts, and then:

- every API request needs a session (cookie `lucida_session` or `Authorization: Bearer`);
- passwords are stored as scrypt hashes (n=2^15, r=8, p=1, 16-byte salt), at least 12 characters;
- five wrong passwords lock the account for 15 minutes;
- a session ends after FORENSIC_SESSION_MINUTES (default 30) without use, and after 12 hours at most;
- roles: admin (everything, all cases, user management), examiner (works on cases they are a
  member of), reviewer (reads member cases and records review decisions), viewer (reads member
  cases only);
- each case lists its members; requests for a case the user is not a member of are refused
  and the refusal is audited.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from .audit import Actor
from .db import Database, now_iso

ROLES = ("admin", "examiner", "reviewer", "viewer")
CASE_ROLES = ("examiner", "reviewer", "viewer")
MAX_FAILURES = 5
LOCK_MINUTES = 15
MAX_SESSION_HOURS = 12
MIN_PASSWORD = 12
# Write actions a reviewer may take (path suffixes).
REVIEW_ACTIONS = ("/review", "/accept", "/notes", "/bookmarks", "/verify")


def enabled() -> bool:
    return os.environ.get("FORENSIC_AUTH", "").strip().lower() == "accounts"


def idle_minutes() -> int:
    try:
        return max(5, min(24 * 60, int(os.environ.get("FORENSIC_SESSION_MINUTES", "30"))))
    except ValueError:
        return 30


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 << 20, dklen=32)
    return f"scrypt$32768$8$1${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 << 20, dklen=len(h) // 2)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuthError(PermissionError):
    pass


class Security:
    def __init__(self, db: Database, audit=None):
        self.db = db
        self.audit = audit

    # Users --------------------------------------------------------------------------------------------

    def has_users(self) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM users WHERE username != 'local' AND password_hash != '' LIMIT 1"))

    def create_user(self, username: str, full_name: str, role: str, password: str, by: Actor | None = None) -> dict:
        username = username.strip().lower()
        if not username.isascii() or not username.replace(".", "").replace("-", "").replace("_", "").isalnum() \
                or not 2 <= len(username) <= 40 or username == "local":
            raise ValueError("user names are 2-40 letters, digits, dots, dashes or underscores")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"passwords need at least {MIN_PASSWORD} characters")
        if not full_name.strip():
            raise ValueError("give the person's full name (it appears in the audit log)")
        if self.db.one("SELECT 1 AS x FROM users WHERE username = ?", (username,)):
            raise ValueError("that user name is taken")
        self.db.execute("INSERT INTO users (username, full_name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
                        (username, full_name.strip()[:120], role, hash_password(password), now_iso()))
        if self.audit and by:
            self.audit.append(by, "user.create", target=username, params={"role": role, "full_name": full_name.strip()})
        return self.get_user(username)

    def get_user(self, username: str) -> dict:
        u = self.db.one("SELECT username, full_name, role, disabled, created_at, failed_logins, locked_until FROM users"
                        " WHERE username = ?", (username,))
        if not u:
            raise ValueError("no such user")
        return u

    def users(self) -> list[dict]:
        return self.db.all("SELECT username, full_name, role, disabled, created_at, locked_until FROM users"
                           " WHERE username != 'local' ORDER BY username")

    def update_user(self, username: str, by: Actor, *, role: str | None = None, disabled: bool | None = None,
                    password: str | None = None) -> dict:
        self.get_user(username)
        if role is not None:
            if role not in ROLES:
                raise ValueError(f"role must be one of {', '.join(ROLES)}")
            self.db.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        if disabled is not None:
            self.db.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), username))
            if disabled:
                self.db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        if password is not None:
            if len(password) < MIN_PASSWORD:
                raise ValueError(f"passwords need at least {MIN_PASSWORD} characters")
            self.db.execute("UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL WHERE username = ?",
                            (hash_password(password), username))
            self.db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        if self.audit:
            self.audit.append(by, "user.update", target=username,
                              params={"role": role, "disabled": disabled, "password_reset": password is not None})
        return self.get_user(username)

    # Sessions -----------------------------------------------------------------------------------------

    def login(self, username: str, password: str, client: str = "") -> tuple[str, dict]:
        username = username.strip().lower()
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        now = _now()
        if row and row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
            self._audit_login(username, False, client, "locked")
            raise AuthError(f"too many wrong passwords; try again after {row['locked_until']}")
        if not row or row["disabled"] or not row["password_hash"] or not check_password(password, row["password_hash"]):
            if row:
                fails = row["failed_logins"] + 1
                lock = _ts(now + timedelta(minutes=LOCK_MINUTES)) if fails >= MAX_FAILURES else None
                self.db.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE username = ?",
                                (0 if lock else fails, lock, username))
            self._audit_login(username, False, client, "wrong user name or password")
            raise AuthError("wrong user name or password")
        self.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE username = ?", (username,))
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions (token_hash, username, created_at, expires_at) VALUES (?,?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(), username, _ts(now),
                         _ts(now + timedelta(minutes=idle_minutes()))))
        self._audit_login(username, True, client, "")
        return token, self.get_user(username)

    def _audit_login(self, username: str, ok: bool, client: str, why: str) -> None:
        if self.audit:
            self.audit.append(Actor(username or "unknown", "human"), "auth.login" if ok else "auth.login_failed",
                              target=username, params={"client": client[:100], "reason": why})

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        th = hashlib.sha256(token.encode()).hexdigest()
        s = self.db.one("SELECT * FROM sessions WHERE token_hash = ?", (th,))
        now = _now()
        if not s:
            return None
        if datetime.fromisoformat(s["expires_at"]) < now or \
                datetime.fromisoformat(s["created_at"]) + timedelta(hours=MAX_SESSION_HOURS) < now:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            return None
        u = self.db.one("SELECT username, full_name, role, disabled FROM users WHERE username = ?", (s["username"],))
        if not u or u["disabled"]:
            return None
        self.db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                        (_ts(now + timedelta(minutes=idle_minutes())), th))
        return u

    def logout(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))

    # Case membership --------------------------------------------------------------------------------------

    def add_member(self, case_id: str, username: str, role: str, by: Actor) -> list[dict]:
        if role not in CASE_ROLES:
            raise ValueError(f"case role must be one of {', '.join(CASE_ROLES)}")
        self.get_user(username)
        self.db.execute("INSERT OR REPLACE INTO case_members (case_id, username, role, added_at) VALUES (?,?,?,?)",
                        (case_id, username, role, now_iso()))
        if self.audit:
            self.audit.append(by, "case.member_add", case_id=case_id, target=username, params={"role": role})
        return self.members(case_id)

    def remove_member(self, case_id: str, username: str, by: Actor) -> list[dict]:
        self.db.execute("DELETE FROM case_members WHERE case_id = ? AND username = ?", (case_id, username))
        if self.audit:
            self.audit.append(by, "case.member_remove", case_id=case_id, target=username)
        return self.members(case_id)

    def members(self, case_id: str) -> list[dict]:
        return self.db.all("SELECT m.username, m.role, m.added_at, u.full_name FROM case_members m JOIN users u"
                           " ON u.username = m.username WHERE m.case_id = ? ORDER BY m.username", (case_id,))

    def case_role(self, user: dict, case_id: str) -> str | None:
        if user["role"] == "admin":
            return "admin"
        m = self.db.one("SELECT role FROM case_members WHERE case_id = ? AND username = ?", (case_id, user["username"]))
        if not m:
            return None
        # The weaker of the account role and the case role applies.
        order = {"examiner": 3, "reviewer": 2, "viewer": 1}
        return min((m["role"], user["role"]), key=lambda r: order.get(r, 0))

    def visible_cases(self, user: dict) -> list[str] | None:
        """None means every case (administrators)."""
        if user["role"] == "admin":
            return None
        return [r["case_id"] for r in self.db.all("SELECT case_id FROM case_members WHERE username = ?", (user["username"],))]

    def case_of(self, params: dict) -> str | None:
        """The case a request is about, from its path parameters."""
        if params.get("case_id"):
            return params["case_id"]
        lookups = {"evidence_id": "SELECT case_id FROM evidence WHERE id = ?",
                   "chain_id": "SELECT case_id FROM chains WHERE id = ?",
                   "run_id": "SELECT case_id FROM detection_runs WHERE id = ?",
                   "track_id": "SELECT case_id FROM tracks WHERE id = ?",
                   "plate_id": "SELECT case_id FROM plates WHERE id = ?",
                   "doc_id": "SELECT case_id FROM annotations WHERE id = ?",
                   "bookmark_id": "SELECT case_id FROM bookmarks WHERE id = ?",
                   "export_id": "SELECT case_id FROM exports WHERE id = ?",
                   "camera_id": "SELECT case_id FROM cameras WHERE id = ?"}
        for k, q in lookups.items():
            if params.get(k):
                r = self.db.one(q, (params[k],))
                return r["case_id"] if r else None
        return None

    def authorize(self, user: dict, method: str, path: str, params: dict) -> None:
        """Raise AuthError unless `user` may make this request."""
        write = method not in ("GET", "HEAD", "OPTIONS")
        admin_only = path.startswith("/api/users") or path.startswith("/api/batches") and write
        if admin_only and user["role"] != "admin":
            raise AuthError("only an administrator can do that")
        case_id = self.case_of(params)
        if case_id is None:
            if write and user["role"] == "viewer":
                raise AuthError("viewers cannot change anything")
            return
        role = self.case_role(user, case_id)
        if role is None:
            if self.audit:
                self.audit.append(Actor(user["full_name"], "human"), "auth.denied", case_id=case_id,
                                  params={"path": path[:200], "method": method})
            raise AuthError("you are not a member of this case")
        if write and ("/members" in path) and role != "admin":
            raise AuthError("only an administrator can change case members")
        if write and role == "viewer":
            raise AuthError("viewers cannot change anything")
        if write and role == "reviewer" and not any(path.endswith(a) or f"{a}/" in path for a in REVIEW_ACTIONS):
            raise AuthError("reviewers can only record review decisions, notes and bookmarks")
