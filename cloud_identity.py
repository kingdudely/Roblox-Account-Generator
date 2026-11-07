#!/usr/bin/env python3
"""Cloud Identity Free: credentials, tenants, identities, backup codes.

One credential ("key") is one tenant. A tenant holds at most 50 identities, and
after roughly 45 inserts Google's abuse filter answers 412 permanently for that
customer -- so keys are consumed, not reused. This module owns everything the
Admin SDK touches; the browser never appears here.

Two credential shapes are accepted:

  * oauth            -- the refresh-token JSON written by the consent flow
                        (`token`, `refresh_token`, `client_id`, `client_secret`)
  * service_account  -- a Google service-account JSON. Needs domain-wide
                        delegation in the admin console; `admin` picks the
                        subject to impersonate, default `admin@<domain>`

A bare `AIza...` API key string is not a credential and cannot manage users --
Admin SDK authorisation is OAuth only.
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.user.security",
]

LEDGER_DIR = ROOT / "accounts" / "cloudidentity"
PER_TENANT = 50          # Cloud Identity Free seats
USABLE_PER_TENANT = 45   # before the abuse filter is expected to trip

FIRST_NAMES = [
    "Camille", "Rafael", "Marta", "Viktor", "Amara", "Diego", "Ines", "Tomas",
    "Yuki", "Noor", "Elias", "Priya", "Mateo", "Sofia", "Kwame", "Leila",
    "Hugo", "Anika", "Omar", "Clara", "Nikolai", "Aisha", "Bruno", "Freya",
    "Ivan", "Zara", "Luca", "Maya", "Idris", "Elena", "Kai", "Rosa",
    "Anders", "Nadia", "Ravi", "Iris", "Thiago", "Mila", "Jonas", "Ayla",
]

LAST_NAMES = [
    "Bianchi", "Costa", "Andersen", "Nakamura", "Okafor", "Silva", "Moreau",
    "Petrov", "Tanaka", "Haddad", "Lindqvist", "Sharma", "Alvarez", "Rossi",
    "Mensah", "Bauer", "Kowalski", "Ferreira", "Ibrahim", "Novak", "Sato",
    "Dubois", "Kaur", "Larsen", "Romero", "Vogel", "Osei", "Yildiz",
    "Marchetti", "Nilsen", "Park", "Cardoso", "Horvat", "Diallo", "Sato",
    "Vargas", "Bakker", "Mendes", "Fischer", "Rahman",
]


@dataclass
class Key:
    """One tenant credential, normalised."""
    kind: str                       # "oauth" | "service_account"
    info: dict
    domain: str | None = None
    admin: str | None = None
    source: str = "<inline>"
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.domain or self.source

    def credentials(self):
        if self.kind == "service_account":
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                self.info, scopes=SCOPES)
            if self.admin:
                creds = creds.with_subject(self.admin)
            return creds

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        info = self.info
        creds = Credentials(
            token=info.get("token"),
            refresh_token=info["refresh_token"],
            token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            scopes=info.get("scopes", SCOPES),
        )
        if not creds.valid:
            creds.refresh(Request())
        return creds


# --------------------------------------------------------------------- keys.txt

def _normalise(info: dict, source: str, domain: str | None = None,
               admin: str | None = None) -> Key:
    if not isinstance(info, dict):
        raise ValueError(f"{source}: credential is not a JSON object")
    if info.get("type") == "service_account":
        kind = "service_account"
    elif "refresh_token" in info and "client_id" in info:
        kind = "oauth"
    else:
        raise ValueError(
            f"{source}: not a service-account or OAuth refresh-token credential. "
            "An `AIza...` API key cannot manage Cloud Identity users.")

    domain = (info.get("domain") or domain or "").strip().lower() or None
    admin = (info.get("admin") or admin or
             (f"admin@{domain}" if kind == "service_account" and domain else None))
    return Key(kind=kind, info=info, domain=domain, admin=admin, source=source)


def load_key_file(path: Path) -> Key:
    path = Path(path)
    return _normalise(json.loads(path.read_text()), str(path))


def load_keys(path: Path) -> list[Key]:
    """Read `keys.txt`.

    Accepts a JSON array, one JSON object per line, or one credential-file path
    per line. A wrapper object may carry `domain` / `admin` for a file whose own
    JSON omits them:

        {"domain": "example.duckdns.org", "file": "accounts/cloudidentity/token.json"}
    """
    path = Path(path)
    text = path.read_text().strip()
    if not text:
        raise ValueError(f"{path} is empty")

    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None

    entries: list = []
    if isinstance(whole, list):
        entries = whole
    elif isinstance(whole, dict) and ("file" in whole or "path" in whole or
                                      "credential" in whole):
        entries = [whole]
    elif isinstance(whole, dict) and ("refresh_token" in whole or
                                      whole.get("type") == "service_account"):
        entries = [whole]
    else:
        entries = [line.strip() for line in text.splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]

    keys: list[Key] = []
    for index, entry in enumerate(entries, 1):
        source = f"{path}:{index}"
        if isinstance(entry, str):
            stripped = entry.strip()
            if stripped.startswith("{"):
                try:
                    keys.append(_normalise(json.loads(stripped), source))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{source}: bad JSON object ({exc})")
                continue
            file_path = Path(stripped)
            if not file_path.is_absolute() and not file_path.exists():
                file_path = ROOT / file_path
            try:
                keys.append(load_key_file(file_path))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}: {stripped!r} is not valid JSON ({exc})")
            continue

        wrapper = entry.get("domain") or entry.get("admin")
        target = entry.get("file") or entry.get("path") or entry.get("credential")
        if isinstance(target, str):
            file_path = Path(target)
            if not file_path.is_absolute():
                file_path = ROOT / file_path
            info = json.loads(file_path.read_text())
            keys.append(_normalise(info, source, entry.get("domain"),
                                   entry.get("admin")))
        elif isinstance(target, dict):
            keys.append(_normalise(target, source, entry.get("domain"),
                                   entry.get("admin")))
        else:
            keys.append(_normalise(entry, source, wrapper, entry.get("admin")))

    if not keys:
        raise ValueError(f"{path}: no credentials found")
    return keys


# --------------------------------------------------------------------- consent

REDIRECT_URI = "http://localhost:8765/"


def authorize(client_json: Path, domain: str, profile: Path,
              token_path: Path, wait: int = 600) -> dict:
    """One-time browser consent: OAuth client -> refresh token -> credential.

    The redirect target has no server behind it, so the navigation lands on
    chrome-error:// and `page.url` loses the query. The outgoing request is
    watched instead -- its URL still carries the code.
    """
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import InstalledAppFlow
    from playwright.sync_api import sync_playwright

    client_json = Path(client_json)
    if not client_json.exists():
        raise FileNotFoundError(
            f"missing OAuth client: {client_json}\n"
            "create one at console.cloud.google.com -> APIs & Services -> "
            "Credentials -> OAuth client ID (Desktop app)")

    flow = InstalledAppFlow.from_client_config(
        json.loads(client_json.read_text()), SCOPES,
        redirect_uri=REDIRECT_URI, autogenerate_code_verifier=True)
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")

    code = None
    captured: dict[str, str] = {}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check",
                  "--disable-dev-shm-usage", "--window-size=1400,1000"],
            locale="en-US", viewport={"width": 1400, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def watch(request):
            if "localhost:8765" in request.url and "code=" in request.url:
                captured["url"] = request.url

        page.on("request", watch)
        print("opening the consent screen -- approve it as the domain admin")
        page.goto(auth_url, wait_until="domcontentloaded", timeout=90000)

        start = time.time()
        while time.time() - start < wait:
            url = captured.get("url") or page.url
            if "localhost:8765" in url and "code=" in url:
                code = (parse_qs(urlparse(url).query).get("code") or [None])[0]
                if code:
                    break
            # consent can hide the target behind a chooser, and an app left in
            # Testing shows a warning interstitial that must be clicked through
            for name in ("Advanced", "Go to", "Continue", "Allow", "Next"):
                try:
                    btn = page.get_by_role("button", name=name)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click()
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    continue
            page.wait_for_timeout(1800)
        ctx.close()

    if not code:
        raise RuntimeError("no authorization code captured")

    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise RuntimeError("no refresh token -- re-consent with prompt=consent")

    credential = {
        "domain": domain.lower(),
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token": creds.token,
        "token_uri": creds.token_uri,
        "scopes": creds.scopes,
    }
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(credential, indent=2) + "\n")
    token_path.chmod(0o600)
    return credential


def append_key(keys_path: Path, credential: dict) -> None:
    """Add one credential to keys.txt, keeping whichever shape it already has."""
    keys_path = Path(keys_path)
    entry = {k: v for k, v in credential.items() if k != "token"}
    try:
        existing = json.loads(keys_path.read_text()) if keys_path.exists() else None
    except json.JSONDecodeError:
        existing = None

    if isinstance(existing, list):
        existing.append(entry)
        keys_path.write_text(json.dumps(existing, indent=2) + "\n")
    elif isinstance(existing, dict):
        keys_path.write_text(json.dumps([existing, entry], indent=2) + "\n")
    else:
        with keys_path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    keys_path.chmod(0o600)


# ------------------------------------------------------------------- Admin SDK
def _proxy_info(url: str):
    import socks
    import httplib2
    from urllib.parse import unquote, urlparse

    parts = urlparse(url if "://" in url else f"socks5://{url}")
    if not parts.hostname or not parts.port:
        raise ValueError(f"proxy url is missing host or port: {url[:40]}")
    # Remote DNS: resolving admin.googleapis.com locally and connecting from a
    # residential exit is the split that gets calls flagged.
    return httplib2.ProxyInfo(
        socks.PROXY_TYPE_SOCKS5, parts.hostname, parts.port,
        proxy_user=unquote(parts.username) if parts.username else None,
        proxy_pass=unquote(parts.password) if parts.password else None,
        proxy_rdns=True)


def service_for(key: Key, proxy: str | None = None):
    from googleapiclient.discovery import build

    creds = key.credentials()
    if not proxy:
        return build("admin", "directory_v1", credentials=creds,
                     cache_discovery=False)

    import httplib2
    from google_auth_httplib2 import AuthorizedHttp

    # Discovery is fetched directly -- free exits time out on the 1 MB document
    # often enough to make a healthy call look broken.
    direct = AuthorizedHttp(creds, http=httplib2.Http(timeout=45))
    service = build("admin", "directory_v1", http=direct, cache_discovery=False)
    service._http = AuthorizedHttp(
        creds, http=httplib2.Http(proxy_info=_proxy_info(proxy), timeout=45))
    return service


def service_from_token(token_path: Path, proxy: str | None = None):
    return service_for(load_key_file(Path(token_path)), proxy)


def http_reason(exc) -> str:
    try:
        payload = json.loads(exc.content)
        return payload.get("error", {}).get("errors", [{}])[0].get("reason", "")
    except Exception:
        return str(exc)[:80]


def mint_codes(service, email: str) -> list[str]:
    """Ten fresh backup verification codes; generating invalidates the old set."""
    service.verificationCodes().generate(userKey=email).execute()
    listing = service.verificationCodes().list(userKey=email).execute()
    return [item["verificationCode"] for item in listing.get("items", [])]


# ---------------------------------------------------------------------- ledger

def ledger_path(domain: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_") or "tenant"
    return LEDGER_DIR / f"{slug}.json"


def load_ledger(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return json.loads(path.read_text())


LEGACY_LEDGER = LEDGER_DIR / "users.json"


def open_ledger(domain: str) -> tuple[Path, list[dict]]:
    """Ledger for one tenant, adopting the pre-main.py users.json on first run."""
    path = ledger_path(domain)
    users = load_ledger(path)
    if users:
        return path, users

    if LEGACY_LEDGER.exists():
        try:
            legacy = json.loads(LEGACY_LEDGER.read_text())
        except json.JSONDecodeError:
            legacy = []
        suffix = "@" + domain.lower()
        matching = [u for u in legacy
                    if str(u.get("email", "")).lower().endswith(suffix)
                    and {"first", "last", "email", "password"} <= set(u)]
        if matching:
            save_ledger(path, matching)
            return path, matching
    return path, []


def save_ledger(path: Path, users: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2) + "\n")
    path.chmod(0o600)


def generate_ledger(domain: str, count: int, existing: list[dict]) -> list[dict]:
    """Extend a ledger to `count` identities: first.lastN@domain."""
    if not domain:
        raise ValueError("a key needs a domain to generate identities")
    rng = random.Random(os.urandom(16))
    users = list(existing)
    taken = {u["email"].lower() for u in users}
    n = len(users)

    while len(users) < count:
        n += 1
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        email = f"{first}.{last}{n}@{domain}".lower()
        if email in taken:
            continue
        taken.add(email)
        from roblox_creator.identity import make_password
        users.append({
            "first": first,
            "last": last,
            "email": email,
            "password": make_password(rng),
        })
    return users


def create_users(service, users: list[dict], dry_run: bool = False) -> dict:
    """Insert every user that is not there yet; reset the password if it is.

    Returns {"created": [...], "reset": [...], "failed": [(user, reason)],
             "blocked": reason|None}. 412 means the tenant's abuse flag is set:
    that is permanent per customer, so stop instead of hammering it.
    """
    from googleapiclient.errors import HttpError

    out = {"created": [], "reset": [], "failed": [], "blocked": None}
    for idx, user in enumerate(users, 1):
        email = user["email"]
        body = {
            "primaryEmail": email,
            "name": {"givenName": user["first"], "familyName": user["last"]},
            "password": user["password"],
            "changePasswordAtNextLogin": False,
        }
        if dry_run:
            print(f"  [{idx:>2}] would create {email}")
            out["created"].append(user)
            continue
        try:
            service.users().insert(body=body).execute()
            out["created"].append(user)
            print(f"  [{idx:>2}] created  {email}")
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            reason = http_reason(exc)
            if status == 412:
                out["blocked"] = reason or "412 conditionNotMet"
                print(f"  [{idx:>2}] blocked  {email}: {out['blocked']}")
                break
            if status == 409 or reason in ("duplicate", "exists"):
                try:
                    service.users().update(
                        userKey=email,
                        body={"password": user["password"],
                              "changePasswordAtNextLogin": False},
                    ).execute()
                    out["reset"].append(user)
                    print(f"  [{idx:>2}] reset    {email}")
                except HttpError as inner:
                    out["failed"].append((user, http_reason(inner)))
                    print(f"  [{idx:>2}] reset !! {email}")
            else:
                out["failed"].append((user, reason))
                print(f"  [{idx:>2}] error    {email}: {reason[:80]}")
        except Exception as exc:
            out["failed"].append((user, f"{type(exc).__name__}: {exc}"))
            print(f"  [{idx:>2}] error    {email}: {type(exc).__name__}")
    return out


def delete_users(service, users: list[dict]) -> dict:
    """Delete every identity in the ledger.

    Addresses stay reserved for ~20 days and are not reusable during that
    window; seats are freed immediately. Rebuild with a NEW ledger.
    """
    from googleapiclient.errors import HttpError

    out = {"deleted": [], "missing": [], "failed": []}
    for idx, user in enumerate(users, 1):
        email = user["email"]
        try:
            service.users().delete(userKey=email).execute()
            out["deleted"].append(user)
            print(f"  [{idx:>2}] deleted  {email}")
        except HttpError as exc:
            if getattr(exc.resp, "status", 0) == 404:
                out["missing"].append(user)
                print(f"  [{idx:>2}] absent   {email}")
            else:
                out["failed"].append((user, http_reason(exc)))
                print(f"  [{idx:>2}] failed   {email}: {http_reason(exc)[:70]}")
        except Exception as exc:
            out["failed"].append((user, f"{type(exc).__name__}: {exc}"))
            print(f"  [{idx:>2}] failed   {email}: {type(exc).__name__}")
    return out
