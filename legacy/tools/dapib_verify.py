#!/usr/bin/env python3
"""Prove `tools/dapib.py` matches the shipped Arkose `578.js`, byte for byte.

This exists because the port is only useful if it is exact. Arkose validates the
transformed answer server-side, so "close enough" is a 400. The check is simple:
feed the same random answers to the real JS (in a stubbed browser context) and to
our Python, and diff.

The JS is executed with `process` absent, `document.hidden` false and a real
`document.activeElement`, so the environment suffix `q` stays empty — the same
state a genuine browser presents.

    ./tools/dapib_verify.py                       # default capture
    ./tools/dapib_verify.py --js path/to/578.js
    ./tools/dapib_verify.py --cases 200 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.dapib import transform  # noqa: E402

DEFAULT_JS = ROOT / "debug" / "dapib-hook" / "20261030-121507" / "dapib-live.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const src = fs.readFileSync(process.argv[3], 'utf8');

function runOne(answer) {
  const sandbox = {};
  vm.createContext(sandbox);
  // Prepend a browser-shaped prelude so the environment check inside the script
  // does not trip: no `process`, visible document, a live activeElement.
  const prelude = `
    var __cap = null, __msg = null, __isErr = false;
    var document = { hidden: false, visibilityState: 'visible', activeElement: {} };
    var window = {
      document: document,
      requestAnimationFrame: function(){},
      cancelAnimationFrame: function(){}
    };
    window.parent = { ae: { answer: [${JSON.stringify(answer)}],
                            dapibReceive: function(o){
                              __cap = o.tanswer;
                              __isErr = (o.tanswer instanceof Error);
                              __msg = (o.tanswer && (o.tanswer.message || o.tanswer.stack)) || null;
                            } } };
    var __out = function(){ return __cap; };
    var __err = function(){ return __isErr ? __msg : null; };
  `;
  try {
    vm.runInContext(prelude, sandbox, { timeout: 5000 });
    vm.runInContext(src, sandbox, { timeout: 5000 });
    return { ok: true, value: sandbox.__out(), err: sandbox.__err() };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

const out = cases.map(runOne);
process.stdout.write(JSON.stringify(out));
"""


def make_case(rng: random.Random) -> dict:
    """A plausibly shaped Arkose answer object: string keys, numeric strings."""
    keys_pool = [str(i) for i in range(1, 12)] + ["a", "b", "c", "x", "k"]
    n = rng.randint(1, 6)
    keys = rng.sample(keys_pool, min(n, len(keys_pool)))
    obj = {}
    for k in keys:
        # values in the wild are digit strings of a few chars, but the port has
        # to survive the edges too: empty, single char, and long enough that the
        # length splice crosses into two digits, plus non-ASCII.
        roll = rng.random()
        if roll < 0.08:
            obj[k] = ""
        elif roll < 0.14:
            obj[k] = "".join(rng.choice(string.printable[:94]) for _ in range(rng.randint(20, 40)))
        elif roll < 0.18:
            obj[k] = "".join(chr(rng.randint(33, 400)) for _ in range(rng.randint(1, 6)))
        else:
            length = rng.randint(1, 7)
            obj[k] = "".join(rng.choice(string.digits) for _ in range(length))
    return obj


def normalise(value) -> object:
    """The JS hands back raw JS object shapes; compare as parsed JSON."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--js", type=Path, default=DEFAULT_JS)
    ap.add_argument("--cases", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1337)
    args = ap.parse_args()

    if not args.js.exists():
        print(f"no captured dapib at {args.js}")
        print("run tools/dapib_capture.py first")
        return 2

    rng = random.Random(args.seed)
    batches = [[make_case(rng) for _ in range(args.cases)]]

    workdir = Path(tempfile.mkdtemp(prefix="dapibverify-"))
    harness_path = workdir / "harness.js"
    harness_path.write_text(HARNESS, encoding="utf-8")

    failures = 0
    total = 0
    for bi, batch in enumerate(batches):
        cases_path = workdir / f"cases{bi}.json"
        cases_path.write_text(json.dumps(batch), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(harness_path), str(cases_path), str(args.js)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            print("node failed:", proc.stderr[:600])
            shutil.rmtree(workdir, ignore_errors=True)
            return 1
        results = json.loads(proc.stdout)

        for answer, res in zip(batch, results):
            total += 1
            if not res.get("ok"):
                print(f"  JS threw on {answer}: {res.get('error')}")
                failures += 1
                continue
            if res.get("err"):
                failures += 1
                if failures <= 3:
                    print(f"\n  dapib threw internally on {answer}:")
                    print(f"    {res['err'][:400]}")
                continue
            want = normalise(res["value"])
            got = transform([answer], q="")
            if want != got:
                failures += 1
                if failures <= 3:
                    print(f"\n  MISMATCH on {answer}")
                    print(f"    js:     {json.dumps(want)[:300]}")
                    print(f"    python: {json.dumps(got)[:300]}")

    shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n  {total - failures}/{total} cases identical")
    if failures:
        print("  PORT IS NOT TRUSTWORTHY — do not submit with it")
        return 1
    print("  port verified: the Python transform reproduces 578.js exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
