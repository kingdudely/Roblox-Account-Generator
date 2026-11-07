"""Account field generation.

Roblox username rules (current): 3-20 chars, [A-Za-z0-9_], cannot start or end
with '_', no consecutive underscores, must clear the word filter. Birthday must
be 13+ or the signup flow stalls on a parental-consent gate.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, asdict

ADJECTIVES = [
    "Nova", "Lunar", "Crimson", "Frost", "Quiet", "Velvet", "Iron", "Amber",
    "Cobalt", "Dusk", "Ember", "Glacier", "Hollow", "Ivory", "Jade", "Krypton",
    "Lucid", "Moss", "Nimbus", "Onyx", "Prism", "Quartz", "Raven", "Sable",
    "Tidal", "Umber", "Vesper", "Wraith", "Xenon", "Yarrow", "Zephyr", "Basalt",
    "Cinder", "Drift", "Echo", "Fable", "Gale", "Haze", "Indigo", "Juniper",
]

NOUNS = [
    "Fox", "Harbor", "Kite", "Lantern", "Marrow", "Nettle", "Orbit", "Pilot",
    "Quill", "Ridge", "Signal", "Thorn", "Anchor", "Beacon", "Cipher", "Delta",
    "Emberling", "Falcon", "Gravel", "Hollow", "Isle", "Junction", "Kestrel",
    "Ledge", "Meridian", "Nocturne", "Obelisk", "Pylon", "Quarry", "Rook",
    "Spire", "Tundra", "Vault", "Warden", "Xylo", "Yonder", "Zenith", "Basin",
    "Canyon", "Dune",
]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# password charset excludes lookalikes and anything the field rejects
_PW_LOWER = "abcdefghijkmnopqrstuvwxyz"
_PW_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_PW_DIGIT = "23456789"
_PW_SYMBOL = "!@#$%^&*-_"


@dataclass
class Identity:
    username: str
    password: str
    birthday_month: str
    birthday_day: str
    birthday_year: str
    birthday_display: str
    gender: str  # "Male" | "Female"

    def to_dict(self) -> dict:
        return asdict(self)


def make_username(rng: random.Random, attempt: int = 0) -> str:
    style = rng.random()
    adj = rng.choice(ADJECTIVES)
    noun = rng.choice(NOUNS)

    if style < 0.45:
        name = f"{adj}{noun}{rng.randint(10, 9999)}"
    elif style < 0.75:
        name = f"{adj}_{noun}{rng.randint(1, 999)}"
    elif style < 0.92:
        name = f"{noun}{rng.randint(100, 99999)}{rng.choice(string.ascii_uppercase)}"
    else:
        name = f"{adj.lower()}x{noun.lower()}{rng.randint(1, 999)}"

    name = name[:20].rstrip("_")
    if len(name) < 3:
        name = (name + "Alt" + str(rng.randint(10, 99)))[:20]
    if attempt:
        # collision retry: swap the trailing digits
        name = (name[:15] + str(rng.randint(1000, 99999)))[:20].rstrip("_")
    return name


def make_password(rng: random.Random, length: int | None = None) -> str:
    length = length or rng.randint(12, 16)
    chars = [
        rng.choice(_PW_UPPER),
        rng.choice(_PW_LOWER),
        rng.choice(_PW_DIGIT),
        rng.choice(_PW_SYMBOL),
    ]
    pool = _PW_LOWER + _PW_UPPER + _PW_DIGIT
    chars += [rng.choice(pool) for _ in range(length - 4)]
    rng.shuffle(chars)
    return "".join(chars)


def make_birthday(rng: random.Random) -> tuple[str, str, str, str]:
    """Returns (month_abbr, day_2digit, year, display) — age 14..30."""
    import datetime

    today = datetime.date.today()
    age = rng.randint(14, 30)
    year = today.year - age
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return (
        MONTHS[month - 1],
        f"{day:02d}",
        str(year),
        f"{day:02d}/{month:02d}/{year}",
    )


def make_identity(rng: random.Random | None = None, attempt: int = 0) -> Identity:
    rng = rng or random.Random()
    month, day, year, display = make_birthday(rng)
    return Identity(
        username=make_username(rng, attempt),
        password=make_password(rng),
        birthday_month=month,
        birthday_day=day,
        birthday_year=year,
        birthday_display=display,
        gender=rng.choice(["Male", "Female"]),
    )


def human_typing_delays(rng: random.Random) -> list[int] | None:
    """Playwright type() accepts a per-key delay; return a jittered value."""
    return rng.randint(55, 140)
