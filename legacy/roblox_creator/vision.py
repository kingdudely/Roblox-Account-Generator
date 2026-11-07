"""Visual Arkose solving — capture the puzzle, pick the answer, click it.

The v2 game-core renders each round as plain DOM inside an iframe:

    <h2>Use the arrows to find the image where ... matches the left image exactly (1 of 5)</h2>
    <img class="key-frame-image">                       reference
    <img class="... answer-frame">                      one candidate at a time
    <a aria-label="Navigate to previous image">
    <a aria-label="Navigate to next image">
    <div class="pip-container" role="img" aria-label="3 of 10">
    <button>Submit</button>

The images are served from /rtig/image?challenge=<n>, so they can be pulled as
bytes, handed to a model, and answered by paging to that index. Solving in the UI
is deliberate: Arkose's own callback fires and Roblox accepts the token, so there
is no token to mint and no /challenge/v1/continue call.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import requests

PROMPT = """You are solving an Arkose Labs "match" challenge.

Read the INSTRUCTION below exactly. It tells you what property to compare between
the KEY image and the candidates. Then look at every candidate and return the
1-based index of the single candidate that satisfies the instruction.

Rules:
- Count carefully, do not guess. Dice: only the symbols on the TOP faces count.
- Exactly one candidate matches. If two look close, recount both.
- If the instruction says "matches the left image exactly", the property named is
  the only thing that matters; background, camera angle and dice count may differ.
- Reply with JSON only: {"index": <int>}

INSTRUCTION: {instruction}

KEY image follows, then candidates numbered 1..{total}."""


class SolveError(RuntimeError):
    pass


@dataclass
class Puzzle:
    instruction: str
    total: int = 0
    current: int = 1
    key: bytes | None = None
    images: dict[int, bytes] = field(default_factory=dict)

    def missing(self) -> list[int]:
        return [i for i in range(1, self.total + 1) if i not in self.images]

    def ready(self) -> bool:
        return self.key is not None and not self.missing()

    def context(self) -> str:
        """Short human-readable summary, handy in logs."""
        return f"{len(self.images)}/{self.total} images, key={'yes' if self.key else 'no'}"


# --------------------------------------------------------------------------- capture


class ImageTap:
    """Collects /rtig/image responses keyed by their challenge index."""

    def __init__(self) -> None:
        self.key: bytes | None = None
        self.images: dict[int, bytes] = {}
        self.urls: list[str] = []
        self._pending: list = []

    def attach(self, ctx) -> None:
        ctx.on("response", self._on_response)

    def _on_response(self, response) -> None:
        url = response.url
        if "/rtig/image" not in url:
            return
        self.urls.append(url)
        try:
            index = int(parse_qs(urlparse(url).query).get("challenge", ["0"])[0])
        except (ValueError, IndexError):
            return
        try:
            body = response.body()
        except Exception:
            self._pending.append((index, response))
            return
        self._store(index, body)

    def drain(self) -> None:
        """Resolve responses whose body was not readable inside the event handler."""
        still: list = []
        for index, response in self._pending:
            try:
                self._store(index, response.body())
            except Exception:
                still.append((index, response))
        self._pending = still

    def _store(self, index: int, body: bytes) -> None:
        if not body:
            return
        if index == 0:
            self.key = body
        else:
            self.images[index] = body


SNAP_READY = r"""() => !!document.querySelector('.key-frame-image') && !!document.querySelector('.answer-frame img')"""


def _snap_pair(frame) -> tuple[bytes | None, bytes | None]:
    """Screenshot the two image boxes.

    The images are painted as CSS background-image from blob: URLs created in
    another browsing context, so fetch() from this frame fails ("Failed to
    fetch") and canvas would be tainted. Playwright renders the composited
    pixels instead, which is exactly what a person sees.
    """
    key = candidate = None
    try:
        key = frame.locator(".key-frame-image").first.screenshot(timeout=5000)
    except Exception:
        key = None
    try:
        candidate = frame.locator(".answer-frame img").first.screenshot(timeout=5000)
    except Exception:
        candidate = None
    return key, candidate


CURSOR = r"""() => {
  const pip = document.querySelector('.pip-container');
  const label = pip ? (pip.getAttribute('aria-label') || '') : '';
  const m = label.match(/(\d+)\s+of\s+(\d+)/);
  const h = document.querySelector('h2 span[role=text], h2 .text');
  return {
    current: m ? parseInt(m[1], 10) : 1,
    total: m ? parseInt(m[2], 10) : 0,
    instruction: h ? (h.innerText || '').trim() : '',
    submit: !!document.querySelector('button.button:not(.reload-button)'),
    done: !document.querySelector('.match-game'),
  };
}"""


def read_cursor(frame) -> dict:
    try:
        return frame.evaluate(CURSOR)
    except Exception:
        return {"current": 1, "total": 0, "instruction": "", "submit": False, "done": True}


def watch(frame, tap: ImageTap, page, seconds: float = 20.0) -> Puzzle:
    """Page through every candidate, capturing images as they load."""
    start = read_cursor(frame)
    puzzle = Puzzle(instruction=start["instruction"], total=start["total"], current=start["current"])
    tap.drain()
    if tap.key:
        puzzle.key = tap.key

    if not puzzle.total:
        # no pips: single-shot game
        key, cand = _snap_pair(frame)
        if key:
            puzzle.key = key
        if cand:
            puzzle.images[1] = cand
            puzzle.total = 1
        return puzzle

    for target in range(1, puzzle.total + 1):
        if read_cursor(frame)["current"] != target:
            page_to(frame, target, page)
        tap.drain()
        if tap.key:
            puzzle.key = tap.key
        if target in tap.images:
            puzzle.images[target] = tap.images[target]
        if len(puzzle.images) < target or puzzle.key is None:
            key, cand = _snap_pair(frame)
            if key and puzzle.key is None:
                puzzle.key = key
            if cand and target not in puzzle.images:
                puzzle.images[target] = cand
    return puzzle


def page_to(frame, target: int, page, rng=None) -> int:
    """Click arrows until the candidate cursor reads `target`. Returns clicks made."""
    current = read_cursor(frame)["current"]
    clicks = 0
    while current != target and clicks < 40:
        forward = target > current
        arrow = "next image" if forward else "previous image"
        try:
            frame.locator(f'[aria-label="Navigate to {arrow}"]').first.click(timeout=4000)
        except Exception as exc:
            raise SolveError(f"arrow click failed: {exc}") from exc
        clicks += 1
        deadline = time.time() + 6.0
        while time.time() < deadline:
            page.wait_for_timeout(120)
            now = read_cursor(frame)["current"]
            if now != current:
                current = now
                break
        else:
            raise SolveError("candidate cursor did not move")
        page.wait_for_timeout((rng.randint(220, 520) if rng else 300))
    return clicks


def submit(frame, page, rng=None) -> None:
    button = frame.locator("button.button:not(.reload-button)").first
    deadline = time.time() + 6.0
    while time.time() < deadline:
        try:
            if not button.is_disabled():
                break
        except Exception:
            break
        page.wait_for_timeout(120)
    page.wait_for_timeout((rng.randint(200, 600) if rng else 300))
    button.click(timeout=6000)


# --------------------------------------------------------------------------- solvers


class VisionSolver(Protocol):
    name: str

    def choose(self, puzzle: Puzzle) -> int: ...


def _mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


def _parts(puzzle: Puzzle, inline_key: str = "inline_data") -> list[dict]:
    """Gemini-shaped parts; OpenAI shape is derived from these."""
    parts: list[dict] = [{"text": PROMPT.format(instruction=puzzle.instruction, total=puzzle.total)}]
    if puzzle.key:
        parts.append({"text": "KEY:"})
        parts.append({inline_key: {"mime_type": _mime(puzzle.key),
                                   "data": base64.b64encode(puzzle.key).decode()}})
    for index in sorted(puzzle.images):
        parts.append({"text": f"CANDIDATE {index}:"})
        parts.append({inline_key: {"mime_type": _mime(puzzle.images[index]),
                                   "data": base64.b64encode(puzzle.images[index]).decode()}})
    return parts


class GeminiSolver:
    """Gemini API, native generateContent, JSON-schema constrained output.

    Accepts one key or a pool of keys from separate Cloud projects (limits are
    per project, so same-project keys share one bucket). A 429 rotates to the
    next healthy key instead of failing the round.
    """

    name = "gemini"

    def __init__(self, api_key: str = "", model: str = "gemini-2.5-flash",
                 timeout: float = 45.0, thinking_budget: int = 0,
                 keys: list[str] | None = None):
        from .keypool import KeyPool

        pool_keys = list(keys or [])
        if api_key and api_key not in pool_keys:
            pool_keys.insert(0, api_key)
        if not pool_keys:
            raise SolveError("gemini solver needs an API key")
        self.pool = KeyPool(pool_keys)
        self.model = model
        self.timeout = timeout
        self.thinking_budget = thinking_budget

    def _body(self, puzzle: Puzzle) -> dict:
        body = {
            "contents": [{"role": "user", "parts": _parts(puzzle)}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                },
            },
        }
        if self.thinking_budget >= 0:
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": self.thinking_budget}
        return body

    def choose(self, puzzle: Puzzle) -> int:
        from .keypool import parse_quota

        body = self._body(puzzle)
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        attempts = max(1, len(self.pool))
        last = "no attempt made"
        for _ in range(attempts):
            key = self.pool.acquire()
            try:
                r = requests.post(url, params={"key": key}, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                self.pool.report(key, 500)
                last = f"transport: {exc}"
                continue
            if r.status_code == 429:
                err = parse_quota(r.text, dict(r.headers))
                self.pool.report(key, 429, err.retry_after, err.per_day)
                last = f"429 ({'RPD' if err.per_day else 'RPM'}, retry in {err.retry_after:.0f}s)"
                continue
            if r.status_code != 200:
                self.pool.report(key, r.status_code)
                last = f"gemini {r.status_code}: {r.text[:160]}"
                continue
            self.pool.report(key, 200)
            try:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                index = int(json.loads(text)["index"])
            except Exception:
                last = f"gemini response unusable: {r.text[:160]}"
                continue
            return _clamp(index, puzzle)
        raise SolveError(f"all keys failed — {last}")


class OpenAICompatSolver:
    """Anything that speaks /v1/chat/completions with image_url: vLLM, Ollama, llama.cpp.

    Local Qwen2.5-VL:  ollama serve && ollama run qwen2.5vl:3b
                       base_url=http://localhost:11434/v1
    """

    name = "openai-compat"

    def __init__(self, base_url: str, model: str, api_key: str = "local",
                 timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def choose(self, puzzle: Puzzle) -> int:
        content: list[dict] = [{"type": "text", "text": PROMPT.format(
            instruction=puzzle.instruction, total=puzzle.total)}]
        if puzzle.key:
            content.append({"type": "text", "text": "KEY:"})
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{_mime(puzzle.key)};base64,{base64.b64encode(puzzle.key).decode()}"}})
        for index in sorted(puzzle.images):
            content.append({"type": "text", "text": f"CANDIDATE {index}:"})
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{_mime(puzzle.images[index])};base64,"
                       f"{base64.b64encode(puzzle.images[index]).decode()}"}})
        body = {"model": self.model, "temperature": 0, "max_tokens": 64,
                "messages": [{"role": "user", "content": content}]}
        url = f"{self.base_url}/chat/completions"
        try:
            r = requests.post(url, json=body, timeout=self.timeout,
                              headers={"authorization": f"Bearer {self.api_key}"})
        except requests.RequestException as exc:
            raise SolveError(f"local transport: {exc}") from exc
        if r.status_code != 200:
            raise SolveError(f"local {r.status_code}: {r.text[:200]}")
        text = r.json()["choices"][0]["message"]["content"]
        return _clamp(_parse_index(text), puzzle)


def _parse_index(text: str) -> int:
    text = (text or "").strip()
    if "{" in text:
        text = text[text.find("{"): text.rfind("}") + 1]
    try:
        return int(json.loads(text)["index"])
    except Exception:
        pass
    digits = "".join(c if c.isdigit() else " " for c in text).split()
    if not digits:
        raise SolveError(f"no index in model output: {text[:120]}")
    return int(digits[0])


def _clamp(index: int, puzzle: Puzzle) -> int:
    if puzzle.total and not (1 <= index <= puzzle.total):
        raise SolveError(f"index {index} outside 1..{puzzle.total}")
    return index


class OnnxSolver:
    """Per-variant ONNX classifier — the published Arkose model zoo.

    Most variants are siamese pair classifiers: `input_left` is the reference
    tile, `input_right` is a candidate, and the single output is a similarity
    score. Pick the candidate with the highest score.

    Models: https://funcaptchamodel.unix.do/<variant>.onnx
            https://huggingface.co/haloworker/td-captcha-model-v1
    """

    name = "onnx"

    # instruction string (or its variant name) -> model file
    VARIANTS = {
        "dicematch": "dicematch.onnx",
        "dice_pair": "dice_pair.onnx",
        "shadows": "shadows.onnx",
        "counting": "counting.onnx",
        "card": "card.onnx",
        "cardistance": "cardistance.onnx",
        "knotsCrossesCircle": "knotsCrossesCircle.onnx",
        "rockstack": "rockstack.onnx",
        "rockstack_v2": "rockstack_v2.onnx",
        "penguins": "penguins.onnx",
        "penguin": "penguin.onnx",
        "frankenhead": "frankenhead.onnx",
        "unbentobjects": "unbentobjects.onnx",
        "conveyor": "conveyor.onnx",
        "coordinatesmatch": "coordinatesmatch.onnx",
        "hopscotch_highsec": "hopscotch_highsec.onnx",
        "hand_number_puzzle": "hand_number_puzzle.onnx",
    }

    def __init__(self, model_dir: Path | str = "models/arkose",
                 initial: str = "dicematch"):
        self.model_dir = Path(model_dir)
        self.initial = initial
        self._sessions: dict = {}
        self._loaded: dict = {}

    # -- model handling -------------------------------------------------------

    def _session(self, variant: str):
        if variant in self._sessions:
            return self._sessions[variant]
        filename = self.VARIANTS.get(variant) or self.VARIANTS[self.initial]
        path = self.model_dir / filename
        if not path.exists():
            raise SolveError(f"missing model {path}")
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise SolveError("onnxruntime not installed") from exc
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 1
        session = ort.InferenceSession(str(path), opts,
                                       providers=["CPUExecutionProvider"])
        names = [i.name for i in session.get_inputs()]
        self._loaded[variant] = ("input_left" in names and "input_right" in names)
        self._sessions[variant] = session
        return session

    @staticmethod
    def _variant_for(instruction: str) -> str:
        text = (instruction or "").lower()
        if "number of" in text and ("symbol" in text or "dice" in text):
            return "counting"
        if "dice" in text and ("total" in text or "add" in text or "sum" in text):
            return "dice_pair"
        if "dice" in text:
            return "dicematch"
        if "shadow" in text:
            return "shadows"
        if "knot" in text:
            return "knotsCrossesCircle"
        if "penguin" in text:
            return "penguins"
        if "rotate" in text or "rollball" in text or "facing" in text:
            return "3d_rollball_objects"
        return "dicematch"

    @staticmethod
    def _tensor(data: bytes):
        import io

        import numpy as np

        try:
            from PIL import Image
        except ImportError as exc:
            raise SolveError("pillow not installed") from exc
        image = Image.open(io.BytesIO(data)).convert("RGB").resize((52, 52))
        return np.asarray(image, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0

    # -- solver interface -----------------------------------------------------

    def choose(self, puzzle: Puzzle) -> int:
        import numpy as np

        variant = self._variant_for(puzzle.instruction)
        session = self._session(variant)
        if puzzle.key is None:
            raise SolveError("no key image captured")
        left = self._tensor(puzzle.key)

        best_score = -np.inf
        best_index = -1
        for index, data in sorted(puzzle.images.items()):
            right = self._tensor(data)
            if self._loaded.get(variant, True):
                score = float(session.run(None, {"input_left": left,
                                                 "input_right": right})[0][0][0])
            else:
                name = session.get_inputs()[0].name
                score = float(session.run(None, {name: right})[0][0][0])
            if score > best_score:
                best_score, best_index = score, index
        if best_index < 0:
            raise SolveError("no candidates scored")
        return best_index


class FileSolver:
    """Hand the puzzle to a human (or an agent) through the filesystem.

    Writes the round to <root>/round-N/{key.img,cand*.img} and waits for
    <root>/round-N/answer to contain the chosen 1-based index. Used to prove the
    pipeline end to end without a model in the loop.
    """

    name = "file"

    def __init__(self, root: Path, timeout: float = 240.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.round = 0

    def choose(self, puzzle: Puzzle) -> int:
        self.round += 1
        folder = self.root / f"round-{self.round}"
        folder.mkdir(parents=True, exist_ok=True)
        if puzzle.key:
            (folder / "key.img").write_bytes(puzzle.key)
        for i, data in puzzle.images.items():
            (folder / f"cand{i}.img").write_bytes(data)
        (folder / "instruction.txt").write_text(puzzle.instruction)
        (folder / "meta.json").write_text(json.dumps({"total": puzzle.total}))

        target = folder / "answer"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if target.exists():
                text = target.read_text().strip()
                if text:
                    try:
                        return _clamp(int(text.split()[0]), puzzle)
                    except (ValueError, SolveError):
                        pass
            time.sleep(0.4)
        raise SolveError(f"no answer written to {target} within {self.timeout:.0f}s")


class ProbeSolver:
    """Deterministic pick for plumbing tests — no model, always the middle candidate."""

    name = "stub"

    def choose(self, puzzle: Puzzle) -> int:
        return max(1, min(puzzle.total, puzzle.total // 2 + 1))


def build_vision_solver(provider: str, api_key: str = "", *,
                        model: str = "", base_url: str = "",
                        keys: list[str] | None = None) -> VisionSolver:
    provider = (provider or "gemini").lower()
    if provider == "gemini":
        return GeminiSolver(api_key, model=model or "gemini-2.5-flash", keys=keys)
    if provider in ("local", "ollama", "vllm", "openai"):
        return OpenAICompatSolver(base_url or "http://localhost:11434/v1",
                                  model or "qwen2.5vl:3b", api_key=api_key or "local")
    if provider == "stub":
        return ProbeSolver()
    if provider == "file":
        return FileSolver(base_url or "debug/pending")
    if provider == "onnx":
        return OnnxSolver(model or "models/arkose")
    raise SolveError(f"unknown vision provider: {provider}")


# --------------------------------------------------------------------------- orchestration


def _frame_gone(frame) -> bool:
    """True only when Arkose tore the iframe down — a fail screen is not a pass."""
    try:
        return frame.is_detached()
    except Exception:
        return True


def _signature(frame) -> str:
    """Changes when Arkose advances to a new round."""
    try:
        return frame.evaluate(
            "() => { const h = document.querySelector('h2 span[role=text], h2 .text');"
            " const k = document.querySelector('.key-frame-image');"
            " return (h ? h.innerText : '') + '|' + (k ? (getComputedStyle(k).backgroundImage"
            " || k.currentSrc || '').slice(-40) : ''); }")
    except Exception:
        return ""


def _verdict(frame, before: str) -> bool | None:
    """Unused per-round verdict hook. Arkose advances the round regardless of
    whether the answer was right, so a round that advanced is not a correct
    answer. Only the challenge-level outcome is trustworthy."""
    return None


def solve_visual(page, frame, tap: ImageTap, solver: VisionSolver, rng=None,
                 out_dir: Path | None = None, max_rounds: int = 12,
                 log=print) -> int:
    """Solve rounds until the frame goes away. Returns rounds answered.

    Per-round feedback does not exist: Arkose moves to the next round whether or
    not the pick was right, and only the challenge as a whole passes or fails.
    So every capture is buffered and stamped with the final outcome — a pass is
    a high-precision label for every round it contained.
    """
    solved = 0
    collected: list[tuple[Puzzle, int]] = []
    deadline = time.time() + 15.0
    while time.time() < deadline and read_cursor(frame)["done"]:
        page.wait_for_timeout(300)
    if read_cursor(frame)["done"]:
        log("    visual: game surface never rendered")
        return solved

    for _ in range(max_rounds):
        if read_cursor(frame)["done"]:
            break
        puzzle = watch(frame, tap, page)
        if not puzzle.ready():
            log(f"    visual: incomplete puzzle ({puzzle.context()}), skipping")
            break
        try:
            index = solver.choose(puzzle)
        except SolveError as exc:
            log(f"    visual: solver failed ({exc})")
            break
        log(f"    visual: {solver.name} -> {index}/{puzzle.total}  [{puzzle.instruction[:60]}]")

        page_to(frame, index, page, rng)
        submit(frame, page, rng)
        collected.append((puzzle, index))
        solved += 1
        page.wait_for_timeout(2600)
        tap.key = None
        tap.images.clear()
        if _frame_gone(frame) or read_cursor(frame)["done"]:
            break

    passed = _frame_gone(frame)
    if collected:
        log(f"    visual: frame {'torn down' if passed else 'still up'} "
            f"after {len(collected)} round(s)")
    if out_dir is not None:
        for puzzle, index in collected:
            _save(out_dir, puzzle, index, solver.name, True if passed else None)
    return solved


def _save(out_dir: Path, puzzle: Puzzle, index: int, solver: str,
          accepted: bool | None) -> None:
    """Write one training example: key, candidates, instruction, pick, verdict."""
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        verdict = {True: "ok", False: "bad", None: "unknown"}[accepted]
        d = out_dir / f"{stamp}-{verdict}-{index}"
        d.mkdir(parents=True, exist_ok=True)
        if puzzle.key:
            (d / "key.img").write_bytes(puzzle.key)
        for i, data in puzzle.images.items():
            (d / f"cand{i}.img").write_bytes(data)
        (d / "label.json").write_text(json.dumps({
            "instruction": puzzle.instruction,
            "total": puzzle.total,
            "index": index,
            "solver": solver,
            "accepted": accepted,
        }, indent=1))
    except Exception:
        pass
