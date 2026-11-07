"""Drive Roblox's CreateAccount form across both live layout variants.

Roblox A/Bs two different signup pages from the same URL. They differ in the
birthday widget, in whether the password is on the first screen, and in the
submit button's identity:

    classic   #MonthDropdown is a native <select>; password field present
              immediately as #signup-password; submit is #signup-button
    radix     #MonthDropdown is a <div> wrapping a Radix combobox and a hidden
              native <select>; no password on screen one; the submit button
              reads Continue and reveals #signup-v2-password on step two

Every helper here is written against measurements from `tools/form_probe.py`
and `tools/submit_probe.py`, not against the selectors that used to be true.
The variant is detected per page, so a pool that serves both layouts needs no
routing.

Sync Playwright API, matching the rest of the Playwright tooling here.
"""

from __future__ import annotations

import random
import re
from typing import Iterable

from playwright.sync_api import Page

from .config import SEL_USERNAME

CLASSIC = "classic"
RADIX = "radix"

# the widget id -> the option text each layout uses for the same date
MONTH_NAME = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

VARIANT_JS = """() => {
  const m = document.querySelector('#MonthDropdown');
  if (!m) return '';
  return m.tagName;
}"""

PASSWORD_IDS = ("#signup-password", "#signup-v2-password")

# A rendered input is not an interactable one: during the step-one transition the
# password field is in the DOM, offsetParent is non-null, and the form still shows
# a spinner over it. Hit-testing the element's own centre is the only check that
# agrees with what a person can click.
VISIBLE_JS = """(sel) => {
  const e = document.querySelector(sel);
  if (!e) return false;
  const r = e.getBoundingClientRect();
  if (r.width < 8 || r.height < 8) return false;
  const s = getComputedStyle(e);
  if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return false;
  const hit = document.elementFromPoint(cx, cy);
  return !!hit && (hit === e || e.contains(hit) || hit.contains(e));
}"""

BUSY_JS = """() => {
  const busy = [...document.querySelectorAll('[aria-busy="true"], button')]
    .filter(e => { const r = e.getBoundingClientRect();
                   return r.width > 0 && r.height > 0
                     && (e.getAttribute('aria-busy') === 'true'
                         || /spinner|loading/i.test((e.className || '').toString())); });
  return busy.length;
}"""

PICK_JS = """([widget, want]) => {
  const root = document.getElementById(widget);
  if (!root) return { ok: false, why: 'no widget' };
  const select = root.tagName === 'SELECT' ? root : root.querySelector('select');
  if (!select) return { ok: false, why: 'no select' };
  const wantInt = /^0*\\d+$/.test(want) ? parseInt(want, 10) : null;
  const rows = [...select.options];
  let hit = rows.find(o => o.text.trim() === want || o.value === want);
  if (!hit && wantInt !== null) {
    hit = rows.find(o => parseInt(o.value, 10) === wantInt
                      || parseInt(o.text, 10) === wantInt);
  }
  if (!hit) return { ok: false, why: 'no option ' + want,
                     have: rows.slice(0, 4).map(o => o.value) };
  select.value = hit.value;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, value: hit.value, text: hit.text.trim() };
}"""

WIDGET_TEXT_JS = """(id) => {
  const e = document.getElementById(id);
  return e ? (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 24) : '';
}"""

SUBMIT_JS = """() => {
  const pick = () => {
    const byId = document.querySelector('#signup-button');
    if (byId) return byId;
    const visible = (e) => {
      const r = e.getBoundingClientRect();
      if (r.width < 60 || r.height < 24) return false;
      const s = getComputedStyle(e);
      return s.display !== 'none' && s.visibility !== 'hidden' && !e.disabled;
    };
    const name = (e) => ((e.getAttribute('aria-label') || '') + ' '
                       + (e.innerText || '') + ' ' + (e.value || ''))
      .replace(/[ \t\n\r]+/g, ' ').trim();
    const candidates = [...document.querySelectorAll('button, [role=button], input[type=submit]')]
      .filter(visible);
    const labelled = candidates.find(e =>
      /^(continue|sign up|create account|sign up with email|next)$/i.test(name(e)));
    if (labelled) return labelled;
    // step two renders an unlabelled submit button under the password field
    return candidates.find(e => e.type === 'submit') || null;
  };
  return pick();
}"""


def detect_variant(page: Page) -> str:
    """Which CreateAccount layout this page is serving."""
    tag = ""
    try:
        tag = page.evaluate(VARIANT_JS) or ""
    except Exception:
        return CLASSIC
    return RADIX if tag.upper() == "DIV" else CLASSIC


def _is_visible(page: Page, selector: str) -> bool:
    try:
        return bool(page.evaluate(VISIBLE_JS, selector))
    except Exception:
        return False


def password_selector(page: Page) -> str | None:
    """The password field's id on this layout, if it is interactable now."""
    for selector in PASSWORD_IDS:
        if _is_visible(page, selector):
            return selector
    return None


def password_pending(page: Page) -> bool:
    """True while a password step is still settling (spinner over the field)."""
    for selector in PASSWORD_IDS:
        if page.query_selector(selector):
            try:
                return bool(page.evaluate(BUSY_JS))
            except Exception:
                return True
    return False


def ensure_layout(page: Page, want: str = CLASSIC, tries: int = 4,
                  timeout: float = 20.0) -> bool:
    """Reload until the requested layout is served.

    The variant is picked per page load, not per identity, and the radix layout's
    second step currently never becomes interactive: Continue mounts
    #signup-v2-password under a spinner that never clears and never issues
    /v2/signup. Reloading is cheap and lands the classic layout within a few
    tries.
    """
    for _ in range(max(1, tries)):
        if detect_variant(page) == want:
            return True
        try:
            page.reload(wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(SEL_USERNAME, state="visible", timeout=int(timeout * 1000))
            page.wait_for_timeout(1200)
        except Exception:
            continue
    return detect_variant(page) == want


def set_birthday(page: Page, month: str, day: str, year: str,
                 rng: random.Random, variant: str | None = None) -> bool:
    """Set the three date widgets; `month` is the 3-letter abbreviation.

    The Radix combobox ignores a plain Playwright selection, but the native
    <select> it wraps is a real form control: setting its value and dispatching
    input+change moves the widget and React's state together (verified — the
    combobox label flips from "Month" to "March").
    """
    variant = variant or detect_variant(page)
    month_name = MONTH_NAME[_month_index(month)]
    pairs = (
        ("MonthDropdown", month_name, month),
        ("DayDropdown", day, day),
        ("YearDropdown", year, year),
    )
    for widget, label, value in pairs:
        if variant == CLASSIC:
            ok = _select_classic(page, widget, label, value, rng)
        else:
            ok = _pick_radix(page, widget, label, value, rng)
        if not ok:
            return False
    return True


def _month_index(month: str) -> int:
    short = month[:3].title()
    for index, name in enumerate(MONTH_NAME):
        if name.startswith(short):
            return index
    return 0


def _select_classic(page: Page, widget: str, label: str, value: str,
                    rng: random.Random) -> bool:
    selector = f"select#{widget}, #{widget} select"
    for candidate in (label, value):
        try:
            page.select_option(selector, label=candidate, timeout=4000)
            return True
        except Exception:
            continue
    try:
        page.select_option(selector, value=value, timeout=3000)
        return True
    except Exception:
        return False


def _pick_radix(page: Page, widget: str, label: str, value: str,
                rng: random.Random) -> bool:
    for candidate in (label, value):
        try:
            result = page.evaluate(PICK_JS, [widget, candidate])
        except Exception:
            continue
        if result and result.get("ok"):
            page.wait_for_timeout(rng.randint(120, 260))
            return True
    return False


def choose_gender(page: Page, gender: str, rng: random.Random) -> bool:
    """Female/Male button — by id on classic, by accessible name on radix."""
    prefer = "#FemaleButton" if gender == "Female" else "#MaleButton"
    if page.query_selector(prefer):
        try:
            page.click(prefer, timeout=5000)
            page.wait_for_timeout(rng.randint(150, 350))
            return True
        except Exception:
            pass
    try:
        page.get_by_role("button", name=gender, exact=True).first.click(timeout=5000)
        page.wait_for_timeout(rng.randint(150, 350))
        return True
    except Exception:
        return False


def submit_handle(page: Page):
    """The element that advances the form right now, or None (JS fallback)."""
    try:
        handle = page.evaluate_handle(SUBMIT_JS)
        return handle.as_element()
    except Exception:
        return None


SUBMIT_NAMES = re.compile(r"^(continue|sign up|create account|sign up with email|next)$",
                          re.IGNORECASE)

CLICK_JS = r"""() => {
  const named = (e) => ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || ''))
    .replace(/[ \t\n\r]+/g, ' ').trim();
  const byId = document.querySelector('#signup-button');
  if (byId && !byId.disabled) { byId.click(); return 'id'; }
  const buttons = [...document.querySelectorAll('button, [role=button]')]
    .filter(e => { const r = e.getBoundingClientRect();
                   return r.width > 60 && r.height > 24 && !e.disabled; });
  const labelled = buttons.find(e => /^(continue|sign up|create account|sign up with email|next)$/i
                                .test(named(e)));
  if (labelled) { labelled.click(); return 'label:' + named(labelled).slice(0, 14); }
  const submit = buttons.find(e => e.type === 'submit');
  if (submit) { submit.click(); return 'submit'; }
  return 'none';
}"""


def click_submit(page: Page, rng: random.Random) -> bool:
    """Press whatever advances the form right now.

    Two traps here, both measured: the radix Continue button carries its label as
    an aria-label (so `innerText` matching sees an empty button), and a React
    overlay can intercept the pointer, which makes Playwright's normal click time
    out on a button that is plainly visible. Force-click, then fall back to a
    direct DOM click, and only then give up.
    """
    try:
        locator = page.locator("#signup-button").first
        if locator.count() and locator.is_visible():
            locator.click(timeout=5000, force=True)
            page.wait_for_timeout(rng.randint(200, 500))
            return True
    except Exception:
        pass

    for name in ("Continue", "Sign up", "Create account", "Next"):
        try:
            locator = page.get_by_role("button", name=name, exact=False).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=4000, force=True)
                page.wait_for_timeout(rng.randint(200, 500))
                return True
        except Exception:
            continue

    try:
        result = page.evaluate(CLICK_JS)
    except Exception:
        result = "none"
    if result != "none":
        page.wait_for_timeout(rng.randint(200, 500))
        return True
    return False


def _click_label(page: Page, name: str) -> bool:
    """Click a button by accessible name, forcing past overlays."""
    try:
        locator = page.get_by_role("button", name=name, exact=False).first
        if locator.count() and locator.is_visible():
            locator.click(timeout=4000, force=True)
            return True
    except Exception:
        pass
    try:
        locator = page.get_by_text(name, exact=False).first
        if locator.count() and locator.is_visible():
            locator.click(timeout=4000, force=True)
            return True
    except Exception:
        pass
    return False


def reveal_password(page: Page, rng: random.Random, timeout: float = 25.0) -> str | None:
    """Advance to an interactive password field and return its selector.

    Two different shapes hide it:

    classic   it is already on screen
    radix     Continue moves to a second step that offers "Add password" or
              "Create a passkey"; the field only exists after choosing the former.
              Without that press the form sits calm and never submits — which is
              exactly the "stuck on step two" we chased for hours.
    """
    already = password_selector(page)
    if already:
        return already

    if not click_submit(page, rng):
        return None

    waited = 0.0
    while waited < min(timeout, 10.0):
        page.wait_for_timeout(500)
        waited += 0.5
        hit = password_selector(page)
        if hit:
            return hit
        if _challenge_frame(page):
            return None
        if _click_label(page, "Add password"):
            for _ in range(12):
                page.wait_for_timeout(400)
                hit = password_selector(page)
                if hit:
                    return hit
            return None
    return password_selector(page)


def _challenge_frame(page: Page) -> bool:
    for frame in page.frames:
        url = frame.url or ""
        if "arkoselabs" in url or "funcaptcha" in url or "px-cdn" in url:
            return True
    return False


def fill_credentials(page: Page, username: str, password: str,
                     rng: random.Random,
                     type_username: "callable | None" = None,
                     type_password: "callable | None" = None) -> str:
    """Fill username and password; returns the step the form ended on.

    `type_username` / `type_password` let the caller pass the repo's human
    typing helpers without this module importing them.
    """
    if type_username:
        type_username(SEL_USERNAME, username)
    else:
        page.fill(SEL_USERNAME, username)
    page.wait_for_timeout(rng.randint(300, 700))

    selector = password_selector(page)
    if selector is None:
        selector = reveal_password(page, rng)
        if selector is None:
            return "username"

    if type_password:
        type_password(selector, password)
    else:
        page.fill(selector, password)
    page.wait_for_timeout(rng.randint(250, 600))
    return "password-filled"


def state(page: Page) -> dict:
    """Compact snapshot for probes and diagnostics."""
    return {
        "url": page.url,
        "variant": detect_variant(page),
        "password": password_selector(page),
        "pending": password_pending(page),
        "challenge": _challenge_frame(page),
        "frames": [f.url[:70] for f in page.frames][:5],
    }


def wait_for_any(page: Page, selectors: Iterable[str], timeout: float = 15.0) -> str | None:
    waited = 0.0
    while waited < timeout:
        for selector in selectors:
            if _is_visible(page, selector):
                return selector
        page.wait_for_timeout(400)
        waited += 0.4
    return None
