# roblox_creator/ — package reference

Two pipelines live here:

- **BloxGen** (`bloxgen.py`) — buy accounts from a vendor. No captcha, no proxies.
  Used by `bloxgen_run.py`.
- **Build** (`creator.py` + `captcha.py` + `browser.py`) — drive the real signup
  flow. Being replaced, hop by hop, by the browserless path in `docs/RUNTIME.md`.

When in doubt about protocol details, `docs/ARKOSE.md` is authoritative — the
module docstrings summarise, that file has the measurements.

## Browserless building blocks (current work)

| module | role |
|---|---|
| `bda.py` | builds the `c` envelope: AES-256-GCM + RSA-OAEP. Verified byte-for-byte against a live capture. Its docstring explains the retired AES-CBC format too. |
| `arkose_py.py` | fingerprints (`Fingerprint`, `load_fingerprints`), the `enhanced_fp` builder (`build_enhanced_fp`), murmur3 x64-128 (`x64hash128`), and the legacy `build_bda`. **`build_enhanced_fp` decides whether Arkose suppresses — see docs.** |
| `ca.py` | `/fc/ca/` wire format: CryptoJS-compatible `encryptECData`/`decryptECData` (EvpKDF + AES-256-CBC), `bio`/`ecdata` blobs, `build_form`. |
| `dapib.py` | the answer transform that produces `tguess`. Currently optional: the server accepted a `/fc/ca/` body with `guess` alone. |
| `payloads.py` | the captured-payload corpus (`data/payloads/`): `load_payloads`, `pick_payload` (prefers desktop), `reseal` (fresh `n`). Desktop payloads get **5 waves instead of 10** — see `docs/RUNTIME.md`. |
| `identity.py` | makes usernames/passwords that pass Roblox's client-side rules. |
| `proxyurl.py` | **the only** proxy parser. Handles socks5/socks5h/http, remote DNS, masking for logs. Every loader delegates here. |

## Browser pipeline (being retired, still the reference for UI behaviour)

| module | role |
|---|---|
| `browser.py` | Playwright session/context/page creation. |
| `creator.py` | the signup flow in UI terms: open page, fill, submit, wait for gate. |
| `captcha.py` | FunCaptcha handling: page hooks, blob extraction, token injection into the captured `onCompleted`, gate detection, and third-party solver adapters. |
| `vision.py` | the in-browser puzzle solver (DOM-shaped puzzles, image fetches, model adapters). |

## Infrastructure

| module | role |
|---|---|
| `config.py` | `Settings`, proxy-file loading, and the endpoint constants. Some `CAPTCHA_*` constants are stale after the endpoint moves — check against `docs/ARKOSE.md`. |
| `proxy_pool.py` | sticky session assignment and rotation. |
| `egress.py` | outbound-IP checks through a proxy. |
| `socks_bridge.py` | local SOCKS bridge so Playwright can use SOCKS with auth. |
| `ratelimit.py` | token buckets for per-proxy pacing. |
| `client_shape.py` | the request-shape lever for `/v2/signup`: which header sets exist, which ones are safe to impose on a real browser (`x-roblox-platform` only — never the UA), and `preferred()`, which returns a shape only when recorded evidence clears both a sample floor and a gain floor. Observations land in `debug/shapes/observations.jsonl`. See `docs/AUTH_SURFACE.md`. |
| `store.py` / `db.py` | where created accounts land (`accounts.jsonl`, cookies, SQLite). |
| `keypool.py` | BloxGen API keys, per-key pacing (1 alt / 25 s). |
| `human.py` | small timing/jitter helpers. |
| `bloxgen.py` | the vendor client. |

## Rules that bite

- `bda.build()` and `arkose_py.build_bda()` are different formats. `build` is
  live; `build_bda` is the retired AES-CBC blob kept for older deployments.
- `Fingerprint.from_raw()` expects one entry of `data/fingerprints.json`. Those
  are all **Android** captures; sending a mobile UA without mobile client hints
  is one of the known incoherences.
- `build_enhanced_fp()` substitutes hardcoded defaults for keys a fingerprint
  lacks (e.g. `speech_default_voice`). Those defaults belong to another device
  and are a coherence bug, not a fallback.
- Usernames must be retried on collision *and* on `captchav2` (the flavour is
  re-rolled per signup attempt).
- Do not add a second proxy parser. `proxyurl.parse()` or nothing.
