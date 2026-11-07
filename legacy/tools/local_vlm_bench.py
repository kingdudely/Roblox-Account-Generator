#!/usr/bin/env python3
"""Score local vision models against the human labels. Chance is 10%.

The question this answers is narrow and worth answering once: can a local VLM
read the card-to-candidate relation well enough to be a *teacher*? Every other
option in this repo has been measured and failed -

    raw NCC on the card           1/40    2%
    shadow-mask IoU               5/40   12%
    icons removed, shadow NCC     6/40   15%
    from-scratch CNN, 40 boards   2/40    2% (loss pinned at ln(10))

- and the hosted VLM in `captcha_vision.py` scores 3.6% on this task. So the
prior is that a local model at this size will also fail. That is exactly why the
experiment is worth running: if one of them clears chance by a wide margin it
becomes the labeller, and if none do, the local-VLM idea is closed with a number
rather than an opinion.

## Why llama.cpp and not transformers

This box has ~3 GB of usable RAM and no GPU. A 2B model in fp16 is 4 GB before
the vision encoder is counted; only a quantized GGUF fits.

## Why a server and not `llama-mtmd-cli`

Measured: the CLI streams generated tokens through the same logger as its
diagnostics, and it prints the chat-template example on startup. Filtering that
noise out by regex is guesswork that silently turns a log line into an "answer" -
the first smoke run scored a `1` that was a template artefact. `llama-server`
exposes `/v1/chat/completions`, so the answer arrives as JSON with nothing else
in the field. The model also loads once for the whole run instead of per board.

## What is measured

Per stripped board: the model is shown the same canvas `browser_solver.py` sends
to the hosted API (`strip_montage.single_image()` - reference card on the left,
numbered candidates beside it), asked the same question, and its `ANSWER=<n>` is
compared to the human label. Non-answers are scored as wrong, never dropped, so a
model that abstains does not look better than one that guesses.

    ./tools/local_vlm_bench.py --model smolvlm500 --limit 5
    ./tools/local_vlm_bench.py --model lfm3-2b
    ./tools/local_vlm_bench.py --report

Results are appended to `debug/vlm/<model>.jsonl` so a slow run can be resumed.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from roblox_creator import strip_montage

LABELS = ROOT / "debug" / "labels" / "labels.jsonl"
VERDICT_LABELS = ROOT / "debug" / "labels" / "verdict.jsonl"
OUT = ROOT / "debug" / "vlm"
MODELS_DIR = Path(os.environ.get("ARKOSE_VLM_DIR", Path.home() / ".cache" / "arkose-vlm"))

TILE_W = strip_montage.TILE_W

# The question, kept identical to the hosted solver's contract (`ANSWER=<n>`,
# 1-based, reference panel excluded) so the numbers are comparable.
PROMPT = """The image shows a puzzle. On the left is a reference panel. To its right are \
numbered candidate panels, numbered left to right and continuing on the next row.

The task: "{instruction}"

Study the reference panel, then decide which candidate panel shows the same \
thing the task asks for. Answer with the candidate's number, and end with exactly \
one line:

ANSWER=<number>"""

PROMPT_BLIND = """The image shows a puzzle. On the left is a reference panel. To its right \
are numbered candidate panels, numbered left to right and continuing on the next row.

Work out what relation the reference panel has to the candidates, then choose the \
candidate that best satisfies it. Answer with the candidate's number, and end with \
exactly one line:

ANSWER=<number>"""

# The binary form. Two panels only, so the task stops being "search a grid" and
# becomes "compare these two shapes" — the smallest question the puzzle can be
# decomposed into. If a model cannot clear 50% here it cannot see the relation at
# all, and no presentation or prompt fixes that.
PAIR_PROMPT = """Two panels are shown side by side. The left panel is a reference from a puzzle. \
The right panel is one candidate answer.

Decide whether the right panel shows the same hidden shape as the left panel, \
allowing for a different scale and position.

Answer with exactly one line:

ANSWER=SAME
or
ANSWER=DIFFERENT"""

DEFAULT_MODELS = {
    "smolvlm500": ("SmolVLM-500M-Instruct-Q8_0.gguf",
                   "mmproj-SmolVLM-500M-Instruct-Q8_0.gguf"),
    "lfm3-2b": ("LFM3-VL-2B-Q4_K_M.gguf", "mmproj-LFM3-VL-2B-Q8_0.gguf"),
    "qwen25vl-3b": ("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf",
                    "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf"),
}


def log(message: str) -> None:
    print(message, flush=True)


def find_server() -> Path | None:
    override = os.environ.get("LLAMA_SERVER")
    if override and Path(override).exists():
        return Path(override)
    found = shutil.which("llama-server")
    if found:
        return Path(found)
    for base in (Path("/tmp/opencode/lcpp"), Path.home() / ".cache" / "llama.cpp"):
        if not base.exists():
            continue
        for candidate in base.rglob("llama-server"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_boards(limit: int = 0) -> list[dict]:
    """Labelled boards, human labels preferred over harvested ones."""
    chosen: dict[str, dict] = {}
    for path in (LABELS, VERDICT_LABELS):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            strip = ROOT / row.get("strip", "")
            if not strip.exists() or not isinstance(row.get("tile"), int):
                continue
            key = str(strip)
            if key in chosen and chosen[key].get("who") != "verdict":
                continue
            chosen[key] = {"strip": strip, "tile": int(row["tile"]),
                           "rule": row.get("rule") or "",
                           "who": row.get("who") or "human"}
    boards = sorted(chosen.values(), key=lambda r: (str(r["strip"].parent),
                                                    r["strip"].name))
    return boards[:limit] if limit else boards


def render(board: dict, path: Path) -> int:
    """Write the canvas for one board, return the candidate count."""
    strip = Image.open(board["strip"]).convert("RGB")
    strip_montage.single_image(strip).save(path)
    return max(1, min(10, strip.width // TILE_W))


def build_prompt(instruction: str) -> str:
    template = PROMPT if instruction.strip() else PROMPT_BLIND
    return template.format(instruction=instruction.strip())


class Server:
    """llama-server bound to a free port, waited on, and reliably torn down."""

    def __init__(self, binary: Path, model: Path, mmproj: Path, threads: int,
                 context: int = 4096):
        self.binary = binary
        self.model = model
        self.mmproj = mmproj
        self.threads = threads
        self.context = context
        self.port = free_port()
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> "Server":
        command = [
            str(self.binary), "-m", str(self.model), "--mmproj", str(self.mmproj),
            "--port", str(self.port), "-t", str(self.threads),
            "-c", str(self.context), "--host", "127.0.0.1",
            "-np", "1", "--no-warmup",
        ]
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("llama-server exited during startup")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/health", timeout=2) as reply:
                    if reply.status == 200:
                        return self
            except Exception:
                time.sleep(1.0)
        raise RuntimeError("llama-server did not become healthy in 180 s")

    def __exit__(self, *exc) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def ask(self, image: Path, prompt: str, timeout: float = 600.0) -> tuple[str, float]:
        """One completion. Returns (all text the model produced, seconds).

        Both `content` and `reasoning_content` are collected, and the token cap is
        generous. Measured: LFM3-VL-2B is a reasoning model — with `max_tokens=64`
        it spent every token inside `reasoning_content` and returned an empty
        `content`, which the first pass scored as a non-answer. A thinking model
        needs room to think and its scratchpad read, or the benchmark measures the
        token cap instead of the model.
        """
        payload = {
            "model": "local",
            "temperature": 0,
            "seed": 0,
            "max_tokens": 768,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64,"
                               + base64.b64encode(image.read_bytes()).decode()}},
                ],
            }],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                body = json.loads(reply.read())
        except Exception as exc:
            return f"<error {type(exc).__name__}>", time.time() - started
        message = (body.get("choices") or [{}])[0].get("message", {}) or {}
        parts = [message.get("reasoning_content") or "",
                 message.get("content") or ""]
        return "\n".join(part for part in parts if part).strip(), \
            time.time() - started


def parse_answer(text: str, count: int) -> int | None:
    """Pull `ANSWER=<n>`, preferring the last one written.

    Last, not first: a reasoning model may mention a candidate early ("candidate
    4 looks similar, but...") and settle on a different one. The final statement
    is the answer; an earlier mention is deliberation.
    """
    matches = re.findall(r"ANSWER\s*[=:]\s*(\d{1,2})", text, re.I)
    if matches:
        value = int(matches[-1])
        return value if 1 <= value <= count else None

    # No explicit marker. Accept only a reply whose final non-empty line is a bare
    # number — anything looser turns deliberation into a guess.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        final = re.sub(r"^[#*>\s]*|[.)*\s]*$", "", lines[-1])
        if re.fullmatch(r"\d{1,2}", final):
            value = int(final)
            return value if 1 <= value <= count else None
    return None


def bench(args) -> int:
    binary = find_server()
    if binary is None:
        log("llama-server not found — set LLAMA_SERVER or unpack a release")
        return 1

    model_name, mmproj_name = DEFAULT_MODELS[args.model]
    model = MODELS_DIR / model_name
    mmproj = MODELS_DIR / mmproj_name
    for path in (model, mmproj):
        if not path.exists():
            log(f"missing {path}")
            return 1

    boards = load_boards(args.limit)
    if not boards:
        log("no labelled boards")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    results = OUT / f"{args.model}.jsonl"
    done: set[str] = set()
    if results.exists() and not args.force:
        for line in results.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("answer") is not None:
                done.add(row["strip"])
        if done:
            log(f"resuming: {len(done)} boards already answered")

    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "vlm_bench"
    scratch.mkdir(parents=True, exist_ok=True)

    log(f"model {args.model}  ({model.stat().st_size // 1048576} MB)  "
        f"boards {len(boards)}  chance 10%")
    hits = misses = skipped = 0
    started = time.time()
    with Server(binary, model, mmproj, args.threads) as server:
        log(f"server healthy on port {server.port}")
        with results.open("a") as sink:
            for number, board in enumerate(boards, 1):
                key = str(board["strip"])
                if key in done:
                    skipped += 1
                    continue
                image = scratch / f"board_{number:03d}.png"
                count = render(board, image)
                text, seconds = server.ask(image, build_prompt(board["rule"]),
                                           args.timeout)
                answer = parse_answer(text, count)
                correct = answer == board["tile"]
                hits += int(correct)
                misses += int(not correct)
                sink.write(json.dumps({
                    "strip": key, "model": args.model, "truth": board["tile"],
                    "answer": answer, "correct": correct, "count": count,
                    "who": board["who"], "seconds": round(seconds, 1),
                    "raw": text[-300:],
                }) + "\n")
                sink.flush()
                flag = "ok  " if correct else ("none" if answer is None else "MISS")
                log(f"  {number:>3}/{len(boards)} {flag} truth={board['tile']:>2} "
                    f"got={answer if answer is not None else '-':>4} {seconds:>6.1f}s")

    scored = hits + misses
    if scored:
        log(f"\n  {args.model}: {hits}/{scored} = {100.0 * hits / scored:.0f}% "
            f"(chance 10%)  {time.time() - started:.0f}s total")
    if skipped:
        log(f"  ({skipped} skipped, already answered)")
    return 0


def pair_board(board: dict, path: Path, rng: random.Random) -> tuple[int, int]:
    """Render the reference beside one candidate. Returns (position, is_match).

    The candidate is the true answer half the time and a random wrong one the
    other half, and `position` says whether it landed left (1) or right (2) — so a
    model that always answers the same side scores exactly 50%.
    """
    from PIL import ImageDraw

    strip = Image.open(board["strip"]).convert("RGB")
    geometry = strip_montage.layout(strip)
    count = geometry["count"]

    reference = strip.crop(geometry["card"]).convert("RGB")
    reference = reference.resize((reference.width * 3, reference.height * 3),
                                 Image.LANCZOS)

    truth = board["tile"]
    if rng.random() < 0.5:
        other = rng.choice([i for i in range(1, count + 1) if i != truth])
        candidate_index, is_match = other, 0
    else:
        candidate_index, is_match = truth, 1

    tile = strip.crop(((candidate_index - 1) * strip_montage.TILE_W, 0,
                       candidate_index * strip_montage.TILE_W,
                       strip_montage.CAND_H)).convert("RGB")
    tile = tile.resize((tile.width * 3, tile.height * 3), Image.LANCZOS)

    position = rng.choice((1, 2))
    panels = [reference, tile] if position == 1 else [tile, reference]
    gap, margin = 24, 24
    width = margin * 2 + sum(p.width for p in panels) + gap
    height = margin * 2 + max(p.height for p in panels)
    canvas = Image.new("RGB", (width, height), (245, 245, 245))
    left = margin
    for panel in panels:
        canvas.paste(panel, (left, margin))
        left += panel.width + gap
    ImageDraw.Draw(canvas)
    canvas.save(path)
    return position, is_match


def parse_pair(text: str) -> str | None:
    """SAME or DIFFERENT, preferring the last statement."""
    matches = re.findall(r"ANSWER\s*[=:]\s*(SAME|DIFFERENT)", text, re.I)
    if not matches:
        return None
    return matches[-1].upper()


def pair_bench(args) -> int:
    """Binary discrimination. 50% is chance; anything near it means hopeless."""
    binary = find_server()
    if binary is None:
        log("llama-server not found — set LLAMA_SERVER or unpack a release")
        return 1

    model_name, mmproj_name = DEFAULT_MODELS[args.model]
    model = MODELS_DIR / model_name
    mmproj = MODELS_DIR / mmproj_name
    for path in (model, mmproj):
        if not path.exists():
            log(f"missing {path}")
            return 1

    boards = load_boards(args.limit)
    trials = args.trials
    rng = random.Random(20261103)
    OUT.mkdir(parents=True, exist_ok=True)
    results = OUT / f"{args.model}-pair.jsonl"

    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "vlm_pair"
    scratch.mkdir(parents=True, exist_ok=True)

    log(f"model {args.model}  binary trials {trials}  chance 50%")
    hits = 0
    with Server(binary, model, mmproj, args.threads) as server:
        log(f"server healthy on port {server.port}")
        with results.open("w") as sink:
            for trial in range(1, min(trials, len(boards)) + 1):
                board = boards[trial - 1]
                image = scratch / f"pair_{trial:03d}.png"
                position, is_match = pair_board(board, image, rng)
                text, seconds = server.ask(image, PAIR_PROMPT, args.timeout)
                verdict = parse_pair(text)
                # Correct when the model says SAME and it is the match, or
                # DIFFERENT and it is not.
                correct = (verdict == "SAME") == bool(is_match) \
                    if verdict else False
                hits += int(correct)
                sink.write(json.dumps({
                    "board": str(board["strip"]), "model": args.model,
                    "candidate_at": position, "is_match": is_match,
                    "verdict": verdict, "correct": correct,
                    "seconds": round(seconds, 1), "raw": text[-400:],
                }) + "\n")
                sink.flush()
                log(f"  {trial:>3}/{trials} {'ok  ' if correct else 'MISS'} "
                    f"candidate={'R' if position == 2 else 'L'} "
                    f"match={int(is_match)} said={str(verdict):<9} {seconds:>6.1f}s")

    log(f"\n  {args.model} binary: {hits}/{min(trials, len(boards))} = "
        f"{100.0 * hits / max(1, min(trials, len(boards))):.0f}% (chance 50%)")
    return 0


def report() -> None:
    """Every model scored so far, side by side."""
    if not OUT.exists():
        log("no results yet")
        return
    log(f"{'model':<16}{'hits':>7}{'boards':>8}{'acc':>7}{'no-answer':>11}{'s/board':>9}")
    for path in sorted(OUT.glob("*.jsonl")):
        rows = []
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        if not rows:
            continue
        hits = sum(1 for r in rows if r.get("correct"))
        none = sum(1 for r in rows if r.get("answer") is None)
        mean = sum(r.get("seconds", 0) for r in rows) / len(rows)
        log(f"{path.stem:<16}{hits:>7}{len(rows):>8}"
            f"{100.0 * hits / len(rows):>6.0f}%{none:>11}{mean:>9.1f}")
    log("\nchance on a ten-candidate board is 10%.")
    log("hosted VLM (captcha_vision.py), measured 2026-11-01: 1/28 = 3.6%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=sorted(DEFAULT_MODELS), default="smolvlm500")
    ap.add_argument("--limit", type=int, default=0,
                    help="score only the first N boards (smoke test)")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--force", action="store_true",
                    help="re-score boards already in the results file")
    ap.add_argument("--pair", action="store_true",
                    help="binary discrimination trial instead of 1-of-10")
    ap.add_argument("--trials", type=int, default=20,
                    help="binary trials (chance 50%)")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report()
        return 0
    if args.pair:
        return pair_bench(args)
    return bench(args)


if __name__ == "__main__":
    raise SystemExit(main())
