# tools/ — reproducers, probes, and one-off instruments

Almost every file here exists to answer exactly one question about the live
system, and to answer it *without a browser*. When something breaks, find the
tool that proves that layer and run it before changing code — most of the
"regressions" in this project were one stale assumption in one layer.

Grouping below is by current relevance. Old instruments are kept because they
carry verified facts (see `docs/ARKOSE.md`), not because they are still run.

## Current path — the browserless chain

| tool | what it does |
|---|---|
| `pure_challenge.py` | signup 403 → blob → `c` → gt2 → PoW → `gfct`. Saves the wave-0 image. The reference implementation of the transport. |
| `pure_ca.py` | same chain, then answers waves against `/fc/ca/` with `--answers`. Prints the response shape per wave. |
| `powrun.js` | runs Arkose's signed PoW worker under Node (no DOM). Used by `pure_challenge.py`. |
| `powrun_host.js` | same worker in the host realm instead of a vm sandbox — faster, same output. |
| `waves_probe.py` | **the difficulty metric**: waves / gameType / instruction per attempt, appended to `debug/waves/<stamp>/rows.jsonl`. `--tag` labels a run; `--from-capture DIR` replays a captured desktop payload. |
| `fp_harvest.py` | turns a captured browser `crypto.json` into a reusable payload under `data/payloads/`; `--list` inventories the corpus. **This is the bottleneck now** — see the desktop/mobile finding in `docs/RUNTIME.md`. |
| `signup_direct.py` | page-warmed, full-header signup, to test whether the captcha can be avoided entirely. |
| `route_sweep.py` | sweeps every auth route in the Cloud API index and reports which sit behind the challenge middleware. Completes the CSRF handshake, so live routes do not read as 403 walls. **Answer in `docs/AUTH_SURFACE.md`: only `/v2/signup` and `/v2/signup/linked` are gated; every captcha-free route is a tombstone.** |
| `challenge_mix.py` | per-client-shape sample of which challenge flavour `/v2/signup` hands out — `captcha` (solvable) vs `captchav2` (not). Interleaved so risk drift cannot favour one variant. Records to `debug/shapes/observations.jsonl`; `--report` reads it back and prints what `client_shape.preferred()` would choose. |
| `endpoint_probe.py` | the narrower first cut of `route_sweep.py`, kept for the body-shape fuzz on `/v1/external/access`. |
| `suppress_probe.py` | does Roblox accept a suppressed (`sup=1`) token? Fresh token per variant. |
| `captchav2_probe.py` | can `captchav2` be completed without a puzzle? Six variants, one fresh challenge each. **Answer: no.** |
| `solve_live.py` | **the product loop.** chain → montage → solver → `/fc/ca/`, with `--create` to run the final hop. Uses the server as the labeller. `--solver {chatgpt,nopecha,vlm}`; `vlm` uses the OVH Qwen client (top-face prompt, no upload budget, no flagged identity) and `--provider` selects the endpoint. |
| `finalhop` (`roblox_creator/finalhop.py`) | the last hop: solved token → `/v2/signup`. Three named variants, tried in order, winner recorded. |
| `harvest_corpus.py` | drive N browser captures with **varied** viewport/DPR/dark-mode and fold each into `data/payloads/`. `--report` shows whether the corpus is actually diverse. |
| `digit_bank.py` | build `data/digits.npz` (the `xN` digit reader) from labelled key bands; `--check` reports **leave-one-out** accuracy. |
| `zip_upload_probe.py` | does the upload API accept an archive, and can the model see inside? **Uploads clean, conversation rejects: `invalid_image_format`.** |
| `nopecha_probe.py` | NopeCHA Arkose recognition: `--status`, `--all`, `--grid 3x2\|5x2`. Free tier is IP-bound and bans this host. |
| `pure_signup.py` | end-to-end signup. **Stale**: still calls the dead `challenge/v1/continue`. |
| `survival.py` | poll created accounts over time; the 18h requirement is measured here. |

## PoW forensics (used to establish the protocol)

| tool | what it proved |
|---|---|
| `pows_tap.py` | first capture of `/pows/*` bodies and the round loop |
| `pows_tap2.py` | full capture incl. worker messages; showed `pow_token` is constant across rounds |
| `powprobe.js` | Proxy around the worker's message — logs exactly which fields it reads |
| `powdigest.js` | logs every `sha512` the worker computes (input + output) |

Together these established that the search is `sha512(prefix + nonce) == target`,
that `iterations` is the nonce **+1**, and that `hash_rate = span / elapsedMs`.

## `c` / gt2

| tool | what it does |
|---|---|
| `bda_hook.py` | hooks `crypto.subtle` in the live page; produced the whole `c` scheme |
| `c_selftest.py` | mints `c` ourselves and posts it; `--from-capture` re-seals a known plaintext |
| `gt2_probe.py` | asks Arkose per proxy whether it returns a token *without* a puzzle |
| `gt2_replay.py` | replays a captured gt2 request verbatim through any proxy |
| `blob_probe.py` | hunted for `data[blob]`'s origin (found: the signup 403 metadata) |

## Solver

| tool | what it does |
|---|---|
| `wave_montage.py` | composes key + 10 candidates into one labelled image (5×2 grid, 2× upscale) |
| `wave_solver.py` | asks the anonymous ChatGPT client for the index; **one** montage image per wave (see `--label-offset`) |
| `chatgpt_anon.py` | anonymous ChatGPT over pure HTTP — sentinel, PoW, Turnstile, uploads, SSE |
| `vlm_bench.py` | score a vision model on `debug/vision/<round>/` without a browser |
| `symbol_counter.py` | count dice symbols by template correlation (no model, no network) |
| `symbol_bank.py` | build the template bank from captured waves |
| `dice_probe.py` | prototype symbol-card detection |

### Upload budget — settled

An anonymous ChatGPT identity allows **~18 uploads**. The solver now spends **one
per wave**: `wave_montage.build_from_strip` cuts the downloaded 2000x400 strip into
the key sprite plus ten numbered candidate tiles and composes them into a single
image. A desktop-payload challenge costs ~5.8 waves, so a whole challenge is ~6
uploads with room to spare.

Two dead ends, both measured, do not repeat them:

- **Zipping the wave** to carry ten images in one upload. The upload API accepts a
  `.zip` through all three stages and the conversation then rejects it with
  `invalid_image_format` (`zip_upload_probe.py`).
- **Reusing one identity** for several waves. It is flagged after the first turn
  (`403 Unusual activity`); a fresh identity per wave answers reliably.

Also note: guest replies are truncated by the server at roughly 150–200
characters, so the prompt must ask for the answer and nothing else.

## Proxy measurement — is the pool alive, and is it worth anything

Full write-up and every measured number: `docs/PROXIES.md`. Two stages that fail
for different reasons; never conflate them.

| tool | what it does |
|---|---|
| `pool_audit.py` | **run this before blaming the pool.** Per file: stage 1 reach (GET through the exit), stage 2 image (full chain then `/rtig/image`). Reports reach%, image%, and error classes. |
| `pool_diversity.py` | distinct hosts, /24s and /16s, plus the heaviest subnets. A file's line count is not its size. |
| `free_pool.py` | harvest 17 public lists, dedup, probe in parallel, optionally cap per /24, write a usable file. |
| `free_throughput.py` | 2 MB payload through the exit. A browser is not one request; 200-on-a-GET proves little. |
| `free_browser.py` | real signup through a free exit, verifies immediately before use, reports form / challenge type. |
| `exit_identity.py` | asks the exit to identify itself — ASN, org, hosting vs residential. |
| `image_lottery.py` | **is the image gate a quota or a lottery?** N mints per exit, wave 0 for each, with the cumulative request count recorded. Measured 53% grant per mint, no decay — see `docs/RUNTIME.md`. |
| `budget_recovery.py` | burn one exit, then re-mint on a schedule (1/5/15/30/60/120 min) to get the recovery curve. Reports IP identity per check so a rotated session cannot fake recovery. |
| `mount_direct.py` | can the game core mount as a top-level page, without the Roblox viewport? Blocked: the puzzle UI needs the gfct `string_table` injected by a parent. Findings in `docs/ARKOSE.md`. |
| `human_solve.py` | opens a real challenge and records a human solving it: every `/rtig/image` status, the **plaintext guesses** (via a `JSON.stringify` hook, before `encryptECData`), and per-wave screenshots. This is how the index convention was finally measured. |
| `browser_solver.py` | the working creator. Drives the browser, intercepts the `/rtig/image` strip per wave, answers with `roblox_creator/captcha_vision.py`, pages the carousel and submits. Do **not** substitute page screenshots — they catch the "3 done" animation and the model answers the progress number. `--presentation single` (default) sends one canvas: card on the left, numbered tiles beside it, ~1442 px wide. `--solver {auto,vision,onnx}` routes easy boards to the local matcher. |
| `verdict_probe.py` | **the labeller probe.** One live attempt that answers four questions about the failure screen: is the correct index in the `/fc/ca/` body, in the game frame's JS state/DOM, in the checked pixels, or nowhere? It answers every round with a cycling index (wrong answers still advance), captures the grading screen by element screenshot, and clicks **Try again** to see whether the board repeats. Offline, `--analyze <case>` re-runs the matcher on a saved capture. Matching is `roblox_creator/verdict.py`; measured 40/40 on human-labelled waves with a realistic perturbation. |
| `local_vlm_bench.py` | **does a local vision model work at all?** Scores a GGUF VLM against the human labels through `llama-server`, on the same canvas `browser_solver.py` sends to the hosted API. Chance is 10%; the hosted VLM measured 3.6%. `--report` prints the table, `--pair` runs a binary discrimination trial. Measured 0/9 across three models — see `docs/RUNTIME.md`, *"Local vision models: measured and rejected"*. |
| `label_ui.py` | **the teacher.** Walks every saved strip, card beside the numbered tiles, one click per wave, writes `debug/labels/labels.jsonl`. Nothing downstream can be scored, trained or trusted without it. |
| `harvest_labels.py` | folds grading-screen captures into `debug/labels/verdict.jsonl`, in the same schema `label_ui.py` writes. Accepts a crop only when it is a candidate, has a revealed index, and clears `--min-score`/`--min-margin`; a human label for the same strip always wins and disagreements are reported, not averaged. `--report` prints the set by teacher, run and rule. |
| `present_ab.py` | runs one strip through several presentations against the same rule and prints what each one answered and how it reasoned. Agreement is not proof, but a run where the answers scatter is a run where the pixels never arrived. |
| `train_matcher.py` | siamese tile matcher distilled from `labels.jsonl` **and** `verdict.jsonl` (`--labels a,b`). `--cv` is leave-one-run-out and is the only accuracy number worth quoting; `--export models/matcher.onnx` writes the two graphs `roblox_creator/matcher.py` loads. |
| `browser_wave_depth.py` | counts `/rtig/image` 200s from a real browser, to compare against the browserless chain's two. |
| `hold_watch.py` | presses the hold pill once and samples it every 400 ms with screenshots, to answer *when* the widget leaves. Answer: not while held — on release. Also the run that found the "Start Puzzle" consent screen. |
| `hold_fill_probe.py` | holds the pill for 12 s and snapshots the widget's subtree + screenshots each second, to find what carries the fill. |
| `hold_network.py` | lets a human press the pill while recording every request/response against the exact `pointerdown`, to see whether the gesture fires traffic automation is missing. |

| `form_probe.py` | maps both live CreateAccount layouts in one run: which birthday widget, whether the password is on screen one, and what the submit button does. It is what showed Roblox A/Bs a Radix two-step form against the old native `<select>` form. `--save-shots` keeps a screenshot per variant. |
| `submit_probe.py` | logs every request, console error and the button's own DOM across a submit, to answer *why nothing was sent*. Found `#signup-v2-password` and that step two has an unlabelled submit button. |
| `flow_probe.py` | walks the whole flow through `BrowserSession` and stops at the gate, reporting `hold` / `arkose` / `passive`. `--no-proxy` validates the form code on direct egress without a pool. |
| `hold_probe.py` | measures the press-and-hold panel at 150 ms resolution: what completion looks like, which hold lengths are accepted, and that rejection is *silent* (no `try again`, no progressbar). Writes a per-attempt timeline. |
| `hold_flow.py` | the resolving rule end to end: hold, retry on `try again`, and on the dead state close the modal and re-submit for a fresh draw. Reports what each submission drew and whether an account landed. |
| `hold_wire.py` | records every request, response and cookie change across a full gate cycle. Answer: there is no verification endpoint to call — the verdict is scored in `main.min.js` and sealed into `_px3`, and the traffic around the gesture is telemetry only. |
| `oauth_capture.py` | records a human Google-SSO signup end to end and keeps the session cookie. Proved the SSO path creates accounts with no captcha (no `/v2/signup`, no `/v2/captcha`, no Arkose), follows `sso-complete`, and validates the session from inside the browser. Re-rolls sessions until the Google button is present. |
| `create_probe.py` | smoke test for the assembled product path — real `Creator`, chosen exit, per-account status. A pass here means the loop creates accounts, not that a probe can drive a page. |

## Older instruments (kept for their findings)

- `dapib_capture.py`, `dapib_hook.py`, `dapib_verify.py` — the `dapib` answer
  transform (now `roblox_creator/dapib.py`), verified against shipped `578.js`.
- `redeem_probe.py` — made the page redeem a minted token; proved the old
  `captcha.roblox.com/v1/funcaptcha/signup/web` endpoint is dead.
- `px_*.py` — PerimeterX modal instrumentation. PX and Arkose are a coin flip
  here, not a chain.
- `aimode_*.py`, `lens_upload.py` — non-ChatGPT vision routes explored earlier.
- `dataset.py` — turns captured puzzles into a training set.
- `proxy_triage.py` — separates "pool dead" from "parser bug" (three different
  failure modes look identical).

## Conventions

- Every tool takes `--proxy` or `--proxy-file` and picks a random sticky exit.
- Tools write captures under `debug/<topic>/<timestamp>/` and nothing else.
- Nothing here is imported by `roblox_creator/` except `pure_challenge.py`,
  which is imported by `pure_ca.py` and `waves_probe.py` as the transport.
- All proxy strings go through `roblox_creator/proxyurl.py`; never hand-parse.
