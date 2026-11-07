"""One parser for every proxy list format the vendors actually hand out.

Why this exists
---------------
The venders ship the same endpoints in four different line shapes, and getting
the shape wrong is silent — a proxy parsed as the wrong scheme does not raise,
it just fails against a port that only speaks SOCKS5.

The formats seen in the wild, all pointing at SOCKS5-only ports:

    socks5://user:pass@host:port          evomi style, CRLF line endings
    user:pass@host:port                   legionproxy style
    host:port:user:pass                   legionproxy style, colon-separated
    host:port                             no auth

The previous loader hard-coded ``http://`` on every bare line, which meant a
SOCKS5-only endpoint was addressed over a scheme it does not implement. It
"worked" often enough to look fine and failed often enough to look like a dead
pool, which is how a thousand perfectly good proxies get blamed for being dead.

Why SOCKS5 and not HTTP
-----------------------
Not decoration. Four concrete differences that change what the origin sees:

1. **No header surface.** An HTTP proxy understands the request and can rewrite
   it, add ``X-Forwarded-For``, or normalise what it does not like. SOCKS5 is a
   byte pipe — it forwards the stream and has no opinion about its contents.
2. **No MITM.** Residential HTTP endpoints sometimes terminate TLS to inspect
   or cache. Then the certificate chain and the ClientHello at the origin are
   the proxy's, not yours. SOCKS5 cannot, by construction.
3. **Remote DNS.** ``socks5h`` resolves the hostname at the exit. Plain
   ``socks5://`` resolves locally and leaks your real DNS. HTTP proxies resolve
   server-side but only for the hosts they allow.
4. **Session identity.** Vendors implement sticky sessions (``_session-<id>``)
   on their SOCKS front-end. Mixing fronts means one logical account is seen
   from two exit IPs, which is exactly the pattern that gets a session flagged.

So the canonical form this module emits is always explicit, never inferred at
the point of use.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

__all__ = [
    "ProxyUrl",
    "parse",
    "load",
    "canonical",
    "load_as_requests",
]

# Schemes a proxy line may legitimately carry. Anything else is a typo.
_SCHEMES = frozenset({"socks5", "socks5h", "socks4", "socks4a", "http", "https"})

# Default for a line with no scheme: the vendor ports are SOCKS5.
DEFAULT_SCHEME = "socks5"


class ProxyParseError(ValueError):
    """The line is not any format we recognise."""


@dataclass(frozen=True)
class ProxyUrl:
    """A normalised proxy endpoint."""

    host: str
    port: int
    user: str = ""
    password: str = ""
    scheme: str = DEFAULT_SCHEME

    # -- rendering -----------------------------------------------------------

    def _creds(self) -> str:
        if not self.user and not self.password:
            return ""
        # quote both halves: vendor passwords contain '_' and sometimes '$',
        # and an unescaped '@' or '#' would split the URL in the wrong place
        return f"{quote(self.user, safe='')}:{quote(self.password, safe='')}@"

    def url(self, scheme: str | None = None) -> str:
        return f"{scheme or self.scheme}://{self._creds()}{self.host}:{self.port}"

    def requests_url(self) -> str:
        """For curl_cffi / requests: plain socks5 resolves DNS locally, which
        leaks the target hostname. socks5h pushes resolution to the exit."""
        scheme = "socks5h" if self.scheme == "socks5" else self.scheme
        return self.url(scheme)

    def host_port(self) -> str:
        return f"{self.host}:{self.port}"

    def masked(self) -> str:
        if not self.user:
            return f"{self.scheme}://{self.host}:{self.port}"
        return f"{self.scheme}://{self.user}:***@{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.url()


# -- detection helpers -------------------------------------------------------


def _is_port(value: str) -> bool:
    if not value.isdigit() or len(value) > 5:
        return False
    return 0 < int(value) < 65536


def _is_host(value: str) -> bool:
    """Cheap but decisive: a host carries a dot, a bracket, or is localhost.

    Deliberately permissive on the TLD side — vendor hostnames are ordinary
    domains, and a mistyped one should fail at connect time with a clear error
    rather than be silently reordered into a password field here.
    """
    if not value:
        return False
    if value.startswith("["):          # bracketed IPv6
        return "]:" in value or value.endswith("]")
    if value.lower() in ("localhost", "localhost.localdomain"):
        return True
    return "." in value and not _is_port(value)


def _split_hostport(value: str) -> tuple[str, int]:
    """Split `host:port` including the bracketed-IPv6 form."""
    if value.startswith("["):
        host, _, tail = value.partition("]")
        host = host + "]"
        port = tail.lstrip(":")
    else:
        host, _, port = value.rpartition(":")
    if not host or not _is_port(port):
        raise ProxyParseError(f"bad host:port in {value!r}")
    return host, int(port)


# -- the parser --------------------------------------------------------------


def parse(raw: str, default_scheme: str = DEFAULT_SCHEME) -> ProxyUrl:
    """Parse one proxy line. Accepts every shape the vendors ship."""
    line = raw.replace("\r", "").strip()
    if not line:
        raise ProxyParseError("empty line")
    if line.startswith("#") or line.startswith("//"):
        raise ProxyParseError("comment")

    scheme = default_scheme
    body = line
    if "://" in line:
        head, _, rest = line.partition("://")
        head = head.lower()
        if head not in _SCHEMES:
            raise ProxyParseError(f"unknown scheme {head!r}")
        scheme = head
        body = rest
        # a scheme-prefixed line keeps its own scheme; only bare lines get the
        # default, so an explicit http:// is never silently upgraded
    if body.startswith("//"):
        body = body[2:]

    # user:pass@host:port  (also tolerates a bare host:port@... typo by rsplit)
    if "@" in body:
        creds, _, hostport = body.rpartition("@")
        user, sep, password = creds.partition(":")
        if not sep:
            user, password = creds, ""
        host, port = _split_hostport(hostport)
        return ProxyUrl(host=host, port=port,
                        user=unquote(user), password=unquote(password),
                        scheme=scheme)

    fields = body.split(":")
    if len(fields) == 2:
        host, port = _split_hostport(body)
        return ProxyUrl(host=host, port=port, scheme=scheme)

    # bracketed IPv6 has colons inside the host, so the generic split below
    # would shred it: `[2001:db8::1]:1080:u:p` becomes six fields
    if body.startswith("["):
        close = body.find("]")
        if close == -1:
            raise ProxyParseError(f"unterminated IPv6 host: {line[:60]!r}")
        host = body[: close + 1]
        tail = body[close + 1:].lstrip(":")
        bits = tail.split(":")
        if not bits or not _is_port(bits[0]):
            raise ProxyParseError(f"bad port after IPv6 host: {line[:60]!r}")
        port = int(bits[0])
        user = bits[1] if len(bits) > 1 else ""
        password = ":".join(bits[2:]) if len(bits) > 2 else ""
        return ProxyUrl(host=host, port=port, user=user, password=password,
                        scheme=scheme)

    if len(fields) >= 4:
        # Two plausible orders: host:port:user:pass (legion) and
        # user:pass:host:port. Decide by shape, not by position.
        if _is_host(fields[0]) and _is_port(fields[1]):
            host, port = fields[0], int(fields[1])
            user, password = fields[2], ":".join(fields[3:])
        elif _is_port(fields[0]) and _is_port(fields[1]):
            # host:port:... where the host is a bare IP that parsed as a port
            # is impossible; this branch is user:pass:host:port with numeric
            # credentials, which we cannot disambiguate safely.
            raise ProxyParseError(
                f"ambiguous 4-field line (numeric user and password): {line[:60]!r}")
        else:
            user, password = fields[0], fields[1]
            host, port = _split_hostport(":".join(fields[2:]))
        return ProxyUrl(host=host, port=port,
                        user=unquote(user), password=unquote(password),
                        scheme=scheme)

    raise ProxyParseError(f"cannot parse proxy: {line[:70]!r}")


# -- file loading ------------------------------------------------------------


def load(path: Path, default_scheme: str = DEFAULT_SCHEME,
         dedupe: bool = False, quiet: bool = False) -> list[str]:
    """Read a proxy file and return canonical URLs.

    Line endings are normalised, so a CRLF list from one vendor and an LF list
    from another behave identically. That single byte was responsible for a
    whole pool reading as dead.
    """
    path = Path(path)
    if not path.exists():
        if not quiet:
            print(f"  ! proxy file not found: {path}")
        return []

    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for line in text.split("\n"):
        try:
            entry = parse(line, default_scheme)
        except ProxyParseError as exc:
            if str(exc) not in ("comment", "empty line"):
                skipped += 1
                if not quiet and skipped <= 3:
                    print(f"  ! skipped: {line[:60]!r} ({exc})")
            continue
        url = entry.url()
        if dedupe:
            if url in seen:
                continue
            seen.add(url)
        out.append(url)

    if not quiet and skipped > 3:
        print(f"  ! {skipped} line(s) unparseable in {path.name}")
    return out


def load_as_requests(path: Path, **kwargs) -> list[str]:
    """Canonical list rendered for HTTP clients (remote DNS)."""
    return [parse(u).requests_url() for u in load(path, **kwargs)]


def canonical(raw: str, default_scheme: str = DEFAULT_SCHEME) -> str:
    return parse(raw, default_scheme).url()


# -- CLI ---------------------------------------------------------------------


def _main() -> int:
    import argparse
    import collections

    ap = argparse.ArgumentParser(description="Parse and audit proxy lists.")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--show", type=int, default=2, help="sample lines to print per file")
    ap.add_argument("--scheme", default=DEFAULT_SCHEME,
                    help="scheme for bare lines (default socks5)")
    args = ap.parse_args()

    for path in args.files:
        raw_lines = [l for l in path.read_text(encoding="utf-8", errors="replace")
                     .replace("\r\n", "\n").split("\n") if l.strip()]
        urls = load(path, args.scheme, quiet=True)
        schemes = collections.Counter(u.split("://")[0] for u in urls)
        hosts = collections.Counter(u.rsplit("@", 1)[-1] for u in urls)
        crash = path.read_bytes().count(b"\r\n")
        print(f"\n{path.name}")
        print(f"  lines           : {len(raw_lines)}")
        print(f"  parsed          : {len(urls)}")
        print(f"  schemes         : {dict(schemes)}")
        print(f"  crlf lines      : {crash}")
        print(f"  distinct exits  : {len(hosts)} host:port")
        for host, count in hosts.most_common(3):
            print(f"      {count:6}  {host}")
        for sample in urls[: args.show]:
            print(f"  e.g.            : {parse(sample).masked()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
