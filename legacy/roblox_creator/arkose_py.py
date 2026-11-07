"""Pure-Python Arkose client — gt2 → (suppressed token | challenge) → answer.

No browser, no Playwright, no VLM required for the happy path. The whole protocol
is four calls:

    GET  /v2/<sitekey>/api.js          -> capi_version + enforcement_hash
    POST /fc/gt2/public_key/<sitekey>  -> token  (with a valid BDA)
                                         token carries sup=1 when Arkose decides
                                         no puzzle is needed
    POST /fc/gfct/                     -> challenge: image urls, waves, dapib
    POST /fc/ca/                       -> {solved: true}  (guess + tguess + bio)

The BDA is the fingerprint blob Arkose validates. It is AES-CBC encrypted with
`<user-agent><6h-aligned-timestamp>` and base64'd, and it encodes 80+ probe
values. Get it wrong and gt2 answers `400 DENIED ACCESS` — which is exactly what
the public `arkose-solver` package hits.

Fingerprints live in data/fingerprints.json (one entry per real device shape).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from .dapib import transform

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROBLOX_SITE_KEY = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
ROBLOX_API = "https://arkoselabs.roblox.com"
ROBLOX_SITE = "https://www.roblox.com"

FINGERPRINT_FILE = Path(__file__).resolve().parent.parent / "data" / "fingerprints.json"


# --------------------------------------------------------------------------- hashing

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF
C1 = 0x87C37B91114253D5
C2 = 0x4CF5AD432745937F


def _rotl64(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & M64


def _fmix64(k: int) -> int:
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & M64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & M64
    k ^= k >> 33
    return k


def murmur3_x64_128(data: bytes, seed: int = 0) -> tuple[int, int]:
    """MurmurHash3 x64 128-bit, as used by Arkose's x64hash128 helper."""
    h1 = h2 = seed & M64
    nblocks = len(data) // 16
    for i in range(nblocks):
        k1, k2 = struct.unpack_from("<QQ", data, i * 16)
        k1 = (k1 * C1) & M64
        k1 = _rotl64(k1, 31)
        k1 = (k1 * C2) & M64
        h1 ^= k1
        h1 = _rotl64(h1, 27)
        h1 = (h1 + h2) & M64
        h1 = (h1 * 5 + 0x52DCE729) & M64

        k2 = (k2 * C2) & M64
        k2 = _rotl64(k2, 33)
        k2 = (k2 * C1) & M64
        h2 ^= k2
        h2 = _rotl64(h2, 31)
        h2 = (h2 + h1) & M64
        h2 = (h2 * 5 + 0x38495AB5) & M64

    tail = data[nblocks * 16:]
    k1 = k2 = 0
    for i in range(len(tail) - 1, 7, -1):
        k2 ^= tail[i] << ((i - 8) * 8)
    if len(tail) > 8:
        k2 = (k2 * C2) & M64
        k2 = _rotl64(k2, 33)
        k2 = (k2 * C1) & M64
        h2 ^= k2
    for i in range(min(len(tail), 8) - 1, -1, -1):
        k1 ^= tail[i] << (i * 8)
    if len(tail) > 0:
        k1 = (k1 * C1) & M64
        k1 = _rotl64(k1, 31)
        k1 = (k1 * C2) & M64
        h1 ^= k1

    h1 ^= len(data)
    h2 ^= len(data)
    h1 = (h1 + h2) & M64
    h2 = (h2 + h1) & M64
    h1 = _fmix64(h1)
    h2 = _fmix64(h2)
    h1 = (h1 + h2) & M64
    h2 = (h2 + h1) & M64
    return h1, h2


def x64hash128(content: str | bytes, seed: int = 0) -> str:
    if isinstance(content, str):
        content = content.encode()
    h1, h2 = murmur3_x64_128(content, seed)
    return f"{h1:016x}{h2:016x}"


def md5hash(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode()
    return hashlib.md5(content).hexdigest()


# --------------------------------------------------------------------------- crypto


def _evp_kdf(password: bytes, salt: bytes, key_size: int = 32, iv_size: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey with MD5, one iteration — what CryptoJS uses."""
    derived = b""
    block = b""
    while len(derived) < key_size + iv_size:
        block = hashlib.md5(block + password + salt).digest()
        derived += block
    return derived[:key_size], derived[key_size:key_size + iv_size]


def aes_encrypt(content: str, password: str) -> str:
    salt = os.urandom(8)
    key, iv = _evp_kdf(password.encode(), salt)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(content.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).encryptor()
    body = encryptor.update(padded) + encryptor.finalize()
    return json.dumps({
        "ct": base64.b64encode(body).decode(),
        "iv": iv.hex(),
        "s": salt.hex(),
    })


def aes_decrypt(encrypted: str, password: str) -> str:
    data = json.loads(encrypted)
    ciphertext = base64.b64decode(data["ct"])
    iv = bytes.fromhex(data["iv"])
    salt = bytes.fromhex(data["s"])
    key, _ = _evp_kdf(password.encode(), salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


# --------------------------------------------------------------------------- headers


def _b64(payload: Any) -> str:
    """base64 of compact JSON — how `bio` and `ecdata` travel in /fc/ca/."""
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def short_esync() -> str:
    now = time.time()
    return str(int(now - (now % 21600)))


def x_ark_esync() -> str:
    stamp = str(int(time.time() * 1000)).zfill(13)
    return f"{stamp[:7]}00{stamp[7:]}"


def x_newrelic_timestamp() -> str:
    return str(int(time.time() * 100000))


def _base_headers(ua: str, sec_ch_ua: str, host: str, referer: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "connection": "keep-alive",
        "host": host,
        "referer": referer,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-site",
        "user-agent": ua,
    }


def _post_headers(ua: str, sec_ch_ua: str, host: str, referer: str, origin: str) -> dict[str, str]:
    headers = _base_headers(ua, sec_ch_ua, host, referer)
    headers.update({
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": origin,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-newrelic-timestamp": x_newrelic_timestamp(),
        "x-requested-with": "XMLHttpRequest",
    })
    return headers


# --------------------------------------------------------------------------- models


@dataclass
class Fingerprint:
    fe: list[str]
    language: str
    user_agent: str
    webgl: dict[str, Any]
    wh: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Fingerprint":
        return cls(
            fe=list(raw["fe"]),
            language=raw.get("language", "en-US"),
            user_agent=raw["user-agent"],
            webgl=raw.get("webgl", {}),
            wh=raw["wh"],
        )

    @property
    def chrome_version(self) -> str:
        for token in self.user_agent.split():
            if token.startswith("Chrome/"):
                return token.split("/")[1].split(".")[0]
        return "132"

    @property
    def sec_ch_ua(self) -> str:
        v = self.chrome_version
        return f'"Not A(Brand";v="8", "Chromium";v="{v}", "Google Chrome";v="{v}"'


@dataclass
class Token:
    raw: str
    game_token: str
    region: str = ""
    at: int = 40
    ag: int = 0
    cdn_url: str = ""
    surl: str = ""
    smurl: str = ""
    sup: bool = False

    @classmethod
    def parse(cls, raw: str) -> "Token":
        parts = raw.split("|")
        fields: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                fields[key] = unquote(value)
        return cls(
            raw=raw,
            game_token=parts[0],
            region=fields.get("r", ""),
            at=int(fields.get("at") or 40),
            ag=int(fields.get("ag") or 0),
            cdn_url=fields.get("cdn_url", ""),
            surl=fields.get("surl", ""),
            smurl=fields.get("smurl", ""),
            sup=("sup=1" in raw),
        )


@dataclass
class Challenge:
    token: Token
    challenge_id: str
    images: list[str]
    waves: int = 0
    game_type: int = 0
    variant: str = ""
    dapib_url: str | None = None
    compatibility_mode: bool = False


class ArkoseError(RuntimeError):
    pass


class Denied(ArkoseError):
    """gt2 rejected the fingerprint (400 DENIED ACCESS)."""


# --------------------------------------------------------------------------- BDA

WEBGL_DEFAULTS: dict[str, str] = {
    "webgl_extensions": "ANGLE_instanced_arrays;EXT_blend_minmax;EXT_color_buffer_half_float;EXT_shader_texture_lod;EXT_texture_filter_anisotropic;EXT_sRGB;OES_element_index_uint;OES_fbo_render_mipmap;OES_standard_derivatives;OES_texture_float;OES_texture_float_linear;OES_texture_half_float;OES_texture_half_float_linear;OES_vertex_array_object;WEBGL_color_buffer_float;WEBGL_compressed_texture_astc;WEBGL_compressed_texture_etc;WEBGL_compressed_texture_etc1;WEBGL_debug_renderer_info;WEBGL_debug_shaders;WEBGL_depth_texture;WEBGL_lose_context;WEBGL_multi_draw",
    "webgl_extensions_hash": "3c34b0dc0e73d2c83a91218a631a242d",
    "webgl_renderer": "WebKit WebGL",
    "webgl_vendor": "WebKit",
    "webgl_version": "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
    "webgl_shading_language_version": "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
    "webgl_aliased_line_width_range": "[1, 10]",
    "webgl_aliased_point_size_range": "[1, 2047]",
    "webgl_antialiasing": "yes",
    "webgl_bits": "8,8,24,8,8,0",
    "webgl_max_params": "16,192,4096,1024,32768,32,4096,31,16,32,1024",
    "webgl_max_viewport_dims": "[32768, 32768]",
    "webgl_unmasked_vendor": "Qualcomm",
    "webgl_unmasked_renderer": "Adreno (TM) 640",
    "webgl_vsf_params": "23,127,127,10,15,15,10,15,15",
    "webgl_vsi_params": "0,31,30,0,31,30,0,31,30",
    "webgl_fsf_params": "23,127,127,10,15,15,10,15,15",
    "webgl_fsi_params": "0,31,30,0,31,30,0,31,30",
}


def _webgl_block(fp: Fingerprint) -> list[dict[str, Any]]:
    block: list[dict[str, Any]] = []
    for key, default in WEBGL_DEFAULTS.items():
        block.append({"key": key, "value": fp.webgl.get(key, default)})
    block.append({"key": "webgl_hash_webgl", "value": x64hash128(_hashable_webgl(block))})
    return block


def _hashable_webgl(block: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in block:
        parts.append(str(item["key"]))
        parts.append(str(item["value"]))
    return ",".join(parts) + ",webgl_hash_webgl,"


def _hashable_fe(fe: list[str]) -> str:
    return ";".join(item.split(":")[1] for item in fe)


def build_bda(fp: Fingerprint, site_key: str, site_url: str, api_url: str,
              capi_version: str, enforcement_hash: str,
              timezone_offset: int | None = None,
              languages: str | None = None) -> str:
    """Assemble + encrypt the fingerprint blob gt2 expects."""
    t0 = time.time()
    enhanced = build_enhanced_fp(fp, site_url, api_url, capi_version,
                                 enforcement_hash, t0=t0, languages=languages)
    fe = list(fp.fe)
    if timezone_offset is not None:
        for i, entry in enumerate(fe):
            if entry.startswith("TO:"):
                fe[i] = f"TO:{timezone_offset}"
                break


def build_enhanced_fp(fp: Fingerprint, site_url: str, api_url: str,
                      capi_version: str, enforcement_hash: str,
                      t0: float | None = None,
                      languages: str | None = None) -> list[dict[str, Any]]:
    """The `enhanced_fp` array: WebGL block, codecs, CSS probes, hashes.

    Arkose uses its presence as a signal about how much it knows about the
    client. A short array (or none) makes the session look like a stripped
    HTTP client, and Arkose answers by *suppressing* the challenge — which
    Roblox then refuses, because for signup it wants a real solve. So this
    array is not decoration: it decides whether a solvable challenge is
    issued at all.
    """
    if t0 is None:
        t0 = time.time()
    webgl = fp.webgl
    enhanced: list[dict[str, Any]] = _webgl_block(fp)
    enhanced.extend([
        {"key": "user_agent_data_brands", "value": webgl.get("user_agent_data_brands", "Not A(Brand,Chromium,Google Chrome")},
        {"key": "user_agent_data_mobile", "value": webgl.get("user_agent_data_mobile", True)},
        {"key": "navigator_connection_downlink", "value": int(float(webgl.get("navigator_connection_downlink", 10)))},
        {"key": "navigator_connection_downlink_max", "value": int(float(webgl.get("navigator_connection_downlink_max", -1)))},
        {"key": "network_info_rtt", "value": webgl.get("network_info_rtt", 100)},
        {"key": "network_info_save_data", "value": webgl.get("network_info_save_data", False)},
        {"key": "network_info_rtt_type", "value": webgl.get("network_info_rtt_type", "wifi")},
        {"key": "screen_pixel_depth", "value": webgl.get("screen_pixel_depth", "24")},
        {"key": "navigator_device_memory", "value": webgl.get("navigator_device_memory", "4")},
        {"key": "navigator_pdf_viewer_enabled", "value": webgl.get("navigator_pdf_viewer_enabled", False)},
        {"key": "navigator_languages", "value": languages or webgl.get("navigator_languages", "en-US,en")},
        {"key": "window_inner_width", "value": webgl.get("window_inner_width", "0")},
        {"key": "window_inner_height", "value": webgl.get("window_inner_height", "0")},
        {"key": "window_outer_width", "value": webgl.get("window_outer_width", "1067")},
        {"key": "window_outer_height", "value": webgl.get("window_outer_height", "480")},
        {"key": "browser_detection_firefox", "value": False},
        {"key": "browser_detection_brave", "value": False},
        {"key": "browser_api_checks", "value": webgl.get("browser_api_checks", [
            "permission_status: true", "eye_dropper: false", "audio_data: true",
            "writable_stream: true", "css_style_rule: true", "navigator_ua: true",
            "barcode_detector: true", "display_names: true", "contacts_manager: false",
            "svg_discard_element: false", "usb: defined", "media_device: defined",
            "playback_quality: true",
        ])},
        {"key": "browser_object_checks", "value": webgl.get("browser_object_checks")},
        {"key": "29s83ih9", "value": md5hash("false") + "⁣"},
        {"key": "audio_codecs", "value": webgl.get("audio_codecs")},
        {"key": "audio_codecs_extended_hash", "value": webgl.get("audio_codecs_extended_hash")},
        {"key": "video_codecs", "value": webgl.get("video_codecs")},
        {"key": "video_codecs_extended_hash", "value": webgl.get("video_codecs_extended_hash")},
        {"key": "media_query_dark_mode", "value": webgl.get("media_query_dark_mode", False)},
        {"key": "css_media_queries", "value": webgl.get("css_media_queries", "0")},
        {"key": "css_color_gamut", "value": webgl.get("css_color_gamut", "srgb")},
        {"key": "css_contrast", "value": webgl.get("css_contrast", "no-preference")},
        {"key": "css_monochrome", "value": webgl.get("css_monochrome", False)},
        {"key": "css_pointer", "value": webgl.get("css_pointer", "coarse")},
        {"key": "css_grid_support", "value": webgl.get("css_grid_support", False)},
        {"key": "headless_browser_phantom", "value": False},
        {"key": "headless_browser_selenium", "value": False},
        {"key": "headless_browser_nightmare_js", "value": False},
        {"key": "headless_browser_generic", "value": 4},
        {"key": "1l2l5234ar2", "value": str(int(t0 * 1000)) + "⁣"},
        {"key": "document__referrer", "value": site_url.rstrip("/")},
        {"key": "window__ancestor_origins", "value": [site_url, site_url]},
        {"key": "window__tree_index", "value": [0, 0]},
        {"key": "window__tree_structure", "value": "[[[]]]"},
        {"key": "window__location_href", "value":
            f"{api_url}/v2/{capi_version}/enforcement.{enforcement_hash}.html"},
        {"key": "client_config__sitedata_location_href", "value": f"{site_url}/arkose/iframe"},
        {"key": "client_config__language", "value": None},
        {"key": "client_config__surl", "value": api_url},
        {"key": "c8480e29a", "value": md5hash(api_url) + "⁢"},
        {"key": "client_config__triggered_inline", "value": False},
        {"key": "mobile_sdk__is_sdk", "value": False},
        {"key": "audio_fingerprint", "value": webgl.get("audio_fingerprint")},
        {"key": "navigator_battery_charging", "value": webgl.get("navigator_battery_charging", True)},
        {"key": "media_device_kinds", "value": webgl.get("media_device_kinds")},
        {"key": "media_devices_hash", "value": webgl.get("media_devices_hash")},
        {"key": "navigator_permissions_hash", "value": webgl.get("navigator_permissions_hash")},
        {"key": "math_fingerprint", "value": webgl.get("math_fingerprint")},
        {"key": "supported_math_functions", "value": webgl.get("supported_math_functions")},
        {"key": "screen_orientation", "value": "landscape-primary"},
        {"key": "rtc_peer_connection", "value": int(float(webgl.get("rtc_peer_connection", 5)))},
        {"key": "4b4b269e68", "value": webgl.get("4b4b269e68")},
        {"key": "6a62b2a558", "value": enforcement_hash},
        {"key": "is_keyless", "value": False},
        {"key": "c2d2015", "value": "29d13b1af8803cb86c2697345d7ea9eb"},
        {"key": "43f2d94", "value": False},
        {"key": "20c15922", "value": True},
        {"key": "4f59ca8", "value": None},
        {"key": "3ea719a", "value": {"supported": True, "formats": ["HDR10", "HLG"], "isHDR": False}},
        {"key": "05d3d24", "value": "d53459d339bbc3faafe9c7c19c2105ca"},
        {"key": "speech_default_voice", "value": webgl.get("speech_default_voice", "Bulgarian Bulgaria || bg_BG")},
        {"key": "speech_voices_hash", "value": webgl.get("speech_voices_hash", "b22257aa2722771cf60dbc3aa7b0e2fe")},
        {"key": "83eb055", "value": False},
        {"key": "4ca87df3d1", "value": "Ow=="},
        {"key": "867e25e5d4", "value": "Ow=="},
        {"key": "d4a306884c", "value": "Ow=="},
    ])
    return repair_geometry(enhanced, fp)


# --------------------------------------------------------------------------- #
# coherence repair
# --------------------------------------------------------------------------- #

# The captured fingerprints carry window geometry that cannot exist in a live
# page: `window_inner_width`/`window_inner_height` are 0, and `window_outer` is
# portrait (384x715) while `screen` is landscape (854x384). Those captures were
# taken in a context where the document had no layout viewport, so replaying them
# verbatim advertises an impossible device. Arkose does not need to be clever to
# notice `innerWidth === 0`.
#
# Rather than invent fresh numbers, this derives geometry from the one anchor a
# real device always reports consistently -- `screen` in `fe` -- and keeps every
# other captured value untouched.

_MOBILE_CHROME_PX = 56      # address bar + system bars, in CSS pixels


def _fe_map(fe: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in fe:
        if ":" in entry:
            key, value = entry.split(":", 1)
            out[key] = value
    return out


def repair_geometry(entries: list[dict[str, Any]],
                    fp: Fingerprint) -> list[dict[str, Any]]:
    """Make window/screen/language values agree with each other.

    The captured geometry violates invariants that hold in every real browser:

        inner_width <= screen_width      outer_width  <= screen_width
        inner_height <= screen_height    outer_height <= screen_height
        inner_width  <= outer_width      inner_height <= outer_height

    e.g. `inner 0x0` with `outer 384x715` on a `854x384` landscape screen. So the
    four window values are **recomputed** from the screen anchor instead of being
    trusted; everything else the device reported is left untouched.
    """
    # Desktop captures come from a real browser and are already self-consistent;
    # only the mobile captures carry the zero/portrait geometry. Applying mobile
    # chrome arithmetic to a desktop fingerprint would break it, so scope it.
    if "Android" not in fp.user_agent and "iPhone" not in fp.user_agent:
        return entries

    fe = _fe_map(fp.fe)
    try:
        screen_w, screen_h = (int(x) for x in fe.get("S", "0,0").split(","))
    except ValueError:
        return entries
    if not (screen_w and screen_h):
        return entries

    # chrome height varies a little per device so accounts are not identical
    jitter = sum(ord(c) for c in str(fp.wh)) % 9
    chrome = _MOBILE_CHROME_PX + jitter

    inner_w = screen_w
    inner_h = max(screen_h - chrome, 1)
    outer_w = screen_w
    outer_h = screen_h

    language = fe.get("L") or "en-US"
    base_language = language.split("-")[0]
    languages = f"{language},{base_language}" if "-" in language else language

    geometry = {
        "window_inner_width": inner_w,
        "window_inner_height": inner_h,
        "window_outer_width": outer_w,
        "window_outer_height": outer_h,
    }

    for entry in entries:
        key = entry.get("key")
        if key in geometry:
            entry["value"] = geometry[key]
        elif key == "navigator_languages":
            # a th-TH fingerprint advertising en-US,en is a contradiction
            if not str(entry.get("value")).startswith(language):
                entry["value"] = languages

    return entries


def pick_fingerprint(pool: list[dict[str, Any]], *,
                     country: str = "US",
                     languages: tuple[str, ...] = ("en-US", "en-GB", "en"),
                     tz_range: tuple[int, int] = (-600, -240)) -> dict[str, Any]:
    """Choose a fingerprint whose locale and timezone fit the exit.

    The pool is a world tour (th-TH/-420, ar-AE/-180, es-ES/+360 …) but every
    proxy is a US residential exit. Sending a Riyadh-locale phone from Virginia
    is a contradiction the risk engine gets for free.
    """
    scored: list[tuple[int, dict[str, Any]]] = []
    for raw in pool:
        fe = _fe_map(raw.get("fe") or [])
        language = fe.get("L", "")
        try:
            tz = int(fe.get("TO", "9999"))
        except ValueError:
            tz = 9999
        score = 0
        if language in languages:
            score += 2
        elif language.split("-")[0] == "en":
            score += 1
        if tz_range[0] <= tz <= tz_range[1]:
            score += 2
        if raw.get("language") == language:
            score += 1
        scored.append((score, raw))
    if not scored:
        return random.choice(pool)
    best = max(s for s, _ in scored)
    return random.choice([raw for s, raw in scored if s == best])



def build_bda(fp: Fingerprint, site_key: str, site_url: str, api_url: str,
              capi_version: str, enforcement_hash: str,
              timezone_offset: int | None = None,
              languages: str | None = None) -> str:
    """Assemble + encrypt the retired AES-CBC `bda` blob.

    Kept for older Arkose deployments. The live Roblox site sends `c` instead —
    see `roblox_creator/bda.py`.
    """
    t0 = time.time()
    enhanced = build_enhanced_fp(fp, site_url, api_url, capi_version,
                                 enforcement_hash, t0=t0, languages=languages)
    fe = list(fp.fe)
    if timezone_offset is not None:
        for i, entry in enumerate(fe):
            if entry.startswith("TO:"):
                fe[i] = f"TO:{timezone_offset}"
                break

    final = [
        {"key": "api_type", "value": "js"},
        {"key": "f", "value": x64hash128(_hashable_fe(fe))},
        {"key": "n", "value": base64.b64encode(str(int(t0)).encode()).decode()},
        {"key": "wh", "value": fp.wh},
        {"key": "enhanced_fp", "value": enhanced},
        {"key": "fe", "value": fe},
        {"key": "ife_hash", "value": x64hash128(", ".join(fe), seed=38)},
        {"key": "jsbd", "value": "{\"HL\":4,\"NCE\":true,\"DT\":\"\",\"NWD\":\"false\",\"DMTO\":1,\"DOTO\":1}"},
    ]
    key = f"{fp.user_agent}{short_esync()}"
    return base64.b64encode(aes_encrypt(json.dumps(final, separators=(",", ":")), key).encode()).decode()


# --------------------------------------------------------------------------- client


class _TlsSession:
    """tls_client shim with a requests-shaped surface.

    Arkose pairs a UA family with a TLS fingerprint. Sending an Android UA over a
    generic Chrome handshake is a contradiction that survives every BDA fix, so
    an Android UA has to leave through an okhttp Android handshake.
    """

    def __init__(self, user_agent: str):
        import re

        import tls_client

        match = re.search(r"Android (\d+(?:\.\d+)*)", user_agent)
        android = (match.group(1).split(".")[0] if match else "13")
        self._session = tls_client.Session(client_identifier=f"okhttp4_android_{android}")
        self._session.timeout_seconds = 25
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.proxies: dict[str, str] | None = None

    def _sync(self) -> None:
        if self.proxies:
            self._session.proxies = self.proxies
        if self.cookies:
            self._session.cookies.update(self.cookies)

    def get(self, url, params=None, headers=None, cookies=None, timeout=None):
        self._sync()
        return self._session.get(url, params=params, headers=headers or self.headers,
                                 cookies=cookies)

    def post(self, url, data=None, json=None, headers=None, cookies=None, timeout=None):
        self._sync()
        return self._session.post(url, data=data, json=json,
                                  headers=headers or self.headers, cookies=cookies)


class ArkoseClient:
    """Requests-only Arkose session for one site key."""

    def __init__(self, site_key: str = ROBLOX_SITE_KEY, api_url: str = ROBLOX_API,
                 site_url: str = ROBLOX_SITE, proxy: str | None = None,
                 fingerprints: list[dict[str, Any]] | None = None,
                 timezone_offset: int | None = None,
                 languages: str | None = None,
                 impersonate: str | None = None,
                 fingerprint: dict[str, Any] | None = None,
                 transport: str = "tls"):
        from curl_cffi import requests as curl_requests

        self.site_key = site_key
        self.api_url = api_url
        self.site_url = site_url
        self.host = api_url.split("://", 1)[1]
        self.timezone_offset = timezone_offset
        self.languages = languages
        self.fingerprints = fingerprints if fingerprints is not None else load_fingerprints()
        if not self.fingerprints:
            raise ArkoseError(f"no fingerprints in {FINGERPRINT_FILE}")
        self.fp = Fingerprint.from_raw(fingerprint or random.choice(self.fingerprints))
        # the TLS handshake has to agree with the UA inside the BDA, or Arkose
        # rejects the pair regardless of how correct the payload is
        self.impersonate = impersonate or self._impersonate_for(self.fp.user_agent)
        self.transport = transport
        headers = _base_headers(self.fp.user_agent, self.fp.sec_ch_ua,
                                self.host, self.site_url + "/")
        if transport == "tls":
            self.session = _TlsSession(self.fp.user_agent)
        else:
            self.session = curl_requests.Session(impersonate=self.impersonate)
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.session.headers = headers
        self.capi_version = ""
        self.enforcement_hash = ""

    @staticmethod
    def _impersonate_for(user_agent: str) -> str:
        version = "132"
        for token in user_agent.split():
            if token.startswith("Chrome/"):
                version = token.split("/")[1].split(".")[0]
        if "Android" in user_agent:
            return f"chrome{version}_android"
        return f"chrome{version}"

    # -- discovery ------------------------------------------------------------

    def load_config(self) -> tuple[str, str]:
        url = f"{self.api_url}/v2/{self.site_key}/api.js"
        resp = self.session.get(url, timeout=25)
        text = resp.text
        try:
            version = text.split("/enforcement.")[0].split('"')[-1]
            enforcement = text.split("/enforcement.")[1].split(".html")[0]
        except IndexError as exc:
            raise ArkoseError(f"could not parse api.js ({resp.status_code}): {text[:120]}") from exc
        self.capi_version = version
        self.enforcement_hash = enforcement
        return version, enforcement

    # -- gt2 ------------------------------------------------------------------

    def gt2(self, blob: str | None = None, origin_page: str | None = None) -> Token:
        if not self.capi_version:
            self.load_config()
        bda = build_bda(
            self.fp, self.site_key, self.site_url, self.api_url,
            self.capi_version, self.enforcement_hash,
            timezone_offset=self.timezone_offset, languages=self.languages,
        )
        referer = f"{self.api_url}/v2/{self.capi_version}/enforcement.{self.enforcement_hash}.html"
        headers = _post_headers(self.fp.user_agent, self.fp.sec_ch_ua,
                                self.host, referer, self.api_url)
        headers["x-ark-esync-value"] = short_esync()
        data = {
            "public_key": self.site_key,
            "capi_version": self.capi_version,
            "capi_mode": "inline",
            "style_theme": "default",
            "rnd": str(random.random()),
            "bda": bda,
            "site": self.site_url,
            "userbrowser": self.fp.user_agent,
        }
        if origin_page:
            data["data[origin_page]"] = origin_page
        if blob:
            data["data[blob]"] = blob

        resp = self.session.post(
            f"{self.api_url}/fc/gt2/public_key/{self.site_key}",
            data=data, headers=headers,
            cookies={"timestamp": headers["x-ark-esync-value"]}, timeout=25,
        )
        if resp.status_code == 400:
            raise Denied(f"gt2 400: {resp.text[:200]}")
        if resp.status_code != 200:
            raise ArkoseError(f"gt2 {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if "error" in body:
            raise ArkoseError(f"gt2 error: {str(body)[:200]}")
        return Token.parse(body["token"])

    # -- challenge ------------------------------------------------------------

    def gfct(self, token: Token) -> Challenge:
        referer = (f"{self.api_url}/fc/assets/ec-game-core/game-core/1.40.0/standard/index.html"
                   f"?session={token.game_token}")
        headers = _post_headers(self.fp.user_agent, self.fp.sec_ch_ua,
                                self.host, referer, self.site_url)
        data = {
            "token": token.game_token,
            "sid": token.region,
            "render_type": "canvas",
            "lang": "",
            "isAudioGame": "false",
            "is_compatibility_mode": "false",
            "apiBreakerVersion": "green",
            "analytics_tier": token.at,
        }
        resp = self.session.post(f"{self.api_url}/fc/gfct/", data=data, headers=headers,
                                 cookies={"timestamp": x_ark_esync()}, timeout=25)
        if resp.status_code != 200:
            raise ArkoseError(f"gfct {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        game = body.get("game_data") or {}
        gui = game.get("customGUI") or {}
        return Challenge(
            token=token,
            challenge_id=body.get("challengeID") or "",
            images=list(gui.get("_challenge_imgs") or []),
            waves=int(game.get("waves") or 0),
            game_type=int(game.get("gameType") or 0),
            variant=str(game.get("instruction_string") or game.get("game_variant") or ""),
            dapib_url=body.get("dapib_url"),
        )

    def fetch_images(self, challenge: Challenge) -> list[bytes]:
        out = []
        for url in challenge.images:
            resp = self.session.get(url, timeout=25)
            if resp.status_code != 200:
                raise ArkoseError(f"image {resp.status_code}: {url[:90]}")
            out.append(resp.content)
        return out

    # -- answer ---------------------------------------------------------------

    def answer(self, challenge: Challenge, guesses: list[int],
               bio: dict[str, str] | None = None,
               ecdata: dict[str, int] | None = None,
               include_tguess: bool = True) -> dict[str, Any]:
        """Submit one round of guesses.

        `guesses` are the chosen carousel indices, one per answered wave, in
        order. The wire format is not what older clients used — `/fc/ca/` now
        wants two AES envelopes rather than a plain comma list:

            guess   AES([{"index": 0}, {"index": 1}], session_token)
            tguess  AES(dapib(answers), session_token)

        The key is the session token (the first segment of the gt2 token), and
        the envelopes are OpenSSL-style: EVP_BytesToKey(MD5) with a fresh 8-byte
        salt carried in `s`, IV carried in `iv`. Both were recovered from a live
        submission and reproduce it byte for byte.
        """
        session = challenge.token.game_token

        plain = [{"index": int(g)} for g in guesses]
        guess = aes_encrypt(json.dumps(plain, separators=(",", ":")), session)

        # the dapib input carries the session token split as an extra key/value
        prefix, _, suffix = session.partition(".")
        answers = [{"index": int(g), prefix: suffix} for g in guesses]
        tanswer = transform(answers)
        tguess = aes_encrypt(json.dumps(tanswer, separators=(",", ":")), session)

        referer = (f"{self.api_url}/fc/assets/ec-game-core/game-core/1.40.0/standard/index.html"
                   f"?session={session}")
        headers = _post_headers(self.fp.user_agent, self.fp.sec_ch_ua,
                                self.host, referer, self.site_url)
        data = {
            "session_token": session,
            "game_token": challenge.challenge_id,
            "sid": challenge.token.region or "us-east-1",
            "guess": guess,
            "render_type": "canvas",
            "analytics_tier": challenge.token.at,
            "bio": _b64(bio if bio is not None else {"mbio": "", "tbio": "", "kbio": ""}),
            "is_compatibility_mode": "false",
            "ecdata": _b64(ecdata if ecdata is not None else {"height": 450, "width": 400}),
        }
        if include_tguess:
            data["tguess"] = tguess

        resp = self.session.post(f"{self.api_url}/fc/ca/", data=data, headers=headers,
                                 cookies={"timestamp": x_ark_esync()}, timeout=25)
        try:
            return resp.json()
        except Exception:
            return {"solved": False, "raw": resp.text[:200]}

    def init_load(self, token: Token) -> None:
        headers = _base_headers(self.fp.user_agent, self.fp.sec_ch_ua,
                                self.host, self.site_url + "/")
        headers.update({"sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
                        "sec-fetch-site": "same-origin"})
        self.session.get(f"{self.api_url}/fc/init-load/",
                         params={"session_token": token.game_token},
                         headers=headers,
                         cookies={"timestamp": short_esync()}, timeout=20)


# --------------------------------------------------------------------------- helpers


def load_fingerprints(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or FINGERPRINT_FILE
    if not target.exists():
        return []
    data = json.loads(target.read_text())
    return data if isinstance(data, list) else [data]
