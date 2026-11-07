# External repos — what each is worth, measured not guessed

A survey of every FunCaptcha/Arkose repo surfaced, with the specific thing to take
from each. None of them is a drop-in: the closest sibling is archived, and the
rest solve a different flavour of the problem.

## The three that matter

### Negt-dev/Funcaptcha-Solve-RSA — Go, 63★, archived, "no longer operational"

The closest thing to this repo that exists publicly. Its layout is our layout:

```
fingerprint/  pick + refresh   ->  arkcrypto/  encrypt BDA  ->  transport/  TLS/JA3/H3
                              ->  Arkose: api.js, /fc/gt2/, /fc/gfct/, /fc/ca/
                              ->  recognition/ answer waves
```

Concretely useful:

- `internal/rsakey/extract.go` — recovers the RSA SPKI from string literals in the
  served JS (`scanStringLiterals` → `recoverSPKIFromLiteral` → `keyToPEM`). This is
  the maintenance path for the constant in `roblox_creator/bda.py`.
- `rsa_keys.json` — keys indexed by `ark_build_id`, with a `"*"` wildcard fallback.
  **Their Roblox key is not ours** (`mxqSOyLrb2S…` vs our `6MllTnWfhMn…`), which is
  direct evidence that the key rotates per Arkose build.
- `internal/fingerprint/refresh.go` — how to grow a fingerprint pool automatically,
  which is the work `tools/harvest_corpus.py` does by hand today.
- `internal/pow/deobfuscate.go` — a second opinion on the PoW worker shim.

**Caveat that outranks the code:** `extract.go` has nothing to extract from the
bundles we can actually fetch. Grepping our cached enforcement bundle, `api.js`
and the game-core chunks for `MII` returns zero RSA bodies (the only long base64
blob is an XML resource). So key recovery is not available for this deployment
from static JS; the practical refresh remains `tools/bda_hook.py`, i.e. capture
one browser `c` and re-derive.

### Wazza0/arkose-funcaptcha-image-solver — Python, pushed 2026-10-28

Solves the **counting game we are facing**: "rockstack, where you get six tiles of
objects alongside a target number and have to click the tile with that many
objects." That is `MatchDiceIconCount` with rocks instead of dice.

Two things, one of which is worth more than the code:

1. **The methodology note is the most valuable paragraph in any of these repos.**
   Pseudo-labelling 600 rounds with GPT-4 gave 73% on validation and **22% in
   production**. The cause: the labeller had a consistent per-count bias, the
   model learned the bias, and validation was measuring "how well does this
   imitate GPT". Real labels plus a plain ResNet18 at 224×224 got ~100%.
   → Never score or train our solver against another VLM's answers. Use
   `/fc/ca/`, where the server is the labeller.
2. A working recipe: fine-tuned ResNet18, one checkpoint for counting objects and
   one for reading the target digit, plus a pure-OpenCV path for the "orbit"
   variant that needs no weights.

### kekeds/funcaptcha-solver — Python, 106★

"100% requests based, fully reversed BDA, unflagged asf" — and it carries a
`roblox_register` preset with our exact sitekey. Two caveats before the prize:

- Its `decrypt_bda.py` is the **retired AES-CBC** scheme (key from
  `arkose.js` `genkey(ua, s)` + a 21600-second time bucket), not the live
  AES-GCM + RSA-OAEP `c`. So its crypto is behind ours.
- It is a *token* service, not a transport we would use.

The prize is the preset's `data` block, which describes what a real Roblox page
reports:

```python
"roblox_register": {
    "siteurl": "https://www.roblox.com",
    "sitekey": "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F",
    "apiurl": "https://arkoselabs.roblox.com",
    "data": {
        "window__ancestor_origins": ["https://www.roblox.com", "https://www.roblox.com"],
        "client_config__sitedata_location_href": "https://www.roblox.com/arkose/iframe",
        "window__tree_structure": "[[[]]]",
        "window__tree_index": [0, 0],
    },
}
```

`roblox_login` is the same but `"[[],[[]]]"` — the nesting differs per surface,
which tells you Arkose reads iframe depth.

**Correction to a hypothesis worth recording:** these are `enhanced_fp` entries,
not `data[...]` fields on the `gt2` form, and `roblox_creator/arkose_py.py`
**already emits all four** (lines ~434-439, from `site_url`). Replaying captured
desktop payloads carries the real values anyway. So this was not a missing lever —
checked before spending a probe run on it.

## Everything else

| repo | verdict |
|---|---|
| **AzureFlow/arkose-fp-docs** | Doc site: catalogue of FunCaptcha **sitekeys** (not RSA keys), plus `decrypt.html`, `breaker.html` (`apiBreakerVersion`), `mouse.html`. Reference for which FP fields Arkose cross-checks; worth reading when tuning coherence, nothing runnable. |
| **SimHub-UI/funcaptcha-solver** | Flask wrapper around kekeds (`arkose.js` + `webgl.json` copied verbatim) with Render deploy files. Adds `ROBLOX_LOGIN_SITEKEY` / `ROBLOX_REGISTER_SITEKEY` and `/fc/api/nojs/public_key/<sk>/configure`. Derivative; corroborates the presets. |
| **gcslaoli/funcaptcha-server** | Siamese net for `3D_Rollball_Animals`, 95%+, 54 MB of weights, 2023. Wrong game. Proves local CV is viable for Arkose art, nothing more. |
| **botswin/BotBrowser** | 2.6k★ Chromium fork with "unified fingerprint defense", pushed daily. **It is a browser**, so it contradicts the no-browser constraint. Only conceivable use is offline payload harvesting, at 700 MB. |
| **realasfngl/perimeter-x** | Deobfuscated PerimeterX 2.27 → 9.3.5. |
| **realasfngl/Perimeter-X-8.9.6** | A PX 8.9.6 solver with a documented flag payload. Both are contingency only — Roblox signup is Arkose, not PX. |
| **realasfngl/geetest_v4** | GeeTest v4. Roblox does not use GeeTest. Unused. |
| **keepitLowkey/py-funcaptcha** | 2020, old BDA, Roblox login sitekey `9F35E182-…` and `roblox-api.arkoselabs.com` (the API host has since moved to `arkoselabs.roblox.com`). Historical. Confirms the same-IP token rule. |
| **pyProjects3/RobloxPyApi3** | 2021 artifact, 8 commits, 2★, unmaintained. **Not browserless** — it calls `webbrowser.open('/fc/gc/?token=…')` and waits for a human, detecting completion with `pyautogui.locateOnScreen('Check.png')`. Every URL points at the dead `roblox-api.arkoselabs.com` host, and it has no PoW, no `/fc/ca/`, no wave submission. Two things survive: (a) its `GetDetailsFromFieldData2` corroborates the signup `fieldData` envelope by independently string-slicing the same `dxBlob` / `unifiedCaptchaId` fields our `README.md` documents; (b) the **direct-mount pattern** — running the challenge as a top-level page rather than inside the 200-asset Roblox viewport. Test that pattern against the current game-core URL, not against `/fc/gc/`. |
| **kiyogo/glowing-octo-train** | Playwright Facebook account factory + Messenger sender. Browser-based, no Arkose internals. |
| **lesleyxyz/ChatGPT (patch-1)** | Four commits, the first "Added zip file support". See below — dead. |
| **realasfngl/ChatGPT** | Our upstream. Our local `tools/cgpt/` implements a newer wire format than it. |

## The zip question, settled

`lesleyxyz/ChatGPT` `patch-1` adds `upload_file(..., is_image=False, is_zip=False)`
to `wrapper/chatgpt.py`. It does not help, for three independent reasons:

1. **Attachment shape.** It still attaches a `file-service://` pointer, exactly like
   `tools/chatgpt_anon.py`. Vision is not an archive parser — a zip contains no
   pixels for the model to see.
2. **The live API refuses it.** `tools/zip_upload_probe.py` walked all three upload
   stages successfully (`/backend-anon/files` → blob `PUT` →
   `process_upload_stream` returns `Succeeded processing`) and then the conversation
   returned `{"detail":"We couldn't process this image. Error code:
   invalid_image_format."}`. The refusal is server-side, after the upload.
3. **It is older than what we run.** The patch is 2025-09-14 against a parent last
   pushed 2025-10-02; our `tools/cgpt/` handles an October-2026 wire format.

And the budget arithmetic removes the motive: a replayed desktop payload costs
~5.8 waves, and one montage per wave is ~6 uploads against an 18-upload identity
budget. Zips would save HTTP calls we were never short of.
