"""Generic CAPTCHA vision solver — no knowledge of the puzzle family.

Every other solver in this repo encodes an assumption: `dicecount` assumes dice,
`wave_montage` assumes ten candidates in a strip, `keycrop` assumes a sticker on a
key card. Those assumptions were each true for one Arkose variant and false for the
next one, which is how the matcher ended up scoring 0/8 on live strips: it was
looking for bright decal plates on a challenge that draws dark glyphs on dice.

This module makes no assumption. It hands the challenge image to a vision model
and asks it to work out what the puzzle is and which option is correct. The model
reads the instruction, identifies the layout, and answers. If Arkose ships a new
game type tomorrow, this keeps working; a hand-written CV pipeline does not.

The output contract is one line, so the caller never parses prose:

    ANSWER=<number>

`<number>` is the 1-based position of the correct candidate, counted left to right
across the candidate row. The model is told to map any layout onto that numbering,
so a reference panel on the left is excluded from the count.

Cost is one call per wave (not one per tile), which is the reason this is usable at
all: ten per-tile calls cost ten times more and give the model less context.
"""

from __future__ import annotations

import base64
import io
import os
import re
import time
from typing import Any

from curl_cffi import requests
from PIL import Image

from roblox_creator.vlm_client import PROVIDERS

__all__ = ["PROMPT", "GRID_PROMPT", "SPLIT_PROMPT", "build_prompt",
           "build_fallback_prompt", "solve"]

PROMPT = """You are solving a web CAPTCHA. The image is one challenge frame.

Work it out yourself. Do not assume a format: the puzzle family changes between
sessions, and the instructions for this one are in the image.

Guidance:
- Read any instruction text first and follow it literally.
- Identify the reference/target (often a panel on the left) and the candidate
  options (often a row or grid of panels).
- Compare carefully. Similar-looking symbols are common and are the usual trap.
- If the task is a count, count each candidate independently and only then compare.
- If the layout is not a simple row, still map your choice onto left-to-right
  numbering of the candidates, ignoring the reference panel.

Think briefly. Then end with exactly one line:

ANSWER=<number>"""


GRID_PROMPT = """You are solving a web CAPTCHA made of a target and several candidate scenes.

The image has a white border and thin white gaps between panels.

- The LEFTMOST panel, standing on its own, is the TARGET ICON. It is not an answer.
- Every other panel is a candidate scene, numbered 1 upward, left to right then top
  to bottom.

The candidate scenes hold the same objects rearranged, plus a small character
standing somewhere. Match the target against them.

The task is one of these; pick whichever the panels actually support:

- which scene has the character standing on, holding, or touching the target object
- which scene contains exactly one instance of the target object (or two, or none)
- which scene places the target object in the stated position (top, bottom, left)
- which single scene differs from the others in the way the target defines

The reliable read is contact and count, not overall similarity: look at where the
character's feet are, and count how many times the target object appears. Say which
object in each scene you matched against and where it sits.

Think briefly. Then end with exactly one line:

ANSWER=<number>"""


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
#
# Measured 2026-11-01 on a live challenge: the `/rtig/image` body is always 400 px
# tall. The top band (190 px) holds the candidates, side by side at 200 px each;
# the bottom-left corner holds the reference card, and the rest of the bottom is
# black filler. Candidate counts of 5 and 10 have both been served, from two
# different art styles, so nothing below may assume a count or a subject.
#
# Two facts forced splitting the strip into two images:
#
#   1. Every vision API resizes what it is given — Claude to a 1568 px long edge,
#      Qwen to a token budget — so a 2000x400 montage arrives at a quarter of its
#      pixels. Sent separately, each half sits under the ceiling untouched.
#   2. In one image the reference and the candidates *share* one resolution budget.
#      The reference is where the rule's subject is drawn, and it is ~125 px wide;
#      giving it a slice of a wide montage is what shrank it to mush.
#
# Two images and not eleven: DeepSeek's vision stack degrades from the third image
# on, and per-tile images would destroy the side-by-side comparison the answer
# depends on.
#
# ## The contract that keeps this generic
#
# Nothing here describes a puzzle *family*. It describes a *presentation*: one
# reference card, then a numbered grid of candidates. That layout is what the
# `rtig` endpoint serves regardless of the game behind it. Everything specific to
# the current challenge — the rule, what to match, how many, which relation — comes
# in as `instruction`, read from the DOM at solve time.
#
# The consequence is deliberate and load-bearing: when Arkose ships new art, or a
# new rule for the same art, this file does not change. The DOM text changes, and
# the model reads it. A prompt that hard-codes "find the tool" or "watch the
# character's feet" is a per-family matcher wearing a vision model's clothes, and it
# scores at chance the moment the family rotates.

PROMPT_TWO = """I have given you images from a puzzle I need solved.

The task: "{instruction}"

Image 1 is the reference — it shows what the task is about, and it is never an
answer. Image 2 is the candidates, numbered 1 to {tiles}: 1 is the top-left, the
count runs left to right and continues on the next row.

Work out which candidate number the puzzle asks for, then end with exactly one
line:

ANSWER=<number>"""

PROMPT_TWO_BLIND = """I have given you images from a puzzle I need solved. The
puzzle's own instructions were not available, so infer the rule from the images.

Image 1 is the reference — it shows what the task is about, and it is never an
answer. Image 2 is the candidates, numbered 1 to {tiles}: 1 is the top-left, the
count runs left to right and continues on the next row.

Work out which candidate number the puzzle asks for, then end with exactly one
line:

ANSWER=<number>"""

PROMPT_ONE = """I have given you an image from a puzzle I need solved. Its panels are placed
side by side.

The task: "{instruction}"

The panel on the far left, standing apart from the rest, is the reference: it
shows what the question is about, and it is never an answer. Every panel after it
is a candidate, and each candidate already carries its own number in its
top-left corner — the count runs left to right and continues on the next row.

Work out which candidate number the puzzle asks for, then end with exactly one
line:

ANSWER=<number>"""

PROMPT_ONE_BLIND = """I have given you an image from a puzzle I need solved. The
puzzle's own instructions were not available, so infer the rule from the image.

The panel on the far left, standing apart from the rest, is the reference: it
shows what the question is about, and it is never an answer. Every panel after it
is a candidate, and each candidate already carries its own number in its
top-left corner — the count runs left to right and continues on the next row.

Work out which candidate number the puzzle asks for, then end with exactly one
line:

ANSWER=<number>"""


def build_prompt(tile_count: int = 0, instruction: str = "",
                 images: int = 2) -> str:
    """Compose the prompt for one challenge.

    Short on purpose. The prompt carries exactly two things that the model cannot
    get from the pixels: which panel is the reference, and how the candidates are
    numbered — because the answer has to index a carousel. Everything else is the
    `instruction` string read from the DOM at solve time, and that is the only
    challenge-specific text anywhere in this module.

    Keeping it this short is also what keeps it robust: when Arkose ships new art,
    or a new rule on the same art, nothing here changes. The DOM sentence changes
    and the model reads it. A prompt that hard-codes "find the tool" or "watch the
    character's feet" is a per-family matcher in a vision model's clothes, and it
    scores at chance the moment the family rotates.
    """
    tiles = tile_count or "the last"
    if images >= 2:
        template = PROMPT_TWO if instruction else PROMPT_TWO_BLIND
    else:
        template = PROMPT_ONE if instruction else PROMPT_ONE_BLIND
    return template.format(instruction=instruction.strip(), tiles=tiles)


def build_fallback_prompt(tile_count: int, previous: int = 0,
                          instruction: str = "", images: int = 2) -> str:
    """Second ask after an unusable answer. Names the error, not the rule."""
    why = (f"I asked for a number from 1 to {tile_count} and you answered "
           f"{previous}, which does not exist here."
           if previous else
           f"Answer with an integer from 1 to {tile_count}.")
    body = build_prompt(tile_count, instruction, images)
    return body.replace("Work out which", why + "\n\nWork out which", 1)


def _data_url(image: Image.Image, max_edge: int) -> str:
    """JPEG data URL, scaled so the longest edge is at most `max_edge`.

    Scaling *up* is allowed and deliberate: a strip is 2000x400 and the panels are
    small, so a downscale destroys the symbols. Only downscale when the source
    genuinely exceeds the budget.
    """
    img = image.convert("RGB")
    longest = max(img.size)
    if max_edge and longest > max_edge:
        ratio = max_edge / longest
        img = img.resize((max(1, int(img.width * ratio)),
                          max(1, int(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _parse(text: str) -> int | None:
    """Pull the answer out of the reply. `ANSWER=` wins; else a trailing integer."""
    if not text:
        return None
    marker = re.search(r"ANSWER\s*[=:]\s*\**\s*(\d{1,2})", text, re.IGNORECASE)
    if marker:
        value = int(marker.group(1))
        return value if 1 <= value <= 20 else None
    tail = re.findall(r"\b(\d{1,2})\b", text[-160:])
    for raw in reversed(tail):
        value = int(raw)
        if 1 <= value <= 20:
            return value
    return None


def solve(image: Image.Image | None = None, provider: str = "lithosai",
          api_key: str = "", max_edge: int = 2000, timeout: float = 120.0,
          retries: int = 2, log=print, prompt: str = "",
          images: list[Image.Image] | None = None,
          instruction: str = "", count: int = 0) -> dict[str, Any]:
    """Ask the model to solve one challenge.

    `image` is the single-montage form; `images` is the two-image form, sent in
    order as image 1, image 2. `instruction` is the sentence the game page renders
    above the board — when it is non-empty the prompt leads with it. `count` is the
    number of candidates, used to bound the answer and to reject out-of-range ones.

    Returns `{"answer": <1-based int or None>, "text": <raw reply>,
    "status": ..., "count": <int>}`. The 1-based answer is the model's own
    numbering; the caller converts to whatever wire convention it uses.
    """
    cfg = PROVIDERS[provider]
    key = api_key or (os.getenv(cfg.key_env, "") if cfg.key_env else "")
    if not key:
        return {"answer": None, "text": "", "status": "no-key"}

    pictures = list(images) if images else ([image] if image is not None else [])
    if not pictures:
        return {"answer": None, "text": "", "status": "no-image"}

    text_prompt = prompt or build_prompt(count, instruction, len(pictures))

    content: list[dict[str, Any]] = [{"type": "text", "text": text_prompt}]
    for picture in pictures:
        content.append({"type": "image_url",
                        "image_url": {"url": _data_url(picture, max_edge)}})

    payload = {
        "model": cfg.model,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": content}],
    }

    last: dict[str, Any] = {}
    for attempt in range(max(1, retries)):
        t0 = time.time()
        try:
            resp = requests.post(f"{cfg.base}/chat/completions", json=payload,
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {key}"},
                                 impersonate="chrome", timeout=timeout)
        except Exception as exc:
            last = {"answer": None, "status": f"error {type(exc).__name__}",
                    "text": "", "seconds": round(time.time() - t0, 1)}
            continue

        if resp.status_code != 200:
            last = {"answer": None, "status": f"http {resp.status_code}",
                    "text": resp.text[:160],
                    "seconds": round(time.time() - t0, 1)}
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(2)
            continue

        try:
            text = (resp.json()["choices"][0].get("message") or {}).get("content") or ""
        except Exception:
            text = ""
        answer = _parse(text)
        if answer is not None and count and not 1 <= answer <= count:
            # The numbering was misread, not the pictures. Say which integers exist
            # and ask again rather than paging to a tile that cannot exist.
            log(f"  solver: {answer} is outside 1..{count}, re-asking")
            payload["messages"] = [{"role": "user", "content": (
                [{"type": "text",
                  "text": build_fallback_prompt(count, answer, instruction,
                                               len(pictures))}]
                + content[1:])}]
            last = {"answer": None, "status": "out-of-range", "text": text,
                    "seconds": round(time.time() - t0, 1)}
            continue
        last = {"answer": answer, "status": "ok" if answer else "unparsed",
                "text": text, "seconds": round(time.time() - t0, 1)}
        if answer is not None:
            break
        if attempt + 1 < retries:
            log("  generic solver: no parsable answer, retrying")
    last["count"] = count
    return last
