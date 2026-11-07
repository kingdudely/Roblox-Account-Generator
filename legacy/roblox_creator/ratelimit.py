"""Evenly-spaced rate limiting.

A fixed `sleep(2)` between accounts produces bursts — all workers wake together,
hammer for a second, idle, repeat. That pattern is itself a fingerprint and it
trips per-ASN rate limits. A shared token bucket spreads the same average rate
into a smooth drip.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_min: float, burst: float = 1.0):
        if rate_per_min <= 0:
            raise ValueError("rate must be positive")
        self.interval = 60.0 / rate_per_min
        self.burst = max(1.0, burst)
        self._lock = threading.Lock()
        self._capacity = self.burst
        self._tokens = self.burst
        self._last_refill = time.monotonic()

    def acquire(self, stop: threading.Event | None = None) -> bool:
        """Block until a slot opens. Returns False if `stop` fired while waiting."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._last_refill = now
                self._tokens = min(self._capacity, self._tokens + elapsed / self.interval)

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                wait = (1.0 - self._tokens) * self.interval

            if stop is not None and stop.is_set():
                return False
            # sleep in small slices so shutdown is responsive
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    return False
                time.sleep(min(0.1, deadline - time.monotonic()))
