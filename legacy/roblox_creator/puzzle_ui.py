"""Drive the real Arkose dice game in a browser, one candidate at a time.

The browser is handed a completely different puzzle UI from the browserless path.
Instead of one 2000x400 strip of ten tiles that has to be reassembled from
`_challenge_imgs`, the v2 game core renders:

    <img>  key panel      symbol + required count, shown at the left
    <img>  one candidate  a single 200x200 tile, paged with arrows
    <div class="pip-container" aria-label="1 of 10">
    <a aria-label="Navigate to previous image"> / "... next image"
    <button>Submit

Two consequences, both good:

1. **The image allowance stops mattering.** The page loads its own images as
   `blob:` URLs; reading those bytes back costs no `/rtig/image` request, so the
   "one completable challenge in six" ceiling does not apply in the browser.
2. **The resolution ceiling stops mattering.** Each candidate is sent to the
   vision model on its own at native 200x200, rather than ten panels crushed into
   one downsampled montage.

So the answer to "how many times does the symbol appear" is asked about exactly
one tile at a time, which is the question the model is actually good at.

The tiles are painted as CSS `background-image: url(blob:...)` on `<img>`
elements rather than `src`, which is why a naive `querySelectorAll('img')` scan
finds nothing to read.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time

__all__ = [
    "GameUI",
    "GameError",
]

# Every blob-backed image on the game screen, decoded to bytes.
GRAB_JS = r"""
async () => {
  const urls = new Set();
  document.querySelectorAll('*').forEach((el) => {
    const bg = getComputedStyle(el).backgroundImage || '';
    if (bg && bg.includes('blob:')) {
      const m = bg.match(/url\(["']?(blob:[^"')]+)["']?\)/);
      if (m) urls.add(m[1]);
    }
    if (el.tagName === 'IMG' && el.src && el.src.startsWith('blob:')) {
      urls.add(el.src);
    }
  });

  const out = [];
  for (const u of urls) {
    try {
      const r = await fetch(u);
      const b = await r.blob();
      const ab = await b.arrayBuffer();
      const y = new Uint8Array(ab);
      let s = '';
      for (let i = 0; i < y.length; i += 0x8000) {
        s += String.fromCharCode.apply(null, y.subarray(i, i + 0x8000));
      }
      // dimensions, so the key can be told apart from a candidate by shape
      const bmp = await createImageBitmap(b);
      out.push({w: bmp.width, h: bmp.height, type: b.type, b64: btoa(s)});
      bmp.close && bmp.close();
    } catch (e) { /* a blob that vanished mid-read is not fatal */ }
  }
  return out;
}
"""

PIP_JS = r"""
() => {
  const p = document.querySelector('.pip-container');
  if (!p) return '';
  const raw = (p.getAttribute('aria-label') || p.innerText || '').trim();
  return raw;
}
"""

INSTRUCTION_JS = r"""
() => {
  // The rule is rendered as the board's heading. Walk the plausible containers in
  // order and take the first one holding a sentence, because the class names move
  // between game builds and a bare `querySelector('h2')` returned nothing on the
  // tool-silhouette board.
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const probes = [
    'h1', 'h2', 'h3', 'h4',
    '[class*="instruction"]', '[class*="Instruction"]',
    '[class*="prompt"]', '[class*="Prompt"]',
    '[class*="title"]', '[class*="Title"]',
    '[aria-label*="nstruction"]', '[data-testid*="nstruction"]',
  ];
  for (const sel of probes) {
    const el = document.querySelector(sel);
    const t = clean(el && el.innerText);
    // A heading is the rule when it reads like one: more than two words, and not
    // a bare counter like "2 / 10" or a label like "Submit".
    if (t.length > 8 && /[a-z]/i.test(t) && !/^\d+\s*\/\s*\d+$/.test(t)) return t.slice(0, 240);
  }
  // Last resort: the frame's own text, trimmed of the control labels.
  const body = clean(document.body && document.body.innerText);
  return body ? body.slice(0, 240) : '';
}
"""

FRAME_TEXT_JS = r"""
() => {
  const t = (document.body && document.body.innerText) || '';
  return t.replace(/\s+/g, ' ').trim().slice(0, 600);
}
"""

SUBMIT_LABELS = ("submit",)
NEXT_LABELS = ("next",)


class GameError(RuntimeError):
    pass


class GameUI:
    """One live dice round, driven by paging and clicking."""

    def __init__(self, page, frame=None, log=None):
        self.page = page
        self._frame = frame
        self.log = log or (lambda m: None)

    async def frame(self):
        """The live game frame.

        Pressing Start Puzzle navigates the frame, so a handle captured before the
        press refers to a detached document and every later `evaluate` silently
        reads nothing. Re-resolving per call is cheap and avoids that entirely.
        """
        for f in self.page.frames:
            url = f.url or ""
            if "game-core" in url and "enforcement" not in url:
                try:
                    n = await f.evaluate(
                        "() => document.body ? document.body.innerText.length : 0")
                except Exception:
                    continue
                if n:
                    self._frame = f
                    return f
        if self._frame is not None:
            return self._frame
        raise GameError("game frame went away")

    # ------------------------------------------------------------------ setup

    @classmethod
    async def attach(cls, page, timeout: float = 90.0, log=None,
                     shots: "Path | None" = None) -> "GameUI":
        """Clear the challenge ladder and return the live dice round.

        Order matters and it used to be wrong. The previous version waited for a
        game-core frame *first*, but a "press and hold" challenge is served from
        the enforcement frame and never mounts a game frame at all — so the wait
        consumed the whole timeout and raised "no game frame" while the screen was
        still asking to be pressed. The ladder is now driven first and the game
        frame is only awaited once the ladder has actually reached the dice.
        """
        from roblox_creator.arkose_ui import find_hold_frame, press_and_hold

        ui = cls(page, None, log=log)
        deadline = time.time() + timeout
        holds = 0
        saw = ""

        while time.time() < deadline:
            if "/home" in page.url or "/discover" in page.url:
                return ui

            # 1. hold callback — checked before anything waits on a frame, and
            # with no URL filter: the widget is served from an about:blank frame
            hframe, hbox = await find_hold_frame(page)
            if hframe is not None and hbox is not None and holds < 5:
                holds += 1
                ui.log(f"  ladder: hold #{holds}")
                if shots is not None:
                    try:
                        await page.screenshot(path=str(shots / f"hold_{holds}.png"))
                    except Exception:
                        pass
                ok = await press_and_hold(page, log=ui.log)
                ui.log(f"  ladder: hold {'pressed' if ok else 'NOT pressed'}")
                await page.wait_for_timeout(1200)
                continue

            # 2. Start Puzzle intro
            clicked = False
            for f in page.frames:
                if "game-core" not in (f.url or ""):
                    continue
                try:
                    for btn in await f.query_selector_all("button"):
                        label = ((await btn.inner_text()) or "").strip().lower()
                        if "start" in label:
                            await btn.click()
                            ui.log("  ladder: clicked Start Puzzle")
                            clicked = True
                            break
                except Exception:
                    pass
                if clicked:
                    break
            if clicked:
                await page.wait_for_timeout(800)

            # 3. already on the dice?
            pip = await ui._pip()
            if re.search(r"\d+\s*(of|/)\s*\d+", pip or "", re.I):
                ui.log(f"  ladder: round ready ({pip.strip()[:20]})")
                return ui

            # 4. report what is on screen, but only when it changes
            note = ""
            for f in page.frames:
                if "arkoselabs" not in (f.url or ""):
                    continue
                try:
                    t = (await f.inner_text("body")).strip()
                except Exception:
                    continue
                if t:
                    note = t[:60].replace("\n", " ")
                    break
            if note and note != saw:
                saw = note
                ui.log(f"  ladder: {note}")
            await page.wait_for_timeout(450)

        raise GameError("ladder never reached the dice")

    # ------------------------------------------------------------------ state

    async def _pip(self) -> str:
        try:
            return await (await self.frame()).evaluate(PIP_JS)
        except Exception:
            return ""

    async def position(self) -> tuple[int, int]:
        """(current, total) from the pip label, e.g. "3 of 10" -> (3, 10)."""
        pip = await self._pip()
        m = re.search(r"(\d+)\s*of\s*(\d+)", pip or "", re.I)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d+)\s*/\s*(\d+)", pip or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        return 1, 10

    async def instruction(self) -> str:
        try:
            return await (await self.frame()).evaluate(INSTRUCTION_JS)
        except Exception:
            return ""

    async def frame_text(self) -> str:
        """Everything the game frame renders, for the run log.

        `instruction()` guesses which line is the rule. This returns the whole
        frame so a run can be audited after the fact and the guess corrected — the
        wording of these boards has changed between builds and guessing blind is
        how a prompt ends up describing the wrong task.
        """
        try:
            return await (await self.frame()).evaluate(FRAME_TEXT_JS)
        except Exception:
            return ""

    # --------------------------------------------------------------- images

    async def images(self) -> list[dict]:
        """The key panel and the current candidate, captured as rendered pixels.

        The game paints its tiles as CSS `background-image: url(blob:...)` and
        revokes each blob once it has loaded, so `fetch(blob)` fails and a canvas
        draw reads an empty `<img>` whose real pixels live in the CSS layer. An
        element screenshot is the one capture that sees what the user sees.

        The context is created at `device_scale_factor=2`, so a 200 px tile comes
        back at 402 px — not new information, but it stops the vision model from
        shrinking an already small crop.
        """
        live = await self.frame()
        try:
            elements = await live.query_selector_all("img")
        except Exception as exc:
            raise GameError(f"query: {type(exc).__name__}: {str(exc)[:80]}")
        out = []
        for el in elements:
            try:
                box = await el.bounding_box()
            except Exception:
                continue
            if not box or box["width"] < 40 or box["height"] < 40:
                continue
            try:
                png = await el.screenshot()
            except Exception:
                continue
            out.append({"w": int(box["width"]), "h": int(box["height"]),
                        "x": int(box["x"]), "y": int(box["y"]), "png": png})
        # left to right: the key card is the leftmost element, candidates sit to
        # its right. Sorting by squareness is wrong — the key is often a square
        # card too, and a mid-render fragment can be any shape at all.
        out.sort(key=lambda d: (d["x"], d["y"]))
        return out

    async def key_and_candidate(self) -> tuple[dict | None, dict | None]:
        """The key card and the current candidate, both after they settle.

        The key card animates in, so a single sample can catch a partial paint and
        hand the counter a fragment of the card. Two identical consecutive
        captures is the cheapest reliable proof that the panel finished drawing.
        """
        prev = None
        imgs: list[dict] = []
        for _ in range(12):
            imgs = await self.images()
            sig = tuple((d["w"], d["h"], len(d["png"])) for d in imgs)
            if imgs and sig == prev and len(imgs) >= 2:
                break
            prev = sig
            await self.page.wait_for_timeout(450)
        if not imgs:
            return None, None
        if len(imgs) == 1:
            return None, imgs[0]
        return imgs[0], imgs[-1]

    async def candidate(self) -> dict | None:
        """The rightmost tile currently shown, without re-reading the key."""
        imgs = await self.images()
        return imgs[-1] if imgs else None

    # ------------------------------------------------------------ navigation

    async def _click_label(self, needle: str) -> bool:
        live = await self.frame()
        for sel in ("a", "button", "[role=button]"):
            try:
                for el in await live.query_selector_all(sel):
                    label = ((await el.get_attribute("aria-label"))
                             or (await el.inner_text()) or "").strip().lower()
                    if needle in label:
                        await el.click()
                        return True
            except Exception:
                continue
        return False

    async def next(self) -> bool:
        return await self._click_label("next")

    async def prev(self) -> bool:
        return await self._click_label("previous") or await self._click_label("prev")

    async def goto(self, index: int, total: int) -> bool:
        """Page to a 1-based index, verifying with the pip each step."""
        for _ in range(total * 2 + 4):
            cur, tot = await self.position()
            if cur == index:
                return True
            ok = await self.next() if index > cur else await self.prev()
            if not ok:
                return False
            await self.page.wait_for_timeout(320)
        cur, _ = await self.position()
        return cur == index

    async def submit(self) -> bool:
        return await self._click_label("submit")

    async def solved(self, timeout: float = 30.0) -> bool:
        """True once the host page leaves CreateAccount (login or a fresh round)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "/home" in self.page.url or "/discover" in self.page.url:
                return True
            await self.page.wait_for_timeout(400)
        return "/home" in self.page.url
