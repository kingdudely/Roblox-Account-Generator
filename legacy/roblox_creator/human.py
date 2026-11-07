"""Pointer and navigation behaviour.

PerimeterX (and Arkose) score the *shape* of input, not just its presence.
Playwright's ``click()`` teleports the cursor and dispatches a click with no
preceding movement; ``fill()`` sets a value with a single input event. Both are
cheap to detect.

Everything here moves the pointer along a curved path with jitter, dwells
before pressing, and types at an uneven cadence with occasional hesitation.
It is not a biometric forgery -- it just stops the input from being obviously
synthetic.
"""

from __future__ import annotations

import random

from playwright.sync_api import Page


class Pointer:
    def __init__(self, page: Page, rng: random.Random):
        self.page = page
        self.rng = rng
        vp = page.viewport_size or {"width": 1440, "height": 900}
        self.w, self.h = vp["width"], vp["height"]
        self.x = rng.uniform(0.2 * self.w, 0.8 * self.w)
        self.y = rng.uniform(0.2 * self.h, 0.65 * self.h)
        page.mouse.move(self.x, self.y)

    # -- movement -------------------------------------------------------------

    def _path(self, tx: float, ty: float):
        """Smoothstep along a quadratic curve with a randomised control point."""
        rng = self.rng
        cx = (self.x + tx) / 2 + rng.uniform(-140, 140)
        cy = (self.y + ty) / 2 + rng.uniform(-90, 90)
        steps = rng.randint(14, 26)
        x0, y0 = self.x, self.y
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            x = (1 - e) ** 2 * x0 + 2 * (1 - e) * e * cx + e * e * tx
            y = (1 - e) ** 2 * y0 + 2 * (1 - e) * e * cy + e * e * ty
            yield x + rng.gauss(0, 0.7), y + rng.gauss(0, 0.7)

    def move_to(self, tx: float, ty: float) -> None:
        for x, y in self._path(tx, ty):
            self.page.mouse.move(x, y)
            self.page.wait_for_timeout(self.rng.randint(6, 22))
            self.x, self.y = x, y

    def wander(self, n: int | None = None) -> None:
        for _ in range(n or self.rng.randint(2, 4)):
            self.move_to(self.rng.uniform(0.1 * self.w, 0.9 * self.w),
                         self.rng.uniform(0.1 * self.h, 0.8 * self.h))
            self.page.wait_for_timeout(self.rng.randint(140, 480))

    # -- actions --------------------------------------------------------------

    def click(self, selector: str, timeout: float = 15_000) -> None:
        el = self.page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        box = el.bounding_box()
        if not box:
            raise RuntimeError(f"no bounding box for {selector}")
        tx = box["x"] + box["width"] * self.rng.uniform(0.35, 0.65)
        ty = box["y"] + box["height"] * self.rng.uniform(0.35, 0.65)
        self.move_to(tx, ty)
        self.page.wait_for_timeout(self.rng.randint(60, 190))
        self.page.mouse.down()
        self.page.wait_for_timeout(self.rng.randint(55, 125))
        self.page.mouse.up()


def type_text(page: Page, text: str, rng: random.Random) -> None:
    """Uneven cadence with word-boundary pauses and occasional hesitation."""
    for ch in text:
        page.keyboard.type(ch)
        delay = rng.randint(45, 135)
        if ch in " ._!":
            delay += rng.randint(25, 110)
        if rng.random() < 0.05:
            delay += rng.randint(160, 420)
        page.wait_for_timeout(delay)


def warm_up(page: Page, url: str, rng: random.Random) -> Pointer:
    """Land on the homepage first. A zero-history session that appears directly
    on a signup form is itself a signal -- PX timestamps the navigation stack."""
    page.goto("https://www.roblox.com/", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(rng.randint(1_600, 3_200))

    pointer = Pointer(page, rng)
    pointer.wander()

    try:
        page.mouse.wheel(0, rng.randint(220, 680))
    except Exception:
        pass
    page.wait_for_timeout(rng.randint(700, 1_800))

    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(rng.randint(900, 2_200))
    return pointer
