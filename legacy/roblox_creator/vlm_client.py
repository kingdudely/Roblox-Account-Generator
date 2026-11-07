"""One vision-counting client for every free endpoint we might use.

The question this answers is narrow and always the same: *how many visible dice
faces in this tile show this symbol?* Every provider below speaks
OpenAI-compatible `/chat/completions` with `image_url` parts, so the only thing
that changes per provider is a base URL, a model id, and whether a key is needed.

Providers worth having, and why:

    ovh        keyless, 2 RPM per IP, Qwen2.5-VL-72B-Instruct. The per-*IP* limit
               is the property that matters: rotate exits and the quota is the
               pool, not a bill.
    mistral    key needed (free tier), ~1 RPS per key, 500K TPM. Much higher
               per-key rate than OVH, but the vision models are document/OCR
               oriented rather than counting oriented.
    groq       key needed, 30 RPM / 1000 RPD, qwen3.8-27b (text+image).
    openrouter key needed, 20 RPM / 50 RPD across all free models.
    gemini     key needed, best-in-class vision but unpublished free limits.

Two behaviours matter more than the choice of model:

1. **Retry on a fresh exit.** Most failures are 429 or a dead proxy, and both are
   solved by asking again from a different IP. A tile that comes back `None`
   poisons the whole wave, because the count vector no longer has one entry per
   tile.
2. **Never guess when the answer is ambiguous.** If two tiles tie at N, that is a
   signal to re-ask those two — not to pick the first.

    client = VisionClient(provider="ovh", exits=[...])
    counts = client.count_wave(key_img, tiles)
"""

from __future__ import annotations

import base64
import io
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from PIL import Image

__all__ = ["Provider", "VisionClient", "PROVIDERS", "count_tile"]


@dataclass
class Provider:
    name: str
    base: str
    model: str
    key_env: str = ""
    keyless: bool = False
    rpm_per_ip: float = 0.0
    max_edge: int = 900
    max_tokens: int = 24
    reasoning: bool = False   # spends budget on chain-of-thought before answering


PROVIDERS: dict[str, Provider] = {
    "lithosai": Provider(
        name="lithosai",
        base="https://api.lithosai.cloud/v1",
        model="deepseek-ai/DeepSeek-V4.1-Flash-ultra",
        key_env="LITHOSAI_API_KEY",
        rpm_per_ip=60.0,
        max_edge=1024,
    ),
    "ovh": Provider(
        name="ovh",
        base="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        model="Qwen2.5-VL-72B-Instruct",
        keyless=True,
        rpm_per_ip=2.0,
    ),
    "ovh-qwen9b": Provider(
        name="ovh-qwen9b",
        base="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        model="Qwen3.5-9B",
        keyless=True,
        rpm_per_ip=2.0,
    ),
    "mistral": Provider(
        name="mistral",
        base="https://api.mistral.ai/v1",
        model="ministral-14b-latest",
        key_env="MISTRAL_API_KEY",
        rpm_per_ip=60.0,
    ),
    "mistral-large": Provider(
        name="mistral-large",
        base="https://api.mistral.ai/v1",
        model="mistral-large-latest",
        key_env="MISTRAL_API_KEY",
        rpm_per_ip=60.0,
    ),
    "groq": Provider(
        name="groq",
        base="https://api.groq.com/openai/v1",
        model="qwen/qwen3.8-27b",
        key_env="GROQ_API_KEY",
        rpm_per_ip=30.0,
    ),
    "openrouter": Provider(
        name="openrouter",
        base="https://openrouter.ai/api/v1",
        model="google/gemma-4-31b-it:free",
        key_env="OPENROUTER_API_KEY",
        rpm_per_ip=20.0,
    ),
    "kilo-dots": Provider(
        name="kilo-dots",
        base="https://api.kilo.ai/api/gateway",
        model="dots-studio/dots-3-note-preview:free",
        keyless=True,
        rpm_per_ip=3.3,        # 200 req/hour per IP, keyless
        max_tokens=3000,       # reasoning model: needs room to finish thinking
        reasoning=True,
    ),
    "kilo-omni": Provider(
        name="kilo-omni",
        base="https://api.kilo.ai/api/gateway",
        model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        keyless=True,
        rpm_per_ip=3.3,
        max_tokens=3000,
        reasoning=True,
    ),
    "kilo-vl": Provider(
        name="kilo-vl",
        base="https://api.kilo.ai/api/gateway",
        model="nvidia/nemotron-3.5-super-vl:free",
        keyless=True,
        rpm_per_ip=3.3,
        max_tokens=4000,
        reasoning=True,
    ),
    "gemini": Provider(
        name="gemini",
        base="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-2.5-flash",
        key_env="GEMINI_API_KEY",
        rpm_per_ip=10.0,
    ),
}

PROMPT_COUNT = (
    "Image 1 shows a single SYMBOL on one die face. "
    "Image 2 shows a pile of dice seen from above. "
    "Count only the TOP faces (the flat squares facing the camera) in image 2 "
    "that show EXACTLY the same SYMBOL as image 1. "
    "Do not count side faces. Do not guess. "
    "Answer with one integer and nothing else."
)

PROMPT_CENSUS = (
    "Image 1 is one die face showing a single SYMBOL. "
    "Image 2 is a pile of dice. Look at each TOP face (flat upward rectangles) "
    "one at a time, left to right, and decide whether it shows that SYMBOL. "
    "Then give the total number of top faces that show it. "
    "Reply in exactly this form: CENSUS: <n>. Nothing else."
)


def _data_url(img: Image.Image, edge: int) -> str:
    buf = io.BytesIO()
    im = img.convert("RGB")
    w, h = im.size
    scale = edge / max(w, h)
    if abs(scale - 1.0) > 0.02:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.LANCZOS)
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@dataclass
class VisionClient:
    provider: str = "ovh"
    exits: list[str] = field(default_factory=list)
    api_key: str = ""
    timeout: float = 90.0
    retries: int = 3

    def __post_init__(self) -> None:
        self.cfg = PROVIDERS[self.provider]
        if not self.api_key and self.cfg.key_env:
            self.api_key = os.getenv(self.cfg.key_env, "")
        self._i = 0
        self.stats = {"ok": 0, "fail": 0, "retries": 0}

    # -------------------------------------------------------------- transport

    def _next_exit(self) -> str | None:
        if not self.exits:
            return None
        self._i += 1
        return self.exits[self._i % len(self.exits)]

    def _post(self, payload: dict[str, Any], exit_url: str | None) -> requests.Response:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        proxies = {"http": exit_url, "https": exit_url} if exit_url else None
        return requests.post(f"{self.cfg.base}/chat/completions", json=payload,
                             headers=headers, proxies=proxies,
                             timeout=self.timeout)

    def _ask(self, prompt: str, images: list[Image.Image],
             max_tokens: int | None = None) -> tuple[str | None, dict]:
        edge = self.cfg.max_edge
        payload = {
            "model": self.cfg.model,
            "temperature": 0,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": prompt}] + [
                    {"type": "image_url",
                     "image_url": {"url": _data_url(im, edge)}}
                    for im in images
                ],
            }],
        }
        last: dict = {}
        for attempt in range(self.retries):
            exit_url = self._next_exit()
            t0 = time.time()
            try:
                r = self._post(payload, exit_url)
            except Exception as exc:
                last = {"error": f"{type(exc).__name__}: {str(exc)[:70]}",
                        "elapsed": round(time.time() - t0, 1)}
                self.stats["retries"] += 1
                continue
            last = {"status": r.status_code, "elapsed": round(time.time() - t0, 1)}
            if r.status_code == 200:
                try:
                    choice = r.json()["choices"][0]
                    msg = choice.get("message") or {}
                    text = msg.get("content") or ""
                    last["finish"] = choice.get("finish_reason")
                    if not text and msg.get("reasoning") and self.cfg.reasoning:
                        # The model thought itself out of output budget. The
                        # reasoning is not an answer, so this is a failure the
                        # caller should retry with more room, not a citation.
                        last["truncated_reasoning"] = len(msg["reasoning"])
                        self.stats["retries"] += 1
                        continue
                    self.stats["ok"] += 1
                    return text, last
                except Exception:
                    last["body"] = r.text[:160]
            else:
                last["body"] = r.text[:160]
            self.stats["retries"] += 1
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        self.stats["fail"] += 1
        return None, last

    # ------------------------------------------------------------------ tasks

    def count(self, key_img: Image.Image, tile: Image.Image,
              prompt: str = PROMPT_COUNT) -> tuple[int | None, dict]:
        text, meta = self._ask(prompt, [key_img, tile])
        if text is None:
            return None, meta
        meta["raw"] = text.strip()[:60]
        m = re.search(r"\d+", text)
        return (int(m.group()) if m else None), meta

    def count_wave(self, key_img: Image.Image, tiles: list[Image.Image],
                   prompt: str = PROMPT_COUNT) -> tuple[list[int | None], list[dict]]:
        counts: list[int | None] = []
        metas: list[dict] = []
        for tile in tiles:
            n, meta = self.count(key_img, tile, prompt)
            counts.append(n)
            metas.append(meta)
        return counts, metas

    def count_wave_parallel(self, key_img: Image.Image, tiles: list[Image.Image],
                            workers: int = 10,
                            prompt: str = PROMPT_COUNT) -> tuple[list[int | None], list[dict]]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda t: self.count(key_img, t, prompt), tiles))
        return ([r[0] for r in results], [r[1] for r in results])


def count_tile(key_img, tile, provider: str = "ovh",
               exits: list[str] | None = None) -> tuple[int | None, dict]:
    """Convenience: one tile, one provider, no client to hold."""
    c = VisionClient(provider=provider, exits=exits or [])
    return c.count(key_img, tile)
