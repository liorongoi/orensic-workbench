"""Local user accounts and sign-in sessions.

The workbench runs on an offline lab machine that several examiners share, so it keeps its
own accounts instead of trusting a name the browser sends. The first person to open a fresh
installation creates the administrator account; the administrator then adds the examiners.
Every action is recorded in the audit log under the signed-in account's full name.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from .audit import Actor
from .db import Database, now_iso

ROLES = ("admin", "examiner")
SESSION_HOURS = 12
MIN_PASSWORD = 8
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# Repeated wrong passwords lock the account name for a short while.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
# scrypt cost: about 50 ms per sign-in on a lab workstation.
_N, _R, _P = 2**14, 8, 1


class AuthError(Exception):
    """Sign-in failed or the session is no longer valid (HTTP 401)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), dk)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def actor_for(user: dict) -> Actor:
    return Actor(user["full_name"], "human")


class Accounts:
    def __init__(self, db: Database):
        self.db = db
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # Users ----------------------------------------------------------------------------------

    def setup_required(self) -> bool:
        return self.db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0

    def create_user(self, username: str, full_name: str, password: str, role: str = "examiner", *,
                    first_admin: bool = False) -> dict:
        username = username.strip().lower()
        full_name = " ".join(full_name.split())
        if not _USERNAME.match(username):
            raise ValueError("user name: 2-32 characters, lower-case letters, digits, dot, dash or underscore")
        if not full_name or len(full_name) > 120:
            raise ValueError("enter the examiner's full name (it appears in the audit log and reports)")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        _check_new_password(password)
        with self.db.transaction():
            if first_admin and not self.setup_required():
                raise PermissionError("the administrator account already exists; sign in instead")
            if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
                raise ValueError(f"the user name {username} is taken")
            if self.db.one("SELECT 1 FROM users WHERE lower(full_name) = lower(?)", (full_name,)):
                raise ValueError(f"another account already uses the name {full_name}; the audit log needs them to differ")
            self.db.execute(
                "INSERT INTO users (username, full_name, role, password_hash, disabled, created_at) VALUES (?,?,?,?,0,?)",
                (username, full_name, "admin" if first_admin else role, hash_password(password), now_iso()),
            )
        return self.get_user(username)

    def get_user(self, username: str) -> dict:
        row = self.db.one("SELECT username, full_name, role, disabled, created_at FROM users WHERE username = ?",
                          (username.strip().lower(),))
        if not row:
            raise LookupError(f"no user {username}")
        return row | {"disabled": bool(row["disabled"])}

    def list_users(self) -> list[dict]:
        rows = self.db.all("SELECT username, full_name, role, disabled, created_at FROM users ORDER BY username")
        return [r | {"disabled": bool(r["disabled"])} for r in rows]

    def update_user(self, username: str, *, role: str | None = None, disabled: bool | None = None,
                    password: str | None = None) -> dict:
        user = self.get_user(username)
        if role is not None and role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        demoting = (role is not None and role != "admin") or bool(disabled)
        if user["role"] == "admin" and demoting:
            admins = self.db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0")["n"]
            if admins <= 1:
                raise ValueError("keep at least one active administrator")
        if password is not None:
            _check_new_password(password)
            self.db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(password), user["username"]))
        if role is not None:
            self.db.execute("UPDATE users SET role = ? WHERE username = ?", (role, user["username"]))
        if disabled is not None:
            self.db.execute("UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), user["username"]))
        if disabled or password is not None:
            self.db.execute("DELETE FROM sessions WHERE username = ?", (user["username"],))
        return self.get_user(username)

    def change_password(self, username: str, current: str, new: str) -> None:
        row = self.db.one("SELECT password_hash FROM users WHERE username = ?", (username,))
        if not row or not check_password(current, row["password_hash"]):
            raise ValueError("the current password is wrong")
        _check_new_password(new)
        self.db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(new), username))

    # Sessions -------------------------------------------------------------------------------

    def login(self, username: str, password: str) -> tuple[str, dict]:
        """Check the password and open a session; returns (token, user)."""
        username = username.strip().lower()
        with self._lock:
            recent = [t for t in self._failures.get(username, []) if time.monotonic() - t < LOCKOUT_SECONDS]
            self._failures[username] = recent
            if len(recent) >= MAX_FAILURES:
                raise AuthError("too many wrong passwords; wait a minute and try again")
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        # Hash even for an unknown name so the response time does not reveal which names exist.
        ok = check_password(password, row["password_hash"] if row else hash_password("x"))
        if not row or not ok or row["disabled"]:
            with self._lock:
                self._failures.setdefault(username, []).append(time.monotonic())
            raise AuthError("wrong user name or password" if not (row and ok) else "this account is disabled")
        with self._lock:
            self._failures.pop(username, None)
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        self.db.execute("INSERT INTO sessions (token_hash, username, created_at, expires_at) VALUES (?,?,?,?)",
                        (_token_hash(token), username, now_iso(), expires.isoformat(timespec="milliseconds")))
        return token, self.get_user(username)

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.one(
            "SELECT u.username, u.full_name, u.role, u.disabled, s.expires_at FROM sessions s"
            " JOIN users u ON u.username = s.username WHERE s.token_hash = ?",
            (_token_hash(token),),
        )
        if not row or row["disabled"] or row["expires_at"] < now_iso():
            return None
        return {"username": row["username"], "full_name": row["full_name"], "role": row["role"]}

    def logout(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def _check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
