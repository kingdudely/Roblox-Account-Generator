"""The `/fc/ca/` answer channel: CryptoJS-compatible envelope plus the payload.

What `/fc/ca/` wants
--------------------
Two encrypted fields and two fixed blobs, all form-urlencoded:

    session_token   gfct's *new* session token (not the gt2 one)
    game_token      gfct's challengeID
    sid             region, from gfct
    render_type     canvas
    analytics_tier  40
    guess           encryptECData(JSON.stringify(guesses), session_token)
    tguess          same plaintext after the dapib transform (optional)
    bio             base64 of {"mbio":"","tbio":"","kbio":""}
    ecdata          base64 of {"height":450,"width":400}
    is_compatibility_mode  false

`guess` accumulates one `{"index": N}` per wave: the first body carries one
entry, the second two, and so on until the server answers `solved`.

The encryption
--------------
`encryptECData` in the game-core bundle is CryptoJS's password-based cipher:

    salt  <- 8 random bytes
    key   <- EvpKDF(password = session_token, salt, keySize 32, ivSize 16)
    iv    <- the second half of that derivation
    ct    <- AES-256-CBC(Pkcs7(plaintext))

and it serialises to `{"ct": b64, "iv": hex, "s": hex}`.

EvpKDF (CryptoJS's default, MD5, one iteration) is a chain:

    block_1 = MD5(password ‖ salt)
    block_n = MD5(block_{n-1} ‖ password ‖ salt)

Concatenate blocks and slice 32 bytes of key then 16 of IV.

Verified against a live capture: decrypting a browser's `guess` with the
session token reproduces `[{"index":0}]` exactly, and the derived IV matches the
`iv` the browser sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "evpkdf", "encrypt_ecdata", "decrypt_ecdata",
    "bio_blob", "ecdata_blob", "build_form",
]

KEY_LEN = 32
IV_LEN = 16
SALT_LEN = 8


def evpkdf(password: bytes, salt: bytes, key_len: int = KEY_LEN,
           iv_len: int = IV_LEN) -> tuple[bytes, bytes]:
    """CryptoJS's EvpKDF with MD5 and a single iteration."""
    want = key_len + iv_len
    out = b""
    block = b""
    while len(out) < want:
        block = hashlib.md5(block + password + salt).digest()
        out += block
    return out[:key_len], out[key_len:key_len + iv_len]


def encrypt_ecdata(plaintext: str | Any, password: str) -> dict[str, str]:
    """Seal one plaintext into the `{ct, iv, s}` envelope the client sends."""
    if not isinstance(plaintext, str):
        plaintext = json.dumps(plaintext, separators=(",", ":"))

    salt = os.urandom(SALT_LEN)
    key, iv = evpkdf(password.encode(), salt)

    padder = sym_padding.PKCS7(128).padder()
    body = padder.update(plaintext.encode()) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(body) + encryptor.finalize()

    return {
        "ct": base64.b64encode(ct).decode(),
        "iv": iv.hex(),
        "s": salt.hex(),
    }


def decrypt_ecdata(envelope: dict[str, str] | str, password: str) -> str:
    """Open a `{ct, iv, s}` envelope — used to check captures and our own output."""
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    ct = base64.b64decode(envelope["ct"])
    salt = bytes.fromhex(envelope["s"])
    iv = bytes.fromhex(envelope["iv"])
    key, _ = evpkdf(password.encode(), salt)

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


def bio_blob() -> str:
    """No biometrics available; the client sends an empty triple, base64'd."""
    return base64.b64encode(
        json.dumps({"mbio": "", "tbio": "", "kbio": ""},
                   separators=(",", ":")).encode()).decode()


def ecdata_blob(height: int = 450, width: int = 400) -> str:
    """The rendered frame size the game reports back."""
    return base64.b64encode(
        json.dumps({"height": height, "width": width},
                   separators=(",", ":")).encode()).decode()


def build_form(session_token: str, game_token: str, sid: str,
               guesses: list[dict[str, Any]], *,
               tguesses: list[dict[str, Any]] | None = None,
               height: int = 450, width: int = 400) -> dict[str, str]:
    """The complete form body for one `/fc/ca/` call."""
    form = {
        "session_token": session_token,
        "game_token": game_token,
        "sid": sid,
        "guess": json.dumps(encrypt_ecdata(json.dumps(guesses, separators=(",", ":")),
                                           session_token), separators=(",", ":")),
        "render_type": "canvas",
        "analytics_tier": "40",
        "bio": bio_blob(),
        "is_compatibility_mode": "false",
        "ecdata": ecdata_blob(height, width),
    }
    if tguesses is not None:
        form["tguess"] = json.dumps(
            encrypt_ecdata(json.dumps(tguesses, separators=(",", ":")), session_token),
            separators=(",", ":"))
    return form
