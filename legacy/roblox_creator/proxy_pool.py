"""Proxy pool with health tracking, cooldowns, and sticky assignment.

At 50k accounts/day a dead proxy must not cost you a browser launch. Every proxy
carries its own record: how often it worked, when it was last used, when it gets
to try again. Bad ones quarantine themselves out of rotation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ProxyState:
    url: str
    ok: int = 0
    fail: int = 0
    streak: int = 0                 # consecutive failures
    last_used: float = 0.0
    cooldown_until: float = 0.0
    banned: bool = False
    ban_count: int = 0

    @property
    def total(self) -> int:
        return self.ok + self.fail

    @property
    def score(self) -> float:
        """Success ratio with a prior, so a fresh proxy is not punished."""
        return (self.ok + 1.0) / (self.total + 2.0)

    def available(self, now: float) -> bool:
        return not self.banned and self.cooldown_until <= now


# cooldown ladder: a proxy that gets blocked comes back slowly
_COOLDOWNS = [30, 120, 600, 1800, 7200]


class ProxyPool:
    def __init__(self, proxies: list[str], max_strikes: int = 5):
        if not proxies:
            raise ValueError("proxy pool is empty")
        self._lock = threading.Lock()
        self._states = [ProxyState(url=p) for p in proxies]
        self._by_url = {s.url: s for s in self._states}
        self.max_strikes = max_strikes

    def __len__(self) -> int:
        return len(self._states)

    def acquire(self, sticky_key: str | None = None) -> ProxyState | None:
        """Least-recently-used among healthy proxies. None only if all are burning."""
        with self._lock:
            now = time.time()
            healthy = [s for s in self._states if s.available(now) and s.streak < self.max_strikes]
            if not healthy:
                # fall back to whatever is closest to cooling down, rather than stalling
                healthy = [s for s in self._states if not s.banned]
                if not healthy:
                    return None
            if sticky_key:
                best = min(healthy, key=lambda s: (s.score, s.last_used))
            else:
                best = min(healthy, key=lambda s: (s.last_used, s.score))
            best.last_used = now
            return best

    def report(self, url: str, outcome: str) -> None:
        """outcome: ok | captcha | blocked | dead"""
        with self._lock:
            state = self._by_url.get(url)
            if state is None:
                return
            now = time.time()
            if outcome == "ok":
                state.ok += 1
                state.streak = 0
                return
            if outcome == "captcha":
                # not the proxy's fault, but it is a signal: decay the score a touch
                state.fail += 1
                return

            state.fail += 1
            state.streak += 1
            if outcome == "dead":
                state.cooldown_until = now + 300
                return

            if state.streak >= self.max_strikes:
                state.banned = True
                state.ban_count += 1
                # a banned proxy can earn its way back after a long sleep
                state.banned = False
                state.streak = 0
                state.cooldown_until = now + _COOLDOWNS[min(state.ban_count, len(_COOLDOWNS)) - 1]
                return

            state.cooldown_until = now + _COOLDOWNS[min(state.streak, len(_COOLDOWNS)) - 1]

    def stats(self) -> str:
        with self._lock:
            now = time.time()
            live = sum(1 for s in self._states if s.available(now))
            cooling = sum(1 for s in self._states if not s.available(now))
            best = max(self._states, key=lambda s: s.score)
            worst = min(self._states, key=lambda s: s.score)
            return (f"proxies live={live} cooling={cooling} "
                    f"best={best.score:.2f} worst={worst.score:.2f}")

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = time.time()
            return [
                {
                    "url": s.url, "ok": s.ok, "fail": s.fail, "streak": s.streak,
                    "banned": s.banned, "cooldown": max(0, int(s.cooldown_until - now)),
                    "score": round(s.score, 3),
                }
                for s in self._states
            ]
