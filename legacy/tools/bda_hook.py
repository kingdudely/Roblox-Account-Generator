#!/usr/bin/env python3
"""Watch Arkose build the `c` blob, from inside the page.

Why this exists
---------------
`c` is what `/fc/gt2/` wants now. It replaced `bda`, it is ~10 KB of base64 that
decodes to three segments (28 / 256 / 7171 bytes), and none of the crypto code
that produces it lives in the statically-fetchable `api.js` — the module that
owns it arrives with the enforcement bundle and is minified per build. Chasing
it statically costs a day per version bump.

So take it at runtime instead. Patch `crypto.subtle` before any page script
runs, run a real signup up to the point where gt2 fires, and read back exactly
which primitives were used with which key material. That is the difference
between "the blob looks like AES-GCM plus an RSA-wrapped key" and knowing.

One run answers all of it:

    algorithm   AES-GCM / RSA-OAEP / ECDH, and the parameter object
    iv          length and bytes
    key         imported raw (then it is symmetric) or spki (then it is RSA)
    plaintext   the exact JSON going in — the same structure our build_bda emits
    output      the blob, to diff against ours segment by segment

    ./tools/bda_hook.py --proxy-file proxies3.txt --wait 60
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import captcha as cap          # noqa: E402
from roblox_creator.browser import DESKTOP_PROFILES, BrowserSession  # noqa: E402
from roblox_creator.config import Settings, load_proxies  # noqa: E402
from roblox_creator.creator import Creator         # noqa: E402
from roblox_creator.identity import make_identity  # noqa: E402
from roblox_creator.store import AccountStore      # noqa: E402

HOOK = r"""
(() => {
  const log = [];
  const MAX = 4096;
  const b64 = (buf) => {
    try {
      const bytes = new Uint8Array(buf);
      let s = "";
      const CH = 0x8000;
      for (let i = 0; i < bytes.length; i += CH) {
        s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
      }
      return btoa(s);
    } catch (e) { return "!" + String(e); }
  };
  const txt = (buf) => {
    try { return new TextDecoder().decode(new Uint8Array(buf)).slice(0, MAX); }
    catch (e) { return "!" + String(e); }
  };
  const alg = (a) => {
    if (!a) return null;
    if (typeof a === "string") return { name: a };
    const out = {};
    for (const k of Object.keys(a)) {
      const v = a[k];
      out[k] = (v instanceof ArrayBuffer || ArrayBuffer.isView(v)) ? "<bytes:" + v.byteLength + ">" : v;
    }
    return out;
  };
  const push = (kind, data) => { try { log.push(Object.assign({ kind, t: Date.now() }, data)); } catch (e) {} };

  const proto = Object.getPrototypeOf(crypto.subtle);
  const wrap = (name) => {
    const orig = proto[name];
    if (typeof orig !== "function") return;
    proto[name] = function (...args) {
      let info = { args: args.map((a) => (typeof a === "string" ? a : alg(a))) };
      try {
        if (name === "importKey") {
          info.format = args[0];
          info.keyData = b64(args[1]);
          info.extractable = args[3];
          info.usages = args[4];
          info.algorithm = alg(args[2]);
          push("importKey", info);
        } else if (name === "encrypt" || name === "decrypt") {
          info.algorithm = alg(args[0]);
          info.keyAlgorithm = args[1] && args[1].algorithm ? alg(args[1].algorithm) : null;
          info.keyType = args[1] && args[1].type;
          info.dataText = txt(args[2]);
          info.dataB64 = b64(args[2]);
          push(name, info);
        } else if (name === "digest") {
          info.algorithm = alg(args[0]);
          info.dataText = txt(args[1]);
          push(name, info);
        } else {
          push(name, info);
        }
      } catch (e) { push(name + "-logerr", { err: String(e) }); }

      const result = orig.apply(this, args);
      if (name === "encrypt" || name === "digest") {
        return result.then((r) => { push(name + "-out", { b64: b64(r) }); return r; });
      }
      if (name === "importKey") {
        return result.then((k) => {
          try {
            push("importKey-out", { type: k.type, alg: alg(k.algorithm),
                                    extractable: k.extractable, usages: k.usages });
          } catch (e) {}
          return k;
        });
      }
      return result;
    };
  };
  ["importKey", "encrypt", "decrypt", "digest", "sign", "deriveKey", "deriveBits"].forEach(wrap);

  const rand = crypto.getRandomValues.bind(crypto);
  crypto.getRandomValues = function (buf) {
    const r = rand(buf);
    try { log.push({ kind: "getRandomValues", t: Date.now(), len: buf.byteLength, b64: b64(buf) }); } catch (e) {}
    return r;
  };

  Object.defineProperty(window, "__arkCrypto", { value: log, configurable: true });
  window.__arkCryptoDump = () => JSON.stringify(log);
})();
"""


def dump_frames(page) -> list[dict]:
    out = []
    for frame in page.frames:
        try:
            raw = frame.evaluate("() => (window.__arkCryptoDump ? window.__arkCryptoDump() : null)")
        except Exception:
            continue
        if not raw:
            continue
        try:
            entries = json.loads(raw)
        except Exception:
            continue
        if entries:
            out.append({"url": frame.url, "entries": entries})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--profile", type=int, default=None,
                    help="index into browser.DESKTOP_PROFILES; default = random. "
                         "Varying it is what makes harvested payloads differ.")
    args = ap.parse_args()

    proxy = args.proxy
    if not proxy and args.proxy_file.exists():
        pool = load_proxies(args.proxy_file)
        proxy = random.choice(pool)

    out = ROOT / "debug" / "bda" / (time.strftime("%Y%m%d-%H%M%S") + (f"-{args.tag}" if args.tag else ""))
    out.mkdir(parents=True, exist_ok=True)

    settings = Settings(headless=args.headless)
    store = AccountStore(Path("/tmp/opencode/bda_hook"))
    creator = Creator(settings, store)
    rng = random.Random()

    gt2_body: list[str] = []
    c_blob: list[str] = []

    with BrowserSession(settings) as sess:
        profile_index = (args.profile if args.profile is not None
                         else random.randrange(len(DESKTOP_PROFILES)))
        profile = DESKTOP_PROFILES[profile_index]
        print(f"  profile {profile_index}: "
              f"{profile['screen']['width']}x{profile['screen']['height']} "
              f"dpr={profile['device_scale_factor']} {profile['color_scheme']}",
              flush=True)
        ctx = sess.new_context(proxy=proxy, profile=profile)
        ctx.add_init_script(HOOK)
        page = sess.new_page(ctx)

        def on_request(req):
            if "/fc/gt2/" in req.url:
                body = req.post_data or ""
                gt2_body.append(body)
                for part in body.split("&"):
                    if part.startswith("c="):
                        c_blob.append(part[2:])

        page.on("request", on_request)

        creator._open(page)
        creator._fill(page, make_identity(rng))
        creator._submit(page)
        gate = cap.wait_for_gate(page, timeout=args.wait)
        print(f"  gate -> {gate}", flush=True)

        deadline = time.time() + 45
        while time.time() < deadline and not c_blob:
            page.wait_for_timeout(500)

        # dump while the page is still alive — the log lives in JS heap
        frames = dump_frames(page)
        print(f"  frames with crypto activity: {len(frames)}")

    print(f"  gt2 fired: {bool(c_blob)}")
    if c_blob:
        blob = c_blob[0]
        print(f"  c len={len(blob)}")
        (out / "c.txt").write_text(blob)
    if gt2_body:
        (out / "gt2-body.txt").write_text(gt2_body[0])

    entries_all: list[dict] = []
    for record in frames:
        entries_all.extend(record["entries"])
    if entries_all:
        (out / "crypto.json").write_text(json.dumps(entries_all, indent=1))

    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
