"""Playwright session wrapper — real Chrome, clean fingerprint, isolated profile."""

from __future__ import annotations

import shutil
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .config import Settings

# Patches that remove the obvious automation tells. Nothing exotic — a real
# Chrome profile with a real UA does most of the work; this closes the gaps.
STEALTH = r"""
(() => {
  // navigator.webdriver
  try {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  } catch (e) {}

  // plugins / mimeTypes on a fresh profile look empty
  try {
    const pluginData = [1, 2, 3, 4, 5].map((i) => ({
      name: "Chrome PDF Plugin " + i,
      filename: "internal-pdf-viewer",
      description: "Portable Document Format",
      length: 1,
    }));
    Object.defineProperty(navigator, "plugins", { get: () => pluginData });
    Object.defineProperty(navigator, "mimeTypes", { get: () => [{ type: "application/pdf" }] });
  } catch (e) {}

  // window.chrome
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};

  // permissions query mismatch
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (params) =>
      params && params.name === "notifications"
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(params);
  }

  // headless WebGL renderer
  try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return "Intel Inc.";
      if (p === 37446) return "Intel Iris OpenGL Engine";
      return getParam.apply(this, arguments);
    };
  } catch (e) {}

  // languages
  try {
    Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
  } catch (e) {}
})();
"""

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-popup-blocking",
    "--disable-infobars",
    "--no-service-autorun",
    "--password-store=basic",
]

DEFAULT_IGNORE = ["--enable-automation", "--no-sandbox"]

# Desktop shapes worth harvesting from.
#
# The payload corpus is only worth building if the entries differ. A single fixed
# context (the default 1440x900 / DPR 1 / light) yields N copies of one device,
# which is the clustering problem the corpus exists to avoid. These are common
# real resolutions; `viewport` is the inner (document) size, which in a real
# browser is always shorter than `screen` by the window chrome — ~90-130 px — and
# that gap is exactly the kind of detail a replay has to get right.
DESKTOP_PROFILES = [
    {"screen": {"width": 1920, "height": 1080},
     "viewport": {"width": 1920, "height": 953}, "device_scale_factor": 1,
     "color_scheme": "light"},
    {"screen": {"width": 1920, "height": 1080},
     "viewport": {"width": 1536, "height": 864}, "device_scale_factor": 1.25,
     "color_scheme": "light"},
    {"screen": {"width": 2560, "height": 1440},
     "viewport": {"width": 2560, "height": 1303}, "device_scale_factor": 1,
     "color_scheme": "dark"},
    {"screen": {"width": 1366, "height": 768},
     "viewport": {"width": 1366, "height": 641}, "device_scale_factor": 1,
     "color_scheme": "light"},
    {"screen": {"width": 1600, "height": 900},
     "viewport": {"width": 1600, "height": 773}, "device_scale_factor": 1,
     "color_scheme": "dark"},
    {"screen": {"width": 1440, "height": 900},
     "viewport": {"width": 1440, "height": 773}, "device_scale_factor": 2,
     "color_scheme": "light"},
    {"screen": {"width": 1280, "height": 1024},
     "viewport": {"width": 1280, "height": 897}, "device_scale_factor": 1,
     "color_scheme": "light"},
    {"screen": {"width": 3840, "height": 2160},
     "viewport": {"width": 1920, "height": 1017}, "device_scale_factor": 2,
     "color_scheme": "dark"},
]


class BrowserSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Playwright | None = None
        self._contexts: list[BrowserContext] = []
        self._bridge = None
        self._bridge_source: str | None = None

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        for ctx in self._contexts:
            try:
                ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # -- context construction -------------------------------------------------

    def _browser_proxy(self, proxy: str | None) -> str | None:
        """Chromium cannot do SOCKS5 auth, so tunnel those through a local bridge."""
        if not proxy:
            return None
        if not proxy.lower().startswith("socks"):
            return proxy
        from .socks_bridge import SocksBridge

        if self._bridge is None or self._bridge_source != proxy:
            if self._bridge is not None:
                self._bridge.stop()
            self._bridge = SocksBridge(proxy)
            self._bridge_source = proxy
        return self._bridge.start()

    def _launch_kwargs(self, proxy: str | None,
                       profile: dict | None = None) -> dict:
        kwargs = {
            "headless": self.settings.headless,
            "args": CHROME_ARGS,
            "ignore_default_args": DEFAULT_IGNORE,
            "locale": "en-US",
            "viewport": {"width": 1440, "height": 900},
            "device_scale_factor": 1,
            "color_scheme": "light",
        }
        if profile:
            kwargs.update(profile)
        if self.settings.executable_path:
            kwargs["executable_path"] = self.settings.executable_path
        browser_proxy = self._browser_proxy(proxy)
        if browser_proxy:
            kwargs["proxy"] = {"server": browser_proxy}
        return kwargs

    def new_context(self, profile_dir: Path | None = None, proxy: str | None = None,
                    timezone: str | None = None,
                    profile: dict | None = None) -> BrowserContext:
        if self._pw is None:
            raise RuntimeError("session not started")
        kwargs = self._launch_kwargs(proxy, profile)
        # timezone must match the egress IP; a random one is worse than none
        if timezone:
            kwargs["timezone_id"] = timezone

        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                ctx = self._pw.chromium.launch_persistent_context(
                    str(profile_dir), channel="chrome", **kwargs
                )
            except Exception:
                ctx = self._pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        else:
            try:
                browser = self._pw.chromium.launch(channel="chrome", **_launch_only(kwargs))
                ctx = browser.new_context(**_context_only(kwargs))
            except Exception:
                browser = self._pw.chromium.launch(**_launch_only(kwargs))
                ctx = browser.new_context(**_context_only(kwargs))

        ctx.add_init_script(STEALTH)
        self._contexts.append(ctx)
        return ctx

    @staticmethod
    def new_page(ctx: BrowserContext) -> Page:
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        return page

    @staticmethod
    def wipe_profile(profile_dir: Path) -> None:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)


def _launch_only(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k not in ("user_agent", "viewport", "screen",
                                                         "locale", "timezone_id",
                                                         "device_scale_factor",
                                                         "color_scheme")}


def _context_only(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k in ("user_agent", "viewport", "screen",
                                                     "locale", "timezone_id",
                                                     "device_scale_factor", "color_scheme")}
