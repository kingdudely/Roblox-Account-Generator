#!/usr/bin/env python3
"""Answer the Arkose callback challenges a real browser actually receives.

`tools/frame_probe.py` showed the browser is handed a *ladder*, not one puzzle:

    passive pass        settings -> gt2 -> fc/a/ -> straight to /home
    proof of work       "Verifying browser..." — solves itself, just wait
    hold callback       "Press and hold ..." — one mouse-down, ~3s
    dice puzzle         MatchDiceIconCount — the thing the browserless path
                        always got, and the only rung that needs answers

Only the last rung needs a solver. This module handles the first three, so the
creator stops throwing away accounts that were one gesture from existing.

The hold is not a click: Arkose measures press duration and release, so the press
is held with a little jitter and released after the ring completes. A failed hold
usually re-presents the same challenge, hence `hold_tries`.

    kind = await handle_challenge(page, log=print)
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import random
import time
from pathlib import Path

from PIL import Image

__all__ = ["probe_state", "find_hold_frame", "press_and_hold", "start_puzzle",
           "handle_challenge", "ChallengeError"]

HOLD_TEXT = ("press and hold", "hold the button", "press & hold")
POW_TEXT = ("verifying browser", "checking your browser")
PUZZLE_URL_MARKERS = ("ec-game-core/game-core", "match-game")
PUZZLE_TEXT = ("select", "how many", "match", "dice")

# The modal a first challenge opens with: a title, a sentence, and one button.
# Nothing else happens until it is clicked — measured on 2026-11-01, a run sat on
# this screen for 150 s waiting for a hold widget that had not been asked for.
# Matched by prefix, not substring: "restart" contains "start" and clicking it
# throws away whatever progress the challenge had.
START_PREFIXES = ("start puzzle", "start challenge", "start game", "begin",
                  "continue", "verify you are human", "start")


class ChallengeError(RuntimeError):
    """The challenge could not be answered by this module."""


async def _arkose_frames(page):
    out = []
    for f in page.frames:
        url = f.url or ""
        if "arkoselabs" in url or "funcaptcha" in url:
            out.append((f, url))
    return out


async def _frame_text(frame) -> str:
    try:
        return (await frame.inner_text("body")) or ""
    except Exception:
        return ""


async def probe_state(page) -> dict:
    """Classify what the page is currently showing.

    The hold is checked first and by content, not by URL: it lives in an
    `about:blank` frame, and it is a modal, so anything else on screen is blocked
    behind it anyway.
    """
    state = {"url": page.url, "kind": "unknown", "frames": []}
    if "/home" in page.url or "/discover" in page.url:
        state["kind"] = "created"
        return state

    hf, hb = await find_hold_frame(page)
    if hf is not None and hb is not None:
        state["kind"] = "hold"
        state["frames"].append({
            "url": (hf.url or "about:blank")[:110],
            "text": f"widget {hb['width']:.0f}x{hb['height']:.0f}"})
        return state

    for f in page.frames:
        url = f.url or ""
        text = (await _frame_text(f)).lower()
        if any(m in url for m in PUZZLE_URL_MARKERS):
            state["kind"] = "puzzle"
            state["frames"].append({"url": url[:110], "text": text[:90]})
            return state
        if any(t in text for t in POW_TEXT):
            state["kind"] = "pow"
            state["frames"].append({"url": url[:110], "text": text[:90]})
            return state

    # A puzzle can also mount without a marker in its own URL when the game core
    # is nested one frame deeper, so fall back to the text it renders.
    for f in page.frames:
        url = f.url or ""
        text = (await _frame_text(f)).lower()
        if any(t in text for t in PUZZLE_TEXT) and "dice" in text:
            state["kind"] = "puzzle"
            state["frames"].append({"url": url[:110], "text": text[:90]})
            return state
    return state


async def start_puzzle(page, log=None) -> bool:
    """Click the challenge's own "Start Puzzle" button, if it is up.

    Some exits open the Arkose modal on a plain consent screen — title, one
    sentence, one green button — and no hold or puzzle renders until that button
    is clicked. Waiting for a hold widget on that screen waits forever, which is
    what a 150 s run did before this existed.
    """
    # Child frames first: when the button belongs to Arkose it lives in the
    # enforcement iframe, and the host page's copy of the same words is the
    # modal wrapper around it.
    frames = [f for f in page.frames if f != page.main_frame] + [page.main_frame]
    for frame in frames:
        try:
            handles = await frame.query_selector_all("button, [role=button], a[role=button]")
        except Exception:
            continue
        for handle in handles:
            try:
                label = ((await handle.inner_text()) or "").strip().lower()
            except Exception:
                continue
            if not any(label.startswith(t) for t in START_PREFIXES):
                continue
            if len(label) > 40:
                continue
            try:
                await handle.click(timeout=4000)
            except Exception:
                continue
            if log:
                log(f"  clicked {label!r}")
            await page.wait_for_timeout(1200)
            return True
    return False


async def find_hold_frame(page):
    """The frame that owns a hold widget: ``(frame, box)`` or ``(None, None)``.

    The hold is served from an ``about:blank`` frame. That is measured, not
    inferred — `tools/hold_probe.py` dumped it: a 310x42 pill containing one div
    with ``aria-label="Press and hold"``. Its URL contains neither "arkoselabs"
    nor "funcaptcha", so a URL filter skips it every single time, which is why the
    hold was never pressed no matter how long the ladder waited.

    The top document is never a candidate. ``innerText`` on the host page includes
    the rendered text of its child iframes, so Roblox's signup page *also*
    "contains" the words Press and hold; pressing its centre clicks the modal
    backdrop and dismisses the challenge instead of answering it.
    """
    # The hold widget lives in an `about:blank` child frame, but its visible text
    # is carried in an `aria-label`, not in the body's text content — so the
    # earlier `inner_text` probe matched the *main* frame (same-origin bleed of
    # the child's rendered text) and then skipped it, leaving nothing. Match on
    # the aria-label instead, which is what the widget actually exposes.
    # Search every frame including the main document: some Arkose variants mount
    # the widget in the host page rather than a child frame.
    for f in page.frames:
        try:
            els = await f.query_selector_all('[aria-label*="hold" i]')
        except Exception:
            els = []
        if not els:
            continue
        for el in els:
            try:
                b = await el.bounding_box()
            except Exception:
                b = None
            if b and b["width"] >= 20 and b["height"] >= 20:
                return f, b
        # Fall back to the frame element's box if no element box was usable.
        try:
            box = await (await f.frame_element()).bounding_box()
        except Exception:
            box = None
        if box:
            return f, box

    # Last resort: a child frame whose body text contains the hold phrase.
    # Keep this for the cases where the widget exposes text rather than an
    # aria-label, but never let the main frame win — its innerText bleeds the
    # child's text and would select the backdrop instead of the widget.
    best = None
    for f in page.frames:
        if f == page.main_frame:
            continue
        try:
            text = ((await f.inner_text("body")) or "").lower()
        except Exception:
            continue
        if not any(t in text for t in HOLD_TEXT):
            continue
        weight = len(text)
        if best is None or weight < best[0]:
            best = (weight, f)
    if best is None:
        return None, None

    frame = best[1]
    try:
        box = await (await frame.frame_element()).bounding_box()
    except Exception:
        box = None
    return frame, box


async def spinner_showing(frame) -> bool:
    """True once the hold pill has been replaced by Arkose's verifying spinner.

    That swap is the acceptance signal, and it is measurable: the pill's
    `[role=progressbar]` sits at 0x0 with its wrapper classed `hidden` while the
    button is offered, and becomes a visible 16x16 spinner the moment the gesture
    is accepted (`debug/hold_watch/20261101-180238/`).
    """
    try:
        return bool(await frame.evaluate("""() => {
          for (const el of document.querySelectorAll('[role=progressbar], [class*=progress-circle]')) {
            const r = el.getBoundingClientRect();
            if (r.width > 5 && r.height > 5) return true;
          }
          return false;
        }"""))
    except Exception:
        return False


async def fill_fraction(page, box, cdp=None) -> float:
    """How much of the pill has filled, read off the pixels, 0.0-1.0.

    The hold is a progress gesture and the progress is painted: sampled from
    `debug/hold_watch/20261101-180238/`, the pill fills left to right in a brighter
    blue — 8/31 columns at 1.2 s, 11/31 at 2.4 s, the whole row by 4.0 s. Reading
    it means the release can be triggered by *completion* instead of a stopwatch,
    which is what a hand does and what a fixed sleep gets wrong in both directions.

    Returns -1.0 when the clip cannot be taken, so the caller can fall back.
    """
    # CDP clips in document coordinates while `bounding_box()` and the mouse are
    # viewport-relative, so a scrolled page lands the clip above the pill — which
    # is exactly what happened: a 310x42 crop of the gender row instead of the
    # button, and a permanent 0% fill.
    try:
        scroll = await page.evaluate("() => ({ x: window.scrollX, y: window.scrollY })")
    except Exception:
        scroll = {"x": 0, "y": 0}
    clip = {"x": float(box["x"]) + scroll["x"], "y": float(box["y"]) + scroll["y"],
            "width": float(box["width"]), "height": float(box["height"]),
            "scale": 1}
    try:
        if cdp is None:
            cdp = await page.context.new_cdp_session(page)
        raw = await asyncio.wait_for(
            cdp.send("Page.captureScreenshot",
                     {"format": "png", "clip": clip, "captureBeyondViewport": False}),
            timeout=5)
        shot = base64.b64decode(raw["data"])
        if os.environ.get("ARKOSE_FILL_DEBUG"):
            Path(os.environ["ARKOSE_FILL_DEBUG"]).parent.mkdir(parents=True, exist_ok=True)
            Path(os.environ["ARKOSE_FILL_DEBUG"]).write_bytes(shot)
    except Exception:
        return -1.0
    try:
        img = Image.open(io.BytesIO(shot)).convert("RGB")
    except Exception:
        return -1.0
    width, height = img.size
    if width < 8 or height < 4:
        return -1.0
    row = img.crop((0, height // 2, width, height // 2 + 1)).resize((width, 1))
    filled = 0
    for x in range(width):
        r, g, b = row.getpixel((x, 0))
        if b > 170 and r < 110:
            filled += 1
    return filled / width


async def press_and_hold(page, log=None, seconds: float = 10.0,
                         tries: int = 3, settle: float = 6.0) -> bool:
    """Press the pill, release, and confirm the gesture was accepted.

    Measured with `tools/hold_watch.py` on 2026-11-01 over a real exit: the pill
    does not leave while the button is down. At 1.2 s, 2.4 s and 4.0 s it still
    reads `Press and hold`; the instant the button comes up it is replaced by a
    spinner and then the widget goes. So the signal comes *after* the release, and
    a hold that never releases never completes — which is exactly what the first
    version did for 25 s a try.

    Acceptance is the spinner (`spinner_showing`), not the widget vanishing: the
    frame lingers for a second or two after the swap, so polling for the frame to
    disappear reads a successful hold as a failure. Each retry holds longer, since
    too short a hold is the other way this fails.
    """
    hold_for = seconds
    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception:
        cdp = None
    for attempt in range(1, tries + 1):
        frame, box = await find_hold_frame(page)
        if frame is None or box is None:
            if log:
                log("  hold: nothing to press")
            # Not an acceptance. "Nothing to press" is the dead rung — the widget is
            # still on screen but its button is gone — so it must not be counted as
            # a hold that took, or the ladder climbs to its retrigger on a phantom.
            return False

        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        if log:
            log(f"  hold #{attempt}: press ({x:.0f},{y:.0f}) "
                f"{box['width']:.0f}x{box['height']:.0f} for {hold_for:.1f}s")
        try:
            el = await frame.query_selector('[aria-label*="hold" i]')
            if el:
                await el.focus()
                await page.wait_for_timeout(100)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(150)
            await page.mouse.down()

            gone = False
            accepted = False
            best_fill = 0.0
            deadline = time.time() + hold_for
            while time.time() < deadline:
                await page.wait_for_timeout(120)
                # a small drift, because a perfectly still pointer is itself a tell
                await page.mouse.move(x + 1.5, y + 0.5)
                current, _ = await find_hold_frame(page)
                if current is None:
                    gone = True
                    break
                # Release when the pill is full. Releasing at half is what makes
                # Arkose answer "Please try again" and start the fill over.
                fraction = await fill_fraction(page, box, cdp)
                if fraction > best_fill:
                    best_fill = fraction
                if fraction >= 0.94:
                    accepted = True
                    break
                # If the pill turns into the verifying spinner while the button is
                # still down, the gesture is complete — release now.
                if await spinner_showing(frame):
                    accepted = True
                    break
            await page.mouse.up()
            if log:
                log(f"  hold: released at {best_fill:.0%} fill (attempt {attempt})")
        except Exception as exc:
            if log:
                log(f"  hold: {type(exc).__name__}: {str(exc)[:60]}")
            try:
                await page.mouse.up()
            except Exception:
                pass
            return False

        if gone:
            if log:
                log(f"  hold: widget gone while held (attempt {attempt})")
            return True

        deadline = time.time() + settle
        while time.time() < deadline:
            await page.wait_for_timeout(250)
            if await spinner_showing(frame):
                if log:
                    log(f"  hold: accepted on attempt {attempt}")
                # Wait for the widget to actually leave, so the caller does not
                # press a pill that is mid-verification.
                leave_deadline = time.time() + 12
                while time.time() < leave_deadline:
                    await page.wait_for_timeout(250)
                    if await find_hold_frame(page) == (None, None):
                        break
                return True
            current, _ = await find_hold_frame(page)
            if current is None:
                if log:
                    log(f"  hold: widget gone after release (attempt {attempt})")
                return True

        if log:
            log("  hold: not accepted, pressing again longer")
        # A pill that did not fill at all is not a short hold, it is a dead pill:
        # measured 2026-11-01, accepted holds read 75-99% at release while dead ones
        # read 0-5% even after 14 s. Holding longer cannot fix construction; it only
        # burns the wave. Report failure and let the caller retrigger.
        if best_fill < 0.25:
            if log:
                log(f"  hold: pill never filled ({best_fill:.0%}) — dead rung")
            return False
        hold_for += 2.0
        await page.wait_for_timeout(400)

    if log:
        log("  hold: gave up after repeated presses")
    return False


async def handle_challenge(page, timeout: float = 75.0, log=None,
                           max_holds: int = 3) -> dict:
    """Drive the challenge ladder until the account lands or a puzzle appears."""
    t0 = time.time()
    holds = 0
    last_kind = ""

    while time.time() - t0 < timeout:
        state = await probe_state(page)
        kind = state["kind"]

        if kind == "created":
            return {"outcome": "created", "elapsed": round(time.time() - t0, 1)}

        if kind == "puzzle":
            return {"outcome": "puzzle", "elapsed": round(time.time() - t0, 1),
                    "frames": state["frames"]}

        if kind == "hold":
            if holds >= max_holds:
                return {"outcome": "hold_failed", "elapsed": round(time.time() - t0, 1)}
            holds += 1
            if log:
                log(f"  challenge: hold #{holds}")
            ok = await press_and_hold(page, log=log)
            await page.wait_for_timeout(random.randint(1200, 2200))
            if log:
                st = await probe_state(page)
                log(f"  post-hold state: {st['kind']} url={page.url[:60]}")
            continue

        if kind == "pow" and last_kind != "pow" and log:
            log("  challenge: proof of work")
        last_kind = kind
        await page.wait_for_timeout(600)

    return {"outcome": "timeout", "elapsed": round(time.time() - t0, 1)}
