"""The examiner Lucida records in the audit log.

Lucida has no sign-in. Every request acts as one built-in local examiner, so the audit log,
reports and the job queue still say who did what. The examiner's name starts as the first
administrator's name on an installation that had accounts, else the computer's account name
(its full name where the system has one), and can be changed from the name at the
top right of the window; the change is recorded in the audit log.
"""

from __future__ import annotations

import getpass
import os
import threading

from .audit import Actor
from .db import Database, now_iso

LOCAL_USERNAME = "local"
MAX_NAME = 120


def actor_for(user: dict) -> Actor:
    return Actor(user["full_name"], "human")


def default_examiner_name() -> str:
    """FORENSIC_EXAMINER if set, else the system account's full name, else its login name."""
    name = os.environ.get("FORENSIC_EXAMINER", "").strip()
    if not name:
        try:
            import pwd

            name = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
        except (ImportError, KeyError, AttributeError):
            name = ""
    if not name:
        try:
            name = getpass.getuser()
        except Exception:  # noqa: BLE001 - no login name on this system
            name = ""
    return clean_name(name) or "Examiner"


def clean_name(name: str) -> str:
    return " ".join(str(name).split())[:MAX_NAME]


class Accounts:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._user: dict | None = None

    def local_user(self) -> dict:
        """The built-in examiner, created on first use."""
        if self._user is None:
            with self._lock:
                if self._user is None:
                    row = self.db.one("SELECT username, full_name, role FROM users WHERE username = ?", (LOCAL_USERNAME,))
                    if not row:
                        # An installation that had accounts keeps its first administrator's name.
                        old = self.db.one("SELECT full_name FROM users WHERE role = 'admin' AND disabled = 0"
                                          " ORDER BY created_at LIMIT 1")
                        name = old["full_name"] if old else default_examiner_name()
                        # No password: nobody signs in; the row only names the examiner.
                        self.db.execute(
                            "INSERT INTO users (username, full_name, role, password_hash, disabled, created_at)"
                            " VALUES (?,?,?,?,0,?)",
                            (LOCAL_USERNAME, name, "admin", "", now_iso()),
                        )
                        row = self.db.one("SELECT username, full_name, role FROM users WHERE username = ?",
                                          (LOCAL_USERNAME,))
                    self._user = dict(row) | {"role": "admin"}
        return dict(self._user)

    def rename(self, full_name: str) -> dict:
        name = clean_name(full_name)
        if not name:
            raise ValueError("enter the examiner's full name (it appears in the audit log and reports)")
        self.local_user()
        with self._lock:
            self.db.execute("UPDATE users SET full_name = ? WHERE username = ?", (name, LOCAL_USERNAME))
            self._user = None
        return self.local_user()
