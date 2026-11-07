"""Signup orchestration: fill, submit, handle the captcha gate, harvest the cookie."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from playwright.sync_api import BrowserContext, Page

from . import captcha as cap
from . import signup_form as form
from . import vision
from .config import (
    ARKOSE_PUBLIC_KEY_SIGNUP,
    ARKOSE_SERVICE_URL,
    AUTHENTICATED_USER,
    COOKIE_NAME,
    SEL_SUBMIT,
    SEL_USERNAME,
    SIGNUP_URL,
    Settings,
)
from .human import Pointer, type_text, warm_up
from .identity import Identity, make_identity
from .store import AccountRecord, AccountStore


class CreateError(RuntimeError):
    def __init__(self, reason: str, detail: str = "", recoverable: bool = False):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.recoverable = recoverable


@dataclass
class Outcome:
    kind: str            # success | captcha | error | timeout
    detail: str = ""
    status: int = 0


def _press(pointer: Pointer, selector: str, text: str, rng: random.Random) -> None:
    pointer.click(selector)
    type_text(pointer.page, text, rng)


class Creator:
    def __init__(self, settings: Settings, store: AccountStore, proxy_pool=None):
        self.settings = settings
        self.store = store
        self.proxy_pool = proxy_pool
        self.rng = random.Random()
        self._events: list[tuple[str, int]] = []
        self.solver = None
        if settings.effective_captcha_mode == "api":
            self.solver = cap.build_solver(settings.captcha_provider, settings.captcha_key)

    # -- public ---------------------------------------------------------------

    def create_one(self, session, index: int) -> AccountRecord:
        identity = make_identity(self.rng)
        while self.store.is_taken(identity.username):
            identity = make_identity(self.rng, attempt=1)

        state = self.proxy_pool.acquire() if self.proxy_pool else None
        proxy = state.url if state else None
        timezone = None
        if proxy:
            from . import egress as egress_mod

            timezone = egress_mod.timezone_for(proxy)

        profile = None
        if not self.settings.reuse_context:
            profile = self.settings.profile_root / f"p{index:04d}"

        record = AccountRecord(
            username=identity.username,
            password=identity.password,
            birthday=identity.birthday_display,
            gender=identity.gender,
            proxy=proxy,
        )

        ctx = None
        try:
            ctx = session.new_context(profile_dir=profile, proxy=proxy, timezone=timezone)
            page = session.new_page(ctx)
            self._run(page, ctx, identity, record, proxy)
            record.captcha = getattr(record, "captcha", "none")
        except CreateError as exc:
            record.status = exc.reason
            record.note = exc.detail[:400]
        except Exception as exc:  # noqa: BLE001 - one bad account must not kill the run
            record.status = "failed"
            record.note = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
            if profile is not None and self.settings.reuse_context is False:
                session.wipe_profile(profile)
            if state is not None:
                self.proxy_pool.report(state.url, self._proxy_outcome(record))

        self.store.append(record)
        return record

    @staticmethod
    def _proxy_outcome(record: AccountRecord) -> str:
        """A username collision says nothing about the proxy — do not punish it."""
        if record.status in ("created", "taken"):
            return "ok"
        if record.status == "captcha_failed":
            return "captcha"
        return "blocked"

    # -- internals ------------------------------------------------------------

    def _run(self, page: Page, ctx: BrowserContext, identity: Identity,
             record: AccountRecord, proxy: str | None) -> None:
        self._events: list[tuple[str, int]] = []
        page.on(
            "response",
            lambda r: self._events.append((r.url, r.status))
            if "/v2/signup" in r.url or "funcaptcha/" in r.url
            else None,
        )

        self._open(page)
        self._fill(page, identity)

        if self.settings.dry_run:
            record.status = "dry_run"
            record.note = "form filled, submit skipped"
            self._dump(page, f"dry_{record.username}")
            page.wait_for_timeout(1500)
            return

        self._submit(page)

        outcome = self._await_outcome(page, ctx, timeout=20.0)
        if outcome.kind == "captcha":
            record.captcha = self._solve(page, ctx, proxy)
            outcome = self._await_outcome(page, ctx, timeout=self.settings.solve_timeout)
            if outcome.kind == "captcha":
                raise CreateError("captcha_failed", "challenge still visible after solve")

        if outcome.kind != "success":
            raise CreateError(self._reason_for(outcome), outcome.detail, recoverable=outcome.kind == "timeout")

        cookie = self._roblosecurity(ctx)
        if not cookie:
            raise CreateError("failed", "signup reported success but no .ROBLOSECURITY cookie")
        record.cookie = cookie

        # Arkose's own verdict: silent pass vs. a challenge we actually solved
        state = cap.challenge_state(page)
        if state:
            if state.get("suppressed") and record.captcha in ("none", ""):
                record.captcha = "suppressed"
            elif not state.get("shown") and not state.get("suppressed") \
                    and state.get("error") and record.captcha in ("none", ""):
                record.captcha = f"error:{str(state['error'])[:60]}"

        if self.settings.verify:
            self._verify(record)
        record.status = "created"

    def _open(self, page: Page) -> None:
        self.pointer = warm_up(page, SIGNUP_URL, self.rng)
        try:
            page.wait_for_selector(SEL_USERNAME, state="visible", timeout=30_000)
        except Exception:
            page.goto("https://www.roblox.com/CreateAccount", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector(SEL_USERNAME, state="visible", timeout=30_000)
        page.wait_for_timeout(self.rng.randint(600, 1600))

    def _fill(self, page: Page, identity: Identity) -> None:
        """Fill the form, whichever of the two live layouts is being served.

        Classic keeps the password on the same screen as the birthday; radix
        hides it behind a Continue press and mounts it as #signup-v2-password.
        The date widgets differ too, so both live in `signup_form`.
        """
        rng = self.rng
        variant = form.detect_variant(page)
        if variant != form.CLASSIC:
            # the radix two-step form mounts its password step under a spinner
            # that never clears, so take the layout that can be finished
            if form.ensure_layout(page, form.CLASSIC, tries=4):
                variant = form.CLASSIC
        page.wait_for_timeout(rng.randint(150, 450))
        if not form.set_birthday(page, identity.birthday_month, identity.birthday_day,
                                 identity.birthday_year, rng, variant):
            raise CreateError("form", f"{variant}: birthday widgets refused selection")
        page.wait_for_timeout(rng.randint(300, 700))

        _press(self.pointer, SEL_USERNAME, identity.username, rng)
        page.wait_for_timeout(rng.randint(350, 900))

        form.choose_gender(page, identity.gender, rng)
        page.wait_for_timeout(rng.randint(250, 700))

        selector = form.password_selector(page)
        if selector is None:
            selector = form.reveal_password(page, rng)
        if selector is None:
            # the challenge mounted before the password step did — _solve takes it
            return
        _press(self.pointer, selector, identity.password, rng)
        page.wait_for_timeout(rng.randint(250, 700))

    def _submit(self, page: Page) -> None:
        # the button is disabled until the form validates; do not race it
        try:
            page.wait_for_selector(SEL_SUBMIT, state="visible", timeout=15_000)
            page.wait_for_function(
                "() => { const b = document.querySelector('#signup-button');"
                " return b && !b.disabled; }",
                timeout=15_000,
            )
        except Exception:
            pass
        # step two of the radix layout renders an unlabelled submit button, so the
        # selector cannot be a constant
        form.click_submit(page, self.rng)

    def _await_outcome(self, page: Page, ctx: BrowserContext, timeout: float) -> Outcome:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._roblosecurity(ctx):
                return Outcome("success", "cookie set")
            url = page.url
            if "/home" in url or "/discover" in url or "/games" in url:
                return Outcome("success", f"redirect {url}")

            for target, status in reversed(self._events):
                if "/v2/signup" not in target or status < 400:
                    continue
                if status == 403:
                    # Roblox answers "Challenge is required" BEFORE Arkose mounts.
                    # This is not terminal — it is the signal a challenge is coming.
                    if self._wait_for_challenge(page, timeout=25.0):
                        return Outcome("captcha", "403 challenge-required, widget rendered", 403)
                    return Outcome("error", "403, no challenge within 25s", 403)
                return Outcome("error", self._dom_error(page) or f"HTTP {status}", status)

            if self._captcha_present(page):
                return Outcome("captcha", "arkose challenge rendered")

            if page.is_closed():
                return Outcome("error", "page closed")
            page.wait_for_timeout(400)

        return Outcome("timeout", "no outcome within window")

    @staticmethod
    def _dom_error(page: Page) -> str:
        """Inline validation text — survives network wrapping and is what a human sees."""
        try:
            return page.evaluate(
                """() => {
                    const sel = ['.form-control-validation', '.text-danger', '[role="alert"]',
                                 '.error-message', '.input-field-validation'];
                    const out = [];
                    for (const s of sel) {
                        for (const el of document.querySelectorAll(s)) {
                            const t = (el.innerText || '').trim();
                            if (t) out.push(t);
                        }
                    }
                    return [...new Set(out)].join(' | ').slice(0, 300);
                }"""
            )
        except Exception:
            return ""

    def _wait_for_challenge(self, page: Page, timeout: float = 20.0) -> bool:
        """Poll for the widget — Arkose mounts ~1.2s after the 403, not before it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._captcha_present(page):
                return True
            if page.is_closed():
                return False
            page.wait_for_timeout(300)
        return False

    def _captcha_present(self, page: Page) -> bool:
        """True if a challenge is actually rendered, wherever it lives."""
        return cap.challenge_kind(page) is not None

    # -- captcha --------------------------------------------------------------

    def _solve(self, page: Page, ctx: BrowserContext, proxy: str | None) -> str:
        mode = self.settings.effective_captcha_mode
        if mode == "off":
            raise CreateError("captcha_failed", "captcha shown and mode=off")

        # attach the image tap before the challenge renders: /rtig/image responses
        # only fire once, and the game fetches them the moment its frame mounts
        if mode == "vision":
            tap = getattr(self, "_vision_tap", None)
            if tap is None:
                tap = vision.ImageTap()
                tap.attach(ctx)
                self._vision_tap = tap

        # stage 1: behavioural gate, if this attempt landed on PX / Arkose hold.
        # The gate resolves three ways: completed, "try again" (the button
        # re-arms, hold it again), or dead — still "Press and hold" with no
        # progress and no retry. The dead one is not winnable from inside, so the
        # modal is closed and the form submitted again, which draws a fresh
        # challenge and often lands the Arkose puzzle instead.
        if getattr(self.settings, "solve_press_hold", True):
            gate = cap.wait_for_gate(page, timeout=20.0)
            if gate == "start_puzzle":
                print("    arkose: start puzzle clicked")
                page.wait_for_timeout(self.rng.randint(900, 1800))
                gate = cap.wait_for_gate(page, timeout=12.0)

            if gate == "press_hold":
                rounds = max(1, int(getattr(self.settings, "px_attempts", 4)))
                for attempt in range(rounds):
                    result = cap.solve_hold_gate(
                        page, self.rng,
                        submit=lambda: form.click_submit(page, self.rng),
                        ctx=ctx,
                        max_holds=8,
                        hold_window=9.0,
                    )
                    print(f"    press-and-hold: {result}")
                    if result == "cleared":
                        # PX clearing does not make the form re-submit itself: the
                        # page just sits on CreateAccount with no cookie. Nudge the
                        # submit once, then let the normal outcome watcher take over.
                        page.wait_for_timeout(self.rng.randint(1200, 2200))
                        if not self._roblosecurity(ctx):
                            form.click_submit(page, self.rng)
                        return "press_hold"
                    if result != "resubmitted" or attempt + 1 >= rounds:
                        break
                    # the re-submission already drew a new challenge
                    gate = cap.wait_for_gate(page, timeout=25.0)
                    if gate == "start_puzzle":
                        page.wait_for_timeout(self.rng.randint(900, 1800))
                        gate = cap.wait_for_gate(page, timeout=12.0)
                    if gate != "press_hold":
                        break

        # stage 2: in-UI visual solve. Arkose renders the puzzle; answering it in
        # the page lets Arkose's own callback hand Roblox the token, so nothing
        # has to be minted or injected.
        if mode == "vision":
            if self._solve_visual(page, ctx):
                return "vision"
            if not self._captcha_present(page):
                return "vision"
            raise CreateError("captcha_failed", "visual solve did not clear the challenge")

        if mode == "manual":
            print("    captcha still up — solve it in the browser window, the run continues on its own")
            return "manual"

        # stage 2: image grid, if Arkose escalated that far
        try:
            page.wait_for_function(cap.HAS_CONFIG, timeout=15_000)
        except Exception:
            pass

        blob = cap.extract_blob(page)
        user_agent = page.evaluate("() => navigator.userAgent")

        challenge = cap.Challenge(
            public_key=ARKOSE_PUBLIC_KEY_SIGNUP,
            page_url=page.url,
            service_url=ARKOSE_SERVICE_URL,
            blob=blob,
            user_agent=user_agent,
            proxy=proxy,
        )

        try:
            token = self.solver.solve(challenge)
        except cap.SolveError as exc:
            raise CreateError("captcha_failed", str(exc))

        result = cap.inject_token(page, token)
        if result in ("no-config", "failed"):
            raise CreateError("captcha_failed", f"token injection failed ({result})")

        # if the page does not resubmit on its own, nudge it
        page.wait_for_timeout(2500)
        if not self._roblosecurity(ctx) and not any("/v2/signup" in u for u, _ in self._events):
            form.click_submit(page, self.rng)
        return "api"

    # -- helpers --------------------------------------------------------------

    def _vision_frame(self, page: Page, timeout: float = 12.0):
        """The Arkose game-core iframe, once it has actually rendered."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frame in page.frames:
                if "ec-game-core" in (frame.url or ""):
                    return frame
            if page.is_closed():
                return None
            page.wait_for_timeout(300)
        return None

    def _solve_visual(self, page: Page, ctx: BrowserContext) -> bool:
        """Solve the rendered Arkose puzzle in-UI. True once the challenge clears."""
        try:
            solver = self.settings.vision_solver()
        except Exception as exc:
            print(f"    vision: solver unavailable ({exc})")
            return False
        if solver is None:
            print("    vision: no key/model configured")
            return False

        frame = self._vision_frame(page, timeout=15.0)
        if frame is None:
            print("    vision: no Arkose game frame")
            return False

        tap = getattr(self, "_vision_tap", None)
        if tap is None:
            tap = vision.ImageTap()
            tap.attach(ctx)
            self._vision_tap = tap
        tap.key = None
        tap.images.clear()

        out_dir = getattr(self.settings, "vision_debug_dir", None)
        rounds = vision.solve_visual(page, frame, tap, solver, self.rng,
                                     out_dir=Path(out_dir) if out_dir else None,
                                     max_rounds=self.settings.vision_max_rounds)
        print(f"    vision: {rounds} round(s) answered")
        if not rounds:
            return False

        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self._roblosecurity(ctx):
                return True
            if not self._captcha_present(page):
                return True
            page.wait_for_timeout(300)
        return False

    @staticmethod
    def _roblosecurity(ctx: BrowserContext) -> str | None:
        try:
            for cookie in ctx.cookies():
                if cookie.get("name") == COOKIE_NAME and cookie.get("value"):
                    return cookie["value"]
        except Exception:
            return None
        return None

    def _verify(self, record: AccountRecord) -> None:
        try:
            resp = requests.get(
                AUTHENTICATED_USER,
                cookies={".ROBLOSECURITY": record.cookie or ""},
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                body = resp.json()
                record.user_id = body.get("id")
                record.display_name = body.get("displayName") or body.get("name")
            elif resp.status_code in (401, 403):
                record.status = "banned"
                record.note = f"auth check {resp.status_code}"
        except requests.RequestException:
            pass  # cookie still good; verification is best-effort

    @staticmethod
    def _reason_for(outcome: Outcome) -> str:
        detail = (outcome.detail or "").lower()
        if "captcha" in detail:
            return "captcha_failed"
        if any(k in detail for k in ("not available", "already taken", "already in use",
                                     "has already", "is taken", "in use")):
            return "taken"
        return "failed"

    @staticmethod
    def _dump(page: Page, tag: str) -> None:
        out = Path("debug")
        out.mkdir(exist_ok=True)
        try:
            page.screenshot(path=str(out / f"{tag}.png"), full_page=True)
            (out / f"{tag}.html").write_text(page.content())
        except Exception:
            pass


def summarize(records: list[AccountRecord]) -> str:
    created = sum(1 for r in records if r.status == "created")
    lines = [f"{created}/{len(records)} created"]
    for r in records:
        mark = "+" if r.status == "created" else "-"
        note = f"  [{r.note[:90]}]" if r.note else ""
        lines.append(f"  {mark} {r.username} ({r.status}, captcha={r.captcha}){note}")
    return "\n".join(lines)
