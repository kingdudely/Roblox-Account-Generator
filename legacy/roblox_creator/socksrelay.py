"""A local SOCKS5 relay so Chromium can use an authenticated upstream proxy.

Chromium's `--proxy-server=socks5://host:port` cannot carry credentials: it will
attempt the handshake with "no auth" and the upstream residential proxy answers
with a method-selection failure. Playwright's `proxy` option fills in username and
password for HTTP proxies by rewriting the URL, but for SOCKS5 it passes the server
straight through, so the same failure appears there too.

The fix is a tiny loopback relay: it speaks unauthenticated SOCKS5 to Chromium and
authenticated SOCKS5 to the provider. One relay per browser context, bound to an
ephemeral port on 127.0.0.1, closed when the context closes.

Only the SOCKS5 CONNECT command is implemented, which is all a browser emits for
HTTPS. BIND and UDP ASSOCIATE are refused rather than half-supported.

    relay = await SocksRelay.start("host:1337", "user", "pass")
    await page.goto("https://example.com")   # via relay.url
    await relay.close()
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
from typing import Any

__all__ = ["SocksRelay"]

SOCKS5 = 0x05
NO_AUTH = 0x00
USER_PASS = 0x02
CONNECT = 0x01
SUCCESS = 0x00
FAILURE = 0x01
CMD_UNSUPPORTED = 0x07
ATYP_UNSUPPORTED = 0x08


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _encode_addr(host: str) -> bytes:
    """ATYP + address bytes for a CONNECT command."""
    try:
        packed = ipaddress.ip_address(host).packed
        atyp = 0x01 if len(packed) == 4 else 0x04
        return bytes([atyp]) + packed
    except ValueError:
        raw = host.encode("idna")
        return bytes([0x03, len(raw)]) + raw


class SocksRelay:
    """Loopback, no-auth SOCKS5 in front of an authenticated upstream."""

    def __init__(self, upstream_host: str, upstream_port: int,
                 username: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username.encode()
        self.password = password.encode()
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.connections = 0
        self.failures = 0

    @classmethod
    async def start(cls, upstream: str, username: str, password: str) -> "SocksRelay":
        host, _, port = upstream.rpartition(":")
        relay = cls(host or upstream, int(port or 1080), username, password)
        relay._server = await asyncio.start_server(
            relay._handle, "127.0.0.1", 0, backlog=64)
        relay.port = relay._server.sockets[0].getsockname()[1]
        return relay

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def __aenter__(self) -> "SocksRelay":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ server

    async def _handle(self, creader: asyncio.StreamReader,
                      cwriter: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            # 1. Chromium greeting: version, nmethods, methods[]
            head = await asyncio.wait_for(creader.readexactly(2), timeout=30)
            if head[0] != SOCKS5:
                return
            n = head[1]
            methods = await asyncio.wait_for(creader.readexactly(n), timeout=30) if n else b""
            if NO_AUTH not in methods:
                cwriter.write(bytes([SOCKS5, 0xFF]))
                await cwriter.drain()
                return
            cwriter.write(bytes([SOCKS5, NO_AUTH]))
            await cwriter.drain()

            # 2. Request: version, cmd, rsv, atyp, addr, port
            req = await asyncio.wait_for(creader.readexactly(4), timeout=30)
            if req[0] != SOCKS5 or req[1] != CONNECT:
                await self._refuse(cwriter, CMD_UNSUPPORTED)
                return
            atyp = req[3]
            if atyp == 0x01:
                host = str(ipaddress.IPv4Address(await creader.readexactly(4)))
            elif atyp == 0x03:
                ln = (await creader.readexactly(1))[0]
                # SOCKS5 carries the hostname as raw ASCII bytes; do not run it
                # through the idna codec, which rejects an `errors` handler.
                host = (await creader.readexactly(ln)).decode("ascii", "replace")
            elif atyp == 0x04:
                host = str(ipaddress.IPv6Address(await creader.readexactly(16)))
            else:
                await self._refuse(cwriter, ATYP_UNSUPPORTED)
                return
            port = struct.unpack("!H", await creader.readexactly(2))[0]

            # 3. Upstream handshake with username/password
            ureader, uwriter = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port),
                timeout=30)
            upstream_writer = uwriter

            uwriter.write(bytes([SOCKS5, 1, USER_PASS]))
            await uwriter.drain()
            choice = await asyncio.wait_for(ureader.readexactly(2), timeout=30)
            if choice[0] != SOCKS5 or choice[1] != USER_PASS:
                await self._refuse(cwriter, FAILURE)
                return
            uwriter.write(bytes([0x01, len(self.username)]) + self.username
                          + bytes([len(self.password)]) + self.password)
            await uwriter.drain()
            auth = await asyncio.wait_for(ureader.readexactly(2), timeout=30)
            if auth[1] != SUCCESS:
                self.failures += 1
                await self._refuse(cwriter, FAILURE)
                return

            uwriter.write(bytes([SOCKS5, CONNECT, 0x00]) + _encode_addr(host)
                          + struct.pack("!H", port))
            await uwriter.drain()
            reply = await asyncio.wait_for(ureader.readexactly(4), timeout=30)
            rep = reply[1]
            # drain the bound address the upstream echoes back
            if reply[3] == 0x01:
                await ureader.readexactly(4 + 2)
            elif reply[3] == 0x03:
                ln = (await ureader.readexactly(1))[0]
                await ureader.readexactly(ln + 2)
            elif reply[3] == 0x04:
                await ureader.readexactly(16 + 2)
            if rep != SUCCESS:
                self.failures += 1
                await self._refuse(cwriter, FAILURE)
                return

            self.connections += 1
            cwriter.write(bytes([SOCKS5, SUCCESS, 0x00, 0x01,
                                 0, 0, 0, 0, 0, 0]))
            await cwriter.drain()

            await asyncio.gather(
                _pipe(creader, uwriter),
                _pipe(ureader, cwriter),
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                ConnectionError, OSError):
            pass
        finally:
            for w in (cwriter, upstream_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter, code: int) -> None:
        try:
            writer.write(bytes([SOCKS5, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
        except Exception:
            pass
