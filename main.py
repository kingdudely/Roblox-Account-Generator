#!/usr/bin/env python3
"""Keys in, Roblox accounts out.

    python main.py --keys keys.txt --out accounts.txt

For every key in `keys.txt` (an array of Cloud Identity credentials, one tenant
each) this creates up to 50 identities over the Admin SDK, then turns each
identity into a Roblox account through Google SSO and appends the credentials
and `.ROBLOSECURITY` cookie to `--out`.

The browser is used for exactly one thing: the Google sign-in itself. Identity
creation, password resets, backup verification codes, the Roblox "one last
thing" step and the session check are all plain HTTPS.

    python main.py --keys keys.txt --create-only     # identities only
    python main.py --keys keys.txt --skip-create      # Roblox only, resume
    python main.py --keys keys.txt --delete --yes     # wipe the ledgers
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cloud_identity as ci

DEFAULT_KEYS = ROOT / "keys.txt"
DEFAULT_OUT = ROOT / "accounts.txt"


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------- output txt

BLOCK_RE = re.compile(r"^email:\s*(\S+)\s*$", re.MULTILINE)


def done_emails(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {m.lower() for m in BLOCK_RE.findall(path.read_text())}


def append_record(path: Path, record: dict, user: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f"username: {record.get('username')}\n"
        f"password: {user['password']}\n"
        f"email: {record.get('identity') or user['email']}\n"
        f"user_id: {record.get('user_id')}\n"
        f"cookie: {record.get('cookie')}\n"
        f"created_at: {record.get('created_at')}\n\n"
    )
    with path.open("a") as fh:
        fh.write(block)
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- delete phase

def phase_delete(keys: list[ci.Key], assume_yes: bool,
                 proxy: str | None = None) -> int:
    work = []
    for key in keys:
        if not key.domain:
            log(f"skip {key.label}: no domain on this key, cannot find its ledger")
            continue
        ledger = ci.open_ledger(key.domain)[1]
        if ledger:
            work.append((key, ledger))

    total = sum(len(users) for _, users in work)
    if not total:
        log("nothing to delete")
        return 0

    log(f"about to delete {total} identities across {len(work)} tenant(s)")
    log("addresses stay reserved ~20 days; seats free immediately")
    if not assume_yes:
        if not sys.stdin.isatty():
            log("refusing without --yes (not a terminal)")
            return 2
        if input("type 'delete' to continue: ").strip().lower() != "delete":
            log("cancelled")
            return 1

    failures = 0
    for key, ledger in work:
        log(f"\n{key.label}  ({len(ledger)} entries)")
        service = ci.service_for(key, proxy)
        result = ci.delete_users(service, ledger)
        failures += len(result["failed"])
    log(f"\ndone, {failures} failures")
    return 0 if not failures else 1


# ---------------------------------------------------------------- create phase

def phase_create(keys: list[ci.Key], count: int, dry_run: bool,
                 proxy: str | None = None) -> list[tuple]:
    """Create the identity ledgers. Returns [(key, service, users)]."""
    prepared = []
    for key in keys:
        if not key.domain:
            log(f"skip {key.label}: key has no domain -- add \"domain\" to keys.txt")
            continue
        ledger_file, users = ci.open_ledger(key.domain)
        target = min(count, ci.PER_TENANT)
        if len(users) < target:
            users = ci.generate_ledger(key.domain, target, users)
            if not dry_run:
                ci.save_ledger(ledger_file, users)

        log(f"\n{key.label}  ({len(users)} identities, {key.kind})")
        try:
            service = ci.service_for(key, proxy)
        except Exception as exc:
            log(f"  credential rejected: {type(exc).__name__}: {str(exc)[:110]}")
            continue

        result = ci.create_users(service, users, dry_run=dry_run)
        if result["blocked"]:
            log(f"  tenant is flagged for creation: {result['blocked']}")
            log("  that flag is per customer and does not clear -- use another key")
        log(f"  created {len(result['created'])}, reset {len(result['reset'])}, "
            f"failed {len(result['failed'])}")
        prepared.append((key, service, users))
    return prepared


# ---------------------------------------------------------------- roblox phase

def phase_roblox(prepared: list[tuple], out_path: Path, args) -> dict:
    from playwright.sync_api import sync_playwright

    from tools.sso_fleet import launch_context, run_identity

    skip = done_emails(out_path)
    if skip:
        log(f"resuming: {len(skip)} identities already in {out_path.name}")

    queue = []
    for key, service, users in prepared:
        for index, user in enumerate(users, 1):
            if user["email"].lower() in skip:
                continue
            queue.append((key, service, user, index, len(users)))

    if args.limit:
        queue = queue[:args.limit]
    if not queue:
        log("nothing to make")
        return {"made": 0, "failed": 0}

    out_dir = ROOT / "debug" / "fleet" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    made = failed = 0
    started = time.time()

    with sync_playwright() as pw:
        ctx = launch_context(pw, args.profile, args.headless, args.proxy)
        try:
            for n, (key, service, user, index, total) in enumerate(queue, 1):
                log(f"\n[{n}/{len(queue)}] {key.label} :: {user['email']}")
                try:
                    record = run_identity(ctx, service, user, index, total,
                                          False, out_dir)
                except Exception as exc:
                    log(f"  aborted: {type(exc).__name__}: {str(exc)[:120]}")
                    record = None
                if record and record.get("cookie"):
                    append_record(out_path, record, user)
                    made += 1
                    log(f"  saved -> {out_path}")
                else:
                    failed += 1
        finally:
            ctx.close()

    elapsed = int(time.time() - started) or 1
    log(f"\nmade {made}, failed {failed} in {elapsed}s "
        f"({made / elapsed * 3600:.1f}/hour)")
    log(f"artifacts -> {out_dir}")
    return {"made": made, "failed": failed}


# ---------------------------------------------------------------------- driver

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cloud Identity keys -> Roblox accounts")
    ap.add_argument("--keys", default=str(DEFAULT_KEYS),
                    help="array of Cloud Identity credentials (default keys.txt)")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="where account blocks are appended (default accounts.txt)")
    ap.add_argument("--count", type=int, default=50,
                    help="identities per key, capped at 50")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap Roblox accounts this run (0 = everything)")
    ap.add_argument("--profile", default=".profiles/ss")
    ap.add_argument("--proxy", default=None,
                    help="socks5://user:pass@host:port for the Admin SDK and browser")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be created, touch nothing")
    ap.add_argument("--create-only", action="store_true",
                    help="stop after the identities exist")
    ap.add_argument("--skip-create", action="store_true",
                    help="do not call the Admin SDK, use the existing ledgers")
    ap.add_argument("--delete", action="store_true",
                    help="delete the identities in each key's ledger")
    ap.add_argument("--yes", action="store_true", help="skip the delete prompt")
    ap.add_argument("--make-key", action="store_true",
                    help="one-time browser consent: add a new key to --keys")
    ap.add_argument("--domain", default=None,
                    help="tenant domain, required with --make-key")
    ap.add_argument("--client-json",
                    default="accounts/cloudidentity/oauth_client.json",
                    help="OAuth client for --make-key")
    ap.add_argument("--token", default="accounts/cloudidentity/token.json",
                    help="where --make-key writes the credential")
    ap.add_argument("--wait", type=int, default=600,
                    help="seconds to wait on the consent screen")
    args = ap.parse_args()

    keys_path = Path(args.keys)
    if not keys_path.is_absolute():
        keys_path = ROOT / keys_path

    if args.make_key:
        if not args.domain:
            log("--make-key needs --domain <the tenant's domain>")
            return 2
        try:
            credential = ci.authorize(
                ROOT / args.client_json, args.domain, ROOT / ".profiles/admin",
                ROOT / args.token, args.wait)
            ci.append_key(keys_path, credential)
        except Exception as exc:
            log(f"key creation failed: {type(exc).__name__}: {str(exc)[:150]}")
            return 1
        log(f"key for {args.domain} added to {keys_path}")
        return 0

    try:
        keys = ci.load_keys(keys_path)
    except Exception as exc:
        log(f"{keys_path}: {exc}")
        return 2
    log(f"{len(keys)} key(s) from {keys_path}")

    if args.delete:
        if args.dry_run:
            for key in keys:
                ledger = ci.open_ledger(key.domain)[1] if key.domain else []
                log(f"{key.label}: {len(ledger)} identities would be deleted")
            return 0
        return phase_delete(keys, args.yes, args.proxy)

    if args.skip_create:
        prepared = []
        for key in keys:
            if not key.domain:
                log(f"skip {key.label}: no domain on this key")
                continue
            users = ci.open_ledger(key.domain)[1]
            if not users:
                log(f"skip {key.label}: no ledger yet")
                continue
            prepared.append((key, ci.service_for(key, args.proxy), users))
    else:
        prepared = phase_create(keys, args.count, args.dry_run, args.proxy)

    if not prepared:
        log("no identities to work with")
        return 1
    if args.create_only or args.dry_run:
        return 0

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    result = phase_roblox(prepared, out_path, args)
    return 0 if result["made"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
