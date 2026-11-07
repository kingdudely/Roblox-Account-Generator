"""Pool of Gemini keys with per-project quota accounting.

Google applies rate limits **per project, not per API key**, so two keys minted
in the same Cloud project share one bucket and rotating between them buys
nothing. What does work is one key per project (or per Google account): each
project has its own RPM/RPD, so N projects ≈ N× the quota.

This pool is honest about that: it keys the bookkeeping on the API key, but the
caller is expected to supply keys from distinct projects. It handles the two
failure shapes Gemini actually returns:

    429 RESOURCE_EXHAUSTED + RetryInfo.retryDelay="37s"     -> per-minute
    429 RESOURCE_EXHAUSTED + quotaId=...RequestsPerDay...    -> per-day, park
                                                                until midnight PT

Usage:

    pool = KeyPool(keys)
    key = pool.acquire()
    try:
        ...
        pool.report(key, 200)
    except QuotaError as exc:
        pool.report(key, 429, exc.retry_after, exc.per_day)
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaError(RuntimeError):
    def __init__(self, message: str, retry_after: float = 60.0, per_day: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.per_day = per_day


def _midnight_pacific() -> float:
    """Seconds until the next midnight in Pacific time (when RPD resets)."""
    now = datetime.now(PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds())


def parse_quota(body: str, headers: dict | None = None) -> QuotaError:
    """Turn a Gemini 429 body into a retry hint."""
    retry = None
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body or "")
    if m:
        retry = float(m.group(1))
    if retry is None and headers:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                retry = float(ra)
            except ValueError:
                retry = None

    per_day = bool(re.search(r"(RequestsPerDay|PerDayPerProject|per day)", body or "", re.I))
    if retry is None:
        retry = _midnight_pacific() if per_day else 60.0
    return QuotaError(body[:300], retry_after=retry, per_day=per_day)


class KeyPool:
    """Round-robin over independent projects, with cooldowns and day budgets."""

    def __init__(self, keys: list[str], rpm: int = 0, rpd: int = 0,
                 default_cooldown: float = 60.0):
        self.keys = [k.strip() for k in keys if k and k.strip()]
        if not self.keys:
            raise ValueError("KeyPool needs at least one key")
        self.rpm = rpm
        self.rpd = rpd
        self.default_cooldown = default_cooldown
        self._lock = threading.Lock()
        self._ready_at = {k: 0.0 for k in self.keys}
        self._used_today = {k: 0 for k in self.keys}
        self._day = {k: None for k in self.keys}
        self._calls: dict[str, deque] = {k: deque() for k in self.keys}
        self.bans = 0

    # -- internals ---------------------------------------------------------

    def _roll_day(self, key: str, now: float) -> None:
        today = datetime.now(PACIFIC).date()
        if self._day[key] != today:
            self._day[key] = today
            self._used_today[key] = 0
            self._ready_at[key] = min(self._ready_at[key], now + _midnight_pacific())

    def _usable(self, key: str, now: float) -> bool:
        if now < self._ready_at[key]:
            return False
        if self.rpd and self._used_today[key] >= self.rpd:
            return False
        if self.rpm:
            calls = self._calls[key]
            while calls and now - calls[0] > 60.0:
                calls.popleft()
            if len(calls) >= self.rpm:
                return False
        return True

    # -- public ------------------------------------------------------------

    def acquire(self, timeout: float = 300.0) -> str:
        """Block until a key is allowed to fire, then charge one request to it."""
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            with self._lock:
                for key in self.keys:
                    self._roll_day(key, now)
                ready = [k for k in self.keys if self._usable(k, now)]
                if ready:
                    key = min(ready, key=lambda k: (self._ready_at[k],
                                                    self._used_today[k]))
                    self._used_today[key] += 1
                    self._calls[key].append(now)
                    return key
                waits = [max(0.0, self._ready_at[k] - now) for k in self.keys]
                wake = min(w for w in waits if w > 0) if any(w > 0 for w in waits) else 5.0
            if time.monotonic() >= deadline:
                raise QuotaError("no key available before timeout", retry_after=wake)
            time.sleep(min(wake, 5.0, max(0.0, deadline - time.monotonic())))

    def report(self, key: str, status: int, retry_after: float | None = None,
               per_day: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if status == 200:
                self._ready_at[key] = min(self._ready_at[key], now)
                return
            if status == 429:
                delay = retry_after if retry_after is not None else self.default_cooldown
                if per_day:
                    delay = max(delay, _midnight_pacific() if delay < 60 else delay)
                self._ready_at[key] = max(self._ready_at[key], now + delay)
                self.bans += 1
                return
            if status in (401, 403):
                # bad key or project-level block: park for a day, do not hammer
                self._ready_at[key] = max(self._ready_at[key], now + 3600)
                self.bans += 1
                return
            if status >= 500:
                self._ready_at[key] = max(self._ready_at[key], now + 20)

    def stats(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "key": f"...{k[-6:]}",
                    "used_today": self._used_today[k],
                    "cooling": max(0, int(self._ready_at[k] - time.monotonic())),
                }
                for k in self.keys
            ]

    def __len__(self) -> int:
        return len(self.keys)


def load_keys(path_or_values: str | None) -> list[str]:
    """Accept a file path, or a comma/newline separated list, or nothing."""
    if not path_or_values:
        return []
    import pathlib

    p = pathlib.Path(path_or_values)
    if p.exists() and p.is_file():
        raw = p.read_text()
    else:
        raw = path_or_values
    out: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out
