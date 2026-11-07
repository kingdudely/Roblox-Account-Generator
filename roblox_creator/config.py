"""Static endpoints and runtime settings.

Every value below was pulled live off www.roblox.com/CreateAccount and
apis.rbxcdn.com/captcha/v1/metadata. They rotate — re-derive if signup starts
failing wholesale (see README "re-deriving the constants").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import proxyurl

# --- endpoints ---------------------------------------------------------------

SIGNUP_URL = "https://www.roblox.com/account/signupredir"
SIGNUP_FALLBACK_URL = "https://www.roblox.com/CreateAccount"

AUTH_API = "https://auth.roblox.com"
SIGNUP_POST = f"{AUTH_API}/v2/signup"
AUTHENTICATED_USER = "https://users.roblox.com/v1/users/authenticated"

# Roblox fronts Arkose through a proxy of their own. The browser obtains an
# Arkose token, then redeems it here; the redeemed value is what /v2/signup eats.
CAPTCHA_METADATA = "https://apis.rbxcdn.com/captcha/v1/metadata"
CAPTCHA_REDEEM_SIGNUP = "https://captcha.roblox.com/v1/funcaptcha/signup/web"
BOT_ACTIVITY_TICKET = "https://apis.roblox.com/v1/arkose/bot-activity-ticket"

# Arkose Labs service host for Roblox.
ARKOSE_SERVICE_URL = "https://arkoselabs.roblox.com"

# from CAPTCHA_METADATA -> funCaptchaPublicKeys
ARKOSE_PUBLIC_KEY_SIGNUP = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
ARKOSE_PUBLIC_KEY_LOGIN = "476068BF-9607-4799-B53D-966BE98E2B81"

# --- DOM ids (stable across the current signup page) -------------------------

SEL_USERNAME = "#signup-username"
SEL_PASSWORD = "#signup-password"
SEL_MONTH = "#MonthDropdown"
SEL_DAY = "#DayDropdown"
SEL_YEAR = "#YearDropdown"
SEL_FEMALE = "#FemaleButton"
SEL_MALE = "#MaleButton"
SEL_SUBMIT = "#signup-button"

# Arkose mounts inside #signup-captcha; the iframe carries a random name attr,
# so match on src/host instead of name.
SEL_CAPTCHA_FRAME = 'iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]'
SEL_CAPTCHA_CONTAINER = "#signup-captcha"

COOKIE_NAME = ".ROBLOSECURITY"

# --- runtime settings --------------------------------------------------------


@dataclass
class Settings:
    count: int = 1
    captcha_mode: str = "auto"          # auto | vision | api | manual | off
    captcha_provider: str = "capsolver"  # capsolver | 2captcha
    captcha_key: str = ""
    # in-UI visual solving (no token minting): gemini API or a local VLM
    vision_provider: str = "gemini"     # gemini | local
    vision_model: str = ""              # gemini-2.5-flash | qwen2.5vl:3b ...
    vision_base_url: str = ""           # local: http://localhost:11434/v1
    vision_key: str = ""                # gemini key; ignored for local
    vision_keys: list[str] = field(default_factory=list)  # extra keys, one per project
    vision_debug_dir: Path | None = Path("debug/vision")
    vision_max_rounds: int = 12        # gfct reports waves=10; leave headroom
    headless: bool = False
    executable_path: str | None = None
    delay_min: float = 6.0
    delay_max: float = 18.0
    action_timeout: float = 45.0
    solve_timeout: float = 180.0
    out_dir: Path = Path("accounts")
    profile_root: Path = Path("profiles")
    proxies: list[str] = field(default_factory=list)
    reuse_context: bool = False
    dry_run: bool = False
    verify: bool = True
    solve_press_hold: bool = True
    px_attempts: int = 3

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        base = cls(
            captcha_key=os.getenv("CAPSOLVER_KEY") or os.getenv("TWOCAPTCHA_KEY") or "",
            vision_key=(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                        or os.getenv("VISION_API_KEY") or ""),
            vision_provider=os.getenv("VISION_PROVIDER") or "gemini",
            vision_model=os.getenv("VISION_MODEL") or "",
            vision_base_url=os.getenv("VISION_BASE_URL") or "",
        )
        for key, value in overrides.items():
            if value is not None and hasattr(base, key):
                setattr(base, key, value)
        if not base.captcha_key:
            base.captcha_key = os.getenv("CAPSOLVER_KEY") or os.getenv("TWOCAPTCHA_KEY") or ""
        if not base.vision_key:
            base.vision_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                               or os.getenv("VISION_API_KEY") or "")
        return base

    def vision_solver(self):
        """Build the in-UI solver, or None when nothing is configured."""
        provider = self.vision_provider or "gemini"
        if provider == "gemini" and not (self.vision_key or self.vision_keys):
            return None
        from .vision import build_vision_solver

        return build_vision_solver(
            provider,
            self.vision_key,
            model=self.vision_model,
            base_url=self.vision_base_url,
            keys=self.vision_keys,
        )

    @property
    def effective_captcha_mode(self) -> str:
        if self.captcha_mode == "auto":
            if self.vision_key or self.vision_keys or self.vision_provider in (
                    "local", "ollama", "vllm", "stub", "file", "onnx"):
                return "vision"
            return "api" if self.captcha_key else "manual"
        return self.captcha_mode

    def pick_proxy(self) -> str | None:
        if not self.proxies:
            return None
        import random

        return random.choice(self.proxies)


def parse_proxy(raw: str) -> str:
    """Normalise one proxy line to an explicit ``socks5://user:pass@host:port``.

    Accepts every shape the vendors ship — ``socks5://user:pass@host:port``,
    ``user:pass@host:port``, ``host:port:user:pass``, ``host:port`` — and always
    emits a scheme, because the upstream ports are SOCKS5-only and a line
    defaulted to ``http://`` fails against them.

    An explicit scheme on the input is honoured, never upgraded: if a list says
    ``http://`` it stays HTTP.
    """
    return proxyurl.parse(raw).url()


def load_proxies(path: Path) -> list[str]:
    """Read a proxy file, tolerating CRLF and every line shape in the wild."""
    return proxyurl.load(path)
