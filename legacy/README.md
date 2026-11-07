# Legacy Buy / Build pipelines

Restored from the pre-SSO tree (tag `pre-sso-cleanup`) so the old methods sit
next to the current Google SSO path. The current path lives at the repo root:
`extension/`, `main.py`, `cloud_identity.py`, `tools/sso_fleet.py`.

## Buy (BloxGen)

Third-party API; paid key, no captcha, no proxies.

```bash
python bloxgen_run.py --plot-only
python bloxgen_run.py --probe --key BLOX-...
python bloxgen_run.py --key BLOX-... --count 100
```

Writes to `accounts/bloxgen/`. Test one key and a small balance before scaling.

## Build (browser pipeline)

Playwright + system Chrome. The manual mode is the reliable one.

```bash
python run.py -n 1 --dry-run --allow-bad-egress
python run.py -n 1 --captcha manual
```

- `--dry-run` fills the form and stops before submit.
- `--captcha manual` keeps the window visible; solve the Arkose challenge when
  it appears and the run continues.
- `--captcha auto/vision/api` uses the old solvers. The browserless path was
  mid-migration when this tree was archived; it is not guaranteed to work
  against the current live challenge.
- A clean residential egress is recommended. `--allow-bad-egress` is a dev
  override, not a production setting.

## Layout

- `roblox_creator/` — the old package. `signup_form.py` was copied from the
  current tree because the pre-SSO snapshot did not carry it.
- `data/` — captured fingerprints and Arkose payload corpus used by the
  browserless/vision path.
- `tools/` — the old research probes. `tools/lens_upload.py` was removed
  because it contained a hardcoded Google API key.
- `docs/` — the old protocol notes (ARKOSE, REPOS, SOLVER, RUNTIME).

## Notes

- Do not commit `accounts/`, `profiles/`, `keys.txt`, `proxies*.txt`, `.env`,
  cookies, or captured sessions.
- The Google SSO method is the current automated path; see `../docs/SSO_SETUP.md`.
