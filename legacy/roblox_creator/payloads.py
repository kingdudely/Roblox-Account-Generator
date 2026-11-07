"""Captured browser payloads — the fingerprint corpus.

Why this exists
---------------
`data/fingerprints.json` holds only *mobile* captures, and replaying them costs
**10 waves** per challenge. A captured *desktop* payload costs **5** (measured:
6 attempts, mean 5.8). Waves multiply the number of visual solves, so the corpus
is the cheapest lever on total solver load.

A payload is the exact `c` plaintext a real browser built — the real `fe`, the
real `wh`, and the whole `enhanced_fp` (97 entries on the desktop capture, where
the synthetic builder produces 90). Only `n` (epoch seconds) has to be refreshed;
the envelope is portable across IPs and re-sealable indefinitely.

Harvest with `tools/fp_harvest.py`, inspect with `--list`.

Trade-off worth knowing
-----------------------
One payload replayed across many accounts is one device identity, which is a
clustering signal. Half the solves is a real win, but a corpus of one is a
survivorship risk. `pick_payload()` therefore spreads across whatever is stored
and only prefers desktop when there is more than one to choose from.
"""

from __future__ import annotations

import base64
import json
import random
import time
from pathlib import Path
from typing import Any

__all__ = ["load_payloads", "pick_payload", "reseal"]

STORE = Path(__file__).resolve().parents[1] / "data" / "payloads"


def load_payloads(store: Path | None = None) -> list[dict[str, Any]]:
    store = store or STORE
    if not store.exists():
        return []
    out = []
    for path in sorted(store.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except Exception:
            continue
    return out


def pick_payload(pool: list[dict[str, Any]], *,
                 prefer_desktop: bool = True) -> dict[str, Any] | None:
    """Choose a payload to replay.

    Desktop is preferred because it halves the wave count, but only when there is
    a choice — a single identity used everywhere would be a worse trade than the
    waves it saves.
    """
    if not pool:
        return None
    desktop = [p for p in pool if not p.get("mobile")]
    if prefer_desktop and len(desktop) >= 2:
        return random.choice(desktop)
    if prefer_desktop and desktop and len(pool) == 1:
        return desktop[0]
    return random.choice(pool)


def reseal(record: dict[str, Any], now: int | None = None) -> tuple[str, str]:
    """Return (c blob, user agent) with a fresh `n`."""
    from . import bda

    stamp = base64.b64encode(
        str(int(now if now is not None else time.time())).encode()).decode()
    payload = [dict(item, value=stamp) if item.get("key") == "n" else item
               for item in record["payload"]]
    return bda.encode(payload), record["user_agent"]
