"""Make the browser report one coherent Windows device, not a Linux box wearing a mask.

Measured 2026-11-04: the automated browser reported

    userAgent  ... X11; Linux x86_64 ...
    platform   Linux x86_64
    webgl      Intel Inc. / Intel Iris OpenGL Engine      (a macOS string, on Linux)
    timezone   America/New_York                            (unrelated to the exit)

while every BDA we minted claimed Windows Chrome on a discrete GPU, and every
exit was US residential. That contradiction is the kind of thing risk engines are
built to notice, and it is the one variable we have not yet tested: 0 passive
challenges in every run so far.

This module samples a single coherent device — GPU with its real ANGLE strings,
screen, core count, memory, connection — and returns the context kwargs plus an
init script that makes `navigator`, `screen` and WebGL agree with it.

    from roblox_creator import device_profile
    device = device_profile.sample()
    ctx = session.new_context(proxy=raw, profile=device_profile.context(device),
                              timezone=device_profile.timezone_for(raw))
    ctx.add_init_script(device_profile.init_script(device))
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

DEVICES = Path(__file__).resolve().parents[1] / ".venv" / "lib" / \
    "python3.13" / "site-packages" / "arkose_solver" / "devices.json"

# Chromium here reports a recent major; keep the UA and the client hints on it
CHROME_MAJOR = "152"
WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)


@dataclass
class Device:
    gpu_vendor: str = "Google Inc. (Intel)"
    gpu_renderer: str = ("ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) "
                         "Direct3D11 vs_5_0 ps_5_0, D3D11)")
    screen_w: int = 1920
    screen_h: int = 1080
    avail_w: int = 1920
    avail_h: int = 1032
    dpr: float = 1.0
    color_depth: int = 32
    cores: int = 8
    memory: int = 8
    downlink: float = 10.0
    rtt: int = 50
    chrome: str = CHROME_MAJOR
    extra: dict = field(default_factory=dict)

    @property
    def ua(self) -> str:
        return WINDOWS_UA.format(major=self.chrome)

    def viewport(self) -> dict:
        """A window smaller than the screen — a maximised window is a fleet tell."""
        width = int(self.screen_w - random.choice((0, 16, 32, 48, 64)))
        height = int(self.screen_h - random.choice((80, 96, 112, 120, 132)))
        return {"width": width, "height": height}


def _weighted(entries: list[dict], rng: random.Random) -> dict:
    total = sum(e.get("weight", 1) for e in entries)
    pick = rng.uniform(0, total)
    upto = 0.0
    for entry in entries:
        upto += entry.get("weight", 1)
        if pick <= upto:
            return entry
    return entries[-1]


def _corpus() -> dict:
    if not DEVICES.exists():
        return {}
    try:
        return json.loads(DEVICES.read_text())
    except Exception:
        return {}


def sample(rng: random.Random | None = None) -> Device:
    """One device, with every field consistent with the same machine."""
    rng = rng or random.Random()
    corpus = _corpus()
    if not corpus:
        return Device(chrome=CHROME_MAJOR)
    gpu = _weighted(corpus["gpus"], rng)
    screen = _weighted(corpus["screens"], rng)
    cores = _weighted(corpus["cores"], rng)
    memory = _weighted(corpus["memory"], rng)
    network = _weighted(corpus["network"], rng)
    return Device(
        gpu_vendor=gpu.get("glVendor", "Google Inc. (Intel)"),
        gpu_renderer=gpu.get("glRenderer", Device.gpu_renderer),
        screen_w=int(screen.get("width", 1920)),
        screen_h=int(screen.get("height", 1080)),
        avail_w=int(screen.get("availWidth", 1920)),
        avail_h=int(screen.get("availHeight", 1032)),
        dpr=float(screen.get("dpr", 1)),
        color_depth=int(screen.get("colorDepth", 32)),
        cores=int(cores.get("value", 8)),
        memory=int(memory.get("value", 8)),
        downlink=float(network.get("downlink", 10)),
        rtt=int(network.get("rtt", 50)),
        chrome=CHROME_MAJOR,
        extra={"gpu_model": gpu.get("model", ""), "vendor": gpu.get("vendor", "")},
    )


def context(device: Device) -> dict:
    """kwargs for `BrowserSession.new_context(profile=...)`."""
    view = device.viewport()
    return {
        "user_agent": device.ua,
        "viewport": view,
        "screen": {"width": device.screen_w, "height": device.screen_h},
        "device_scale_factor": device.dpr if device.dpr in (1, 1.25, 1.5, 2) else 1,
    }


def timezone_for(proxy: str | None) -> str | None:
    """Timezone that matches the exit IP; None lets Chrome decide."""
    try:
        from . import egress
        return egress.timezone_for(proxy)
    except Exception:
        return None


INIT_TEMPLATE = r"""
(() => {
  const D = %(device)s;
  const define = (obj, prop, get) => {
    try { Object.defineProperty(obj, prop, { get, configurable: true }); } catch (e) {}
  };

  define(navigator, 'platform', () => 'Win32');
  define(navigator, 'vendor', () => 'Google Inc.');
  define(navigator, 'hardwareConcurrency', () => D.cores);
  define(navigator, 'deviceMemory', () => D.memory);
  define(navigator, 'maxTouchPoints', () => 0);
  define(navigator, 'webdriver', () => undefined);

  // Client Hints: the one probe that catches a UA string change on its own
  const brands = [
    { brand: 'Chromium', version: D.chrome },
    { brand: 'Google Chrome', version: D.chrome },
    { brand: 'Not/A)Brand', version: '99' },
  ];
  const uaData = {
    brands,
    mobile: false,
    platform: 'Windows',
    getHighEntropyValues: async (hints) => {
      const out = { brands, mobile: false, platform: 'Windows' };
      if (hints.includes('platformVersion')) out.platformVersion = '10.0.0';
      if (hints.includes('architecture')) out.architecture = 'x86';
      if (hints.includes('bitness')) out.bitness = '64';
      if (hints.includes('model')) out.model = '';
      if (hints.includes('uaFullVersion')) out.uaFullVersion = D.chrome + '.0.0.0';
      if (hints.includes('fullVersionList')) out.fullVersionList = brands;
      return out;
    },
    toJSON() { return { brands, mobile: false, platform: 'Windows' }; },
  };
  if (D.include_uadata) define(navigator, 'userAgentData', () => uaData);

  // Screen geometry has to agree with the window and with the BDA
  define(screen, 'width', () => D.screen_w);
  define(screen, 'height', () => D.screen_h);
  define(screen, 'availWidth', () => D.avail_w);
  define(screen, 'availHeight', () => D.avail_h);
  define(screen, 'colorDepth', () => D.color_depth);
  define(screen, 'pixelDepth', () => D.color_depth);
  define(window, 'devicePixelRatio', () => D.dpr);

  const patchGL = (proto) => {
    if (!proto) return;
    const original = proto.getParameter;
    proto.getParameter = function (param) {
      if (param === 37445) return D.gpu_vendor;    // UNMASKED_VENDOR_WEBGL
      if (param === 37446) return D.gpu_renderer;  // UNMASKED_RENDERER_WEBGL
      return original.apply(this, arguments);
    };
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // connection, so the network report matches the device sample
  if (navigator.connection) {
    define(navigator.connection, 'rtt', () => D.rtt);
    define(navigator.connection, 'downlink', () => D.downlink);
  }
})();
"""


def init_script(device: Device, include_uadata: bool = True) -> str:
    payload = {
        "include_uadata": include_uadata,
        "cores": device.cores,
        "memory": device.memory,
        "screen_w": device.screen_w,
        "screen_h": device.screen_h,
        "avail_w": device.avail_w,
        "avail_h": device.avail_h,
        "color_depth": device.color_depth,
        "dpr": device.dpr,
        "gpu_vendor": device.gpu_vendor,
        "gpu_renderer": device.gpu_renderer,
        "chrome": device.chrome,
        "rtt": device.rtt,
        "downlink": device.downlink,
    }
    return INIT_TEMPLATE % {"device": json.dumps(payload)}


def describe(device: Device) -> str:
    return (f"{device.screen_w}x{device.screen_h}@{device.dpr} "
            f"cores={device.cores} mem={device.memory} "
            f"gpu={device.extra.get('vendor', '?')} "
            f"ua=Win{device.chrome}")
