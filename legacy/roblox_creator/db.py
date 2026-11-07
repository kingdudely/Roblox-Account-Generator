"""SQLite store — one writer lock, WAL, real queries.

JSONL append works for ten accounts. At 35/min it means concurrent appends to one
file, O(n) duplicate checks, and no way to ask "how many survived verification".
SQLite with WAL handles the write rate without breaking a sweat and gives you a
queryable record of what your proxies and captchas actually did.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .store import AccountRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username     TEXT PRIMARY KEY,
    password     TEXT NOT NULL,
    birthday     TEXT,
    gender       TEXT,
    user_id      INTEGER,
    display_name TEXT,
    cookie       TEXT,
    status       TEXT NOT NULL,
    note         TEXT,
    proxy        TEXT,
    captcha      TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status  ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_created ON accounts(created_at);
CREATE INDEX IF NOT EXISTS idx_proxy   ON accounts(proxy);
"""


class AccountDB:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / "accounts.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- AccountStore interface ----------------------------------------------

    def is_taken(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM accounts WHERE lower(username)=? AND status='created' LIMIT 1",
                (username.lower(),),
            )
            return cur.fetchone() is not None

    def append(self, record: AccountRecord) -> None:
        row = record.to_dict()
        with self._lock:
            self._conn.execute(
                """INSERT INTO accounts
                   (username, password, birthday, gender, user_id, display_name,
                    cookie, status, note, proxy, captcha, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(username) DO UPDATE SET
                     status=excluded.status, note=excluded.note,
                     cookie=excluded.cookie, captcha=excluded.captcha""",
                (
                    row["username"], row["password"], row["birthday"], row["gender"],
                    row["user_id"], row["display_name"], row["cookie"], row["status"],
                    row["note"], row["proxy"], row["captcha"], row["created_at"],
                ),
            )
            self._conn.commit()

    # -- reporting ------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def counts(self) -> dict[str, int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT status, COUNT(*) FROM accounts GROUP BY status ORDER BY 2 DESC"
            )
            return {status: n for status, n in cur.fetchall()}

    def captcha_breakdown(self) -> dict[str, int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT captcha, COUNT(*) FROM accounts WHERE status='created' GROUP BY captcha"
            )
            return {name or "none": n for name, n in cur.fetchall()}

    def proxy_breakdown(self, limit: int = 10) -> list[tuple[str, int, int]]:
        """(proxy, created, total) — worst performers first."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT proxy,
                          SUM(CASE WHEN status='created' THEN 1 ELSE 0 END) AS ok,
                          COUNT(*) AS total
                   FROM accounts WHERE proxy IS NOT NULL
                   GROUP BY proxy HAVING total >= 3
                   ORDER BY (CAST(ok AS REAL) / total) ASC LIMIT ?""",
                (limit,),
            )
            return [(p, ok, total) for p, ok, total in cur.fetchall()]

    def created_since(self, hours: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                """SELECT COUNT(*) FROM accounts
                   WHERE status='created' AND created_at >= datetime('now', ?)""",
                (f"-{hours} hours",),
            )
            return cur.fetchone()[0]

    # -- exports --------------------------------------------------------------

    def export_credentials(self, path: Path | None = None) -> Path:
        path = path or (self.out_dir / "credentials.txt")
        with self._lock:
            cur = self._conn.execute(
                "SELECT username, password FROM accounts WHERE status='created' ORDER BY created_at"
            )
            rows = cur.fetchall()
        path.write_text("".join(f"{u}:{p}\n" for u, p in rows))
        return path

    def export_cookies(self, path: Path | None = None) -> Path:
        path = path or (self.out_dir / "cookies.json")
        with self._lock:
            cur = self._conn.execute(
                """SELECT username, user_id, cookie FROM accounts
                   WHERE status='created' AND cookie IS NOT NULL ORDER BY created_at"""
            )
            rows = cur.fetchall()
        path.write_text(json.dumps(
            [{"username": u, "userId": i, ".ROBLOSECURITY": c} for u, i, c in rows],
            indent=2,
        ))
        return path
