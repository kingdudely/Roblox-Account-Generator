"""FunCaptcha handling for the current Roblox signup flow.

Flow as it exists on the live page (derived from Challenge.js + CaptchaCore.js):

    1. page POSTs /v1/arkose/bot-activity-ticket  -> ticket
    2. Arkose SDK renders into #signup-captcha with
           ArkoseEnforcement.setConfig({ data: { blob }, onCompleted, onSuppress, ... })
    3. onSuppress(token) fires with no interaction when the risk score is low
       onCompleted({token}) fires after a visible challenge
    4. handler dispatches SET_CHALLENGE_COMPLETED, then POSTs the token to
       https://captcha.roblox.com/v1/funcaptcha/signup/web
    5. /v2/signup consumes the redeemed captcha value

Because the SDK instance is reachable at runtime, a solver token is injected by
calling the captured `onCompleted` callback directly — no DOM token input to
fight, no React internals to reach.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

import requests


# --------------------------------------------------------------------------- #
# hook installed before any page script runs
# --------------------------------------------------------------------------- #

PAGE_HOOK = r"""
(() => {
  const H = (window.__lunar = window.__lunar || { net: [], config: null, instance: null });

  // ---- mask patched functions as native (toString + name are fingerprint bait) ----
  const nativeToString = Function.prototype.toString;
  const masked = new WeakMap();
  const mask = (fn, original) => {
    try { masked.set(fn, nativeToString.call(original)); } catch (e) {}
    try { Object.defineProperty(fn, "name", { value: original.name, configurable: true }); } catch (e) {}
    try { Object.defineProperty(fn, "length", { value: original.length, configurable: true }); } catch (e) {}
    return fn;
  };
  const fakeToString = function () {
    if (masked.has(this)) return masked.get(this);
    return nativeToString.call(this);
  };
  mask(fakeToString, nativeToString);
  Function.prototype.toString = fakeToString;

  // ---- network tap: signup + captcha redeem responses ----
  const record = (entry) => { try { H.net.push(entry); } catch (e) {} };
  const isInteresting = (u) => /\/v2\/signup|funcaptcha\//.test(String(u || ""));

  const origFetch = window.fetch;
  window.fetch = mask(function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const p = origFetch.apply(this, arguments);
    if (isInteresting(url)) {
      p.then(async (res) => {
        let body = "";
        try { body = await res.clone().text(); } catch (e) {}
        record({ kind: "fetch", url: String(url), status: res.status, body: body.slice(0, 4000) });
      }).catch(() => {});
    }
    return p;
  }, origFetch);

  const xOpen = XMLHttpRequest.prototype.open;
  const xSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = mask(function (m, u) {
    this.__lunar_url = u;
    return xOpen.apply(this, arguments);
  }, xOpen);
  XMLHttpRequest.prototype.send = mask(function () {
    this.addEventListener("load", function () {
      if (isInteresting(this.__lunar_url)) {
        record({ kind: "xhr", url: String(this.__lunar_url), status: this.status,
                 body: String(this.responseText || "").slice(0, 4000) });
      }
    });
    return xSend.apply(this, arguments);
  }, xSend);

  // ---- Arkose v2 tap: the live build exposes setupEnforcement0(config) +
  //      arkoselabsClientApi<rand>, NOT window.ArkoseEnforcement. Defining a
  //      stub for a global the SDK never writes is a fingerprint tell (it shows
  //      up in Object.keys(window)), so non-enumerable and name-targeted only.
  function captureSetup(v) {
    return mask(function (cfg) {
      try { H.config = cfg; H.instance = this; } catch (e) {}
      return v.apply(this, arguments);
    }, v);
  }
  ["setupEnforcement0", "setupEnforcement1", "setupEnforcement2"].forEach(function (name) {
    let fn;
    try {
      Object.defineProperty(window, name, {
        configurable: true,
        enumerable: false,
        get: function () { return fn; },
        set: function (v) { try { fn = captureSetup(v); } catch (e) { fn = v; } }
      });
    } catch (e) {}
  });
})();
"""


INJECT_TOKEN = r"""
(token) => {
  const H = window.__lunar || {};
  const cfg = H.config;
  if (!cfg) return "no-config";

  // the V2 SDK calls onCompleted({ token })
  if (typeof cfg.onCompleted === "function") {
    try { cfg.onCompleted({ token: token }); return "onCompleted:object"; } catch (e) {}
    try { cfg.onCompleted(token); return "onCompleted:raw"; } catch (e) {}
  }
  if (typeof cfg.onSuppress === "function") {
    try { cfg.onSuppress({ token: token }); return "onSuppress:object"; } catch (e) {}
  }
  return "failed";
}
"""


READ_NET = "() => (window.__lunar && window.__lunar.net) || []"
HAS_CONFIG = "() => !!(window.__lunar && window.__lunar.config)"
READ_CHALLENGE_STATE = "() => (window.__lunar && window.__lunar.events) || null"


# --------------------------------------------------------------------------- #
# challenge description + solvers
# --------------------------------------------------------------------------- #


@dataclass
class Challenge:
    public_key: str
    page_url: str
    service_url: str
    blob: str | None
    user_agent: str
    proxy: str | None = None


class SolveError(RuntimeError):
    pass


class Solver(Protocol):
    name: str

    def solve(self, challenge: Challenge) -> str: ...


def _proxy_parts(proxy: str | None) -> tuple[str, str, str, str] | None:
    if not proxy:
        return None
    from urllib.parse import urlparse

    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not p.hostname or not p.port:
        return None
    return (p.scheme or "http", p.hostname, str(p.port), "")


class CapSolverSolver:
    """https://capsolver.com — handles Arkose V2 blobs."""

    name = "capsolver"
    API = "https://api.capsolver.com"

    def __init__(self, api_key: str, poll_interval: float = 3.0, timeout: float = 180.0):
        if not api_key:
            raise SolveError("CAPSOLVER_KEY is not set")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.session = requests.Session()

    def _task(self, challenge: Challenge) -> dict:
        task = {
            "type": "FunCaptchaTaskProxyLess",
            "websiteURL": challenge.page_url,
            "websitePublicKey": challenge.public_key,
            "userAgent": challenge.user_agent,
        }
        if challenge.blob:
            task["data"] = json.dumps({"blob": challenge.blob})
        if challenge.proxy:
            parts = _proxy_parts(challenge.proxy)
            if parts:
                scheme, host, port, _ = parts
                task["type"] = "FunCaptchaTask"
                task["proxyType"] = scheme if scheme in ("http", "https", "socks5") else "http"
                task["proxyAddress"] = host
                task["proxyPort"] = int(port)
                if "@" in challenge.proxy:
                    import urllib.parse as up

                    ui = up.urlparse(challenge.proxy if "://" in challenge.proxy else f"http://{challenge.proxy}")
                    if ui.username:
                        task["proxyLogin"] = up.unquote(ui.username)
                        task["proxyPassword"] = up.unquote(ui.password or "")
        return task

    def solve(self, challenge: Challenge) -> str:
        payload = {"clientKey": self.api_key, "task": self._task(challenge)}
        resp = self.session.post(f"{self.API}/createTask", json=payload, timeout=30).json()
        if resp.get("errorId"):
            raise SolveError(f"capsolver createTask: {resp.get('errorCode')} {resp.get('errorDescription')}")
        task_id = resp.get("taskId")
        if not task_id:
            raise SolveError(f"capsolver createTask: no taskId ({resp})")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            out = self.session.post(
                f"{self.API}/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
                timeout=30,
            ).json()
            if out.get("errorId"):
                raise SolveError(f"capsolver getTaskResult: {out.get('errorCode')} {out.get('errorDescription')}")
            if out.get("status") == "ready":
                token = (out.get("solution") or {}).get("token")
                if not token:
                    raise SolveError(f"capsolver returned no token: {out}")
                return token
        raise SolveError("capsolver timed out")


class TwoCaptchaSolver:
    """https://2captcha.com — cheaper, slower, less reliable on Arkose V2."""

    name = "2captcha"
    IN = "https://2captcha.com/in.php"
    RES = "https://2captcha.com/res.php"

    def __init__(self, api_key: str, poll_interval: float = 5.0, timeout: float = 180.0):
        if not api_key:
            raise SolveError("TWOCAPTCHA_KEY is not set")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.session = requests.Session()

    def solve(self, challenge: Challenge) -> str:
        data = {
            "key": self.api_key,
            "method": "funcaptcha",
            "publickey": challenge.public_key,
            "pageurl": challenge.page_url,
            "serviceURL": challenge.service_url,
            "userAgent": challenge.user_agent,
            "json": 1,
        }
        if challenge.blob:
            data["data[blob]"] = challenge.blob
        if challenge.proxy:
            from urllib.parse import urlparse, unquote

            p = urlparse(challenge.proxy if "://" in challenge.proxy else f"http://{challenge.proxy}")
            if p.hostname and p.port:
                data["proxy"] = f"{p.hostname}:{p.port}"
                data["proxytype"] = p.scheme.upper() if p.scheme else "HTTP"
                if p.username:
                    data["proxy"] = f"{unquote(p.username)}:{unquote(p.password or '')}@{p.hostname}:{p.port}"

        resp = self.session.post(self.IN, data=data, timeout=30).json()
        if resp.get("status") != 1:
            raise SolveError(f"2captcha in.php: {resp.get('request')}")
        task_id = resp["request"]

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            out = self.session.get(
                self.RES,
                params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
                timeout=30,
            ).json()
            if out.get("status") == 1:
                return out["request"]
            if out.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                raise SolveError(f"2captcha res.php: {out.get('request')}")
        raise SolveError("2captcha timed out")


class SolvexSolver:
    """https://solvex.run — Arkose-specialist, HTTP/3, tuned on Roblox signup.

    Two endpoints, standard REST, and the solution says which kind of pass it
    was: `suppressed` (no puzzle needed), `pow` (proof-of-work solved), or
    `challenge` (it saw and solved the images). `verified` is the field that
    matters — an unverified token is worthless to Roblox.

    Pricing is per successful solve (~$0.50/1000), so failures are free.
    """

    name = "solvex"
    API = "https://api.solvex.run"
    # the service keeps an Arkose fingerprint per UA flavour and rejects mismatches,
    # so the UA has to be the one it built the fingerprint for
    SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")

    def __init__(self, api_key: str, poll_interval: float = 0.5,
                 timeout: float = 120.0, use_http3: bool = True,
                 solve_pow: bool = True, user_agent: str | None = None):
        if not api_key:
            raise SolveError("SOLVEX_API_KEY is not set")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.use_http3 = use_http3
        self.solve_pow = solve_pow
        self.user_agent = user_agent or self.SAFARI_UA
        self.session = requests.Session()

    def _task(self, challenge: Challenge) -> dict:
        task: dict = {
            "type": "FunCaptchaTask",
            "websiteURL": challenge.page_url,
            "websitePublicKey": challenge.public_key,
            "userAgent": self.user_agent,
            "useHttp3": self.use_http3,
            "solvePow": self.solve_pow,
        }
        if challenge.blob:
            task["data"] = challenge.blob
        proxy = _solvex_proxy(challenge.proxy)
        if proxy:
            task.update(proxy)
        return task

    def solve(self, challenge: Challenge) -> str:
        payload = {"clientKey": self.api_key, "task": self._task(challenge)}
        resp = self.session.post(f"{self.API}/createTask", json=payload, timeout=30)
        body = _solver_json(resp, "solvex createTask")
        task_id = body.get("taskId")
        if not task_id:
            raise SolveError(f"solvex createTask: no taskId ({str(body)[:160]})")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            out = _solver_json(
                self.session.post(f"{self.API}/getTaskResult",
                                  json={"clientKey": self.api_key, "taskId": task_id},
                                  timeout=30),
                "solvex getTaskResult")
            if out.get("status") != "ready":
                continue
            solution = out.get("solution") or {}
            token = solution.get("token")
            if not token:
                raise SolveError(f"solvex returned no token: {str(out)[:160]}")
            if solution.get("verified") is False:
                raise SolveError(f"solvex returned an unverified token "
                                 f"(challenge={solution.get('challengeType')})")
            return token
        raise SolveError("solvex timed out")


def _solver_json(resp: requests.Response, label: str) -> dict:
    if resp.status_code >= 400:
        raise SolveError(f"{label} HTTP {resp.status_code}: {resp.text[:160]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise SolveError(f"{label} non-JSON: {resp.text[:160]}") from exc
    if body.get("errorId"):
        raise SolveError(f"{label}: {body.get('errorDescription') or body.get('errorCode')}")
    return body


def _solvex_proxy(proxy: str | None) -> dict | None:
    """Solvex wants the proxy split into fields, credentials included."""
    if not proxy:
        return None
    from urllib.parse import unquote, urlparse

    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not p.hostname or not p.port:
        return None
    scheme = (p.scheme or "http").lower()
    if scheme == "socks5h":
        scheme = "socks5"
    elif scheme == "socks4a":
        scheme = "socks4"
    out = {
        "proxyType": scheme,
        "proxyAddress": p.hostname,
        "proxyPort": int(p.port),
    }
    if p.username:
        out["proxyLogin"] = unquote(p.username)
    if p.password:
        out["proxyPassword"] = unquote(p.password)
    return out


def build_solver(provider: str, api_key: str) -> Solver:
    provider = provider.lower()
    if provider == "capsolver":
        return CapSolverSolver(api_key)
    if provider in ("2captcha", "twocaptcha"):
        return TwoCaptchaSolver(api_key)
    if provider == "solvex":
        return SolvexSolver(api_key)
    raise SolveError(f"unknown solver provider: {provider}")


# --------------------------------------------------------------------------- #
# page-side helpers
# --------------------------------------------------------------------------- #


def install_hook(context) -> None:
    context.add_init_script(PAGE_HOOK)


def captcha_visible(page, timeout: float = 12.0) -> bool:
    from .config import SEL_CAPTCHA_FRAME

    try:
        page.wait_for_selector(SEL_CAPTCHA_FRAME, state="attached", timeout=timeout * 1000)
        return True
    except Exception:
        return False


def extract_blob(page) -> str | None:
    """The data-exchange blob Arkose wants, from any place it surfaces."""
    from urllib.parse import urlparse, parse_qs

    for frame in page.frames:
        url = frame.url or ""
        if "arkoselabs" not in url and "funcaptcha" not in url:
            continue
        query = parse_qs(urlparse(url).query)
        for key in ("data", "blob", "dataExchangeBlob", "data_exchange_blob"):
            if key in query and query[key]:
                return query[key][0]

    try:
        net = page.evaluate(READ_NET) or []
    except Exception:
        return None
    for entry in reversed(net):
        raw = entry.get("body") or ""
        if not raw.strip().startswith("{"):
            continue
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for key in ("dataExchangeBlob", "data_exchange_blob", "blob"):
            if isinstance(body.get(key), str):
                return body[key]
    return None


def inject_token(page, token: str) -> str:
    try:
        return page.evaluate(INJECT_TOKEN, token) or "no-result"
    except Exception as exc:  # pragma: no cover - surface as status
        return f"error:{exc}"


def net_entries(page) -> list[dict]:
    try:
        return page.evaluate(READ_NET) or []
    except Exception:
        return []


def challenge_state(page) -> dict | None:
    """What Arkose did: suppressed (silent pass) / shown (grid) / error."""
    try:
        return page.evaluate(READ_CHALLENGE_STATE)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# behavioural gate: "Press and hold"
#
# Arkose's current first stage is not a perception puzzle — it is an interaction
# signature. It samples hold duration, pointer travel and timing jitter, then
# decides whether the input came from a hand. There is no audio track and no
# token to inject: you either produce plausible input or you fail. Generated
# jitter inside a human range is the whole solution, and completing it in-run
# lets Arkose's own callback fire, which is immune to SDK version changes.
# --------------------------------------------------------------------------- #

PRESS_HOLD_PROBE = r"""() => {
  const byLabel = (el) => {
    const label = (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
    return label.indexOf('press and hold') !== -1 || label.indexOf('press & hold') !== -1;
  };
  for (const el of document.querySelectorAll('[aria-label]')) {
    if (!byLabel(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 20) {
      return { x: r.x, y: r.y, w: r.width, h: r.height, label: el.getAttribute('aria-label') };
    }
  }
  return null;
}"""


def find_press_hold(page) -> dict | None:
    """Locate the hold target in whatever frame owns it, in page coordinates."""
    for frame in page.frames:
        try:
            info = frame.evaluate(PRESS_HOLD_PROBE)
        except Exception:
            continue
        if not info:
            continue
        try:
            if frame is page.main_frame:
                ox = oy = 0.0
            else:
                box = frame.frame_element().bounding_box()
                if not box:
                    continue
                ox, oy = box["x"], box["y"]
        except Exception:
            continue
        return {
            "frame": frame,
            "x": ox + info["x"] + info["w"] / 2,
            "y": oy + info["y"] + info["h"] / 2,
            "label": info.get("label"),
        }
    return None


def has_press_hold(page) -> bool:
    return find_press_hold(page) is not None


def start_puzzle(page, timeout: float = 0.0) -> bool:
    """Click Arkose's "Start Puzzle" gate if it is up.

    Arkose renders a static Verification panel with a Start Puzzle button before
    the actual challenge. Nothing happens until it is clicked, which is why a
    probe that only looks for the puzzle sees nothing.
    """
    deadline = time.time() + timeout
    while True:
        for frame in page.frames:
            if "arkoselabs" not in (frame.url or ""):
                continue
            try:
                loc = frame.locator("text=Start Puzzle")
                if loc.count():
                    loc.first.click(timeout=3000)
                    return True
            except Exception:
                continue
        if time.time() >= deadline:
            return False
        page.wait_for_timeout(300)


def wait_for_gate(page, timeout: float = 20.0) -> str | None:
    """Wait until either the Arkose Start-Puzzle gate or a PX hold is actionable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if start_puzzle(page):
            return "start_puzzle"
        if find_press_hold(page):
            return "press_hold"
        if page.is_closed():
            return None
        page.wait_for_timeout(300)
    return None


def wait_for_press_hold(page, timeout: float = 20.0) -> dict | None:
    """The modal shows a spinner before the button mounts — poll, don't probe once."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = find_press_hold(page)
        if hit:
            return hit
        if page.is_closed():
            return None
        page.wait_for_timeout(250)
    return None


HOLD_FRAME_STATE = r"""() => {
  const txt = (document.body.innerText || '').toLowerCase();
  const btn = document.querySelector('[aria-label*="hold" i], button');
  let btnVis = false;
  if (btn) { const r = btn.getBoundingClientRect(); btnVis = r.width > 0 && r.height > 0; }
  return {
    text: txt.slice(0, 200),
    button: btnVis,
    failed: /try again|failed|error/.test(txt),
  };
}"""


def _hold_state(page, frame) -> str:
    """"failed" | "gone" | "holding" for the PX modal."""
    try:
        state = frame.evaluate(HOLD_FRAME_STATE)
    except Exception:
        return "gone"
    if state.get("failed"):
        return "failed"
    try:
        visible = page.evaluate(
            "() => { const r = document.querySelector('#px-captcha'); if (!r) return false;"
            " const s = getComputedStyle(r); const b = r.getBoundingClientRect();"
            " return s.display !== 'none' && s.visibility !== 'hidden'"
            " && b.width > 0 && b.height > 0; }")
    except Exception:
        visible = True
    return "holding" if visible else "gone"


PROGRESS = r"""() => {
  const walk = (root, out) => {
    for (const e of root.querySelectorAll('*')) { out.push(e); if (e.shadowRoot) walk(e.shadowRoot, out); }
    return out;
  };
  const els = walk(document, []);
  const p = els.find(e => e.getAttribute && e.getAttribute('role') === 'progressbar');
  if (!p) return null;
  const now = parseFloat(p.getAttribute('aria-valuenow') || '0');
  const max = parseFloat(p.getAttribute('aria-valuemax') || '0');
  return { now, max, done: max > 0 && now >= max };
}"""


def _progress_done(frame) -> bool:
    try:
        state = frame.evaluate(PROGRESS)
    except Exception:
        return False
    return bool(state and state.get("done"))


MODAL_PROBE = r"""() => {
  const e = document.querySelector('#px-captcha');
  if (!e) return false;
  const s = getComputedStyle(e);
  const b = e.getBoundingClientRect();
  return s.display !== 'none' && s.visibility !== 'hidden'
         && b.width > 0 && b.height > 0;
}"""


def _modal_visible(page) -> bool:
    """Is the gate up — in whatever document owns it?

    The panel renders inside the PX/HUMAN frame, so the top document usually has
    no #px-captcha at all. Probing only the top document reported every gate as
    already gone, which made the solver claim a clear that never happened.
    """
    if find_press_hold(page) is not None:
        return True
    for frame in page.frames:
        try:
            if frame.evaluate(MODAL_PROBE):
                return True
        except Exception:
            continue
    return False


def _wait_outcome(page, frame, timeout: float = 8.0) -> str:
    """After release: 'accepted' once the modal is gone, 'rejected' on try-again."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = frame.evaluate(
                "() => (document.body.innerText || '').toLowerCase().slice(0, 200)")
        except Exception:
            return "accepted"
        if "try again" in text or "failed" in text:
            return "rejected"
        try:
            visible = page.evaluate(
                "() => { const r = document.querySelector('#px-captcha'); if (!r) return false;"
                " const s = getComputedStyle(r); const b = r.getBoundingClientRect();"
                " return s.display !== 'none' && s.visibility !== 'hidden'"
                " && b.width > 0 && b.height > 0; }")
        except Exception:
            return "accepted"
        if not visible:
            return "accepted"
        page.wait_for_timeout(250)
    return "timeout"


def dismiss_challenge(page) -> bool:
    """Close the PX modal so the form can be submitted again.

    The coin flip is per submission: a refused hold is not the end of the
    attempt, it is one bad draw. Closing and re-submitting often lands Arkose
    instead, which is a puzzle we can actually answer.

    The affordance is a Radix close button whose accessible name is "Cancel"
    (not "Close"), so label matching has to cover that word.
    """
    selectors = (
        '[aria-label="Cancel"]',
        '[aria-label*="Cancel" i]',
        '[aria-label*="Close" i]',
        'button[title*="Close" i]',
    )
    for frame in page.frames:
        for selector in selectors:
            try:
                loc = frame.locator(selector)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2500)
                    page.wait_for_timeout(350)
                    return True
            except Exception:
                continue
    # last resort: Radix dialogs close on Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(350)
        return not _modal_visible(page)
    except Exception:
        return False


def press_and_hold(page, rng, hit: dict | None = None, duration: float | None = None,
                   max_hold: float = 6.4, attempts: int = 3) -> float | None:
    """Hold for the gesture window, release, then read the verdict.

    PX verifies on release. The ring filling is the "you may let go now" signal,
    not a finish line — holding past it and never releasing stalls the widget,
    and releasing a beat early gets "Please try again". So: dense pointer stream,
    hold until the ring completes (or the window elapses), release, then wait for
    the modal to close. A rejection re-arms the button, so retry on it.
    """
    total = 0.0
    for attempt in range(attempts):
        hit = (hit if attempt == 0 and hit else None) or find_press_hold(page)
        if not hit:
            return total or None
        frame = hit.get("frame")
        x, y = hit["x"], hit["y"]

        page.mouse.move(x - rng.uniform(2, 6), y - rng.uniform(1, 5), steps=rng.randint(4, 8))
        page.wait_for_timeout(rng.randint(60, 180))
        page.mouse.move(x, y, steps=rng.randint(2, 4))
        page.wait_for_timeout(rng.randint(40, 110))
        page.mouse.down()

        window = duration if duration is not None else rng.uniform(max_hold - 1.4, max_hold)
        held = 0.0
        dx = dy = 0.0
        while held < window:
            dx += rng.gauss(0, 0.16)
            dy += rng.gauss(0, 0.13)
            dx = max(-1.6, min(1.6, dx))
            dy = max(-1.3, min(1.3, dy))
            page.mouse.move(x + dx, y + dy, steps=rng.randint(2, 5))
            step = rng.uniform(0.012, 0.026)
            page.wait_for_timeout(int(step * 1000))
            held += step
            if frame is not None and held > 2.0 and _progress_done(frame):
                break

        page.wait_for_timeout(rng.randint(40, 120))
        page.mouse.up()
        total += held

        if frame is None:
            return total
        outcome = _wait_outcome(page, frame)
        if outcome == "accepted":
            return total
        page.wait_for_timeout(rng.randint(500, 1100))
    return total or None


HOLD_PANEL_JS = r"""() => {
  const walk = (root, out) => {
    for (const e of root.querySelectorAll('*')) {
      out.push(e);
      if (e.shadowRoot) walk(e.shadowRoot, out);
    }
    return out;
  };
  const els = walk(document, []);
  const btn = els.find(e => e.getAttribute
                        && /press and hold/i.test(e.getAttribute('aria-label') || ''));
  const raw = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
  const low = raw.toLowerCase();
  let box = null;
  if (btn) {
    const r = btn.getBoundingClientRect();
    box = { x: r.x + r.width / 2, y: r.y + r.height / 2,
            w: r.width, h: r.height };
  }
  return {
    box,
    text: raw.slice(0, 160),
    completed: low.indexOf('challenge completed') !== -1
               || low.indexOf('completed, please wait') !== -1,
    rejected: low.indexOf('try again') !== -1
              || low.indexOf('failed') !== -1
              || low.indexOf('something went wrong') !== -1,
    waiting: low.indexOf('requires verification') !== -1
             || low.indexOf('until verified') !== -1,
  };
}"""


def hold_panel(frame) -> dict | None:
    """Read the gate's own state; None once the frame is gone."""
    try:
        return frame.evaluate(HOLD_PANEL_JS)
    except Exception:
        return None


def _panel_completed(page) -> bool:
    """True if any frame is showing the gate's completion message."""
    for frame in page.frames:
        state = hold_panel(frame)
        if state and state.get("completed"):
            return True
    return False


def _hold_release_wait(page, ctx=None, timeout: float = 12.0,
                       completed_grace: float = 6.0) -> bool:
    """True once the gate is gone, or has held "completed" long enough to trust.

    Acceptance shows "Human Challenge completed, please wait" and then closes as
    the form re-submits, so a completed panel that lingers past the grace window
    counts as accepted — waiting on it forever would turn a pass into a timeout.
    """
    started = time.time()
    completed_since: float | None = None
    while time.time() - started < timeout:
        if _panel_completed(page):
            completed_since = completed_since or time.time()
            if time.time() - completed_since >= completed_grace:
                return True
        elif not _modal_visible(page):
            return True
        else:
            completed_since = None
        if ctx is not None:
            try:
                if any(c.get("name") == ".ROBLOSECURITY" for c in ctx.cookies()):
                    return True
            except Exception:
                pass
        page.wait_for_timeout(300)
    return not _modal_visible(page)


GATE_STATE_JS = r"""() => {
  const walk = (root, out) => {
    for (const e of root.querySelectorAll('*')) {
      out.push(e);
      if (e.shadowRoot) walk(e.shadowRoot, out);
    }
    return out;
  };
  const els = walk(document, []);
  const btn = els.find(e => e.getAttribute
                        && /press and hold/i.test(e.getAttribute('aria-label') || ''));
  const panel = els.find(e => e.id === 'px-captcha')
             || els.find(e => e.getAttribute && e.getAttribute('role') === 'dialog');
  const raw = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
  const low = raw.toLowerCase();
  let box = null;
  if (btn) {
    const r = btn.getBoundingClientRect();
    box = { x: r.x + r.width / 2, y: r.y + r.height / 2,
            w: r.width, h: r.height, disabled: !!btn.disabled };
  }
  const visible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el), b = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden'
           && b.width > 0 && b.height > 0;
  };
  return {
    box,
    panel: visible(panel),
    text: raw.slice(0, 160),
    completed: low.indexOf('challenge completed') !== -1
               || low.indexOf('completed, please wait') !== -1,
    rejected: low.indexOf('try again') !== -1
              || low.indexOf('failed') !== -1
              || low.indexOf('something went wrong') !== -1,
    verifying: low.indexOf('requires verification') !== -1
               || low.indexOf('until verified') !== -1,
  };
}"""


def gate_state(page) -> str:
    """One of: gone | pressable | retry | completed | dead | none.

    `dead` is the case the panel makes easy to misread: the dialog is still on
    screen, the hold is over, and the blue button no longer carries its
    "Press and hold" label — so there is nothing left to press and nothing that
    will resolve. It is not a pass.
    """
    panel_seen = False
    for frame in page.frames:
        try:
            state = frame.evaluate(GATE_STATE_JS)
        except Exception:
            continue
        if not state:
            continue
        if state.get("completed"):
            return "completed"
        if state.get("rejected"):
            return "retry"
        if state.get("panel"):
            panel_seen = True
        box = state.get("box")
        if box and box.get("w", 0) > 20 and box.get("h", 0) > 20 \
                and not box.get("disabled"):
            return "pressable"
    if panel_seen:
        return "dead"
    return "gone" if find_press_hold(page) is None else "pressable"


def _settle_gate(page, ctx=None, timeout: float = 14.0) -> str:
    """Listen through the panel's loading phase and report what it settled into.

    A completed panel shows "please wait" while the form re-submits; it can also
    fall back to "try again". Only a closed panel is a pass.
    """
    started = time.time()
    while time.time() - started < timeout:
        state = gate_state(page)
        if state == "gone":
            return "gone"
        if state == "retry":
            return "retry"
        if state == "dead":
            return "dead"
        if ctx is not None:
            try:
                if any(c.get("name") == ".ROBLOSECURITY" for c in ctx.cookies()):
                    return "gone"
            except Exception:
                pass
        page.wait_for_timeout(400)
    return gate_state(page)


def _close_and_redraw(page, rng, submit) -> str:
    """The dead state: dismiss the panel and submit again for a new draw."""
    if not dismiss_challenge(page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            pass
    if _modal_visible(page):
        return "exhausted"
    if submit is None:
        return "exhausted"
    page.wait_for_timeout(rng.randint(800, 1700))
    try:
        submit()
    except Exception:
        return "exhausted"
    return "resubmitted"


def solve_hold_gate(page, rng, submit=None, ctx=None, max_holds: int = 8,
                    hold_window: float = 9.0, min_holds: int = 2,
                    verbose: bool = False) -> str:
    """Press and hold until the panel resolves, then act on what it says.

    The panel's cycle, as observed: a full hold loads, then either closes or
    answers "Please try again"; a rejected hold re-arms, and the *next* hold
    often leaves the blue button without its "Press and hold" label and does
    nothing at all. That last state cannot be won from inside.

    So: keep pressing while the button is pressable, and stop on exactly one of

        the panel closes          -> cleared
        the button stops being
        pressable (dead)          -> close it and submit again for a new draw

    "Please try again" is never a stop condition — it means press again. A
    single silent hold is not a dead state either, which is why the first
    `min_holds` presses never take the dead branch.

    Returns "cleared" | "resubmitted" | "exhausted" | "none".
    """
    if gate_state(page) == "gone" and not _modal_visible(page):
        return "none"

    holds = 0
    for _ in range(max_holds):
        state = gate_state(page)
        if verbose:
            print(f"      gate: {state}", flush=True)
        if state == "gone":
            return "cleared"
        if state == "dead":
            return _close_and_redraw(page, rng, submit)
        if state == "completed":
            after = _settle_gate(page, ctx)
            if after == "gone":
                return "cleared"
            if after == "dead":
                return _close_and_redraw(page, rng, submit)
            # completed fell back to a retry: press again

        hit = find_press_hold(page)
        if not hit:
            # no target but the panel is up -> nothing left to press
            if _modal_visible(page):
                return _close_and_redraw(page, rng, submit)
            return "cleared"

        holds += 1
        # The panel keeps showing "Please try again" from the previous round, so
        # its text is only meaningful as a *transition*. Snapshot it at press time
        # and ignore a retry that was already there — otherwise every hold after
        # the first aborts within a few dozen milliseconds on stale text.
        baseline = gate_state(page)
        if verbose:
            print(f"      hold {holds} down (baseline {baseline})", flush=True)
        x, y = hit["x"], hit["y"]
        page.mouse.move(x - rng.uniform(2, 7), y - rng.uniform(2, 6),
                        steps=rng.randint(4, 8))
        page.wait_for_timeout(rng.randint(140, 320))
        page.mouse.move(x, y, steps=rng.randint(2, 4))
        page.wait_for_timeout(rng.randint(180, 480))
        page.mouse.down()

        started = time.time()
        state = baseline
        while time.time() - started < hold_window:
            page.mouse.move(x + rng.uniform(-2.6, 2.6), y + rng.uniform(-2.2, 2.2),
                            steps=rng.randint(2, 5))
            page.wait_for_timeout(rng.randint(35, 75))
            if time.time() - started < 3.0:
                continue          # the panel cannot answer before the ring fills
            state = gate_state(page)
            if state == "gone":
                break
            if state == "completed":
                break
            if state == "retry" and baseline != "retry":
                break
        page.wait_for_timeout(rng.randint(40, 120))
        page.mouse.up()

        if state == "gone":
            return "cleared"

        settled = _settle_gate(page, ctx)
        if verbose:
            print(f"      after hold {holds}: {settled}", flush=True)
        if settled == "gone":
            return "cleared"
        if settled == "dead" and holds >= min_holds:
            return _close_and_redraw(page, rng, submit)
        if settled in ("retry", "pressable", "completed"):
            page.wait_for_timeout(rng.randint(500, 1100))
            continue
    return "exhausted"


CHALLENGE_PROBE = r"""() => {
  if (document.querySelector('[aria-label="Press and hold"]')) return 'press_hold';
  const t = ((document.body && document.body.innerText) || '').toLowerCase();
  if (t.indexOf('verifying browser') !== -1) return 'pow';
  if (t.indexOf('hold the button') !== -1 || t.indexOf('press and hold') !== -1) return 'press_hold';
  if (t.indexOf('verify you') !== -1 && t.indexOf('human') !== -1) return 'human';
  for (const f of document.querySelectorAll('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]')) {
    const r = f.getBoundingClientRect();
    if (r.width > 40 && r.height > 40) return 'arkose_frame';
  }
  return null;
}"""


def challenge_kind(page) -> str | None:
    """What is on screen right now. Top document first, then each frame.

    Arkose v2 renders the gate in the top document (the frames are about:blank),
    so an iframe-src selector alone reports a visible challenge as absent.
    """
    for frame in page.frames:
        try:
            kind = frame.evaluate(CHALLENGE_PROBE)
        except Exception:
            continue
        if kind:
            return kind
    return None


def new_captcha_id() -> str:
    return str(uuid.uuid4())
