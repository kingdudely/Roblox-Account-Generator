#!/usr/bin/env python3
"""Anonymous ChatGPT client — no account, no API key, no browser.

The `chatgpt.com/backend-anon` surface is reachable without logging in. It is
gated by three checks that all run in-process here:

  * sentinel `p` token      — a fingerprint blob built from a fixed config
  * proof-of-work           — FNV-1a search against the returned seed/difficulty
  * Turnstile               — bytecode is decrypted and decompiled, then the
                              VM's operations are replayed to produce a payload
                              the server accepts

The VM machinery is vendored under `tools/cgpt` (see that package's docstring
for provenance). Everything else — HTTP, image upload, SSE parsing — is ours
and written against the October 2026 wire format.

    ./tools/chatgpt_anon.py --ask "reply with pong"
    ./tools/chatgpt_anon.py --ask "how many dice?" --image debug/vision/x/cand1.img
    ./tools/chatgpt_anon.py --bench debug/vision --n 9

Notes
-----
* One `AnonChat` keeps its cookie identity (`oai-did`) for its lifetime, which
  is what the sentinel expects. Each `ask()` still opens a *fresh* conversation
  via /f/conversation/prepare, so calls do not leak context into each other.
* The sentinel token carries `expire_after` (~540 s). It is cached and refreshed
  per expiry rather than per call, which keeps a warm call near 1 s.
* The response stream is SSE with `delta_encoding: v1`: assistant text arrives
  as cumulative snapshots at `v.message.content.parts`, not as append deltas.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curl_cffi import requests as creq

from tools.cgpt import Challenges, Headers, VM

BASE = "https://chatgpt.com"
UA = Headers.DEFAULT["user-agent"]
WINDOW_KEYS = (
    "location", "self", "document", "name", "navigator", "history", "status",
    "frames", "length", "top", "opener", "parent", "frameElement", "customElements",
    "closed", "external", "screen", "innerWidth", "innerHeight", "devicePixelRatio",
    "localStorage", "sessionStorage", "indexedDB", "caches", "performance",
)
REACT_HINTS = ("location", "onpageshow", "onfocus", "onblur", "onclick", "ondrag")


class SentinelError(RuntimeError):
    """The server refused the sentinel handshake."""


class AnonChat:
    """One anonymous identity, reused across many conversations."""

    def __init__(self, proxy: str | None = None, timeout: float = 60.0,
                 locale: tuple[str, str, str] = ("en-US", "US", "America/New_York")):
        self.timeout = timeout
        self.proxy = proxy
        self.session = creq.Session(impersonate="chrome", timeout=timeout)
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.session.headers = dict(Headers.DEFAULT)

        self.lang, self.region, self.tz = locale
        self.ip_info = self._geo()
        self.tz_name = self.ip_info[5]
        self.build = ""
        self.device_id = ""
        self.start_time = 0
        self.sid = ""
        self._sentinel: dict[str, Any] = {}
        self._sentinel_at = 0.0
        self.asks = 0

        self._open_site()

    # ------------------------------------------------------------------ setup

    def _geo(self) -> list[str]:
        """Locate the exit IP. The values feed the sentinel fingerprint, so a
        mismatch with the real IP is a signal the server can score."""
        try:
            r = self.session.get(f"http://ip-api.com/json/?fields=status,country,"
                                 f"regionName,city,lat,lon,timezone,query",
                                 timeout=15)
            d = r.json()
            if d.get("status") == "success":
                return [d.get("query", "1.2.3.4"), d.get("city", "New York"),
                        d.get("regionName", "New York"), str(d.get("lat", 40.7)),
                        str(d.get("lon", -74.0)), d.get("timezone", self.tz)]
        except Exception:
            pass
        return ["1.2.3.4", "New York", "New York", "40.7", "-74.0", self.tz]

    def _open_site(self, tries: int = 3) -> None:
        last = None
        for _ in range(tries):
            try:
                r = self.session.get(BASE, timeout=self.timeout)
                if r.status_code == 200 and 'data-build="' in (r.text or ""):
                    self.session.cookies.update(r.cookies)
                    self.build = r.text.split('data-build="')[1].split('"')[0]
                    self.device_id = self.session.cookies.get("oai-did") or str(uuid.uuid4())
                    self.start_time = int(time.time() * 1000)
                    self.sid = str(uuid.uuid4())
                    return
                last = f"status {r.status_code}"
            except Exception as e:  # proxy flake
                last = str(e)[:80]
            time.sleep(1.5)
        raise SentinelError(f"could not open chatgpt.com ({last})")

    def _config(self, process_time: float | None = None) -> list:
        """The fingerprint array the sentinel hashes into its `p` token."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            now = datetime.now(ZoneInfo(self.tz_name))
        except Exception:
            now = datetime.now(ZoneInfo("UTC"))
        stamp = now.strftime("%a %b %d %Y %H:%M:%S GMT%z (%Z)")
        return [
            4880,
            stamp,
            4294705152,
            random.random(),
            UA,
            None,
            self.build,
            self.lang,
            f"{self.lang},en",
            random.random(),
            "webkitGetUserMedia\u2212function webkitGetUserMedia() { [native code] }",
            random.choice(REACT_HINTS),
            random.choice(WINDOW_KEYS),
            (process_time if process_time is not None else random.randint(800, 1400)) + random.random(),
            self.sid,
            "",
            20,
            self.start_time,
        ]

    def _headers(self, kind: str) -> dict:
        h = dict(getattr(Headers, kind))
        h["oai-client-version"] = self.build
        h["oai-device-id"] = self.device_id
        self.session.headers = dict(getattr(Headers, kind))
        self.session.headers.update({"oai-client-version": self.build,
                                     "oai-device-id": self.device_id})
        return h

    # --------------------------------------------------------------- sentinel

    def _solve_sentinel(self) -> dict:
        """Requirements -> PoW -> Turnstile. Cached until it nears expiry."""
        self._headers("REQUIREMENTS")
        p_value = Challenges.generate_token(self._config())
        r = self.session.post(f"{BASE}/backend-anon/sentinel/chat-requirements",
                              json={"p": p_value}, timeout=self.timeout)
        if r.status_code != 200:
            raise SentinelError(f"chat-requirements {r.status_code}: {r.text[:160]}")
        d = r.json()
        if "detail" in d:
            raise SentinelError(f"chat-requirements rejected: {d['detail']}")

        pow_cfg = d.get("proofofwork") or {}
        if not pow_cfg.get("required", True):
            proof = ""
        else:
            proof = Challenges.solve_pow(pow_cfg["seed"], pow_cfg["difficulty"],
                                         self._config())

        ts = d.get("turnstile") or {}
        if not ts.get("required", True) or not ts.get("dx"):
            turnstile = ""
        else:
            turnstile = VM.get_turnstile(ts["dx"], p_value, str(self.ip_info[:-1]))

        self._sentinel = {
            "token": d.get("token", ""),
            "proof": proof,
            "turnstile": turnstile,
            "persona": d.get("persona"),
            "force_login": d.get("force_login"),
            "expire_after": float(d.get("expire_after") or 300),
            "config": self._config(),
        }
        self._sentinel_at = time.time()
        return self._sentinel

    def _sentinel_fresh(self) -> dict:
        age = time.time() - self._sentinel_at
        if not self._sentinel or age > max(60.0, self._sentinel["expire_after"] - 60):
            return self._solve_sentinel()
        return self._sentinel

    # ------------------------------------------------------------ conversation

    def _conduit(self) -> str:
        self._headers("CONDUIT")
        body = {
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": "client-created-root",
            "model": "auto",
            "timezone_offset_min": 0,
            "timezone": self.tz_name,
            "history_and_training_disabled": True,
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": [],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
        }
        r = self.session.post(f"{BASE}/backend-anon/f/conversation/prepare",
                              json=body, timeout=self.timeout)
        if r.status_code != 200:
            raise SentinelError(f"prepare {r.status_code}: {r.text[:160]}")
        d = r.json()
        token = d.get("conduit_token")
        if not token:
            raise SentinelError(f"prepare gave no conduit: {json.dumps(d)[:200]}")
        return token

    def ask(self, message: str, image: Path | None = None) -> dict:
        """One self-contained conversation turn. Returns text plus metadata."""
        return self.ask_many(message, [image] if image is not None else [])

    def ask_many(self, message: str, images: list[Path] | None = None,
                 labels: list[str] | None = None) -> dict:
        """One turn carrying several images, each optionally label-prefixed.

        The avalanche of separate uploads is deliberate: one conversation with
        11 attachments is what the Arkose puzzle actually needs, and it keeps
        the whole decision inside a single model context.
        """
        t0 = time.time()
        images = [Path(i) for i in (images or [])]

        uploads: list[dict] = []
        if images:
            for i in images:
                uploads.append(self.upload_image(i))
            payload_msg = _multimodal_message(message, uploads,
                                              labels or [f"image {n + 1}" for n in range(len(images))])
        else:
            payload_msg = _text_message(message)

        return self._converse(payload_msg, uploads, t0)

    def ask_uploaded(self, message: str, uploads: list[dict],
                     labels: list[str] | None = None) -> dict:
        """One turn over files already registered with `upload_*`.

        Lets a caller attach something whose upload path differs from
        `upload_image` — a non-image mime, or an archive — without duplicating
        the conversation body.
        """
        payload_msg = (_multimodal_message(message, uploads,
                                           labels or [f"file {n + 1}" for n in range(len(uploads))])
                       if uploads else _text_message(message))
        return self._converse(payload_msg, uploads, time.time())

    def _converse(self, payload_msg: dict, uploads: list[dict],
                  t0: float) -> dict:
        sent = self._sentinel_fresh()
        conduit = self._conduit()
        t1 = random.randint(6000, 9000)
        self._headers("CONVERSATION")
        self.session.headers.update({
            "oai-echo-logs": f"0,{t1},1,{t1 + random.randint(1000, 1200)}",
            "openai-sentinel-chat-requirements-token": sent["token"],
            "openai-sentinel-proof-token": sent["proof"],
            "openai-sentinel-turnstile-token": sent["turnstile"],
            "x-conduit-token": conduit,
        })

        body = {
            "action": "next",
            "messages": [payload_msg],
            "parent_message_id": "client-created-root",
            "model": "auto",
            "timezone_offset_min": 0,
            "timezone": self.tz_name,
            "history_and_training_disabled": True,
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": [],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": True,
                "time_since_loaded": random.randint(3, 6),
                "page_height": 1219,
                "page_width": 1440,
                "pixel_ratio": 1,
                "screen_height": 1440,
                "screen_width": 3440,
            },
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
        }
        r = self.session.post(f"{BASE}/backend-anon/f/conversation",
                              json=body, timeout=self.timeout)
        raw = r.text or ""
        if r.status_code != 200:
            raise SentinelError(f"conversation {r.status_code}: {raw[:200]}")
        if "Unusual activity" in raw:
            raise SentinelError("IP flagged: unusual activity")

        parsed = parse_stream(raw)
        if not parsed["text"] and parsed["error"]:
            raise SentinelError(parsed["error"])
        parsed["raw_stream"] = raw

        self.asks += 1
        return {
            "text": parsed["text"],
            "conversation_id": parsed["conversation_id"],
            "message_id": parsed["message_id"],
            "model": parsed["model"],
            "plan": parsed["plan"],
            "force_login": sent.get("force_login"),
            "elapsed": round(time.time() - t0, 2),
            "upload": uploads,
            "raw_stream": parsed.get("raw_stream", ""),
        }

    def ask_text(self, message: str) -> str:
        return self.ask(message)["text"]

    # ------------------------------------------------------------------ upload

    def upload_image(self, image: Path) -> dict:
        """files -> PUT to blob -> process_upload_stream. Returns file metadata."""
        data = image.read_bytes()
        mime = mimetypes.guess_type(image.name)[0] or "image/png"
        name = f"{uuid.uuid4()}.png" if not image.suffix else f"{uuid.uuid4()}{image.suffix}"

        width = height = 0
        try:
            from PIL import Image
            with Image.open(image) as im:
                width, height = im.size
        except Exception:
            pass

        self._headers("REQUIREMENTS")
        r = self.session.post(f"{BASE}/backend-anon/files", json={
            "file_name": name,
            "file_size": len(data),
            "use_case": "multimodal",
            "timezone_offset_min": 0,
            "reset_rate_limits": False,
        }, timeout=self.timeout)
        if r.status_code != 200:
            raise SentinelError(f"file init {r.status_code}: {r.text[:200]}")
        j = r.json()
        file_id, upload_url = j.get("file_id"), j.get("upload_url")
        if not (file_id and upload_url):
            raise SentinelError(f"file init missing fields: {json.dumps(j)[:200]}")

        self._headers("FILE")
        self.session.headers["content-type"] = mime
        up = self.session.put(upload_url, data=data, timeout=self.timeout)
        if up.status_code not in (200, 201):
            raise SentinelError(f"blob put {up.status_code}: {up.text[:160]}")

        self._headers("REQUIREMENTS")
        pr = self.session.post(f"{BASE}/backend-anon/files/process_upload_stream",
                               json={"file_id": file_id,
                                     "use_case": "multimodal",
                                     "index_for_retrieval": False,
                                     "file_name": name}, timeout=self.timeout)
        ok = "Succeeded processing" in (pr.text or "")
        if not ok:
            raise SentinelError(f"process_upload_stream: {pr.text[:200]}")

        return {"file_id": file_id, "name": name, "mime": mime,
                "size": len(data), "width": width, "height": height}


# --------------------------------------------------------------------- payloads


def _text_message(text: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "author": {"role": "user"},
        "create_time": round(time.time(), 3),
        "content": {"content_type": "text", "parts": [text]},
        "metadata": {
            "selected_github_repos": [],
            "selected_all_github_repos": False,
            "serialization_metadata": {"custom_symbol_offsets": []},
        },
    }


def _multimodal_message(text: str, uploads: list[dict],
                        labels: list[str]) -> dict:
    """Build the multimodal payload. Each image is preceded by its label so the
    model can refer to candidates by index without counting attachments."""
    msg = _text_message(text)
    parts: list = []
    attachments: list = []
    for up, label in zip(uploads, labels):
        parts.append(label)
        parts.append({
            "content_type": "image_asset_pointer",
            "asset_pointer": f"file-service://{up['file_id']}",
            "size_bytes": up["size"],
            "width": up["width"],
            "height": up["height"],
        })
        attachments.append({
            "id": up["file_id"],
            "size": up["size"],
            "name": up["name"],
            "mime_type": up["mime"],
            "width": up["width"],
            "height": up["height"],
            "source": "local",
        })
    msg["content"] = {"content_type": "multimodal_text", "parts": parts}
    msg["metadata"]["attachments"] = attachments
    return msg


# ------------------------------------------------------------------------ SSE


def parse_stream(raw: str) -> dict:
    """Read the October-2026 SSE stream.

    Events of interest:
      * `{"type":"resume_conversation_token", "conversation_id": ...}`
      * `{"event":"delta","data":{"v":{"message":{...,"author":{"role":"assistant"},
         "content":{"parts":["..."]}}}}}`  — cumulative snapshots, last one wins
      * `{"o":"patch","v":[{"p":"/message/content/parts/0","o":"append","v":...}]}`
      * `{"type":"message_stream_complete"}`, `{"type":"server_ste_metadata"}`
    """
    out = {"text": "", "conversation_id": "", "message_id": "",
           "model": "", "plan": "", "error": "", "events": 0}

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        out["events"] += 1

        kind = d.get("type")
        if kind == "resume_conversation_token":
            out["conversation_id"] = d.get("conversation_id") or out["conversation_id"]
            continue
        if kind == "message_stream_complete":
            out["conversation_id"] = d.get("conversation_id") or out["conversation_id"]
            continue
        if kind == "server_ste_metadata":
            md = d.get("metadata") or {}
            out["plan"] = md.get("plan_type") or out["plan"]
            out["model"] = md.get("model_slug") or out["model"]
            continue

        # legacy shape: flat append onto parts/0
        if d.get("o") == "append" and d.get("p") == "/message/content/parts/0":
            v = d.get("v")
            if isinstance(v, str):
                out["text"] += v
            continue

        # patch shape
        if d.get("o") == "patch" and isinstance(d.get("v"), list):
            for op in d["v"]:
                if not isinstance(op, dict):
                    continue
                if op.get("p") == "/message/content/parts/0" and op.get("o") == "append":
                    v = op.get("v")
                    if isinstance(v, str):
                        out["text"] += v
                elif op.get("p") == "/message/status" and op.get("v") == "finished_successfully":
                    pass
            continue

        # 2026 shape: message snapshots
        msg = (d.get("v") or {}).get("message") if isinstance(d.get("v"), dict) else None
        if isinstance(msg, dict):
            author = (msg.get("author") or {}).get("role")
            content = msg.get("content") or {}
            parts = content.get("parts")
            if author == "assistant" and isinstance(parts, list) and parts:
                joined = "".join(p for p in parts if isinstance(p, str))
                if joined:
                    out["text"] = joined          # cumulative: last wins
                    out["message_id"] = msg.get("id") or out["message_id"]
            continue

    return out


# ---------------------------------------------------------------------- proxy


_BRIDGES: dict[str, object] = {}


def normalise_proxy(line: str) -> str:
    """Any vendor line shape -> something curl can actually use.

    Accepted: ``socks5://user:pass@host:port``, ``user:pass@host:port``,
    ``host:port:user:pass``, ``host:port``. Always resolved to an explicit
    scheme first, because these ports are SOCKS5-only and a line defaulted to
    ``http://`` fails against them.

    The pool is SOCKS5 with RFC 1929 auth, which curl_cffi fails on ("cannot
    complete SOCKS5 connection"). `SocksBridge` re-exposes each one as a local
    HTTP proxy; started once per URL and cached for the process lifetime.
    """
    line = line.strip()
    if not line:
        return ""
    from roblox_creator.proxyurl import parse as _parse

    line = _parse(line).url()
    if line.startswith("socks5://"):
        if line not in _BRIDGES:
            from roblox_creator.socks_bridge import SocksBridge

            _BRIDGES[line] = SocksBridge(line).start()
        return _BRIDGES[line]  # type: ignore[return-value]
    if "://" not in line:
        return "http://" + line
    return line


def load_proxies(path: Path) -> list[str]:
    return [p for p in (normalise_proxy(l) for l in
                        path.read_text().replace("\r", "").splitlines()) if p]


# ------------------------------------------------------------------------ CLI


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ask", default=None, help="text prompt")
    ap.add_argument("--image", type=Path, default=None, help="image to attach")
    ap.add_argument("--proxy", default=None, help="http://user:pass@host:port")
    ap.add_argument("--proxy-file", type=Path, default=None)
    ap.add_argument("--n", type=int, default=3, help="bench: waves to run")
    args = ap.parse_args()

    proxies = []
    if args.proxy:
        proxies = [args.proxy]
    elif args.proxy_file and args.proxy_file.exists():
        proxies = load_proxies(args.proxy_file)

    proxy = proxies[0] if proxies else None
    chat = AnonChat(proxy)
    print(f"  build {chat.build[:16]}  tz {chat.tz_name}  device {chat.device_id[:8]}")

    if args.ask:
        r = chat.ask(args.ask, args.image)
        print(f"\n  [{r['elapsed']}s] plan={r['plan']} model={r['model']}")
        print(f"  ANSWER: {r['text']!r}")
        return 0 if r["text"] else 1

    print("nothing to do; pass --ask")
    return 2


if __name__ == "__main__":
    sys.exit(main())
