#!/usr/bin/env python3
"""Send Google's outbound 244444 SMS through a TextVerified rental.

Why this exists
---------------
`tools/gsm_sms.py` proved the gate is an outbound SMS: the QR page asks the phone
to send `Send this message without editing. (CODE)` to shortcode **244444**. A
receive-only OTP service cannot do that, and no SMS API can address a shortcode —
Twilio and Vonage both state it outright.

TextVerified is the exception. Their rentals expose **SMS Send**, and their
"verified shortcode" list names Google's shortcodes explicitly:

    Google 244444 96831 7664400 22000

So a ~$1.50 one-day rental can send the message a SIM would send, which means the
Cloud Identity signup gate no longer needs a modem or a phone.

Cost arithmetic that matters: one SMS creates one Cloud Identity tenant, and one
tenant is ~45–50 Google identities. That is roughly $0.03 per Roblox account,
against $0.45–1.00 for a bought Gmail account.

Credentials (TextVerified dashboard → API):
    export TEXTVERIFIED_API_USERNAME=you@example.com
    export TEXTVERIFIED_API_KEY=...

    ./tools/textverified_sms.py --quote
    ./tools/textverified_sms.py --buy
    ./tools/textverified_sms.py --list
    ./tools/textverified_sms.py --send-from-rpc debug/google_sms_intent/<stamp>/result.json

Notes that are not guesses
--------------------------
- The SDK calls below are the real published surface (`textverified` 0.1.1):
  `ReservationsAPI.pricing/create/list_nonrenewable` and `SMSApi.send`.
- The purchase and send paths are **untested against a funded account**. `--quote`
  and `--list` are safe; `--buy` spends money; `--send` spends a send allowance.
- TextVerified warns that the Enhanced label alone does not guarantee SMS sending
  and that the shortcode list can change. If a send is rejected, that is the API
  answering honestly, not this tool failing.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.gsm_sms import body_from_rpc

SERVICE = "Google"


def client():
    try:
        import textverified as tv
    except ImportError:
        raise SystemExit(
            "textverified missing: .venv/bin/pip install textverified")
    key = os.environ.get("TEXTVERIFIED_API_KEY")
    user = os.environ.get("TEXTVERIFIED_API_USERNAME")
    if not key or not user:
        raise SystemExit(
            "set TEXTVERIFIED_API_USERNAME and TEXTVERIFIED_API_KEY "
            "(TextVerified dashboard -> API)")
    return tv.TextVerified(api_key=key, api_username=user), tv


def show(obj, indent: int = 2) -> None:
    pad = " " * indent
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            print(f"{pad}{f.name}: {getattr(obj, f.name)}")
    else:
        print(f"{pad}{obj}")


def do_quote(tv_client, tv) -> int:
    req = tv.RentalPriceCheckRequest(
        service_name=SERVICE,
        area_code=False,
        number_type=tv.NumberType.MOBILE,
        capability=tv.ReservationCapability.SMS,
        is_renewable=False,
        duration=tv.RentalDuration.ONE_DAY,
        always_on=False,
    )
    snap = tv_client.reservations.pricing(req)
    print("quote for a one-day, non-renewable, mobile, SMS rental:")
    show(snap)
    return 0


def do_buy(tv_client, tv) -> int:
    req = tv.NewRentalRequest(
        allow_back_order_reservations=False,
        service_name=SERVICE,
        capability=tv.ReservationCapability.SMS,
        number_type=tv.NumberType.MOBILE,
        duration=tv.RentalDuration.ONE_DAY,
        is_renewable=False,
    )
    sale = tv_client.reservations.create(req)
    print("rental created:")
    show(sale)
    rid = getattr(sale, "reservation_id", None) or getattr(sale, "id", None)
    if rid:
        print(f"\nreservation: {rid}")
        print("export it:  export TV_RESERVATION_ID=" + str(rid))
    return 0


def do_list(tv_client, tv) -> int:
    for r in tv_client.reservations.list_nonrenewable():
        num = getattr(r, "phone_number", None) or getattr(r, "number", None)
        rid = getattr(r, "reservation_id", None) or getattr(r, "id", None)
        state = getattr(r, "state", None)
        print(f"{rid}  {num}  {state}")
    return 0


def do_send(tv_client, reservation: str, to: str, body: str) -> int:
    ok = tv_client.sms.send(reservation_id=reservation, send_to=to, content=body)
    print(f"send to {to}: {'accepted' if ok else 'rejected'}")
    print(f"body: {body}")
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quote", action="store_true", help="price a rental")
    ap.add_argument("--buy", action="store_true", help="create the rental")
    ap.add_argument("--list", action="store_true", help="list active rentals")
    ap.add_argument("--send", action="store_true", help="send one message")
    ap.add_argument("--send-from-rpc", type=Path, default=None,
                    help="result.json from google_sms_intent_probe.py")
    ap.add_argument("--reservation", default=os.environ.get("TV_RESERVATION_ID"))
    ap.add_argument("--to", default=None, help="destination, e.g. 244444")
    ap.add_argument("--body", default=None)
    args = ap.parse_args()

    tv_client, tv = client()

    if args.quote:
        return do_quote(tv_client, tv)
    if args.buy:
        return do_buy(tv_client, tv)
    if args.list:
        return do_list(tv_client, tv)

    if args.send_from_rpc:
        to, body = body_from_rpc(args.send_from_rpc)
    elif args.send:
        if not args.to or not args.body:
            raise SystemExit("--send needs --to and --body")
        to, body = args.to, args.body
    else:
        ap.print_help()
        return 1

    if not args.reservation:
        raise SystemExit("no reservation: pass --reservation or set TV_RESERVATION_ID")
    return do_send(tv_client, args.reservation, to, body)


if __name__ == "__main__":
    raise SystemExit(main())
