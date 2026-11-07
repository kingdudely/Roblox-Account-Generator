#!/usr/bin/env python3
"""Generate a Google Admin bulk-upload CSV of Cloud Identity users.

Why this exists
---------------
One verified domain gives 50 identities. Creating them by hand in the admin
console is 49 rounds of typing; the console accepts a CSV instead, so this emits
one -- plus a JSON ledger of the same credentials, because every identity has to
be signed into a browser later for the SSO step and the passwords are generated,
not remembered.

    ./tools/identity_csv.py --domain example.duckdns.org --count 49
    ./tools/identity_csv.py --domain x.duckdns.org --count 10 --out /tmp/id --no-license

Upload at admin.google.com -> Directory -> Users -> Bulk update users.
Notes that matter:
  * the license column is optional; if the console rejects the value, re-run with
    --no-license and assign the Cloud Identity license to the batch afterwards
  * "Change Password at Next Sign-In" is FALSE on purpose -- a forced change
    would stall an automated sign-in
  * passwords avoid quotes, spaces and ambiguous glyphs so they survive CSV and
    can be typed into a login form without escaping
"""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import sys
from pathlib import Path

FIRST = [
    "Mateo", "Lucia", "Emil", "Noor", "Priya", "Tomas", "Ada", "Rafael",
    "Ines", "Jonas", "Amara", "Kaito", "Sofia", "Dmitri", "Leila", "Hugo",
    "Nadia", "Omar", "Elena", "Tariq", "Mira", "Bruno", "Yara", "Felix",
    "Anouk", "Ravi", "Zofia", "Malik", "Chiara", "Soren", "Aisha", "Viktor",
    "Nadia", "Pablo", "Freya", "Idris", "Camille", "Andrei", "Sana", "Theo",
    "Marta", "Kenji", "Alina", "Diego", "Ingrid", "Yusuf", "Paula", "Nils",
    "Rosa", "Arman",
]

LAST = [
    "Vasquez", "Lindqvist", "Moreau", "Okafor", "Petrov", "Kaur", "Sorensen",
    "Almeida", "Novak", "Haddad", "Ferraro", "Kimura", "Duarte", "Volkov",
    "Benali", "Meyer", "Kowalski", "Rahman", "Costa", "Ibrahim", "Larsen",
    "Marchetti", "Osei", "Halvorsen", "Bianchi", "Aziz", "Renard", "Sokolov",
    "Delgado", "Nakamura", "Weber", "Ferreira", "Yilmaz", "Andersen", "Silva",
    "Kovacs", "Diallo", "Bauer", "Mancini", "Haugen", "Reyes", "Zielinski",
]

# no quotes, no backslashes, no lookalikes: survives CSV and manual entry
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
SYMBOLS = "-_"


def password(length: int = 18) -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(length - 2))
    return f"{secrets.choice(ALPHABET)}{body}{secrets.choice(SYMBOLS)}{secrets.randbelow(90) + 10}"


def slug(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="e.g. example.duckdns.org")
    ap.add_argument("--count", type=int, default=49)
    ap.add_argument("--out", default="accounts/cloudidentity")
    ap.add_argument("--license", default="Cloud Identity Free",
                    help="value for the New License column")
    ap.add_argument("--no-license", action="store_true",
                    help="omit the license column entirely")
    ap.add_argument("--start", type=int, default=1, help="first index in the email suffix")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    rows: list[dict] = []
    for i in range(args.count):
        while True:
            first = secrets.choice(FIRST)
            last = secrets.choice(LAST)
            local = f"{slug(first)}.{slug(last)}{args.start + i}"
            if local not in used:
                used.add(local)
                break
        rows.append({
            "first": first,
            "last": last,
            "email": f"{local}@{args.domain}",
            "password": password(),
        })

    header = [
        "First Name [Required]",
        "Last Name [Required]",
        "Email Address [Required]",
        "Password [Required]",
        "Org Unit Path [Required]",
        "Change Password at Next Sign-In",
    ]
    if not args.no_license:
        header.append("New License [UPLOAD ONLY]")

    csv_path = out / "users.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(header)
        for r in rows:
            line = [r["first"], r["last"], r["email"], r["password"], "/", "FALSE"]
            if not args.no_license:
                line.append(args.license)
            writer.writerow(line)

    ledger = out / "users.json"
    ledger.write_text(json.dumps(rows, indent=2))
    ledger.chmod(0o600)

    print(f"{len(rows)} identities -> {csv_path}")
    print(f"credentials ledger -> {ledger} (mode 600)")
    print("\nfirst three:")
    for r in rows[:3]:
        print(f"  {r['email']:<45} {r['password']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
