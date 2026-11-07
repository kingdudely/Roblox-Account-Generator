"""The Arkose `dapib` answer transform, ported to pure Python.

What this actually is
---------------------
The remote entry the server hands out with each challenge (`dapib_url`, e.g.
`/dapib/us-east-1/<uuid>/578.js?expiry=..&mac=..`) is a 3 KB script that does one
thing: it reads the plaintext answer off `window.parent.ae.answer`, mangles it,
and hands the result straight back through `ae.dapibReceive({tanswer})`. The game
core then encrypts that string into the `tguess` field of `/fc/ca/`.

So the field that gets described as "the RSA `c` payload" is not cryptography and
not a device attestation. It is four passes of string arithmetic. Arkose has
shipped at least two versions of it; this module implements the current one,
recovered from a live session on 2026-10-30, and verified against it by
`tools/dapib_verify.py`.

The current pipeline, per answer object
---------------------------------------
1. **Twin pass.** For every key, seed a Park-Miller LCG from the value
   (`sum(code*(10 + code%3))`), Fisher-Yates the characters with it, and store the
   result under `<key>s`. The original key is kept.
2. **Swap pass.** For every key: sanitise the *value* to `[A-Za-z0-9_$]`, prefix
   `_` if it does not start with a letter/underscore/dollar, and then write
   `obj[sanitised_value] = key`. Keys and values trade places. This is why the
   wire format contains keys like `_06` and `1_70041488017` — they are mangled
   value strings, carrying key names as their values.
3. **Length pass.** Rename every key by splitting the decimal length of its value
   around the middle of its own digits: length 17 -> `1<key>7`.
4. **Remap pass.** Map every character code through
   `((c-35)**2 + 17) // 2 % 101 + 35`, then insert `-` at index 4 and every fifth
   position after.

The caller then appends an optional environment character `q`; it is empty in a
genuine browser and a random uppercase letter when the page believes it is being
driven (`process` present, prerender, no `document.activeElement`). We present as
a browser, so `q` stays empty.

An earlier version of this script also built a character tree and appended
breadth-first flattened `<key>tr` twins; production has since dropped that. If a
future capture grows `tr` keys again, the version went back — re-capture, don't
guess.
"""

from __future__ import annotations

import math
import re

__all__ = ["transform", "remap_char", "VerificationError"]

# Park-Miller / MINSTD — the JS hardcodes these, they are not tunable.
_MOD = 2**31 - 1
_MUL = 0xBC8F
_QUOT = _MOD // _MUL
_REM = _MOD % _MUL

_OFFSET = 0x23   # 35
_TWIST = 0x11    # 17
_ALPHA = 0x65    # 101

_UNSAFE = re.compile(r"[^a-zA-Z0-9_$]")
_LEAD_OK = re.compile(r"^[a-zA-Z_$]")


class VerificationError(RuntimeError):
    """Our output disagrees with the shipped script. Never ship past this."""


class _Lcg:
    """Schrage's method, matching the JS arithmetic including its float divide."""

    __slots__ = ("w",)

    def __init__(self, seed: int) -> None:
        self.w = int(seed)

    def next(self) -> float:
        m = self.w // _QUOT
        y = self.w % _QUOT
        a = _MUL * y - _REM * m
        self.w = a if a > 0 else a + _MOD
        return self.w / _MOD


def remap_char(code: int) -> int:
    """One character code through the fourth pass."""
    z = code - _OFFSET
    n = z * z + _TWIST
    w = math.floor(n / 2)
    return w % _ALPHA + _OFFSET


def _seed(text: str) -> int:
    total = 0
    for ch in text:
        c = ord(ch)
        total += c * (10 + c % 3)
    return total


def _shuffle(value: str) -> str:
    chars = list(value)
    if not chars:
        return ""
    lcg = _Lcg(_seed(value))
    for i in range(len(chars) - 1, 0, -1):
        j = math.floor(lcg.next() * (i + 1))
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _sanitise(value: str) -> str:
    name = _UNSAFE.sub("", value)
    if not _LEAD_OK.match(name):
        name = "_" + name
    return name


def _splice_length(key: str, length: int) -> str:
    digits = str(length)
    half = len(digits) // 2
    return digits[:half] + key + digits[half:]


def _remap(value: str) -> str:
    out = [chr(remap_char(ord(c))) for c in value]
    pos = 4
    while pos < len(out):
        out.insert(pos, "-")
        pos += 5
    return "".join(out)


def _is_array_index(key: str) -> bool:
    """True for keys a JS engine hoists to the front of an object."""
    if not key or not key.isascii() or not key.isdigit():
        return False
    if len(key) > 10 or (key[0] == "0" and len(key) > 1):
        return False
    return int(key) < 2**32 - 1


def _js_keys(obj: dict[str, str]) -> list[str]:
    """Reproduce `Object.keys` order: array indices ascending, then insertion.

    Not cosmetic. Passes 2 and 3 add keys while deleting others, so collisions
    are resolved by iteration order, and JS does not use insertion order for
    numeric keys. Iterating wrongly changes the output silently.
    """
    indices = sorted((k for k in obj if _is_array_index(k)), key=int)
    rest = [k for k in obj if not _is_array_index(k)]
    return indices + rest


def transform(answers: list[dict[str, object]], q: str = "") -> list[dict[str, str]]:
    """Apply the dapib pipeline to a plaintext answer.

    `answers` mirrors `ae.answer`: a list of dicts, one per answered round, each
    holding the chosen `index` plus a session-token split. `q` is the environment
    suffix — empty for a genuine-browser presentation, which is what we emulate.

    Everything mutates in place, as the JS does, because the passes collide by
    design: pass 2 overwrites keys that pass 3 has not reached yet, and pass 3
    renames keys that pass 2 just created. Building fresh dicts reproduces the
    arithmetic but not the collisions.
    """
    w: list[dict[str, str]] = []
    for obj in answers:
        row: dict[str, str] = {}
        for key in _js_keys(obj):  # type: ignore[arg-type]
            row[key] = str(obj[key])
        w.append(row)

    # 1 — twin pass
    for obj in w:
        for key in _js_keys(obj):
            obj[f"{key}s"] = _shuffle(obj[key])

    # 2 — swap pass: the value becomes a key, the key name becomes the value
    for obj in w:
        for key in _js_keys(obj):
            obj[_sanitise(obj[key])] = key

    # 3 — length pass: rename each key using the length of what it now holds
    for obj in w:
        for key in _js_keys(obj):
            value = obj[key]
            new_key = _splice_length(key, len(value))
            obj[new_key] = value
            if key in obj:
                del obj[key]

    # 4 — remap pass
    for obj in w:
        for key in _js_keys(obj):
            obj[key] = _remap(obj[key])

    if q:
        for obj in w:
            for key in _js_keys(obj):
                obj[key] = obj[key] + q
    return w


def transform_one(answer: dict[str, object], q: str = "") -> str:
    """One round, serialised as the JSON string the game core encrypts."""
    import json

    return json.dumps(transform([answer], q=q), separators=(",", ":"))
