"""Which client shape gets which challenge flavour from `/v2/signup`.

`captcha` is Arkose: it carries a `dataExchangeBlob`, and every transport in this
repo can solve it. `captchav2` is the other flavour — no blob, and nothing has
ever satisfied it (`tools/captchav2_probe.py`). So the *ratio* between the two
is throughput, and the server picks it per request from the risk engine.

`x-roblox-platform` is one of those inputs. It is a plain request header, which
makes it the only shape lever safe to pull from inside a browser: overriding
`user-agent` without also changing the TLS fingerprint and `navigator.userAgent`
is a stronger tell than the flavour it might buy back.

Nothing here decides anything on its own. `preferred()` returns a shape only
when there is enough evidence to beat the default, and returns `None` otherwise
so the caller leaves the request alone.

    from roblox_creator import client_shape
    client_shape.record("android", "captcha", blob=True)
    client_shape.preferred()          # -> "android" | None
    client_shape.browser_headers("android")
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBSERVATIONS = Path(os.environ.get("SHAPE_LOG",
                                   ROOT / "debug" / "shapes" / "observations.jsonl"))

SOLVABLE = "captcha"
UNSOLVABLE = "captchav2"

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# Header sets used by the curl probes. `user-agent` entries here are for the
# prober only — see BROWSER_SAFE below.
SHAPES: dict[str, dict[str, str]] = {
    "desktop": {"user-agent": DESKTOP_UA},
    "wininet": {"user-agent": "Roblox/WinInet"},
    "android": {"user-agent": "Roblox/Android/2.600.0",
                "x-roblox-platform": "Android"},
    "ios": {"user-agent": "Roblox/iOS/2.600.0",
            "x-roblox-platform": "iOS"},
    "pcgdk": {"user-agent": "Roblox/WinInet",
              "x-roblox-platform": "PCGDK"},
    "pc": {"user-agent": "Roblox/WinInet",
           "x-roblox-platform": "PC"},
}

DEFAULT_SHAPE = "desktop"

# Anything outside this set would desync from the browser that is actually
# sending the request: the TLS fingerprint is Chromium's, `navigator.userAgent`
# is Chromium's, and only the header would say otherwise.
BROWSER_SAFE = ("x-roblox-platform",)


@dataclass(frozen=True)
class Observation:
    shape: str
    flavour: str
    blob: bool
    stamp: float

    @property
    def solvable(self) -> bool:
        return self.flavour == SOLVABLE


def record(shape: str, flavour: str, blob: bool = False,
           path: Path | None = None) -> Observation:
    obs = Observation(shape=shape, flavour=flavour or "-", blob=bool(blob),
                      stamp=time.time())
    target = path or OBSERVATIONS
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(obs)) + "\n")
    return obs


def load(path: Path | None = None) -> list[Observation]:
    target = path or OBSERVATIONS
    if not target.exists():
        return []
    out: list[Observation] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out.append(Observation(shape=doc["shape"], flavour=doc["flavour"],
                                   blob=bool(doc.get("blob")),
                                   stamp=float(doc.get("stamp", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def tally(obs: list[Observation] | None = None) -> dict[str, Counter]:
    counts: dict[str, Counter] = {}
    for o in (obs if obs is not None else load()):
        counts.setdefault(o.shape, Counter())[o.flavour] += 1
    return counts


def solvable_ratio(shape: str, obs: list[Observation] | None = None) -> float | None:
    rows = [o for o in (obs if obs is not None else load()) if o.shape == shape]
    if not rows:
        return None
    resolved = [o for o in rows if o.flavour in (SOLVABLE, UNSOLVABLE)]
    if not resolved:
        return None
    return sum(1 for o in resolved if o.solvable) / len(resolved)


def preferred(*, min_samples: int = 25, min_gain: float = 0.15,
              obs: list[Observation] | None = None) -> str | None:
    """Best shape by solvable ratio, or None if the evidence does not justify it.

    Returns None when the best candidate does not beat the default by
    `min_gain` on at least `min_samples` resolved observations. The caller is
    expected to treat None as "send the request the way the browser wants to".
    """
    rows = obs if obs is not None else load()
    if not rows:
        return None

    baseline = solvable_ratio(DEFAULT_SHAPE, rows)
    best, best_ratio = None, -1.0
    for shape in SHAPES:
        resolved = [o for o in rows
                    if o.shape == shape and o.flavour in (SOLVABLE, UNSOLVABLE)]
        if len(resolved) < min_samples:
            continue
        ratio = solvable_ratio(shape, rows)
        if ratio is None:
            continue
        if ratio > best_ratio:
            best, best_ratio = shape, ratio

    if best is None:
        return None
    if baseline is None:
        return best
    if best_ratio - baseline < min_gain:
        return None
    return best


def browser_headers(shape: str) -> dict[str, str]:
    """The subset of a shape that is safe to impose on a real browser request."""
    return {k: v for k, v in SHAPES.get(shape, {}).items() if k in BROWSER_SAFE}


def report() -> str:
    obs = load()
    if not obs:
        return "no observations yet — run tools/challenge_mix.py on a live exit"
    counts = tally(obs)
    lines = []
    width = max(len(s) for s in counts)
    for shape in sorted(counts):
        c = counts[shape]
        n = sum(c.values())
        ratio = solvable_ratio(shape, obs)
        shown = "  n/a" if ratio is None else f"{ratio:>4.0%}"
        lines.append(f"  {shape:<{width}}  n={n:<4} solvable={shown}  "
                     + "  ".join(f"{k}={v}" for k, v in c.most_common()))
    choice = preferred()
    lines.append(f"  preferred: {choice or '(default — no shape beats it yet)'}")
    return "\n".join(lines)
