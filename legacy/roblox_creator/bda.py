"""The `c` blob that `/fc/gt2/` wants — hybrid RSA-OAEP + AES-256-GCM.

What changed
------------
Arkose used to take a `bda` field: a JSON array of fingerprint probes, AES-CBC
encrypted with `<user-agent><6h-aligned-timestamp>`. That format is retired.
The field is now named `c`, it is ~10 KB instead of ~4 KB, and the ciphertext
is not decryptable with anything the client knows — which is the point.

The new scheme, recovered from `crypto.subtle` at runtime rather than guessed:

    key   <- getRandomValues(32)                       AES-256, encrypt-only
    iv    <- getRandomValues(12)
    rsa   <- RSA-OAEP(SHA-256, spki public key, key)   256 bytes
    gcm   <- AES-GCM(key, iv, json, tag 128)          ct || 16-byte tag

    c     = b64(iv || tag) + b64(rsa) + b64(ct)

Three base64 segments, concatenated, no separator. Verified byte-for-byte
against a live capture: every segment matched, including the tag that the
browser splits off the end of the GCM output and moves into segment zero.

Why the split matters
---------------------
It is the reason a captured blob cannot be reused. The RSA public key is static
and public, so the *client* can wrap any key it likes — but the server is the
only party that can unwrap it. So `c` is not a signature the client can forge
at will; it is a sealed envelope. It is still portable across IPs (the server
reads it, it does not bind it to the connection), and it is single-use (the
server marks it consumed). Measured: one fresh `c` yielded a token from a
different exit IP, and the same `c` was rejected on the second attempt.

So the generator is the whole game, and this module is it.

What the plaintext has to be
----------------------------
An ordered list of `{"key","value"}` pairs, exactly as the client assembles it:

    api_type | f | n | wh | enhanced_fp | fe | ife_hash | jsbd | c

`f` and `ife_hash` are murmur3-x64-128 digests over `fe`, so they cannot be
invented independently of it — they are what makes the fingerprint internally
consistent. `n` is base64 of the current epoch *seconds* and is the only field
that has to be fresh.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .arkose_py import Fingerprint, build_enhanced_fp, x64hash128

__all__ = ["RSA_SPKI_B64", "encode", "build_payload", "build"]

SITE_URL = "https://www.roblox.com"
API_URL = "https://arkoselabs.roblox.com"
CAPI_VERSION = "4.6.1"
ENFORCEMENT_HASH = "6d450e22d179b1343143a539d5b90815"

# The RSA-2048 public key the client imports with format="spki". Static per
# deployment — it lives in the enforcement bundle, not in api.js. Read it out of
# the crypto hook (`tools/bda_hook.py`) and paste it here when it rotates.
RSA_SPKI_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA6MllTnWfhMnI3gPImycyZwwdqj4+"
    "6hxt/pmLrjgN3zj+tRWrSN89O4Vbh5ihKFHw1VWJ9KKVFZQ+WadG1izDKR4KQpweLuWM3SxoX"
    "WpLLDaDkp+8tSqAJ4WRUL0yp1lM9p1d67BMiRkhbVvj/ab3Q2V0kxKZHvrX2QQklpRIodWYj"
    "Bo18nd4k63e7dIh9B7Rxf20nAnrt2cflmnZ2C4Hs6hyaP7oZPDlXS9q/Guu2woPa+8TGBiED"
    "Uv5bja2G18IF9UWF6ftvpTK2w/N/LuMIGSazg0tJ03VlV8fiY8e0SYs3Cwv5PhW5PkViKFN3"
    "VnQbopE7wTqBJg3vd96kfJxaQIDAQAB"
)

_RSA_CACHE: Any = None

# `O.uz` in api.js — sent as the final `{"key":"c"}` pair. It is a constant, and
# leaving it out shifts nothing that is hashed, but the server compares the
# array shape, so keep it.
_COPYRIGHT = "Copyright (c) 2026 Arkose Labs. All Rights Reserved."

# `jsbd` is a small environment block. HL is a hardware bucket, NWD is
# "normalised webdriver". These are not part of any digest — they ride along.
_JSBD = '{"HL":3,"NCE":true,"DT":"Roblox","NWD":"faked","DMTO":1,"DOTO":1}'


def _public_key():
    global _RSA_CACHE
    if _RSA_CACHE is None:
        _RSA_CACHE = serialization.load_der_public_key(base64.b64decode(RSA_SPKI_B64))
    return _RSA_CACHE


def encode(payload: list[dict[str, Any]]) -> str:
    """Seal one plaintext array into the `c` wire string."""
    plaintext = json.dumps(payload, separators=(",", ":")).encode()

    key = os.urandom(32)
    iv = os.urandom(12)

    # AESGCM.encrypt returns ct||tag; the browser posts them in different
    # segments, so split before composing
    sealed = AESGCM(key).encrypt(iv, plaintext, None)
    ct, tag = sealed[:-16], sealed[-16:]

    wrapped = _public_key().encrypt(
        key,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    b64 = lambda raw: base64.b64encode(raw).decode()
    return f"{b64(iv + tag)}{b64(wrapped)}{b64(ct)}"


def build_payload(
    fp: Fingerprint,
    *,
    now: int | None = None,
    ife_seed: int = 38,
    fe: list[str] | None = None,
    enhanced_fp: list[dict[str, Any]] | None = None,
    wh: str | None = None,
    jsbd: str = _JSBD,
    copyright_text: str = _COPYRIGHT,
) -> list[dict[str, Any]]:
    """Assemble the plaintext array exactly as `pe()` does in api.js.

    `f` hashes the *values* of `fe` joined with ";". `ife_hash` hashes the raw
    `fe` entries joined with ", " under seed 38. Both reproduced exactly from a
    live capture, so a synthetic fingerprint stays internally consistent.
    """
    entries = list(fe if fe is not None else fp.fe)
    values = [e.split(":", 1)[1] if ":" in e else e for e in entries]

    if enhanced_fp is None:
        # a short (or absent) enhanced_fp makes Arkose suppress the challenge,
        # and Roblox refuses a suppressed token for signup — so it is required
        enhanced_fp = build_enhanced_fp(fp, SITE_URL, API_URL,
                                        CAPI_VERSION, ENFORCEMENT_HASH)

    stamp = int(now if now is not None else time.time())

    return [
        {"key": "api_type", "value": "js"},
        {"key": "f", "value": x64hash128(";".join(values))},
        {"key": "n", "value": base64.b64encode(str(stamp).encode()).decode()},
        {"key": "wh", "value": wh or fp.wh},
        {"key": "enhanced_fp", "value": enhanced_fp},
        {"key": "fe", "value": entries},
        {"key": "ife_hash", "value": x64hash128(", ".join(entries), ife_seed)},
        {"key": "jsbd", "value": jsbd},
        {"key": "c", "value": copyright_text},
    ]


def build(fp: Fingerprint, **kwargs) -> str:
    return encode(build_payload(fp, **kwargs))
