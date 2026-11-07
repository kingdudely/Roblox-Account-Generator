#!/usr/bin/env python3
"""Set and verify a DuckDNS TXT record.

Why this exists
---------------
Cloud Identity Free needs the domain proven before it hands out identities, and
Google proves ownership by reading a TXT record. DuckDNS exposes exactly one TXT
slot per subdomain through its update API, and sub-subdomains inherit the
parent's value, so one call covers `x.duckdns.org` and everything under it.

The value Google shows is `google-site-verification=...`; set it, then confirm it
is visible from public resolvers *before* clicking Verify in the admin console.

Two behaviours measured against the live API, both of which cost time if unknown:

  * send `txt` **without** an `ip` parameter. Passing `ip` makes DuckDNS treat the
    request as an A-record update and silently ignore the TXT value (it answers
    `NOCHANGE` while the old value stays).
  * the slot is one-way. `clear=true` and an empty `txt` both answer `OK` without
    removing the record. Overwrite is the only operation that works, which is
    fine because Google reads one value.

    ./tools/domain_txt.py --domain lunarsky --token <token> --txt google-site-verification=abc123
    ./tools/domain_txt.py --domain lunarsky --token <token> --show
    ./tools/domain_txt.py --domain lunarsky --token <token> --clear
"""

from __future__ import annotations

import argparse
import subprocess
import urllib.parse
import urllib.request

API = "https://www.duckdns.org/update"


def call(domain: str, token: str, txt: str | None, clear: bool = False) -> str:
    params = {"domains": domain, "token": token, "verbose": "true"}
    if clear:
        params["clear"] = "true"
    elif txt is not None:
        params["txt"] = txt
    url = API + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace").strip()


def dig_txt(fqdn: str, resolvers: tuple[str, ...] = ("1.1.1.1", "8.8.8.8")) -> list[str]:
    lines = []
    for resolver in resolvers:
        try:
            res = subprocess.run(
                ["dig", "+short", "TXT", fqdn, f"@{resolver}"],
                capture_output=True, text=True, timeout=15,
            )
            value = (res.stdout or res.stderr).strip() or "(empty)"
            lines.append(f"{resolver}: {value.splitlines()[0][:120]}")
        except Exception as exc:
            lines.append(f"{resolver}: failed {str(exc)[:60]}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="subdomain only, no .duckdns.org")
    ap.add_argument("--token", required=True, help="DuckDNS account token")
    ap.add_argument("--txt", help="TXT value, e.g. google-site-verification=...")
    ap.add_argument("--clear", action="store_true", help="remove the current TXT record")
    ap.add_argument("--show", action="store_true", help="read the current TXT record")
    ap.add_argument("--skip-dig", action="store_true")
    args = ap.parse_args()

    if not (args.show or args.clear or args.txt):
        ap.error("give --txt, --clear or --show")

    if not args.show:
        print(f"update -> {call(args.domain, args.token, args.txt, args.clear)}")

    if args.skip_dig:
        return 0

    fqdn = f"{args.domain}.duckdns.org"
    print(f"txt {fqdn}")
    for line in dig_txt(fqdn):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
