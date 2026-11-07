"""Egress inspection — what Roblox actually sees when you connect.

Two things kill account creation silently:

1. Datacenter / VPN egress. Arkose scores the ASN before it looks at anything
   else. Cloudflare WARP (AS13335), AWS, DigitalOcean, Hetzner etc. push the
   risk score to maximum and turn a 15% challenge rate into 100%.

2. GeoIP mismatch. A browser claiming America/New_York behind an IP that
   geolocates to Frankfurt is an instant tell, and the previous version of this
   tool picked a *random* timezone per context — actively harmful once proxies
   are in play. Timezone now comes from the egress IP.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import requests

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
GEO_URL = "https://ipinfo.io/json"

# ASNs that will get you challenged on sight
DATACENTER_ASNS: dict[str, str] = {
    "AS13335": "Cloudflare WARP / hosting",
    "AS16509": "Amazon AWS",
    "AS14618": "Amazon AWS",
    "AS14061": "DigitalOcean",
    "AS20473": "Vultr / Choopa",
    "AS63949": "Akamai / Linode",
    "AS16276": "OVH",
    "AS24940": "Hetzner",
    "AS213230": "Hetzner",
    "AS396982": "Google Cloud",
    "AS8075": "Microsoft Azure",
    "AS9009": "M247",
    "AS51167": "Contabo",
    "AS45102": "Alibaba Cloud",
    "AS31898": "Oracle Cloud",
    "AS60781": "LeaseWeb",
    "AS29802": "Hivelocity",
}

WARP_IPV4_PREFIXES = ("104.28.",)


@dataclass(frozen=True)
class Egress:
    ip: str
    country: str
    region: str
    city: str
    timezone: str
    asn: str
    org: str
    warp: bool
    colo: str

    @property
    def is_datacenter(self) -> bool:
        return self.asn in DATACENTER_ASNS

    @property
    def label(self) -> str:
        if self.warp:
            return "Cloudflare WARP"
        return DATACENTER_ASNS.get(self.asn, self.org or self.asn or "unknown")

    def describe(self) -> str:
        return (f"{self.ip} | {self.city}, {self.country} | tz={self.timezone} | "
                f"{self.asn} {self.label}" + (" | warp=on" if self.warp else ""))


_cache: dict[str, Egress] = {}
_cache_lock = threading.Lock()


def probe(proxy: str | None = None, timeout: float = 12.0) -> Egress:
    """Resolve the real egress through `proxy` (or direct). Cached per proxy."""
    key = proxy or "direct"
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    proxies = {"http": proxy, "https": proxy} if proxy else None

    geo: dict = {}
    try:
        resp = requests.get(GEO_URL, proxies=proxies, timeout=timeout,
                            headers={"User-Agent": "curl/8"})
        if resp.status_code == 200:
            geo = resp.json()
    except requests.RequestException:
        pass

    trace: dict[str, str] = {}
    if not geo:
        try:
            resp = requests.get(TRACE_URL, proxies=proxies, timeout=timeout)
            for line in resp.text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    trace[k] = v
        except requests.RequestException:
            pass

    org = geo.get("org", "") or ""
    asn = ""
    if org.startswith("AS"):
        asn = org.split()[0]

    ip = geo.get("ip") or trace.get("ip", "")
    warp_flag = trace.get("warp") == "on" or ip.startswith(WARP_IPV4_PREFIXES)

    result = Egress(
        ip=ip,
        country=geo.get("country", trace.get("loc", "")),
        region=geo.get("region", ""),
        city=geo.get("city", ""),
        timezone=geo.get("timezone", ""),
        asn=asn,
        org=org,
        warp=bool(warp_flag),
        colo=trace.get("colo", ""),
    )
    with _cache_lock:
        _cache[key] = result
    return result


def verdict(egress: Egress) -> tuple[bool, list[str]]:
    """(acceptable, reasons if not)."""
    if not egress.ip:
        return False, ["unreachable — no response through this route"]

    reasons: list[str] = []
    if egress.warp:
        reasons.append(
            "Cloudflare WARP is active (AS13335) — every signup will be challenged. "
            "Run `warp-cli disconnect` before this tool."
        )
    elif egress.is_datacenter:
        reasons.append(
            f"datacenter egress {egress.asn} ({DATACENTER_ASNS[egress.asn]}) — "
            "challenge rate will be near 100%"
        )
    if not egress.timezone:
        reasons.append("could not resolve egress timezone; browser locale will mismatch the IP")
    return (not reasons, reasons)


def timezone_for(proxy: str | None) -> str | None:
    """Timezone to hand the browser so it matches the IP. None = let Chrome decide."""
    try:
        egress = probe(proxy)
    except Exception:
        return None
    return egress.timezone or None
