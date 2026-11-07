#!/usr/bin/env python3
"""Create and repair Cloud Identity users through the Admin SDK.

Why this exists
---------------
The admin console's bulk form generates a random password per user and shows it
once, on a results page that only exists while the browser is open. That is a race
we lose: seven accounts were created before the page could be read and their
passwords are now unrecoverable. The API has no such race -- you pass the password
in, it is the password, and an update overwrites whatever was there before.

Two modes:

    # one-time: exchange an OAuth client for a refresh token
    ./tools/admin_api_users.py --mode auth --client-json accounts/cloudidentity/oauth_client.json

    # create every user in the ledger that does not exist, and reset the password
    # of any that do
    ./tools/admin_api_users.py --mode sync
    ./tools/admin_api_users.py --mode sync --dry-run
    ./tools/admin_api_users.py --mode sync --existing-only

`auth` opens the admin profile with Playwright, walks the consent screen as the
admin, and catches the `code=` off the localhost redirect. No local server needed:
the redirect fails to load and that is fine, the URL is the payload.

Note on the token: an OAuth app left in "Testing" issues refresh tokens that expire
after 7 days. Re-run `--mode auth` when it lapses.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# `admin.directory.user` manages accounts; `admin.directory.user.security`
# reads and mints security material (backup verification codes, 2SV state).
# Both are needed: the second one is the whole point of this tool now.
SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.user.security",
]
SECURITY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.security"
REDIRECT_URI = "http://localhost:8765/"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1400,1000",
]


def totp_now() -> str:
    """Current code for the admin's authenticator, if one is stored."""
    from tools.totp_enroll import totp

    path = ROOT / "accounts/cloudidentity/admin_totp.json"
    return totp(json.loads(path.read_text())["secret"])


def do_codes(args) -> int:
    """Mint backup verification codes for a user and print them.

    These are the credential that gets past "Verify it's you" without a phone.
    Google hands out ten per user; each is single-use, so one account sign-in
    burns one code and nine survive for re-signs.
    """
    service = build_service(ROOT / args.token)
    user = args.user
    # these hang off the service root, not off users -- the discovery paths
    # include /users/ but the client nests them at the top level
    service.verificationCodes().generate(userKey=user).execute()
    listing = service.verificationCodes().list(userKey=user).execute()
    codes = [item.get("verificationCode") for item in listing.get("items", [])]
    print(f"{user}: {len(codes)} codes")
    for code in codes:
        print(f"  {code}")
    if args.save:
        out = ROOT / args.save
        out.parent.mkdir(parents=True, exist_ok=True)
        record = {"user": user, "codes": codes,
                  "minted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        out.write_text(json.dumps(record, indent=2))
        out.chmod(0o600)
        print(f"saved -> {out}")
    return 0


def do_auth(client_json: Path, profile: Path, token_path: Path, wait: int) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from playwright.sync_api import sync_playwright

    config = json.loads(client_json.read_text())
    flow = InstalledAppFlow.from_client_config(
        config, SCOPES, redirect_uri=REDIRECT_URI, autogenerate_code_verifier=True)
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")

    code = None
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, args=LAUNCH_ARGS,
            locale="en-US", viewport={"width": 1400, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # The redirect target is http://localhost:8765 with no server behind it, so
        # Chromium turns the navigation into chrome-error://chromewebdata/ and
        # page.url loses the query string. Watch the outgoing request instead --
        # the URL is complete there even when the load fails.
        captured: dict[str, str] = {}

        def watch(request):
            if "localhost:8765" in request.url and "code=" in request.url:
                captured["url"] = request.url

        page.on("request", watch)

        print("opening the consent screen -- approve it as the admin")
        page.goto(auth_url, wait_until="domcontentloaded", timeout=90000)

        start = time.time()
        last = None
        while time.time() - start < wait:
            url = captured.get("url") or page.url
            if url != last:
                last = url
                print(f"  …{int(time.time() - start):>3}s  {url[:100]}")
            if "localhost:8765" in url and "code=" in url:
                params = parse_qs(urlparse(url).query)
                code = (params.get("code") or [None])[0]
                if code:
                    break
            # the admin has 2SV on, so consent can demand a fresh TOTP code
            try:
                for sel in ('input[type="tel"]', 'input[name="totpPin"]',
                            'input[aria-label*="code" i]'):
                    fld = page.locator(sel)
                    if fld.count() and fld.first.is_visible():
                        fld.first.fill(totp_now())
                        print("  filled admin TOTP code")
                        for nm in ("Next", "Verify"):
                            try:
                                b = page.get_by_role("button", name=nm)
                                if b.count() and b.first.is_visible():
                                    b.first.click()
                                    break
                            except Exception:
                                continue
                        page.wait_for_timeout(2500)
                        break
            except Exception:
                pass

            # the consent screen may hide the target account behind a chooser
            # an unverified app in Testing mode shows a warning interstitial:
            # Advanced -> "Go to <app> (unsafe)" must be clicked through
            for name in ("Advanced", "Go to", "Continue", "Allow", "Next"):
                try:
                    btn = page.get_by_role("button", name=name)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click()
                        print(f"  clicked {name}")
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    continue
            page.wait_for_timeout(1800)
        ctx.close()

    if not code:
        print("no authorization code captured")
        return 1

    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }, indent=2))
    token_path.chmod(0o600)
    print(f"refresh token saved -> {token_path}")
    print(f"  refresh_token present: {bool(creds.refresh_token)}")
    return 0


def build_service(token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    data = json.loads(token_path.read_text())
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data["refresh_token"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes", SCOPES),
    )
    if not creds.valid:
        creds.refresh(Request())
    return build("admin", "directory_v1", credentials=creds, cache_discovery=False)


def do_sync(args) -> int:
    from googleapiclient.errors import HttpError

    users = json.loads((ROOT / args.users).read_text())
    service = build_service(ROOT / args.token)

    created = existing = failed = 0
    for idx, user in enumerate(users, 1):
        local = user["email"].split("@")[0]
        body = {
            "primaryEmail": user["email"],
            "name": {"givenName": user["first"], "familyName": user["last"]},
            "password": user["password"],
            "changePasswordAtNextLogin": False,
        }

        if args.existing_only:
            action = "reset"
        else:
            action = "insert"

        try:
            if action == "insert":
                if args.dry_run:
                    print(f"[{idx:>2}] would create {user['email']}")
                    created += 1
                    continue
                service.users().insert(body=body).execute()
                created += 1
                print(f"[{idx:>2}] created {local}")
            else:
                raise RuntimeError("skip-insert")
        except HttpError as exc:
            reason = ""
            try:
                reason = json.loads(exc.content).get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:
                reason = str(exc)[:80]

            if reason in ("duplicate", "exists") or exc.resp.status == 409:
                # already there: make the password known instead of guessing it
                try:
                    if args.dry_run:
                        print(f"[{idx:>2}] would reset  {user['email']}")
                        existing += 1
                        continue
                    service.users().update(
                        userKey=user["email"],
                        body={"password": user["password"], "changePasswordAtNextLogin": False},
                    ).execute()
                    existing += 1
                    print(f"[{idx:>2}] reset   {local}")
                except HttpError as inner:
                    failed += 1
                    print(f"[{idx:>2}] reset failed {local}: {str(inner)[:90]}")
            else:
                failed += 1
                print(f"[{idx:>2}] error   {local}: {reason[:90]}")
        except RuntimeError:
            try:
                if args.dry_run:
                    print(f"[{idx:>2}] would reset  {user['email']}")
                    existing += 1
                    continue
                service.users().update(
                    userKey=user["email"],
                    body={"password": user["password"], "changePasswordAtNextLogin": False},
                ).execute()
                existing += 1
                print(f"[{idx:>2}] reset   {local}")
            except HttpError as inner:
                failed += 1
                print(f"[{idx:>2}] reset failed {local}: {str(inner)[:90]}")
        except Exception as exc:
            failed += 1
            print(f"[{idx:>2}] unexpected {local}: {str(exc)[:90]}")

    print(f"\ncreated {created}, reset {existing}, failed {failed}")
    return 0


def do_wipe(args) -> int:
    """Delete every user in the ledger.

    Read this before running it:

      * Google moves deleted users to "Recently deleted users" and keeps the
        address reserved for 20 days. You cannot recreate the same address until
        it is permanently deleted, which is only available in the admin console.
      * So a wipe followed by a re-sync with the same ledger will fail on every
        row. To rebuild, generate a NEW ledger with different local parts.
      * Deleting frees licence seats, not addresses. Seats are the thing the
        50-user cap actually counts.
    """
    from googleapiclient.errors import HttpError

    users = json.loads((ROOT / args.users).read_text())
    service = build_service(ROOT / args.token)
    ok = failed = 0
    for idx, user in enumerate(users, 1):
        try:
            service.users().delete(userKey=user["email"]).execute()
            ok += 1
            print(f"[{idx:>2}] deleted {user['email']}")
        except HttpError as exc:
            failed += 1
            print(f"[{idx:>2}] {user['email']} !! {str(exc)[:80]}")
    print(f"\ndeleted {ok}, failed {failed}")
    print("addresses stay reserved ~20 days; rebuild with a new ledger")
    return 0


def do_recover(args) -> int:
    """Attach recovery info to every user.

    Google chooses which login challenge to present from the information that
    exists on the account. Fresh managed users have none, so phone verification is
    the only door offered. `recoveryEmail` is writable through the Admin SDK, and
    a Gmail plus-address gives every account its own tag on one inbox -- so a
    challenge can be answered by email instead of a phone number.
    """
    from googleapiclient.errors import HttpError

    users = json.loads((ROOT / args.users).read_text())
    service = build_service(ROOT / args.token)
    tpl = args.recovery_template
    if "{local}" not in tpl:
        print("template must contain {local}")
        return 2

    ok = failed = 0
    for idx, user in enumerate(users, 1):
        local = user["email"].split("@")[0]
        target = tpl.format(local=local, email=user["email"])
        try:
            service.users().update(
                userKey=user["email"], body={"recoveryEmail": target}).execute()
            ok += 1
            print(f"[{idx:>2}] {local:<28} -> {target}")
        except HttpError as exc:
            failed += 1
            print(f"[{idx:>2}] {local:<28} !! {str(exc)[:90]}")

    print(f"\nrecovery email set: {ok}, failed: {failed}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auth", "sync", "recover", "wipe", "codes"],
                    required=True)
    ap.add_argument("--user", default=None, help="target email for --mode codes")
    ap.add_argument("--save", default=None, help="where to store generated codes")
    ap.add_argument("--recovery-template", default="",
                    help="e.g. 'poucher1001+{local}@gmail.com' -- {local} is the user's local part")
    ap.add_argument("--client-json", default="accounts/cloudidentity/oauth_client.json")
    ap.add_argument("--token", default="accounts/cloudidentity/token.json")
    ap.add_argument("--users", default="accounts/cloudidentity/users.json")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--wait", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--existing-only", action="store_true")
    args = ap.parse_args()

    if args.mode == "auth":
        client = (ROOT / args.client_json)
        if not client.exists():
            print(f"missing OAuth client: {client}")
            print("create one at console.cloud.google.com -> APIs & Services -> Credentials")
            return 2
        return do_auth(client, ROOT / args.profile, ROOT / args.token, args.wait)

    if args.mode == "codes":
        if not args.user:
            print("--mode codes needs --user <email>")
            return 2
        return do_codes(args)

    if args.mode == "wipe":
        if not (ROOT / args.token).exists():
            print("missing token -- run --mode auth first")
            return 2
        return do_wipe(args)

    if args.mode == "recover":
        if not (ROOT / args.token).exists():
            print("missing token -- run --mode auth first")
            return 2
        return do_recover(args)

    token = ROOT / args.token
    if not token.exists():
        print(f"missing token: {token} -- run --mode auth first")
        return 2
    return do_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
