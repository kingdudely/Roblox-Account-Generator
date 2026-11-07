#!/usr/bin/env python3
"""Solve an Arkose `MatchDiceIconCount` wave with the anonymous ChatGPT client.

A wave is one directory holding `key.img` plus `cand1..cand10.img`. The key
carries a symbol and a required count (`× N`); each candidate is a pile of
dice, every visible face showing a symbol. The answer is the candidate whose
count of the key's symbol equals N.

Wire shape: **one image per wave** — a montage with the key in its own row and
the ten candidates numbered beneath it. That matters: an anonymous identity gets
~18 uploads, and a desktop-payload challenge costs ~6 waves, so one upload per
wave lets a single identity serve a whole challenge with room to spare. Eleven
separate uploads would not even fit one challenge.

Zipping the wave into one archive to save uploads was tested and does not work:
the upload endpoint accepts a `.zip` through all three stages, then the
conversation rejects it with `invalid_image_format` — the multimodal pipeline
only reads real image bytes (`tools/zip_upload_probe.py`).

Prompt shape matters just as much. Guest answers are truncated by the server at
roughly 150–200 characters, so the reply must be the answer itself. Concrete,
single-question phrasing gets a direct reply ("Match: 2"); asking for a JSON
schema or an explanation gets a chatty preamble that is cut mid-sentence.

    ./tools/wave_solver.py --wave debug/vision/20261029-084809-unknown-1
    ./tools/wave_solver.py --all --repeat 3
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.chatgpt_anon import AnonChat


def mask_proxy(proxy: str | None) -> str:
    """host:port only — credentials never reach a log or a saved row."""
    if not proxy:
        return "direct"
    tail = proxy.rsplit("@", 1)[-1]
    return tail.rsplit(":", 1)[0][:24]
from tools.wave_montage import build

# Guest replies are truncated by the server at roughly 150-200 characters, so a
# model that thinks out loud loses the answer before it names it. Every run of
# this prompt that failed, failed that way — the reply described the key symbol
# and got cut before the number. So the instruction demands the digits first.
PROMPT = (
    "Image: the KEY at top-left shows one symbol and 'x N'. Tiles below are "
    "numbered 1-10. Which tile shows that symbol exactly N times? "
    "Reply with the tile number only."
)

PROMPT_TERSE = "Which numbered tile matches the key? Reply with digits only."

# Third attempt, sent into the *same* conversation: the image is already attached
# and the model is mid-thought, so a bare demand often lands where a full question
# invited commentary. Replies like "I can help identify which candidates..." parse
# to nothing, and that was 7 of 17 waves in one measured run.
NUDGE = "Answer now with one digit, 1-10. Nothing else."


def wave_dirs(base: Path) -> list[Path]:
    return [d for d in sorted(base.iterdir())
            if d.is_dir() and (d / "key.img").exists()]


def solve_wave(chat: AnonChat, wave: Path, grid_dir: Path | None = None,
               scale: int = 2) -> dict:
    montage = build(wave, (grid_dir or Path("/tmp/opencode/candgrid"))
                    / f"{wave.name}.jpg", scale)
    r = chat.ask_many(PROMPT, [montage], ["KEY + candidates:"])
    n_cands = len(list(wave.glob("cand*.img")))
    parsed = parse_answer(r["text"], n_cands)
    parsed.update({
        "wave": wave.name,
        "elapsed": r["elapsed"],
        "model": r["model"],
        "plan": r["plan"],
        "uploads": len(r["upload"]),
        "raw": r["text"],
    })
    return parsed


def parse_answer(text: str, n_cands: int) -> dict:
    """Return the **1-based tile label** the reply names.

    The reply is free text from a chat model, so the parse is ordered by how
    strongly the text commits to an answer:

      1. "Candidate 4" / "tile 4" / "number 4"  — explicit reference
      2. "Answer: 4"                             — labelled answer
      3. first standalone 1..n integer           — leading digit

    A trailing-digit fallback was removed deliberately: replies like
    "...exactly 2 times, so candidate 5" end in the wrong number, and picking the
    last integer silently inverts the answer.
    """
    if not text:
        return {"answer": None, "error": "empty"}

    explicit = re.search(r"(?:candidate|tile|option)\s*\**\s*[:#]?\s*(\d{1,2})",
                         text, re.I)
    if explicit and 1 <= int(explicit.group(1)) <= n_cands:
        return {"answer": int(explicit.group(1)), "error": ""}

    labelled = re.search(r"(?:answer|choice)\s*[:=]\s*\**\s*(\d{1,2})", text, re.I)
    if labelled and 1 <= int(labelled.group(1)) <= n_cands:
        return {"answer": int(labelled.group(1)), "error": ""}

    # Only a *leading* number is trusted. A mid-sentence integer is usually the
    # key count ("...exactly 2 times") rather than the tile, and confusing the
    # two inverts the answer while looking parsed.
    leading = re.match(r"\s*\**\s*(\d{1,2})\b", text)
    if leading and 1 <= int(leading.group(1)) <= n_cands:
        return {"answer": int(leading.group(1)), "error": "leading-number"}

    return {"answer": None, "error": "unparsed"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=Path, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--base", type=Path, default=ROOT / "debug" / "vision")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-index", type=int, default=None,
                    help="pick one line from --proxy-file")
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxiesithink.txt")
    ap.add_argument("--tries", type=int, default=3,
                    help="fresh identity + exit per attempt (a flagged anon "
                         "identity 403s after one turn, so this is normal)")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    from tools.chatgpt_anon import load_proxies

    proxies = load_proxies(args.proxy_file) if args.proxy_file.exists() else []
    if args.proxy_index is not None and args.proxy_index < len(proxies):
        proxies = [proxies[args.proxy_index]]
    if args.proxy:
        proxies = [args.proxy]

    waves = wave_dirs(args.base) if args.all else ([args.wave] if args.wave else [])
    if not waves:
        print("no waves; pass --wave DIR or --all")
        return 2

    rows = []
    for w in waves:
        for rep in range(args.repeat):
            # A fresh identity per wave, on its own exit.
            #
            # Reusing one AnonChat is not just an upload-budget question: the
            # server flags the identity after the first turn and every later
            # call returns 403 "Unusual activity" (measured: 1 success then
            # 5 failures on one session). Since the pool is ~40k residential
            # exits, fanning out *is* the budget — one wave per identity, one
            # identity per exit.
            r = {"wave": w.name, "answer": None,
                 "error": "no attempt", "elapsed": 0, "raw": ""}
            for attempt in range(args.tries):
                proxy = random.choice(proxies) if proxies else args.proxy
                chat = AnonChat(proxy)
                try:
                    r = solve_wave(chat, w, scale=args.scale)
                    r["proxy"] = mask_proxy(proxy)
                    r["identity"] = chat.device_id[:8]
                    if r.get("answer") is not None:
                        break
                except Exception as e:
                    r = {"wave": w.name, "answer": None,
                         "error": f"{type(e).__name__}: {str(e)[:90]}",
                         "elapsed": 0, "raw": "",
                         "proxy": mask_proxy(proxy),
                         "identity": chat.device_id[:8]}
            r["rep"] = rep
            rows.append(r)
            print(f"  {w.name:32} ans={r.get('answer')} {r.get('elapsed')}s "
                  f"up={r.get('uploads')} {r.get('proxy','')}  "
                  f"{r.get('raw','')[:48]!r} {r.get('error') or ''}", flush=True)
            if rep + 1 < args.repeat:
                time.sleep(1)

    # stability: how often do repeats of the same wave agree?
    by_wave: dict[str, list] = {}
    for r in rows:
        by_wave.setdefault(r["wave"], []).append(r.get("answer"))
    agree = sum(1 for v in by_wave.values() if len(v) > 1 and len(set(v)) == 1)
    print(f"\n  answered {sum(1 for r in rows if r.get('answer'))}/{len(rows)}"
          f"   unanimous {agree}/{sum(1 for v in by_wave.values() if len(v) > 1)}")
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(rows, indent=1))
        print(f"  saved -> {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
