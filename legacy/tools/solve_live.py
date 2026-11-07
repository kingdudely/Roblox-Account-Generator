#!/usr/bin/env python3
"""Play a live Arkose challenge end to end: transport -> vision -> `/fc/ca/`.

Supports two solvers:
  --solver chatgpt   (default) anonymous ChatGPT identity per wave
  --solver vlm       VisionClient (OVH etc.) counting top faces per tile,
                     no upload budget, no flagged identity.

    ./tools/solve_live.py --proxy-file proxies.txt --attempts 3 --create
    ./tools/solve_live.py --solver vlm --provider ovh --proxy-file proxies.txt \\
        --attempts 3 --create
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

from roblox_creator import ca, payloads
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_ca as pca
from tools import pure_challenge as pc
from tools.wave_montage import build_from_strip, split_strip
from tools.wave_solver import NUDGE, PROMPT, PROMPT_TERSE, parse_answer

N_PROMPT = ("This image shows a die face with a symbol, followed by an 'x' and a "
            "number. What is that number? Answer with one integer only.")


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


# ------------------------------------------------------------------- solvers

def ask_chatgpt(montage: Path, proxies: list[str | None],
                tries: int = 5, offset: int = -1) -> tuple[int | None, dict]:
    from tools.chatgpt_anon import AnonChat

    order: list[str | None] = [None]
    order += [p for p in proxies if p]
    order = order[:max(tries, 1)]

    last = "no attempt"
    for proxy in order:
        try:
            chat = AnonChat(proxy)
        except Exception as exc:
            last = f"open: {type(exc).__name__}: {str(exc)[:70]}"
            continue
        reply = None
        parsed = {"answer": None, "error": "no reply"}
        for prompt in (PROMPT, PROMPT_TERSE, NUDGE):
            try:
                reply = chat.ask_many(prompt, [montage], ["KEY + candidates:"])
            except Exception as exc:
                last = f"ask: {type(exc).__name__}: {str(exc)[:70]}"
                break
            parsed = parse_answer(reply.get("text") or "", 10)
            if parsed["answer"] is not None:
                break
        if parsed["answer"] is None:
            last = f"unparsed: {(reply or {}).get('text', '')[:70]}"
            continue
        return parsed["answer"] + offset, {"label": parsed["answer"],
                                      "model": reply.get("model"),
                                      "plan": reply.get("plan"),
                                      "raw": (reply.get("text") or "")[:110],
                                      "elapsed": reply.get("elapsed"),
                                      "identity": chat.device_id[:8]}
    return None, {"error": last}


def ask_nopecha(strip: Path, key: str | None) -> tuple[int | None, dict]:
    from tools.nopecha_probe import compose_grid, pick, solve_grid, wave_files

    key_img, cands = wave_files_from_strip(strip)
    answers = []
    detail = []
    for n, group in enumerate((cands[:5], cands[5:10])):
        mask = solve_grid(compose_grid([key_img] + group), PROMPT, key)
        if isinstance(mask, dict):
            detail.append(mask)
            continue
        tile, note = pick(mask)
        answers.append(None if tile is None else n * 5 + tile)
        detail.append({"mask": mask, "tile": tile, "note": note})
    got = [a for a in answers if a is not None]
    return (got[0] if got else None), {"grids": detail}


def wave_files_from_strip(strip: Path) -> tuple[Path, list[Path]]:
    out = Path("/tmp/opencode/live_wave") / strip.stem
    out.mkdir(parents=True, exist_ok=True)
    from wave_montage import split_strip
    key_img, tiles = split_strip(strip)
    key_path = out / "key.img"
    key_img.save(key_path, format="PNG")
    paths = []
    for i, tile in enumerate(tiles, start=1):
        path = out / f"cand{i}.img"
        tile.save(path, format="PNG")
        paths.append(path)
    return key_path, paths


def answer_wave_vlm(strip: Path, wave_index: int, args, client, log) -> int | None:
    from roblox_creator.keycrop import symbol_crop
    import numpy as np
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor

    key_img, tiles = split_strip(strip)
    text, _ = client._ask(N_PROMPT, [key_img.convert("RGB")], max_tokens=8)
    n = None
    if text:
        m = re.search(r"\d+", text)
        if m:
            v = int(m.group())
            if 1 <= v <= 6:
                n = v
    if n is None:
        log(f"  wave {wave_index}: could not read N")
        return None
    log(f"  wave {wave_index}: N={n}")

    key_rgb = np.array(key_img.convert("RGB"))
    symbol = symbol_crop(key_rgb)
    if symbol is None:
        symbol = key_img.convert("RGB")
        log("  symbol crop failed; using whole card")
    else:
        log(f"  symbol crop {symbol.size}")

    pil_tiles = [t.convert("RGB") for t in tiles]
    counts: list[int | None] = [None] * len(pil_tiles)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(pil_tiles)))) as pool:
        futs = {pool.submit(client.count, symbol, t): j
                for j, t in enumerate(pil_tiles)}
        for fut, j in list(futs.items()):
            try:
                c, meta = fut.result()
            except Exception:
                c = None
            counts[j] = c
            log(f"  [{j + 1:2}/{len(pil_tiles)}] count={c}")
    log(f"  counted in {time.time() - t0:.1f}s")

    hits = [i + 1 for i, c in enumerate(counts) if c is not None and c == n]
    if not hits:
        ranked = sorted(
            [(c if c is not None else -1, i + 1) for i, c in enumerate(counts)],
            reverse=True)
        if ranked and ranked[0][0] >= 0:
            hits = [ranked[0][1]]
            log(f"  no tile has {n}; guessing tile {hits[0]}")
        else:
            return None
    if len(hits) > 1:
        log(f"  tie on {hits}; trying {hits[0]}")
    return hits[0] - 1


# ---------------------------------------------------------------------- waves

def solver_exits(args) -> list[str | None]:
    exits: list[str | None] = [None]
    if args.solver_proxy_file and args.solver_proxy_file.exists():
        pool = load_proxies(args.solver_proxy_file)
        for _ in range(6):
            if not pool:
                break
            exits.append(parse_proxy(random.choice(pool)).url())
    return exits


def answer_wave_cv(strip: Path, wave_index: int, args, log) -> int | None:
    """Deterministic CV solver — no model, no network, milliseconds."""
    import numpy as np
    from wave_montage import split_strip
    from roblox_creator.dicecount import solve

    key_img, tiles = split_strip(strip)
    key_arr = np.array(key_img.convert("RGB"))[:, :, ::-1].copy()
    tile_arrs = [np.array(t.convert("RGB"))[:, :, ::-1].copy() for t in tiles]
    result = solve(key_arr, tile_arrs)
    log(f"  cv: N={result['n']} (conf {result['n_conf']}) "
        f"counts={result['counts']} answer={result['answer']}")
    if result.get("error"):
        log(f"  cv error: {result['error']}")
        return None
    if result["answer"] is None:
        return None
    if result["answer"] is None:
        return None
    if not result.get("confident", False):
        log(f"  cv ambiguous (candidates={result.get('ambiguous')}); "
            f"guessing tile {result['answer']}")
    return result["answer"] - 1  # 0-based index


def answer_wave_generic(strip: Path, wave_index: int, args, log) -> int | None:
    """Hand the frame to the model and let it decide what the puzzle is.

    No layout assumption, no per-tile calls: one image, one answer. This is the
    path that survives Arkose changing the game type.
    """
    from PIL import Image
    from roblox_creator.captcha_vision import solve as vision_solve

    result = vision_solve(Image.open(strip), provider=args.provider,
                          max_edge=2000, log=log)
    log(f"  generic: answer={result['answer']} ({result['status']}, "
        f"{result['seconds']}s)")
    if result["answer"] is None:
        return None
    return result["answer"] - 1


def answer_wave(session, proxy_url: str, blob: bytes, wave_index: int,
                args, out: Path, client=None) -> int | None:
    strip = out / f"strip{wave_index:02d}.jpg"
    strip.write_bytes(blob)
    montage = build_from_strip(strip, out / f"wave{wave_index:02d}.jpg")
    log(f"wave {wave_index}: montage {montage.name} "
        f"({montage.stat().st_size // 1024} KB)")

    if args.fixed is not None:
        log(f"  fixed answer {args.fixed + 1}")
        return args.fixed

    t0 = time.time()
    if getattr(args, "solver", "chatgpt") in ("cv", "vlm", "hybrid", "generic"):
        if args.solver == "generic":
            ans = answer_wave_generic(strip, wave_index, args, log)
        elif args.solver == "vlm":
            ans = answer_wave_vlm(strip, wave_index, args, client, log)
        elif args.solver == "cv":
            ans = answer_wave_cv(strip, wave_index, args, log)
        else:  # hybrid: CV first, VLM fallback
            ans = answer_wave_cv(strip, wave_index, args, log)
            if ans is None and client is not None:
                log("  cv failed/ambiguous; trying vlm")
                ans = answer_wave_vlm(strip, wave_index, args, client, log)
        log(f"  answer {None if ans is None else ans + 1} "
            f"in {time.time() - t0:.1f}s")
        return ans
    if args.solver == "nopecha":
        answer, meta = ask_nopecha(strip, args.nopecha_key or None)
    else:
        answer, meta = ask_chatgpt(montage, solver_exits(args),
                                   offset=args.label_offset)
    log(f"  answer {None if answer is None else answer + 1} "
        f"in {time.time() - t0:.1f}s  {json.dumps(meta)[:120]}")
    return answer


# ---------------------------------------------------------------------- play

def play(proxy: str, args, out: Path, client=None) -> dict:
    proxy_url = parse_proxy(proxy).url()
    result: dict = {"proxy": parse_proxy(proxy).masked(), "waves": [],
                    "challenge_id": "", "challenge_type": "",
                    "images_ok": 0, "waves_expected": None}

    try:
        challenge = pc.request_blob(proxy_url)
    except Exception as exc:
        result["error"] = f"request_blob: {type(exc).__name__}: {str(exc)[:60]}"
        return result
    if not challenge:
        result["error"] = "no challenge"
        return result

    result["challenge_id"] = challenge.get("challenge_id", "")
    result["challenge_type"] = challenge.get("challenge_type", "")

    pool = payloads.load_payloads()
    record = payloads.pick_payload(pool)
    session = pc.new_session(proxy_url)
    if record is not None:
        c_blob, ua = payloads.reseal(record)
        result["client"] = "mobile" if record.get("mobile") else "desktop"
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
    else:
        from roblox_creator.arkose_py import Fingerprint, load_fingerprints, pick_fingerprint
        fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
        ua = fp.user_agent
        result["client"] = "synthetic-mobile"
        minted = pc.gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"], fp,
                        session=session)

    token = minted.get("token", "")
    if not token:
        result["error"] = f"gt2 {minted.get('status')}"
        return result

    pow_result = pc.pows(proxy_url, token, ua, session=session)
    if not pow_result.get("ok"):
        last = (pow_result.get("rounds") or [{}])[-1]
        result["error"] = f"pow {last.get('action') or pow_result.get('stage')}"
        return result

    game = pc.gfct(proxy_url, token, ua, session=session)
    if game.get("status") != 200 or "game_data" not in game:
        result["error"] = "gfct"
        return result

    data = game["game_data"]
    game_token = game.get("challengeID") or ""
    session_token = game.get("session_token") or ""
    sid = game.get("sid") or "us-east-1"
    result.update(waves_expected=data.get("waves"),
                  difficulty=data.get("game_difficulty"),
                  instruction=data.get("instruction_string"))
    log(f"challenge: {data.get('waves')} waves, {data.get('instruction_string')}")

    referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/standard/"
               f"index.html?session={session_token}")
    images = data.get("customGUI", {}).get("_challenge_imgs") or []
    next_url = images[0] if images else None

    guesses: list[dict] = []
    for wave in range(int(data.get("waves") or 0) + 2):
        if not next_url:
            result["error"] = "no next image"
            break
        blob = pca.fetch_image(session, next_url, referer, ua, attempts=2)
        if not blob:
            result["error"] = f"wave {wave} image fetch"
            result["died_at_wave"] = wave
            break
        result["images_ok"] = result.get("images_ok", 0) + 1

        index = answer_wave(session, proxy_url, blob, wave, args, out, client)
        if index is None:
            result["waves"].append({"wave": wave, "answer": None,
                                    "outcome": "solver-failed"})
            break

        guesses.append({"index": index})
        form = ca.build_form(session_token, game_token, sid, guesses)
        resp = pca.submit(session, form, ua, referer)
        entry = {
            "wave": wave,
            "answer": index,
            "status": resp.get("status"),
            "response": resp.get("response"),
            "incorrect": resp.get("incorrect_guess"),
            "solved": bool(resp.get("solved")),
        }
        result["waves"].append(entry)
        log(f"  -> {entry['response']!r} incorrect={entry['incorrect']} "
            f"solved={entry['solved']}")

        if entry["solved"]:
            result["solved"] = True
            result["decryption_key"] = resp.get("decryption_key")
            if args.create:
                result["finalhop"] = finish_account(challenge, token)
            break
        if entry["incorrect"]:
            break

        imgs = resp.get("_challenge_imgs") or []
        next_url = imgs[0] if imgs else None
    else:
        result["error"] = "wave limit"

    answered = [w for w in result["waves"] if w.get("solved")]
    attempted = [w for w in result["waves"] if w.get("answer") is not None]
    result["waves_correct"] = len(answered)
    result["waves_attempted"] = len(attempted)
    return result


def finish_account(challenge: dict, token: str) -> dict:
    from roblox_creator import finalhop

    ctx = {k: challenge.get(k) for k in
           ("challenge_id", "challenge_type", "metadata", "raw_metadata",
            "body", "headers", "csrf")}
    hop = finalhop.finish(challenge["session"], ctx, token)
    if hop["created"]:
        log(f"  ACCOUNT CREATED via '{hop['winner']}'")
        log(f"  cookies: {len(challenge['session'].cookies)}")
    else:
        for row in hop["tried"]:
            log(f"  {row['variant']:<11} -> {row['signup']['status']} "
                f"{str(row['signup']['body'])[:110]}")
    return hop


def replay(dirpath: Path, args, client=None) -> int:
    strips = sorted(dirpath.glob("strip*.jpg")) + sorted(dirpath.glob("wave*.jpg"))
    strips = [p for p in strips if p.stat().st_size > 10_000]
    if not strips:
        log(f"no strips in {dirpath}")
        return 2
    if args.solver == "vlm" and client is None:
        from roblox_creator.vlm_client import VisionClient
        client = VisionClient(provider=args.provider, exits=[])
    for strip in strips[:args.attempts or None]:
        montage = build_from_strip(strip, Path("/tmp/opencode/replay") / strip.name)
        t0 = time.time()
        if args.solver == "nopecha":
            answer, meta = ask_nopecha(strip, args.nopecha_key or None)
        elif args.solver in ("vlm", "cv", "hybrid"):
            if args.solver == "vlm":
                ans = answer_wave_vlm(strip, 0, args, client, log)
            elif args.solver == "cv":
                ans = answer_wave_cv(strip, 0, args, log)
            else:
                ans = answer_wave_cv(strip, 0, args, log)
                if ans is None and client is not None:
                    ans = answer_wave_vlm(strip, 0, args, client, log)
            answer, meta = ans, {}
        else:
            answer, meta = ask_chatgpt(montage, solver_exits(args),
                                       offset=args.label_offset)
        log(f"{strip.name:22} answer={None if answer is None else answer + 1} "
            f"{time.time() - t0:.1f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--solver-proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--solver", default="cv",
                    choices=("chatgpt", "nopecha", "vlm", "cv", "hybrid", "generic"))
    ap.add_argument("--provider", default="lithosai")
    ap.add_argument("--nopecha-key", default="")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--tries", type=int, default=3, help="exits per attempt")
    ap.add_argument("--label-offset", type=int, default=-1)
    ap.add_argument("--fixed", type=int, default=None)
    ap.add_argument("--replay", type=Path, default=None)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    if args.replay:
        return replay(args.replay, args, None)

    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    out = ROOT / "debug" / "live" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    exits = [parse_proxy(x).url() for x in pool]
    random.shuffle(exits)
    client = None
    if args.solver in ("vlm", "hybrid"):
        from roblox_creator.vlm_client import VisionClient
        client = VisionClient(provider=args.provider, exits=exits)

    rows = []
    for attempt in range(args.attempts):
        for t in range(args.tries):
            proxy = random.choice(pool)
            log(f"[{attempt + 1}/{args.attempts}] {parse_proxy(proxy).masked()}")
            row = play(proxy, args, out, client)
            row["attempt"] = attempt
            row["try"] = t
            expected = row.get("waves_expected") or 0
            short = expected and row.get("images_ok", 0) < expected
            if not row.get("waves_attempted") or short:
                why = row.get("error") or (
                    f"only {row.get('images_ok', 0)}/{expected} images served")
                log(f"  retry on a new exit: {why}")
                continue
            break
        rows.append(row)
        log("")

    solved = sum(1 for r in rows if r.get("solved"))
    attempts_that_answered = [r for r in rows if r.get("waves_attempted")]
    correct = sum(r["waves_correct"] for r in attempts_that_answered)
    attempted = sum(r["waves_attempted"] for r in attempts_that_answered)
    complete = [r for r in rows
                if r.get("waves_expected") and r.get("images_ok") == r["waves_expected"]]
    died = [r for r in rows if r.get("died_at_wave") is not None]
    dist: dict[int, int] = {}
    for r in rows:
        dist[r.get("images_ok", 0)] = dist.get(r.get("images_ok", 0), 0) + 1
    print(f"  solved {solved}/{len(rows)} attempts")
    print(f"  images per challenge: {dict(sorted(dist.items()))}")
    print(f"  challenges that could have been completed: "
          f"{len(complete)}/{len(rows)}   image-starved: {len(died)}")
    for r in died[:6]:
        print(f"    died at wave {r['died_at_wave']}/{r.get('waves_expected')} "
              f"after {r.get('images_ok')} images ({r.get('difficulty')=})")
    if attempted:
        print(f"  waves correct {correct}/{attempted} "
              f"({100 * correct / attempted:.0f}% per-wave)")
        per = correct / attempted
        for n in (5, 6, 10):
            print(f"    implied pass rate at {n} waves: {100 * per ** n:.0f}%")

    (out / "rows.json").write_text(json.dumps(rows, indent=1))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(rows, indent=1))
    log(f"-> {out}")
    if client is not None:
        print(f"  vlm stats: {client.stats}")
    return 0 if solved else 1


if __name__ == "__main__":
    raise SystemExit(main())