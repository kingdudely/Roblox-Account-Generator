#!/usr/bin/env python3
"""Read the correctness screen. Four questions, one live attempt.

Everything expensive in this repo has been unmeasurable because Arkose gives no
per-wave feedback on the browserless path: a wrong answer and a right one both
come back `not answered`. But the *browser's* failure screen is different — it
reads "That was not quite right." and shows one candidate tile checked green
(measured 2026-11-01, `debug/browser_solver/20261101-195306/final.png`). If that
screen carries the correct answer, it is a dense, free labeller and the whole
solver problem turns into a supervised one.

This probe answers four things, in the order they decide the architecture:

    1. NETWORK   is the correct index in the /fc/ca/ response body?
    2. DOM       is it in the game frame's JS state or DOM attributes?
    3. PIXELS    does the checked crop match the correct candidate (and which
                 round's card does it belong to)?
    4. RETRY     does "Try again" re-serve the same board or a fresh one?

(1) and (2) decide whether the labeller is a parse or a CV job. (4) decides
whether the puzzle is beatable outright: if the board repeats, answer anything,
read the revealed tile, retry, submit it, and per-wave accuracy stops mattering.

How it drives: it answers every round with a cycling index (1, 2, 3, ...), which
is wrong often enough to guarantee a verdict, and different enough per round that
a revealed tile cannot be confused with the answer that was submitted. Wrong
answers advance the round; the challenge is graded once at the end.

    ./tools/verdict_probe.py --proxy-file proxies.txt
    ./tools/verdict_probe.py --proxy-file proxies.txt --tries 3 --answer 1

Nothing is inferred from a screenshot while the page is mid-animation: the tile
crops come from element.screenshot() on the verdict screen itself, and the
strips come from the /rtig/image response bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image
from playwright.async_api import async_playwright

from roblox_creator import config, verdict
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)

OUT = ROOT / "debug" / "verdict"

# One route for everything that can carry a verdict. Arkose and Roblox's captcha
# redemption are different hosts; both are here so a body is never lost because
# of a hostname.
NET_PATTERN = re.compile(
    r"(arkoselabs\.roblox\.com|/fc/|/rtig/|/pows/|captcha\.roblox\.com)")

# What the grading screen says. Measured string, not a guess: "That was not quite
# right." on the ten-round shadow family.
VERDICT_TEXT = ("not quite right", "that was not quite", "try again",
                "incorrect")

# Guess plaintext, captured before encryptECData turns the list into ciphertext.
GUESS_HOOK = """
(() => {
  if (window.__verdictGuessHook) return;
  window.__verdictGuessHook = true;
  window.__verdictGuesses = [];
  const original = JSON.stringify;
  JSON.stringify = function (value, ...rest) {
    try {
      if (Array.isArray(value) && value.length &&
          value[0] && typeof value[0] === 'object' && 'index' in value[0]) {
        window.__verdictGuesses.push(JSON.parse(original.call(this, value)));
      }
    } catch (e) {}
    return original.call(this, value, ...rest);
  };
})();
"""

# Anything correctness-shaped on `window`. Bounded on purpose: a JSON.stringify of
# every global would hang on a game core, so only named keys are copied.
STATE_JS = """
() => {
  const out = {};
  const seen = new Set();
  for (const key of Object.keys(window)) {
    if (seen.size >= 60) break;
    if (!/correct|answer|solution|verdict|truth|guess|challenge|game/i.test(key)) continue;
    let value;
    try { value = window[key]; } catch (e) { continue; }
    if (value === null || typeof value === 'function' || typeof value === 'undefined') continue;
    let text;
    try { text = JSON.stringify(value); } catch (e) { continue; }
    if (!text || text.length > 6000) continue;
    try { out[key] = JSON.parse(text); seen.add(key); } catch (e) {}
  }
  return out;
}
"""

# Tags every blob-backed image and every check/correct-looking element with
# `data-probe-idx`, so Playwright can screenshot each one at its own resolution
# rather than cropping a page screenshot by guesswork.
ELEMENT_JS = """
() => {
  const rows = [];
  let i = 0;
  for (const el of document.querySelectorAll('*')) {
    if (i >= 300) break;
    const cs = getComputedStyle(el);
    const bg = cs.backgroundImage || '';
    const src = el.tagName === 'IMG' ? (el.src || '') : '';
    const isBlob = bg.includes('blob:') || src.startsWith('blob:');
    const cls = String(el.className || '').slice(0, 120);
    const aria = el.getAttribute('aria-label') || '';
    if (!isBlob && !/check|correct|success|right|tick/i.test(cls + ' ' + aria)) continue;
    el.setAttribute('data-probe-idx', String(i));
    const r = el.getBoundingClientRect();
    rows.push({
      i, tag: el.tagName.toLowerCase(), cls, aria, blob: isBlob,
      w: Math.round(r.width), h: Math.round(r.height),
      x: Math.round(r.x), y: Math.round(r.y),
      attrs: Array.from(el.attributes)
        .filter(a => a.name.startsWith('data-') || a.name.startsWith('aria-'))
        .map(a => [a.name, String(a.value).slice(0, 100)]),
    });
    i++;
  }
  return rows;
}
"""


def log(message: str) -> None:
    print(message, flush=True)


def _decode(body: bytes, ctype: str) -> str | None:
    """Text bodies only. A JPEG is not a verdict, and it is already saved."""
    if not body:
        return None
    if "image/" in ctype or "octet-stream" in ctype or "font" in ctype:
        return None
    head = body[:1]
    if not (head in (b"{", b"[") or "text" in ctype or "json" in ctype
            or "javascript" in ctype):
        return None
    try:
        return body.decode("utf-8", "replace")
    except Exception:
        return None


class Capture:
    """Strips and every Arkose body, kept in arrival order."""

    def __init__(self, case: Path):
        self.case = case
        self.net = case / "net"
        self.net.mkdir(parents=True, exist_ok=True)
        self.strips: list[Path] = []
        self.entries: list[dict] = []
        self.statuses: list[int] = []
        self._n = 0

    async def on_route(self, route) -> None:
        request = route.request
        try:
            response = await route.fetch()
        except Exception as exc:
            log(f"    route fetch failed: {type(exc).__name__}")
            await route.continue_()
            return

        entry = {"method": request.method, "url": request.url[:300],
                 "status": response.status,
                 "type": (response.headers.get("content-type") or "")[:60]}
        body = b""
        try:
            body = await response.body()
        except Exception:
            pass

        if "/rtig/image" in request.url:
            self.statuses.append(response.status)
            if response.status == 200 and body:
                path = self.case / f"strip_{len(self.strips) + 1:02d}.jpg"
                path.write_bytes(body)
                self.strips.append(path)
                entry["strip"] = path.name
                log(f"    rtig 200 {len(body)} bytes -> {path.name}")
            else:
                log(f"    rtig {response.status}")
        else:
            text = _decode(body, entry["type"])
            if text:
                self._n += 1
                saved = self.net / f"{self._n:03d}.txt"
                saved.write_text(text)
                entry["saved"] = saved.name
                entry["preview"] = text[:200].replace("\n", " ")
                log(f"    net {response.status} {request.url[-70:]}")

        self.entries.append(entry)
        await route.fulfill(response=response)


# ---------------------------------------------------------------- page helpers

async def frame_texts(page) -> list[dict]:
    out = []
    for frame in page.frames:
        url = frame.url or ""
        if "arkoselabs" not in url and "funcaptcha" not in url:
            continue
        try:
            text = (await frame.inner_text("body")) or ""
        except Exception:
            continue
        out.append({"url": url[:200], "text": text})
    return out


async def read_verdict(page) -> dict | None:
    """The grading screen, if it is showing."""
    for item in await frame_texts(page):
        low = item["text"].lower()
        for needle in VERDICT_TEXT:
            if needle in low:
                return {"needle": needle, "frame": item["url"],
                        "text": item["text"][:600]}
    return None


async def click_retry(page) -> bool:
    for frame in page.frames:
        for selector in ("button", "[role=button]", "a"):
            try:
                elements = await frame.query_selector_all(selector)
            except Exception:
                continue
            for element in elements:
                try:
                    label = ((await element.get_attribute("aria-label"))
                             or (await element.inner_text()) or "").strip().lower()
                except Exception:
                    continue
                if label in ("try again", "retry") or "try again" in label:
                    try:
                        await element.click(timeout=3000)
                        return True
                    except Exception:
                        continue
    return False


def pick_answer(spec: str, round_index: int, total: int) -> int:
    """1-based candidate for this round. `cycle` is the probe default.

    Cycling matters: the verdict screen may be showing either the correct tile or
    the tile we submitted, and a constant answer would make those indistinguishable.
    """
    if spec == "cycle":
        return (round_index % max(1, total)) + 1
    if "," in spec:
        values = [int(part) for part in spec.split(",") if part.strip()]
        return values[round_index % len(values)] if values else 1
    value = int(spec)
    return min(max(1, value), max(1, total))


# ------------------------------------------------------------------- capture

async def capture_verdict(page, case: Path) -> dict:
    """Everything the grading screen exposes, saved beside a page screenshot."""
    out = case / "verdict"
    out.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(out / "page.png"))
    except Exception:
        pass

    frames: list[dict] = []
    for index, frame in enumerate(page.frames):
        url = frame.url or ""
        if "arkoselabs" not in url and "funcaptcha" not in url:
            continue
        info: dict = {"index": index, "url": url[:300], "shots": []}
        try:
            info["text"] = (await frame.inner_text("body"))[:3000]
        except Exception:
            pass
        try:
            info["html_len"] = len(await frame.content())
            (out / f"frame_{index}.html").write_text(await frame.content())
        except Exception:
            pass
        try:
            info["state"] = await frame.evaluate(STATE_JS)
        except Exception as exc:
            info["state_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        try:
            info["elements"] = await frame.evaluate(ELEMENT_JS)
        except Exception as exc:
            info["elements_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            info["elements"] = []

        try:
            handles = await frame.query_selector_all("[data-probe-idx]")
        except Exception:
            handles = []
        for handle in handles:
            try:
                tag = await handle.get_attribute("data-probe-idx")
                box = await handle.bounding_box()
                path = out / f"img_{index}_{tag}.png"
                await handle.screenshot(path=str(path))
            except Exception:
                continue
            info["shots"].append({
                "file": path.name,
                "tag": tag,
                "box": {k: round(v, 1) for k, v in (box or {}).items()},
            })
        frames.append(info)

    (out / "frames.json").write_text(json.dumps(frames, indent=1))
    return {"frames": frames}


# ------------------------------------------------------------------- analysis

def _load_strips(case: Path) -> list[Path]:
    return sorted(
        [p for p in case.glob("strip_*.jpg")
         if re.fullmatch(r"strip_\d+", p.stem)],
        key=lambda p: int(p.stem.split("_")[1]))


def _overlap(a: dict, b: dict) -> float:
    """Intersection area of two boxes. Playwright uses width/height on a
    bounding_box and the element dump uses w/h — accept both."""
    ax, ay = a.get("x", 0), a.get("y", 0)
    aw, ah = a.get("w", a.get("width", 0)), a.get("h", a.get("height", 0))
    bx, by = b.get("x", 0), b.get("y", 0)
    bw, bh = b.get("w", b.get("width", 0)), b.get("h", b.get("height", 0))
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return float(ix * iy)


def analyse_case(case: Path, answers: list[dict] | None = None) -> dict:
    """Match every saved verdict crop back to the strip it came from."""
    strips = _load_strips(case)
    frames_path = case / "verdict" / "frames.json"
    result: dict = {"strips": [p.name for p in strips], "shots": [],
                    "dom_hits": [], "network_hits": []}
    if not strips or not frames_path.exists():
        return result

    frames = json.loads(frames_path.read_text())

    # (2) DOM: a correctness key anywhere in the dumped state is a label.
    for frame in frames:
        state = frame.get("state") or {}
        for path, value in verdict.scan_state(state):
            result["dom_hits"].append(
                {"frame": frame.get("url"), "path": path, "value": value})
        for row in frame.get("elements") or []:
            blob = " ".join(str(row.get(key, "")) for key in ("cls", "aria"))
            if re.search(r"check|correct|success|tick", blob, re.I):
                result["dom_hits"].append(
                    {"frame": frame.get("url"), "element": row})

    # (1) NETWORK: the same scan over every saved body that parses as JSON.
    for path in sorted((case / "net").glob("*.txt")):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        for key, value in verdict.scan_state(payload):
            result["network_hits"].append(
                {"file": path.name, "path": key, "value": value})

    answer_by_wave: dict[int, int] = {}
    for row in answers or []:
        wave = row.get("round", row.get("wave"))
        if wave is not None and row.get("answer") is not None:
            answer_by_wave[int(wave)] = row["answer"]

    for frame in frames:
        checks = [row for row in (frame.get("elements") or [])
                  if re.search(r"check|correct|success|tick",
                               " ".join(str(row.get(k, ""))
                                        for k in ("cls", "aria")), re.I)]

        loaded = []
        for shot in frame.get("shots") or []:
            path = case / "verdict" / shot["file"]
            if not path.exists():
                continue
            try:
                image = Image.open(path).convert("RGB")
            except Exception:
                continue
            if image.width < 40 or image.height < 40:
                continue
            loaded.append((shot, image, verdict.match_board(image, strips)))

        # Match every crop against every strip. Measured on 40 human-labelled
        # waves with a realistic perturbation (0.92 scale, 5 px offset, a green
        # check overlay): tile -> (wave, index) is 40/40 this way. Restricting
        # the search to a wave inferred from the small reference card is *worse*
        # — plain NCC names the right card only 13/40 — so the card is reported
        # as context and never used as a constraint.
        for shot, image, hit in loaded:
            tile = hit["tile"]
            kind = hit["kind"]
            card = hit["card"]
            checked = any(_overlap(shot.get("box") or {}, check) > 0
                          for check in checks)
            hit_wave = card["wave"] if kind == "card" else tile["wave"]
            submitted = answer_by_wave.get(hit_wave)
            revealed = (tile["index"] if kind == "tile" else None)
            result["shots"].append({
                "file": shot["file"],
                "box": shot.get("box"),
                "card": card,
                "tile": tile,
                "kind": kind,
                "strip": strips[hit_wave - 1].name,
                "wave": hit_wave,
                "checked_element": checked,
                "submitted_for_wave": submitted,
                "revealed": revealed,
                "revealed_differs_from_submitted": (
                    None if (submitted is None or revealed is None)
                    else revealed != submitted),
            })

    return result


def print_analysis(analysis: dict) -> None:
    if analysis["dom_hits"]:
        log("  DOM correctness keys:")
        for hit in analysis["dom_hits"][:8]:
            log(f"    {hit}")
    if analysis["network_hits"]:
        log("  NETWORK correctness keys:")
        for hit in analysis["network_hits"][:8]:
            log(f"    {hit}")
    if not analysis["shots"]:
        log("  no verdict crops to match")
        return
    log("  verdict crops:")
    for shot in analysis["shots"]:
        card, tile = shot["card"], shot["tile"]
        flag = " CHECK" if shot["checked_element"] else ""
        again = " retry" if shot.get("seen_after_retry") else ""
        if shot["kind"] == "card":
            log(f"    {shot['file']:<18} reference -> wave {card['wave']:>2} "
                f"({card['score']:.2f}/{card['margin']:.2f}){flag}{again}")
        else:
            log(f"    {shot['file']:<18} candidate wave {tile['wave']:>2} "
                f"#{tile['index']:>2} ({tile['score']:.2f}/{tile['margin']:.2f})"
                f"{flag}{again}  submitted={shot['submitted_for_wave']}")
    answers = [s for s in analysis["shots"] if s["kind"] == "tile"]
    if answers:
        best = max(answers, key=lambda s: (s["checked_element"],
                                           s["tile"]["margin"]))
        log(f"  -> checked candidate: wave {best['wave']} tile "
            f"{best['tile']['index']} (submitted {best['submitted_for_wave']})")
        if best["revealed_differs_from_submitted"]:
            log("     it differs from what was submitted — the screen reveals "
                "the correct answer")


def selftest() -> int:
    """Reproduce the labeller numbers from the human labels, with no browser.

    Each labelled tile is perturbed the way the verdict panel perturbs it — 0.93
    scale, a few pixels of offset, a green check overlay — and matched back
    against every strip of its run. This is the reproducer for the 40/40 claim in
    `docs/RUNTIME.md`; if the matcher regresses, this is where it shows.
    """
    from PIL import ImageDraw

    labels_path = ROOT / "debug" / "labels" / "labels.jsonl"
    if not labels_path.exists():
        log("no human labels to test against")
        return 1
    rows = []
    for line in labels_path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = ROOT / row.get("strip", "")
        if path.exists() and isinstance(row.get("tile"), int):
            rows.append((path, row["tile"]))

    by_run: dict[Path, dict[str, int]] = {}
    for path, tile in rows:
        by_run.setdefault(path.parent, {})[path.name] = tile

    total = tile_kind = card_kind = tile_full = 0
    for run, mapping in sorted(by_run.items()):
        strips = _load_strips(run)
        if len(strips) < 5:
            continue
        for name, index in mapping.items():
            path = run / name
            if not path.exists():
                continue
            strip = Image.open(path).convert("RGB")
            wave = strips.index(path) + 1
            total += 1

            tile = strip.crop(((index - 1) * 200, 0, index * 200, 190))
            query = Image.new("RGB", (200, 190), (20, 20, 20))
            query.paste(tile.resize((186, 177), Image.LANCZOS), (5, 6))
            draw = ImageDraw.Draw(query)
            draw.ellipse((2, 2, 30, 30), fill=(60, 180, 90))
            hit = verdict.match_board(query, strips)
            tile_kind += hit["kind"] == "tile"
            tile_full += (hit["kind"] == "tile"
                          and hit["tile"]["wave"] == wave
                          and hit["tile"]["index"] == index)

            reference = verdict.card(strip)
            card_query = Image.new(
                "RGB", (reference.width + 10, reference.height + 10), (20, 20, 20))
            card_query.paste(reference.resize(
                (int(reference.width * 0.92), int(reference.height * 0.92)),
                Image.LANCZOS), (5, 5))
            card_hit = verdict.match_board(card_query, strips)
            card_kind += card_hit["kind"] == "card"

    if not total:
        log("no labels on disk that still resolve to a strip")
        return 1
    print(f"  {total} labelled waves")
    print(f"  tile crop classified as tile : {tile_kind}/{total}")
    print(f"  tile -> wave + index + kind  : {tile_full}/{total}")
    print(f"  card crop classified as card : {card_kind}/{total}")
    return 0


# ------------------------------------------------------------------- the loop

async def reach_board(page, capture: Capture, limit: float) -> tuple[object, str]:
    """Drive the challenge ladder until a board is up, or the account lands."""
    from roblox_creator.arkose_ui import find_hold_frame, press_and_hold
    from roblox_creator.puzzle_ui import GameError, GameUI
    from tools.browser_solver import dismiss_and_retrigger, press_signup

    started = time.time()
    holds = failed = retriggered = 0
    last_strips = len(capture.strips)
    stalls = 0

    while time.time() - started < limit:
        await page.wait_for_timeout(1000)
        if "/home" in page.url or "/discover" in page.url:
            return None, "created"

        frame, box = await find_hold_frame(page)
        if frame is not None and box is not None:
            took = await press_and_hold(page, log=log)
            holds += int(took)
            failed += int(not took)
            if holds >= 2 or failed >= 2:
                if retriggered < 3:
                    retriggered += 1
                    log(f"  retriggering ({retriggered}/3)")
                    await dismiss_and_retrigger(page)
                    await press_signup(page)
                    holds = failed = 0
                else:
                    return None, "hold-dead"
            continue

        try:
            ui = await GameUI.attach(page, timeout=3, log=None)
        except GameError:
            ui = None
        if ui is not None:
            log("  board is up")
            return ui, "board"

        if len(capture.strips) > last_strips:
            last_strips = len(capture.strips)
            stalls = 0
        else:
            stalls += 1
        if stalls >= 30 and retriggered < 3:
            retriggered += 1
            log(f"  no strip for {stalls}s, retriggering ({retriggered}/3)")
            await dismiss_and_retrigger(page)
            await press_signup(page)
            stalls = 0
            last_strips = len(capture.strips)

    return None, "ladder-timeout"


async def run(playwright, raw: str, case: Path, args) -> dict:
    from roblox_creator.puzzle_ui import GameUI

    parsed = parse_proxy(raw)
    capture = Capture(case)
    result: dict = {"exit": parsed.masked(), "case": str(case),
                    "outcome": "unknown", "answers": [], "verdict": None,
                    "retry": None, "rtig": []}
    browser = None
    relay = None
    try:
        relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                       parsed.user, parsed.password)
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        await context.add_init_script(GUESS_HOOK)
        await context.route(NET_PATTERN, capture.on_route)

        page = await context.new_page()
        log(f"exit {parsed.masked()}")
        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(config.SEL_USERNAME, timeout=20000)
        user, password = make_credentials()
        await fill_form(page, user, password, human=True)
        result["username"] = user
        log(f"submitted {user}")

        ui, state = await reach_board(page, capture, args.limit)
        result["ladder"] = state
        if state != "board":
            result["outcome"] = state
            return result

        started = time.time()
        consumed = 0
        rounds = 0
        while rounds < args.max_rounds and time.time() - started < args.limit:
            await page.wait_for_timeout(1200)
            if "/home" in page.url:
                result["outcome"] = "created"
                return result

            found = await read_verdict(page)
            if found:
                result["verdict"] = found
                break

            if len(capture.strips) <= consumed:
                continue
            strip = capture.strips[consumed]
            consumed += 1

            try:
                _, total = await ui.position()
            except Exception:
                total = 10
            index = pick_answer(args.answer, rounds, total)
            try:
                instruction = await ui.instruction()
            except Exception:
                instruction = ""
            log(f"  round {rounds + 1}: {strip.name} -> candidate {index} "
                f"of {total}")
            if not await ui.goto(index, total):
                log("  could not page to that candidate")
            await page.wait_for_timeout(500)
            if not await ui.submit():
                await page.wait_for_timeout(900)
                found = await read_verdict(page)
                if found:
                    result["verdict"] = found
                    break
                log("  submit control went away")
                break
            rounds += 1
            result["answers"].append(
                {"round": rounds, "answer": index, "strip": strip.name,
                 "total": total, "instruction": instruction[:240]})
            await page.wait_for_timeout(2500)

            found = await read_verdict(page)
            if found:
                result["verdict"] = found
                break

        if result["verdict"] is None:
            found = await read_verdict(page)
            if found:
                result["verdict"] = found
        if result["verdict"] is None:
            # The last submit can take a beat to grade; give it a window rather
            # than calling the run a failure at the moment the board vanishes.
            for _ in range(15):
                await page.wait_for_timeout(1000)
                found = await read_verdict(page)
                if found:
                    result["verdict"] = found
                    break
        if result["verdict"] is None:
            result["outcome"] = "no-verdict"
            return result

        log(f"  VERDICT: {result['verdict']['text'][:120]!r}")
        before = len(capture.strips)
        capture_verdict_result = await capture_verdict(page, case)
        result["verdict"]["frames"] = len(capture_verdict_result["frames"])

        # Q4 — what does the retry control do to the board?
        clicked = await click_retry(page)
        retry: dict = {"clicked": clicked}
        if clicked:
            await page.wait_for_timeout(4000)
            try:
                await page.screenshot(path=str(case / "after_retry.png"))
            except Exception:
                pass
            retry["new_strips"] = len(capture.strips) - before
            retry["text"] = [item["text"][:200] for item in await frame_texts(page)]
            if capture.strips:
                newest = capture.strips[-1]
                scores = [round(verdict.board_similarity(newest, old), 3)
                          for old in capture.strips[:-1][-30:]]
                retry["best_board_match"] = max(scores) if scores else None
                retry["same_board"] = (retry["best_board_match"] or 0) > 0.97
        result["retry"] = retry
        result["rtig"] = capture.statuses
        result["outcome"] = "verdict"
        return result
    except Exception as exc:
        result["outcome"] = f"error {type(exc).__name__}: {str(exc)[:120]}"
        return result
    finally:
        result["rtig"] = capture.statuses
        result["net"] = len(capture.entries)
        (case / "network.json").write_text(json.dumps(capture.entries, indent=1))
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        if relay is not None:
            try:
                await relay.close()
            except Exception:
                pass


# ---------------------------------------------------------------------- main

async def probe(args) -> int:
    if args.proxy:
        proxies = [args.proxy]
    else:
        proxies = load_proxies(args.proxy_file)
    if not proxies:
        log("no exits")
        return 1
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            case = OUT / stamp
            case.mkdir(parents=True, exist_ok=True)
            log(f"\n=== {stamp} ===")
            row = await run(playwright, raw, case, args)
            log(f"--- {row['outcome']}  ({row.get('rtig')})")
            analysis = analyse_case(case, row.get("answers"))
            (case / "analysis.json").write_text(json.dumps(analysis, indent=1))
            print_analysis(analysis)
            retry = row.get("retry")
            if retry:
                log(f"  retry: clicked={retry.get('clicked')} "
                    f"new_strips={retry.get('new_strips')} "
                    f"same_board={retry.get('same_board')} "
                    f"(best {retry.get('best_board_match')})")
            (case / "report.json").write_text(json.dumps(row, indent=1))
            log(f"  saved {case}")
            if row["outcome"] in ("verdict", "created"):
                return 0
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", help="one proxy line, instead of a pool file")
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--limit", type=float, default=600.0,
                    help="seconds for the whole attempt")
    ap.add_argument("--max-rounds", type=int, default=12)
    ap.add_argument("--answer", default="cycle",
                    help="'cycle' (default), one integer, or a comma list")
    ap.add_argument("--analyze", type=Path,
                    help="re-run the offline matcher on a saved case directory")
    ap.add_argument("--selftest", action="store_true",
                    help="score the labeller against the human labels, no browser")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.analyze:
        case = args.analyze
        answers = []
        report = case / "report.json"
        if report.exists():
            answers = json.loads(report.read_text()).get("answers") or []
        analysis = analyse_case(case, answers)
        print_analysis(analysis)
        (case / "analysis.json").write_text(json.dumps(analysis, indent=1))
        return 0

    return asyncio.run(probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
