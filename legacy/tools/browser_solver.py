#!/usr/bin/env python3
"""Create an account with a browser, answering each wave from the intercepted strip.

Why the browser, given "no browser" was the goal: measured on 2026-11-01, the
browserless chain gets two wave images and then `403 DENIED ACCESS` on the third,
while a real browser on the same exits got **ten 200s and created an account**
(`debug/human/20261101-171342`). A human reported the working sequence: press and
hold twice, close the popup, click Sign Up again — the *second* challenge is the
one that serves every wave. The browserless path never re-triggers, so it keeps
taking the starved first challenge.

So this drives the browser, and solves with the model. The strip is taken from the
network, not from a screenshot: a page screenshot catches the "3 done" progress
animation and a vision model answers `3`. The `/rtig/image` response is the clean
puzzle, the same bytes the browserless path fetches.

Pipeline per wave:

    rtig response body -> strip image -> generic vision solver -> candidate index
    -> page the carousel to that candidate -> Submit -> wait for the next strip

    ./tools/browser_solver.py --proxy-file proxies2.txt --limit 600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from roblox_creator import config
from roblox_creator import client_shape
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import LAUNCH_ARGS, STEALTH_JS, UA, fill_form, make_credentials
from tools.verdict_probe import capture_verdict, click_retry, read_verdict

OUT = ROOT / "debug" / "browser_solver"

# Rule wording that identifies a family not worth grinding. Substring match
# against the DOM instruction, lowercased; empty means attempt everything.
#
# "shadow" was in here for one run and came back out. The reason it went in was a
# reference card the model could not name — four passes returned "jet", "compass",
# "Chinese character" and "watering can" for the same 122 px glyph. The reason it
# came out is that the card was never the problem: the *presentation* was. On one
# canvas with the card enlarged, the same model reads the same card as a cloud over
# a rocket, and reads a watering can and a hat with shoes on the other strips.
UNSOLVABLE_RULES: tuple[str, ...] = ()

# How far the matcher's top candidate must beat the runner-up before `--solver
# auto` trusts it. Cosine over a 256-d embedding, so the scale is roughly 0..1 and
# a real lead is a few hundredths. Not tuned against real labels yet — there are
# none — so treat this as a starting point, not a measurement.
ONNX_MARGIN = 0.02


def log(message: str) -> None:
    print(message, flush=True)


def resolve_shape(requested: str) -> str:
    """`auto` picks a shape only when the recorded evidence beats the default.

    `off` sends the request exactly as the browser would, which is what every
    build before this one did — keep it reachable.
    """
    if requested in ("off", ""):
        return ""
    if requested != "auto":
        return requested
    picked = client_shape.preferred()
    if picked and picked != client_shape.DEFAULT_SHAPE:
        log(f"  shape auto -> {picked} (beats the default on recorded evidence)")
        return picked
    return client_shape.DEFAULT_SHAPE


class StripBank:
    """Collects `/rtig/image` bodies in arrival order."""

    def __init__(self, case: Path):
        self.case = case
        self.strips: list[Path] = []
        self.statuses: list[int] = []

    async def on_route(self, route) -> None:
        """Fetch the strip ourselves, then hand the browser the same bytes.

        Listening on `response` was tried first and every body came back empty —
        two 200s, both 0 bytes — because by the time the async listener resumes,
        the response has been discarded. Intercepting the request and fulfilling
        it keeps the body in our hands and is transparent to the page.
        """
        try:
            response = await route.fetch()
        except Exception as exc:
            log(f"    rtig fetch failed: {type(exc).__name__}")
            await route.continue_()
            return
        self.statuses.append(response.status)
        if response.status != 200:
            log(f"    rtig {response.status}")
            await route.fulfill(response=response)
            return
        try:
            body = await response.body()
        except Exception as exc:
            log(f"    rtig 200 body unavailable: {type(exc).__name__}")
            await route.fulfill(response=response)
            return
        # The endpoint does not only serve the dice strip. Measured 2026-11-01:
        # the "move the person onto the matching icon" game pulls its key image and
        # its scene from the same `/rtig/image` URL, two per wave. Saving whatever
        # arrives, by sniffed format, is what lets the caller notice which game it
        # is holding instead of silently discarding both.
        if body[:3] == b"\xff\xd8\xff":
            suffix = ".jpg"
        elif body[:8] == b"\x89PNG\r\n\x1a\n":
            suffix = ".png"
        elif body[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            suffix = ".bin"
        if not body:
            log("    rtig 200 but empty body")
            await route.fulfill(response=response)
            return
        path = self.case / f"strip_{len(self.strips) + 1:02d}{suffix}"
        path.write_bytes(body)
        self.strips.append(path)
        log(f"    rtig 200 {len(body)} bytes {suffix}")
        await route.fulfill(response=response)


async def dismiss_and_retrigger(page) -> bool:
    """Close the challenge popup and press Sign Up again.

    This is the step the human used to get a challenge that serves every wave. The
    close control is the only button in the modal that is not the submit control,
    so it is found by its label rather than by position.
    """
    for frame in page.frames:
        for button in await frame.query_selector_all("button, [role=button]"):
            try:
                label = ((await button.get_attribute("aria-label"))
                         or (await button.inner_text()) or "").strip().lower()
            except Exception:
                continue
            if label in ("close", "dismiss", "x") or "close" in label:
                try:
                    await button.click(timeout=3000)
                    log("  closed the challenge popup")
                    await page.wait_for_timeout(1500)
                    return True
                except Exception:
                    continue
    # No labelled close control: Escape dismisses most Arkose modals, and it is
    # harmless when it does not.
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1200)
        log("  pressed Escape to dismiss the challenge")
        return True
    except Exception:
        return False


async def press_signup(page) -> bool:
    for button in await page.query_selector_all("button"):
        try:
            label = ((await button.inner_text()) or "").strip().lower()
        except Exception:
            continue
        if "sign up" in label:
            try:
                await button.click(timeout=3000)
                log("  pressed Sign Up again")
                await page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


async def pick_answer(strip_img, pictures, total: int, instruction: str,
                      provider: str, vision_solve, log, solver: str) -> dict:
    """Route one board to the local matcher or to the vision API.

    `auto` is the only sane default while the matcher is still unproven: it takes
    a board the local model is *sure* about and hands the rest to the API. The
    certainty gate is the margin between the top two cosine scores, not the top
    score itself — a matcher can be confident and wrong at the same time, but when
    the top two candidates are a rounding error apart it is demonstrably torn, and
    that board is exactly the one worth paying 2.8 s for.
    """
    if solver in ("onnx", "auto"):
        from roblox_creator.matcher import Matcher
        matcher = Matcher()
        if matcher.available():
            started = time.time()
            local = matcher.solve(strip_img)
            elapsed = round(time.time() - started, 3)
            took = solver == "onnx" or local["margin"] >= ONNX_MARGIN
            if took:
                log(f"  matcher: -> {local['answer']} (margin "
                    f"{local['margin']}, {elapsed}s)")
                return {"answer": local["answer"], "status": "onnx",
                        "seconds": elapsed,
                        "text": f"scores={local['scores']}\nmargin={local['margin']}"}
            log(f"  matcher torn (margin {local['margin']} < {ONNX_MARGIN}) "
                f"— asking the API")
        elif solver == "onnx":
            log("  no matcher on disk; falling back to the vision API")

    return vision_solve(images=pictures, provider=provider, max_edge=1568,
                        log=log, instruction=instruction, count=total)


async def note_verdict(page, case: Path, result: dict, log) -> bool:
    """Capture the grading screen if it is showing. True when it was.

    The screen carries the correct tile, which is the only dense label this
    system can get for free. It is also destroyed the moment "Try again" is
    clicked, so the capture has to happen before the retry — not after, and not
    from a page screenshot taken while the panel animates.
    """
    found = await read_verdict(page)
    if not found:
        return False
    result["verdict"] = found
    result["outcome"] = "wrong-answer"
    log(f"  grading screen: {found['text'][:120]!r}")
    try:
        await page.screenshot(path=str(case / "verdict.png"))
    except Exception:
        pass
    try:
        captured = await capture_verdict(page, case)
        result["verdict_crops"] = sum(len(frame.get("shots") or [])
                                      for frame in captured["frames"])
        log(f"  captured {result['verdict_crops']} verdict crops")
    except Exception as exc:
        log(f"  verdict capture failed: {type(exc).__name__}: {str(exc)[:60]}")
    return True


async def run(playwright, raw: str, limit: float, provider: str,
              presentation: str = "single", solver: str = "auto",
              shape: str = "") -> dict:
    from roblox_creator.arkose_ui import find_hold_frame, press_and_hold, start_puzzle
    from roblox_creator.captcha_vision import solve as vision_solve
    from roblox_creator.puzzle_ui import GameError, GameUI
    from roblox_creator.strip_montage import (candidates_image, single_image,
                                              target_image)
    from PIL import Image

    parsed = parse_proxy(raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    case = OUT / stamp
    case.mkdir(parents=True, exist_ok=True)
    bank = StripBank(case)
    result = {"exit": parsed.masked(), "outcome": "unknown", "waves": [],
              "rtig": bank.statuses, "case": str(case)}

    browser = None
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        await context.route("**/rtig/image**", bank.on_route)

        # Which challenge flavour the server hands out is risk-scored, and the
        # client shape is one of the inputs. Only `x-roblox-platform` is imposed
        # here — the UA stays whatever Chromium actually is, because a header
        # that disagrees with the TLS fingerprint buys less than it costs.
        imposed = client_shape.browser_headers(shape) if shape else {}

        async def tap_signup_headers(route) -> None:
            if not imposed:
                await route.continue_()
                return
            headers = {**route.request.headers, **imposed}
            await route.continue_(headers=headers)

        if imposed:
            await context.route("**/v2/signup**", tap_signup_headers)

        def note_flavour(response) -> None:
            if "/v2/signup" not in response.url:
                return
            flavour = response.headers.get("rblx-challenge-type", "")
            if flavour in (client_shape.SOLVABLE, client_shape.UNSOLVABLE):
                client_shape.record(shape or client_shape.DEFAULT_SHAPE, flavour)
                log(f"  challenge flavour: {flavour} (shape={shape})")

        context.on("response", note_flavour)

        page = await context.new_page()

        log(f"exit {parsed.masked()}")
        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(config.SEL_USERNAME, timeout=20000)
        user, password = make_credentials()
        await fill_form(page, user, password, human=True)
        result["username"] = user
        log(f"submitted {user}")

        # A frame every few seconds. Debugging a stall from logs alone is guesswork
        # — the ladder has at least four screens and they look nothing alike — and
        # the captures cost nothing but disk.
        stop_shots = asyncio.Event()

        async def snapshot_loop() -> None:
            index = 0
            while not stop_shots.is_set():
                try:
                    await page.screenshot(path=str(case / f"frame_{index:03d}.png"),
                                          timeout=8000)
                except Exception:
                    pass
                index += 1
                try:
                    await asyncio.wait_for(stop_shots.wait(), timeout=6)
                except asyncio.TimeoutError:
                    continue

        shots = asyncio.create_task(snapshot_loop())

        started = time.time()
        holds = 0
        accepted_holds = 0
        retriggered = 0
        start_clicks = 0
        ui: GameUI | None = None
        solved_waves = 0
        consumed = 0
        stalls = 0
        last_strips = 0
        verdict_retries = 0

        while time.time() - started < limit:
            await page.wait_for_timeout(1000)

            if "/home" in page.url:
                result["outcome"] = "created"
                break

            # The grading screen is not a failure of the transport and it is not
            # a submit bug: the board was answered, Arkose graded the whole
            # challenge, and at least one answer was wrong. Capture it (it carries
            # the correct tile) before doing anything else, because the retry
            # control destroys it.
            if await note_verdict(page, case, result, log):
                if verdict_retries < 2:
                    verdict_retries += 1
                    if await click_retry(page):
                        log(f"  clicked Try again ({verdict_retries}/2)")
                        await page.wait_for_timeout(3000)
                        consumed = len(bank.strips)
                        solved_waves = 0
                        retriggered = 0
                        start_clicks = 0
                        accepted_holds = 0
                        holds = 0
                        stalls = 0
                        ui = None
                        result["outcome"] = "retrying"
                        continue
                break

            # The ladder: a hold widget means the challenge has not started yet.
            #
            # Measured by hand on 2026-11-01 and confirmed by the user: it asks for
            # two holds, accepts both, and then serves a third pill that never
            # fills. Nothing clears it — the challenge has to be closed and Sign Up
            # pressed again, which mints one that serves waves. So this counts
            # *accepted* holds and closes on the second, rather than waiting for a
            # failure that never comes.
            hold_frame, hold_box = await find_hold_frame(page)
            if hold_frame is not None and hold_box is not None:
                try:
                    rung = (await hold_frame.inner_text("body") or "").strip()
                except Exception:
                    rung = ""
                if rung:
                    log(f"  hold rung: {rung[:120].replace(chr(10), ' ')}")
                took = await press_and_hold(page, log=log)
                if took:
                    accepted_holds += 1
                else:
                    holds += 1
                    log(f"  hold did not take ({holds})")
                if accepted_holds >= 2 or holds >= 2:
                    if retriggered < 3:
                        retriggered += 1
                        why = ("two holds accepted" if accepted_holds >= 2
                               else "hold would not take")
                        log(f"  closing the challenge ({why}), retriggering "
                            f"({retriggered}/3)")
                        await dismiss_and_retrigger(page)
                        await press_signup(page)
                        accepted_holds = 0
                        holds = 0
                        start_clicks = 0
                        ui = None
                    elif accepted_holds == 0:
                        # Three remints and the pill has never filled once. That is
                        # not a challenge waiting on patience, it is a proxy whose
                        # session the widget will not verify — measured 2026-11-01,
                        # ten straight sub-5%-fill attempts on one exit while the
                        # next exit served waves immediately. Grinding it only
                        # spends the whole time budget on one dead exit.
                        log(f"  hold never took after {retriggered} remints — "
                            f"this exit is dead, moving on")
                        result["outcome"] = "hold-dead"
                        break
                continue

            if ui is None:
                # The consent screen that opens a challenge has no hold and no
                # puzzle until its own button is clicked.
                if start_clicks < 3:
                    if await start_puzzle(page, log=log):
                        start_clicks += 1
                        continue
                try:
                    ui = await GameUI.attach(page, timeout=3, log=None)
                except GameError:
                    ui = None

            # Arkose serves several game types from the same `/rtig/image`
            # endpoint. The dice family arrives as one 2000x400 strip per wave,
            # which the carousel and the vision solver are built for. The
            # "move the person onto the matching icon" family pulls two loose
            # images per wave and wants arrow presses — measured 2026-11-01, and
            # a run sat on one for 90 s while `attach` reported success because a
            # game-core frame existed. Idle is therefore counted by strips, not by
            # frames: no strip for 25 s means this is not the family we can drive,
            # so close it and mint again, which is what the human did by hand.
            if len(bank.strips) > last_strips:
                last_strips = len(bank.strips)
                stalls = 0
            else:
                stalls += 1

            if stalls >= 25 and retriggered < 3:
                retriggered += 1
                log(f"  no strip for {stalls}s, retriggering ({retriggered}/3)")
                await dismiss_and_retrigger(page)
                await press_signup(page)
                stalls = 0
                start_clicks = 0
                ui = None
                continue

            if ui is None:
                continue

            # A new strip arrived -> answer that wave.
            if len(bank.strips) <= consumed:
                continue
            strip = bank.strips[consumed]
            consumed += 1

            try:
                _, total = await ui.position()
            except Exception:
                total = 10

            # The rule lives in the DOM, not in the pixels. Every wave this was
            # solved without it was a guess — measured 2026-11-01, 2/10 against
            # hand labels, which is chance for five candidates. The sentence is
            # also the only challenge-specific input the solver gets, which is what
            # lets `captcha_vision` stay family-blind.
            try:
                instruction = await ui.instruction()
            except Exception:
                instruction = ""
            try:
                board = await ui.frame_text()
            except Exception:
                board = ""
            if board:
                log(f"  board: {board[:180]}")
            if instruction:
                log(f"  rule: {instruction[:140]}")

            strip_img = Image.open(strip).convert("RGB")

            # One canvas, card on the left, numbered tiles to its right, at
            # 1442 px wide — inside the 1568 px ceiling every provider applies, so
            # nothing is resampled on the way in. Measured 2026-11-01: given this
            # presentation the model names the reference correctly (a cloud and a
            # rocket, a watering can, a hat and shoes) and reasons about each
            # candidate; given the two-image split it had called the same card a
            # jet, a compass and a Chinese character across four passes. The split
            # is kept behind --presentation split because it is still the better
            # frame when a family serves tiles too small to survive one canvas.
            if presentation == "single":
                pictures = [single_image(strip_img)]
                names = ("single",)
            else:
                pictures = [target_image(strip_img), candidates_image(strip_img)]
                names = ("target", "grid")
            for label, picture in zip(names, pictures):
                picture.save(case / f"{strip.stem}_{label}.jpg", quality=95)

            answer = await pick_answer(strip_img, pictures, total, instruction,
                                       provider, vision_solve, log, solver)

            index = answer["answer"]
            log(f"  wave {solved_waves + 1}: strip {strip.name} -> {index} "
                f"({answer['status']}, {answer['seconds']}s, {total} candidates)")
            if answer.get("text"):
                (case / f"{strip.stem}_reply.txt").write_text(
                    f"rule: {instruction}\n\n{answer['text']}")
            if index is None:
                result["waves"].append({"wave": solved_waves + 1, "answer": None})
                result["outcome"] = "solver-failed"
                break

            if not await ui.goto(index, total):
                log(f"  could not page to {index}; submitting where we are")
            await page.wait_for_timeout(600)
            if not await ui.submit():
                # The last submit is expected to go away: Arkose grades the board
                # the moment the final guess lands, so the control can be gone
                # before the click. That is the grading screen, not a failure.
                await page.wait_for_timeout(1500)
                if await note_verdict(page, case, result, log):
                    break
                result["outcome"] = "submit-failed"
                break
            solved_waves += 1
            result["waves"].append({"wave": solved_waves, "answer": index,
                                    "strip": strip.name,
                                    "instruction": instruction[:240]})
            log(f"  wave {solved_waves} submitted with candidate {index}")

            # Let the next wave render before the loop looks again.
            await page.wait_for_timeout(4000)

        if result["outcome"] == "unknown":
            result["outcome"] = f"stopped after {solved_waves} waves"

        result["waves_done"] = solved_waves
        result["rtig_ok"] = sum(1 for s in bank.statuses if s == 200)
        try:
            await page.screenshot(path=str(case / "final.png"))
        except Exception:
            pass
        (case / "run.json").write_text(json.dumps(result, indent=1))
        return result
    except Exception as exc:
        result["outcome"] = f"error {type(exc).__name__}: {str(exc)[:90]}"
        return result
    finally:
        try:
            stop_shots.set()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies2.txt")
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--limit", type=float, default=480.0)
    ap.add_argument("--provider", default="lithosai")
    ap.add_argument("--presentation", default="single",
                    choices=("single", "split"))
    ap.add_argument("--solver", default="auto",
                    choices=("auto", "vision", "onnx"))
    ap.add_argument("--shape", default="auto",
                    help=("client shape for /v2/signup: auto picks a shape only "
                          "when recorded evidence beats the default; off sends "
                          "the request untouched. Known: "
                          + ", ".join(client_shape.SHAPES)))
    args = ap.parse_args()

    shape = resolve_shape(args.shape)
    log(f"client shape: {shape or 'off'}")

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            row = await run(playwright, raw, args.limit, args.provider,
                            args.presentation, args.solver, shape)
            log(f"\n--- {row['outcome']} ---")
            log(f"  rtig ok={row.get('rtig_ok', 0)}  waves done={row.get('waves_done', 0)}")
            for wave in row["waves"]:
                log(f"    wave {wave['wave']}: {wave.get('answer')}")
            if row["outcome"] == "created":
                log(f"  CREATED {row.get('username')}")
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
