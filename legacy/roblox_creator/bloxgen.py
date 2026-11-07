"""BloxGen API client — end-to-end Roblox account generation.

    POST https://core.bloxgen.net/api/generate   {"apiKey", "type", "region"?}
    GET  https://core.bloxgen.net/api/balance?apiKey=...
    GET  https://core.bloxgen.net/api/prices?apiKey=...
    GET  https://core.bloxgen.net/api/daily-limit?apiKey=...

Throughput is capped by the **daily limit per key**, not by the cooldown, and the
limit depends on role (docs.bloxgen.net/guides/roles):

    role      alt/day   +30d   +1y    5y    dump   18+
    free        20       -      -      -      -      -
    premium     50      15      7      -      -      -
    18 ultra   100      30     15     10      5      3

So 50k alts/day needs 500 Ultra keys, not fifteen. Prices per generated account
(USD): alt 0.0004, +30d 0.0006, +1y 0.0008, 5y 0.0012, dump 0.005, 18+ 0.01.
Accounts arrive with a live .ROBLOSECURITY cookie; no browser, captcha or proxy
is involved on our side.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .store import AccountRecord, AccountStore

BASE = "https://core.bloxgen.net"
DEFAULT_COOLDOWN = 25.0


class BloxGenError(RuntimeError):
    pass


class Cooldown(BloxGenError):
    def __init__(self, seconds: float):
        super().__init__(f"cooldown {seconds:.0f}s")
        self.seconds = seconds


class OutOfStock(BloxGenError):
    pass


class DailyLimit(BloxGenError):
    pass


class Fatal(BloxGenError):
    pass


class Retryable(BloxGenError):
    pass


@dataclass
class KeyState:
    key: str
    ready_at: float = 0.0
    dead: bool = False
    reason: str = ""
    made: int = 0
    spent: float = 0.0
    cooldown: float = DEFAULT_COOLDOWN
    lock: threading.Lock = field(default_factory=threading.Lock)

    def masked(self) -> str:
        return self.key[:9] + "…" + self.key[-4:] if len(self.key) > 16 else "BLOX-…"


class BloxGenClient:
    def __init__(self, key: str, timeout: float = 30.0):
        self.key = key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "roblox-alt-creator/1.0",
        })

    def balance(self) -> tuple[float, str]:
        r = self.session.get(f"{BASE}/api/balance",
                             params={"apiKey": self.key}, timeout=self.timeout)
        body = self._json(r)
        if not body.get("success"):
            raise Fatal(body.get("message") or f"balance failed ({r.status_code})")
        data = body.get("data") or {}
        return float(data.get("balance", 0.0)), str(data.get("role", "?"))

    def prices(self) -> dict[str, float]:
        """Per-account price for every type this key's role can generate."""
        r = self.session.get(f"{BASE}/api/prices",
                             params={"apiKey": self.key}, timeout=self.timeout)
        body = self._json(r)
        if not body.get("success"):
            raise Fatal(body.get("message") or f"prices failed ({r.status_code})")
        return {k: float(v) for k, v in (body.get("data") or {}).items()}

    def daily_limit(self) -> dict:
        """Remaining quota per account type: {remaining, limit, reset, ...}."""
        r = self.session.get(f"{BASE}/api/daily-limit",
                             params={"apiKey": self.key}, timeout=self.timeout)
        body = self._json(r)
        if not body.get("success"):
            raise Fatal(body.get("message") or f"daily-limit failed ({r.status_code})")
        return body.get("data") or {}

    def generate(self, account_type: str = "alt", region: str | None = None) -> dict:
        payload = {"apiKey": self.key, "type": account_type}
        if region:
            payload["region"] = region
        r = self.session.post(f"{BASE}/api/generate", json=payload, timeout=self.timeout)
        body = self._json(r)
        if body.get("success"):
            return body.get("data") or {}
        self._raise(body, r.status_code)
        raise Retryable(body.get("message") or f"generate failed ({r.status_code})")

    @staticmethod
    def _json(r: requests.Response) -> dict:
        try:
            return r.json()
        except Exception:
            raise Retryable(f"non-JSON response ({r.status_code}): {r.text[:120]}")

    @staticmethod
    def _raise(body: dict, status: int) -> None:
        err = str(body.get("error") or "")
        msg = str(body.get("message") or "")
        if err == "Too Many Requests" or status == 429:
            if "Daily" in err or "Daily" in msg:
                raise DailyLimit(msg or "daily limit reached")
            seconds = float(body.get("timeRemaining") or 25_000) / 1000.0
            raise Cooldown(max(1.0, seconds))
        if status == 404:
            raise OutOfStock(msg or "no accounts available")
        if status in (401, 403):
            raise Fatal(msg or f"unauthorized ({status})")
        if status in (500, 503):
            raise Retryable(msg or f"server error ({status})")
        raise Retryable(msg or f"unexpected response ({status})")


class BloxGenPool:
    """Schedules several keys independently, one account per 25s each."""

    def __init__(self, keys: list[str], account_type: str = "alt",
                 region: str | None = None, out_dir: Path | str = "accounts"):
        if not keys:
            raise BloxGenError("no API keys given")
        self.states = [KeyState(k) for k in keys]
        self.account_type = account_type
        self.region = region
        self.store = AccountStore(Path(out_dir))
        self.created = 0
        self.attempts = 0
        self.spent = 0.0
        self.failures: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t0 = 0.0

    # -- reporting ------------------------------------------------------------

    def _note_failure(self, reason: str) -> None:
        with self._lock:
            self.failures[reason] = self.failures.get(reason, 0) + 1

    def stats(self) -> str:
        elapsed = max(1e-6, time.time() - self._t0)
        rate = self.created / elapsed * 60.0
        alive = sum(1 for s in self.states if not s.dead)
        lines = [
            f"  created {self.created}  attempts {self.attempts}  "
            f"spent ${self.spent:.4f}  {rate:.1f}/min  keys alive {alive}/{len(self.states)}",
        ]
        if self.created:
            eta = ""
            lines.append(f"  avg ${self.spent / self.created:.5f}/account{eta}")
        if self.failures:
            lines.append("  failures: " + ", ".join(f"{k}={v}" for k, v in sorted(self.failures.items())))
        return "\n".join(lines)

    # -- worker ---------------------------------------------------------------

    def _worker(self, state: KeyState, count: int) -> None:
        client = BloxGenClient(state.key)
        out_of_stock = 0
        while not self._stop.is_set():
            with self._lock:
                if self.created >= count:
                    return
            wait = state.ready_at - time.time()
            if wait > 0:
                if self._stop.wait(min(wait, 5.0)):
                    return
                continue

            try:
                data = client.generate(self.account_type, self.region)
            except Cooldown as exc:
                state.ready_at = time.time() + exc.seconds
                self._note_failure("cooldown")
                continue
            except OutOfStock:
                out_of_stock += 1
                self._note_failure("out_of_stock")
                if out_of_stock >= 5:
                    state.dead = True
                    state.reason = "out of stock"
                    return
                state.ready_at = time.time() + 30.0
                continue
            except DailyLimit as exc:
                state.dead = True
                state.reason = f"daily limit: {exc}"
                return
            except Fatal as exc:
                state.dead = True
                state.reason = str(exc)
                return
            except Retryable as exc:
                self._note_failure("retryable")
                state.ready_at = time.time() + 15.0
                state.reason = str(exc)[:80]
                continue

            out_of_stock = 0
            record = AccountRecord(
                username=str(data.get("username") or ""),
                password=str(data.get("password") or ""),
                birthday="",
                gender="",
                user_id=data.get("id"),
                cookie=data.get("cookie"),
                status="created",
                note=f"bloxgen:{data.get('type') or self.account_type}",
                proxy=None,
                captcha="none",
            )
            self.store.append(record)

            with self._lock:
                self.created += 1
                self.attempts += 1
                cost = float(data.get("cost") or 0.0)
                self.spent += cost
                state.made += 1
                state.spent += cost
                done = self.created

            state.ready_at = time.time() + state.cooldown
            if done % 25 == 0:
                print(f"  [{done}/{count}] {record.username}  ({self.stats().strip()})", flush=True)

    # -- entry ----------------------------------------------------------------

    def probe(self) -> int:
        """Check every key. Returns how many are usable."""
        good = 0
        for state in self.states:
            try:
                balance, role = BloxGenClient(state.key).balance()
                print(f"  OK  {state.masked():<18} role={role:<8} balance=${balance:.4f}")
                good += 1
            except BloxGenError as exc:
                print(f"  BAD {state.masked():<18} {exc}")
        return good

    def run(self, count: int, workers: int | None = None) -> int:
        self._t0 = time.time()
        threads = [
            threading.Thread(target=self._worker, args=(state, count), daemon=True)
            for state in self.states
        ]
        # stagger starts so all keys do not hit the endpoint in the same instant
        for i, t in enumerate(threads):
            self.states[i].ready_at = time.time() + i * 0.4
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                if all(s.dead for s in self.states):
                    break
                with self._lock:
                    if self.created >= count:
                        break
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n  interrupting…")
        finally:
            self._stop.set()
            for t in threads:
                t.join(timeout=5)
        return self.created
