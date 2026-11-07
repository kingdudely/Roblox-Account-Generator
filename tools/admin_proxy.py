#!/usr/bin/env python3
"""Admin SDK over a SOCKS5 exit instead of the host's own IP.

Why this exists
---------------
`users.insert` currently answers:

    412 "A request for user account creation is disallowed as we've detected
         some potentially abusive behavior."

Google's abuse engine scores the caller, and every request so far came from
Cloudflare WARP (`104.28.225.114`, AS13335) — a shared datacenter range. If that
412 is network-scored, the same call through a residential exit answers
differently. This module exists to answer that with one API call.

`httplib2.ProxyInfo(..., proxy_rdns=True)` keeps DNS on the exit, which matters:
resolving admin.googleapis.com from the local resolver and connecting from a
residential IP is the classic split that gets flagged.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import httplib2
import socks
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.user.security",
]


def credentials_from(token_path: Path) -> Credentials:
    data = json.loads(Path(token_path).read_text())
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
    return creds


def proxy_info_for(url: str) -> httplib2.ProxyInfo:
    """socks5://user:pass@host:port -> ProxyInfo with remote DNS."""
    parts = urlparse(url if "://" in url else f"socks5://{url}")
    if not parts.hostname or not parts.port:
        raise ValueError(f"proxy url is missing host or port: {url[:40]}")
    return httplib2.ProxyInfo(
        socks.PROXY_TYPE_SOCKS5,
        parts.hostname,
        parts.port,
        proxy_user=unquote(parts.username) if parts.username else None,
        proxy_pass=unquote(parts.password) if parts.password else None,
        proxy_rdns=True,
    )


def build_service_via(token_path: Path, proxy_url: str):
    """Admin SDK `directory_v1` whose every request leaves through `proxy_url`.

    Discovery is fetched directly, then the transport is swapped. Free exits time
    out on the 1 MB discovery document often enough that fetching it through one
    makes the whole call look broken before it has sent a single request.
    """
    creds = credentials_from(token_path)
    direct = AuthorizedHttp(creds, http=httplib2.Http(timeout=45))
    service = build("admin", "directory_v1", http=direct, cache_discovery=False)
    service._http = AuthorizedHttp(
        creds, http=httplib2.Http(proxy_info=proxy_info_for(proxy_url), timeout=45))
    return service


def egress_ip(proxy_url: str) -> str:
    """The IP Google will see, for logging. Best effort."""
    h = httplib2.Http(proxy_info=proxy_info_for(proxy_url), timeout=30)
    try:
        _, body = h.request("https://api.ipify.org", "GET")
        return body.decode().strip()
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        return f"<unreachable: {type(exc).__name__}>"
