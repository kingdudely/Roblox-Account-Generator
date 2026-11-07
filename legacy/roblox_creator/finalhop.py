"""Final hop: trade a solved Arkose token for a Roblox account.

Everything before this is proven; this step is the one that has never run,
because it needs a challenge that reached `solved`. So it is written as a small
set of named variants rather than one guess — when it does run, the variant that
worked is recorded and everything else is wrong by elimination.

What Roblox wants
-----------------
The signup 403 handed back three headers:

    rblx-challenge-id        us-central-<uuid>
    rblx-challenge-type      captcha
    rblx-challenge-metadata  base64 JSON, incl. dataExchangeBlob

The resubmit repeats the original signup body with those three headers restored,
and `captchaToken` set to the Arkose token that the challenge was solved under.
`sharedParameters` on this deployment says `useContinueMode: false`, which is why
the legacy resubmit is the expected path and `apis.roblox.com/challenge/v1/continue`
is the fallback rather than the primary.

Variants
--------
`merged`     original metadata dict + captchaToken     (expected path)
`token-only` {"captchaToken": token}                    (metadata not echoed back)
`continue`   POST the broker, then resubmit without metadata

All three are attempted in order against the *same* session and the *same*
challenge id, because the token is bound to both.
"""

from __future__ import annotations

import base64
import json
from typing import Any

__all__ = ["VARIANTS", "attempt", "finish"]

CONTINUE = "https://apis.roblox.com/challenge/v1/continue"


def _b64(doc: dict) -> str:
    return base64.b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()


def _resubmit(session, ctx: dict, token: str, metadata: dict | None) -> dict:
    from roblox_creator.config import SIGNUP_POST

    headers = dict(ctx["headers"])
    headers["rblx-challenge-id"] = ctx["challenge_id"]
    headers["rblx-challenge-type"] = ctx.get("challenge_type") or "captcha"
    if metadata is not None:
        headers["rblx-challenge-metadata"] = _b64(metadata)
    else:
        # some builds echo the header verbatim; keep whatever the 403 carried
        headers["rblx-challenge-metadata"] = ctx.get("raw_metadata", "")

    resp = session.post(SIGNUP_POST, json=ctx["body"], headers=headers)
    out = {"status": resp.status_code, "body": resp.text[:300]}
    for key, value in resp.headers.items():
        if key.lower().startswith("rblx-"):
            out[key] = value[:120]
    return out


def _continue(session, ctx: dict, token: str) -> dict:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/CreateAccount",
    }
    if ctx.get("csrf"):
        headers["x-csrf-token"] = ctx["csrf"]
    payload = {
        "challengeId": ctx["challenge_id"],
        "challengeType": ctx.get("challenge_type") or "captcha",
        "challengeMetadata": json.dumps({"captchaToken": token}, separators=(",", ":")),
    }
    resp = session.post(CONTINUE, json=payload, headers=headers)
    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text[:300]
    return {"status": resp.status_code, "body": body}


VARIANTS = ("merged", "token-only", "continue")


def attempt(session, ctx: dict, token: str, variant: str) -> dict:
    """One named completion attempt. Returns the HTTP outcome, not a verdict."""
    if variant == "continue":
        broker = _continue(session, ctx, token)
        final = _resubmit(session, ctx, token, metadata=dict(ctx["metadata"]))
        return {"variant": variant, "broker": broker, "signup": final,
                "created": final["status"] in (200, 201)}

    if variant == "token-only":
        final = _resubmit(session, ctx, token, metadata={"captchaToken": token})
        return {"variant": variant, "signup": final,
                "created": final["status"] in (200, 201)}

    metadata = dict(ctx["metadata"])
    metadata["captchaToken"] = token
    final = _resubmit(session, ctx, token, metadata=metadata)
    return {"variant": variant, "signup": final,
            "created": final["status"] in (200, 201)}


def finish(session, ctx: dict, token: str, variants: tuple[str, ...] = VARIANTS) -> dict:
    """Try variants until one produces an account, and report what each said.

    A refused variant costs one signup POST and does not consume the challenge —
    the token stays valid — so trying all three is cheaper than getting it wrong
    and re-running a five-wave challenge.
    """
    tried = []
    for variant in variants:
        outcome = attempt(session, ctx, token, variant)
        tried.append(outcome)
        if outcome["created"]:
            return {"created": True, "winner": variant, "tried": tried}
    return {"created": False, "tried": tried}
