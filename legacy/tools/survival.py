#!/usr/bin/env python3
"""Track whether created accounts are still alive, hour by hour.

Creation is the easy half. The funder's warning is the real one: accounts have to
survive 18+ hours, and nothing in this repo measured that. This does.

What it records per check:
  * `auth` — HTTP status from users.roblox.com/v1/users/authenticated with the
    account's stored .ROBLOSECURITY. 200 means the session still exists.
  * `exists` — whether the username still resolves publicly. Separates "session
    invalidated" from "account deleted".
  * `alive` — the verdict: auth 200.

Checks accumulate in a `survival_checks` table so age curves are real data, not
memory. Then `--report` answers the only question that matters: what fraction of
accounts from each cohort is alive at 1h, 6h, 18h, 24h, 72h — sliced by proxy,
captcha path, and accounts-per-proxy, which is where bans usually come from.

    ./tools/survival.py --check                 # probe everything due
    ./tools/survival.py --check --min-age 60    # only accounts older than 1h
    ./tools/survival.py --report
    ./tools/survival.py --report --by proxy
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUTHENTICATED = "https://users.roblox.com/v1/users/authenticated"
USER_LOOKUP = "https://users.roblox.com/v1/usernames/users"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

SCHEMA = """
CREATE TABLE IF NOT EXISTS survival_checks (
    username   TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    age_hours  REAL NOT NULL,
    auth       INTEGER,
    exists_    INTEGER,
    alive      INTEGER NOT NULL,
    verdict    TEXT,
    login      INTEGER,
    note       TEXT,
    PRIMARY KEY (username, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_sc_user ON survival_checks(username);
CREATE INDEX IF NOT EXISTS idx_sc_age  ON survival_checks(age_hours);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # older DBs predate the verdict/login columns
    have = {r[1] for r in conn.execute("PRAGMA table_info(survival_checks)")}
    for col, decl in (("verdict", "TEXT"), ("login", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE survival_checks ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def _session():
    from curl_cffi import requests as creq

    return creq.Session(impersonate="chrome", timeout=25,
                        headers={"user-agent": UA, "accept": "application/json"})


def probe(cookie: str, username: str, proxy: str | None = None) -> dict:
    """One liveness probe. Never raises — a network failure is 'unknown', not 'dead'."""
    s = _session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}

    auth, exists, note = None, None, ""
    try:
        r = s.get(AUTHENTICATED, cookies={".ROBLOSECURITY": cookie})
        auth = r.status_code
        if r.status_code == 200:
            body = r.json()
            note = f"id={body.get('id')}"
    except Exception as e:
        note = f"auth error: {type(e).__name__}"
        s.close()
        return {"auth": None, "exists": None, "alive": None, "note": note}

    # only worth asking whether the name still resolves when the session is gone
    if auth != 200:
        try:
            r = s.post(USER_LOOKUP, json={"usernames": [username],
                                          "excludeBannedUsers": False})
            if r.status_code == 200:
                exists = 1 if (r.json().get("data") or []) else 0
        except Exception:
            pass

    s.close()
    return {"auth": auth, "exists": exists, "alive": 1 if auth == 200 else 0,
            "note": note}


def login_probe(username: str, password: str, proxy: str | None = None) -> dict:
    """Try to obtain a *fresh* session with the stored credentials.

    Roblox wants a CSRF token: the first POST answers 403 with `x-csrf-token`,
    and the retry carries it. A 200 here means the account is still usable even
    if its stored cookie was killed — which is exactly the "session flagged"
    case the funder described, and a very different business outcome from a
    terminated account.
    """
    from curl_cffi import requests as creq

    s = creq.Session(impersonate="chrome", timeout=30,
                     headers={"user-agent": UA, "accept": "application/json",
                              "origin": "https://www.roblox.com",
                              "referer": "https://www.roblox.com/"})
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}

    body = {"ctype": "Username", "cvalue": username,
            "password": password, "secure": True}
    out = {"login": None, "note": "", "cookie": None}
    try:
        r = s.post("https://auth.roblox.com/v2/login", json=body)
        token = r.headers.get("x-csrf-token")
        if r.status_code == 403 and token:
            r = s.post("https://auth.roblox.com/v2/login", json=body,
                       headers={"x-csrf-token": token})
        out["login"] = r.status_code
        if r.status_code == 200:
            out["cookie"] = s.cookies.get(".ROBLOSECURITY")
        else:
            try:
                out["note"] = (r.json().get("errors") or [{}])[0].get("message", "")[:80]
            except Exception:
                out["note"] = (r.text or "")[:80]
    except Exception as e:
        out["note"] = f"{type(e).__name__}"
    finally:
        s.close()
    return out


def _age_hours(created_at: str) -> float:
    try:
        t = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0


def due_accounts(conn: sqlite3.Connection, min_age: float, recheck_after: float,
                 limit: int) -> list[sqlite3.Row]:
    """Accounts that are created, have a cookie, and have not been checked recently."""
    rows = conn.execute(
        """
        SELECT a.username, a.password, a.cookie, a.proxy, a.captcha, a.created_at,
               (SELECT MAX(c.checked_at) FROM survival_checks c
                 WHERE c.username = a.username) AS last_check
          FROM accounts a
         WHERE a.status = 'created' AND a.cookie IS NOT NULL
         ORDER BY a.created_at ASC
        """
    ).fetchall()

    now = time.time()
    out = []
    for r in rows:
        age = _age_hours(r["created_at"])
        if age < min_age:
            continue
        if r["last_check"]:
            try:
                last = datetime.fromisoformat(r["last_check"]).timestamp()
            except Exception:
                last = 0
            if now - last < recheck_after * 3600:
                continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def do_check(args) -> int:
    conn = connect(args.db)
    rows = due_accounts(conn, args.min_age, args.recheck_after, args.limit)
    if not rows:
        print("  nothing due")
        return 0
    print(f"  checking {len(rows)} account(s)")

    for r in rows:
        age = _age_hours(r["created_at"])
        res = probe(r["cookie"], r["username"], r["proxy"] if args.via_proxy else None)

        verdict = "unknown"
        login = None
        if res["alive"] == 1:
            verdict = "alive"
        elif res["alive"] == 0:
            if res["exists"] == 0:
                verdict = "deleted"
            elif args.login_probe:
                lp = login_probe(r["username"], r["password"],
                                 r["proxy"] if args.via_proxy else None)
                login = lp["login"]
                if login == 200:
                    verdict = "session_flagged"
                    res["note"] = (res["note"] + " | login ok").strip(" |")
                elif login == 429:
                    verdict = "rate_limited"
                else:
                    verdict = "locked"
                    if lp["note"]:
                        res["note"] = (res["note"] + " | " + lp["note"]).strip(" |")
            else:
                verdict = "cookie_dead"

        conn.execute(
            "INSERT OR REPLACE INTO survival_checks "
            "(username, checked_at, age_hours, auth, exists_, alive, verdict, login, note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (r["username"],
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             round(age, 3), res["auth"], res["exists"], res["alive"] or 0,
             verdict, login, res["note"] or ""),
        )
        conn.commit()
        print(f"    {verdict:16} {r['username']:24} {age:6.1f}h auth={res['auth']} "
              f"exists={res['exists']} login={login} {res['note']}")
    return 0


def report(args) -> int:
    conn = connect(args.db)
    total = conn.execute("SELECT COUNT(*) FROM accounts WHERE status='created'").fetchone()[0]
    checked = conn.execute("SELECT COUNT(DISTINCT username) FROM survival_checks").fetchone()[0]
    print(f"  accounts created: {total}   ever checked: {checked}")

    rows = conn.execute(
        """
        SELECT c.username, c.age_hours, c.alive, c.auth,
               a.proxy, a.captcha, a.created_at
          FROM survival_checks c JOIN accounts a ON a.username = c.username
        """
    ).fetchall()
    if not rows:
        print("  no checks yet — run --check first")
        return 0

    # survival curve: each account counted once per bucket, at its latest check
    buckets = [(0, 1), (1, 6), (6, 18), (18, 24), (24, 72), (72, 10**9)]
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        cur = latest.get(r["username"])
        if cur is None or r["age_hours"] > cur["age_hours"]:
            latest[r["username"]] = r

    def verdict_of(r) -> str:
        try:
            v = r["verdict"]
        except (IndexError, KeyError):
            v = None
        if v:
            return v
        return "alive" if r["alive"] else "cookie_dead"

    print("\n  by verdict (latest check each)")
    counts: dict[str, int] = {}
    for r in latest.values():
        counts[verdict_of(r)] = counts.get(verdict_of(r), 0) + 1
    for v, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {v:16} {n:>5}  {n / len(latest):>5.0%}")

    print("\n  survival curve")
    print(f"  {'age':>10}  {'usable':>14}  {'session_flagged':>16}  {'dead':>6}")
    for lo, hi in buckets:
        grp = [r for r in latest.values() if lo <= r["age_hours"] < hi]
        if not grp:
            continue
        usable = sum(1 for r in grp if verdict_of(r) == "alive")
        flagged = sum(1 for r in grp if verdict_of(r) == "session_flagged")
        dead = len(grp) - usable - flagged
        print(f"  {f'{lo}-{hi}h':>10}  {usable:>6}/{len(grp):<7}"
              f"  {flagged:>8}/{len(grp):<7}  {dead:>6}")

    if args.by:
        key = args.by
        print(f"\n  by {key}")
        groups: dict[str, list] = {}
        for r in latest.values():
            v = r[key] if key in r.keys() else None
            groups.setdefault(str(v), []).append(r)
        for v, grp in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            alive = sum(1 for r in grp if r["alive"])
            print(f"    {v[:44]:46} {alive:>4}/{len(grp):<4} {alive / len(grp):>5.0%}")

    # linkage: do accounts created from the same proxy die together?
    print("\n  accounts per proxy (top)")
    for r in conn.execute(
        """SELECT proxy, COUNT(*) n FROM accounts
            WHERE status='created' GROUP BY proxy ORDER BY n DESC LIMIT 8"""):
        print(f"    {str(r['proxy'])[:52]:54} {r['n']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "accounts" / "accounts.db")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--min-age", type=float, default=0.0,
                    help="only accounts at least this many hours old")
    ap.add_argument("--recheck-after", type=float, default=1.0,
                    help="hours before an account is probed again")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--login-probe", action="store_true",
                    help="when the cookie is dead, try logging in to separate "
                         "'session flagged' from 'account locked' (use sparingly)")
    ap.add_argument("--via-proxy", action="store_true",
                    help="probe through the account's own proxy")
    ap.add_argument("--by", default=None, choices=["proxy", "captcha"],
                    help="break the report down by a column")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db}")
        return 2
    if args.check:
        do_check(args)
    if args.report or not args.check:
        report(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
