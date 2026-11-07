#!/usr/bin/env python3
"""Label saved puzzle strips by hand, fast. The teacher for any small model.

## Why this exists

Every accuracy claim in this repo so far rests on labels that were read off a
montage the labeler already distrusted, so the measured 2/10 was measuring bad
ground truth as much as a bad solver. Meanwhile a small ONNX model is the only way
to get from ~3 s of vision API per wave to milliseconds, and distillation needs one
thing that does not exist yet: **labels that are actually right**.

The human is the only valid teacher for this. The vision model is not — it answered
"jet", "compass", "Chinese character 水" and "watering can" for the same reference
card across four passes on 2026-11-01. Distilling a model from a teacher that
disagrees with itself teaches the student to disagree with itself, faster.

## What it does

Walks every saved strip under `debug/browser_solver/*/`, shows the reference card
enlarged next to the candidate tiles, and records one click per wave. Card on the
left, tiles in a grid on the right, numbered the way the carousel is numbered: 1 is
the top-left, counting left to right, continuing on the next row.

    ./tools/label_ui.py                 # serve on 8765 and open a browser
    ./tools/label_ui.py --port 9000 --root debug/browser_solver

Keyboard: `1`-`9` then `0` for tile 10, `s` to skip, `u` to undo the last label.

Writes `debug/labels/labels.jsonl`, one line per wave:

    {"strip": "...", "tile": 3, "card": "card_01.jpg", "rule": "Use the Arrows...",
     "when": "2026-11-01T20:11:04", "ms": 2140}

and prints agreement against whatever the solver answered on that run, which is the
number that matters: if a run scored 0/10 the labels prove it, and if it scored 8/10
this run was not worth labelling.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "debug" / "labels"

TILE_W = 200
CAND_H = 190
CARD_Y = 200
CARD_W = 150

RAW_STRIP = re.compile(r"strip_\d+\.jpg")

CARD_W_PX = 340     # on-screen width of the reference card column
TILE_MAX = 300      # cap on a single candidate cell


def _data_url(image: Image.Image, quality: int = 92) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _sharpen(image: Image.Image, percent: int = 110) -> Image.Image:
    return image.filter(ImageFilter.UnsharpMask(radius=2, percent=percent,
                                                threshold=2))


def describe(strip: Path, image: Image.Image) -> dict:
    """One labelable wave: the strip itself, plus the numbers the page needs.

    The strip is sent once rather than as eleven crops. At ~150 KB a strip that is
    ten times less data over the wire than inline tiles, and the page does the same
    cropping in CSS, so a queue of forty waves loads without a visible pause.
    """
    width, height = image.size
    count = max(1, width // TILE_W)

    rule = ""
    reply = strip.with_name(strip.stem + "_reply.txt")
    if reply.exists():
        for line in reply.read_text().splitlines():
            if line.startswith("rule:"):
                rule = line[5:].strip()
                break

    return {"strip": str(strip.relative_to(ROOT)),
            "img": _data_url(image, quality=88),
            "width": width, "height": height,
            "count": count, "card_y": CARD_Y, "card_w": CARD_W,
            "tile_w": TILE_W, "tile_h": CAND_H,
            "rule": rule}


def collect(root: Path, limit: int) -> list[Path]:
    """Raw `/rtig/image` strips, newest first.

    Matched by name, not by glob. `strip_*.jpg` also catches the derived
    `<stem>_target.jpg` and `<stem>_grid.jpg` montages the solver writes next to
    each strip, and those are already-upscaled crops — feeding one back into the
    cropper produces a zoomed wall of brick with no tiles in it. Only
    `strip_<n>.jpg` is the untouched 1000x400 or 2000x400 source.
    """
    found = [p for p in root.glob("*/strip_*.jpg") if RAW_STRIP.match(p.name)]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit] if limit else found


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>label</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; font:14px/1.4 system-ui, sans-serif; background:#12100f; color:#e8e2dc; }
 header { padding:9px 14px; background:#1d1a18; display:flex; gap:16px;
          align-items:baseline; border-bottom:1px solid #2e2926; flex-wrap:wrap; }
 header b { font-weight:600; font-variant-numeric:tabular-nums; }
 .dim { color:#8d8379; }
 main { display:flex; gap:12px; padding:12px 14px; align-items:flex-start; }
 .card, .cell { background-repeat:no-repeat; border-radius:4px; }
 .card { flex:0 0 auto; outline:1px solid #2e2926; }
 .grid { display:grid; gap:8px; }
 .cell { position:relative; cursor:pointer; outline:2px solid transparent; }
 .cell span { position:absolute; top:1px; left:5px; font:700 17px system-ui;
              color:#fff; text-shadow:0 0 5px #000, 0 0 3px #000; }
 .cell:hover { outline-color:#c8b8a4; }
 .cell.done { outline-color:#59b36b; }
 .rule { padding:7px 14px; color:#cbb9a2; }
 kbd { background:#2b2724; border-radius:3px; padding:1px 5px; font:12px monospace; }
</style>
<header>
  <b id="pos">-</b>
  <span class="dim" id="rule"></span>
  <span class="dim">click a tile / <kbd>1</kbd>-<kbd>0</kbd> &middot; <kbd>s</kbd> skip
  &middot; <kbd>u</kbd> undo</span>
</header>
<div class="rule" id="hint"></div>
<main>
  <div class="card" id="card"></div>
  <div class="grid" id="grid"></div>
</main>
<script>
const waves = WAVES;
const CARD_W = %(cardw)d;
let at = 0, started = Date.now(), undo = [];

const grid = document.getElementById('grid');
const card = document.getElementById('card');

// One scale for the whole session, measured against the first strip. Cards and
// tiles come from the same bitmap, so a single factor keeps them at identical
// feature size -- which is the entire point of the exercise.
function layout(w) {
  const avail = Math.max(900, Math.min(window.innerWidth - 40, 1700));
  const gap = 8;
  let tileW = Math.floor((avail - CARD_W - 6 * gap) / 5);
  tileW = Math.min(tileW, %(tilemax)d);
  return { tileW: tileW, cardW: CARD_W, gap: gap };
}

function paint(node, img, imgW, imgH, scale, sx, sy, sw, sh) {
  node.style.width = (sw * scale) + 'px';
  node.style.height = (sh * scale) + 'px';
  node.style.backgroundImage = 'url(' + img + ')';
  node.style.backgroundSize = (imgW * scale) + 'px ' + (imgH * scale) + 'px';
  node.style.backgroundPosition = (-sx * scale) + 'px ' + (-sy * scale) + 'px';
}

function render() {
  const pos = document.getElementById('pos');
  if (at >= waves.length) {
    pos.textContent = 'done \u2014 ' + waves.length + ' labelled';
    document.getElementById('rule').textContent = '';
    card.style.display = 'none'; grid.innerHTML = '';
    return;
  }
  const w = waves[at];
  const box = layout(w);

  pos.textContent = (at + 1) + ' / ' + waves.length;
  document.getElementById('rule').textContent = w.rule || '(no rule captured)';
  document.getElementById('hint').textContent =
    w.strip + '  \u00b7  ' + w.count + ' candidates';

  card.style.display = 'block';
  paint(card, w.img, w.width, w.height, box.cardW / w.card_w,
        0, w.card_y, w.card_w, w.height - w.card_y);

  const scale = box.tileW / w.tile_w;
  const rows = Math.ceil(w.count / 5);
  grid.style.gridTemplateColumns = 'repeat(5, ' + box.tileW + 'px)';
  grid.innerHTML = '';
  for (let i = 0; i < w.count; i++) {
    const d = document.createElement('div');
    d.className = 'cell';
    paint(d, w.img, w.width, w.height, scale,
          i * w.tile_w, 0, w.tile_w, w.tile_h);
    d.innerHTML = '<span>' + (i + 1) + '</span>';
    d.onclick = () => record(i + 1);
    grid.appendChild(d);
  }
  started = Date.now();
}

async function record(tile) {
  if (at >= waves.length) return;
  const w = waves[at];
  await fetch('/label', {method: 'POST', body: JSON.stringify(
    {strip: w.strip, tile: tile, rule: w.rule, count: w.count,
     ms: Date.now() - started})});
  undo.push(at);
  grid.children[tile - 1].classList.add('done');
  at++;
  setTimeout(render, 110);
}

document.addEventListener('keydown', (e) => {
  if (e.key === 's') { at++; render(); return; }
  if (e.key === 'u') {
    if (!undo.length) return;
    at = undo.pop(); fetch('/undo', {method: 'POST'}); render(); return;
  }
  if (!waves[at]) return;
  const n = e.key === '0' ? 10 : parseInt(e.key, 10);
  if (n >= 1 && n <= waves[at].count) record(n);
});

render();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    waves: list[dict] = []
    out: Path = OUT / "labels.jsonl"
    who: str = "human"

    def log_message(self, *args):  # quiet; the page is the interface
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (PAGE % {"cardw": CARD_W_PX, "tilemax": TILE_MAX}
                    ).replace("WAVES", json.dumps(self.waves))
            self._send(body.encode(), "text/html; charset=utf-8")
        else:
            self._send(b"", "text/plain", 404)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/label":
            row = json.loads(raw)
            row["when"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            row["who"] = self.who
            self.out.parent.mkdir(parents=True, exist_ok=True)
            with self.out.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            print(f"  {row['strip']} -> {row['tile']}", flush=True)
        elif self.path == "/undo":
            if self.out.exists():
                lines = self.out.read_text().splitlines()
                self.out.write_text("\n".join(lines[:-1]) + ("\n" if lines[:-1] else ""))
        self._send(b"ok", "text/plain")

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def report(out: Path) -> None:
    """How our labels compare to what the solver answered, run by run."""
    if not out.exists():
        return
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    if not rows:
        return
    by_run: dict[str, list[int]] = {}
    for row in rows:
        run = str(Path(row["strip"]).parent.name)
        by_run.setdefault(run, []).append(row["tile"])

    print(f"\n{len(rows)} labels in {out}")
    for run, tiles in sorted(by_run.items()):
        run_json = ROOT / "debug" / "browser_solver" / run / "run.json"
        detail = ""
        if run_json.exists():
            try:
                data = json.loads(run_json.read_text())
                answers = [w.get("answer") for w in data.get("waves", [])]
                hits = sum(1 for a, t in zip(answers, tiles) if a == t)
                detail = (f"  solver {hits}/{min(len(answers), len(tiles))} "
                          f"outcome={data.get('outcome')}")
            except Exception:
                pass
        print(f"  {run}: {len(tiles)} labelled{detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT / "debug" / "browser_solver")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--limit", type=int, default=60,
                    help="most recent strips to queue")
    ap.add_argument("--out", type=Path, default=OUT / "labels.jsonl")
    ap.add_argument("--who", default="human",
                    help="tag on each row; a second pass labels the same strips "
                         "into a different file so the two can be diffed")
    ap.add_argument("--open", action="store_true", help="open a browser")
    args = ap.parse_args()

    strips = collect(args.root, args.limit)
    if not strips:
        print(f"no strips under {args.root}")
        return 1

    waves = []
    for path in strips:
        try:
            waves.append(describe(path, Image.open(path).convert("RGB")))
        except Exception as exc:
            print(f"  skip {path.name}: {type(exc).__name__}: {exc}")

    Handler.waves = waves
    Handler.out = args.out
    Handler.who = args.who

    print(f"{len(waves)} waves queued from {args.root}")
    print(f"labels append to {Handler.out}")
    print(f"http://127.0.0.1:{args.port}/")

    report(Handler.out)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        report(Handler.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
