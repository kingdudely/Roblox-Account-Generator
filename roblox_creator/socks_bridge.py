"""Local HTTP-CONNECT bridge in front of an authenticated SOCKS5 proxy.

Chromium cannot authenticate to a SOCKS5 proxy — it has no dialog and no
username/password fields for that scheme, and Playwright's `proxy` option only
carries credentials for HTTP proxies. Residential pools are almost always
`socks5://user:pass@host:port`, which leaves the browser unable to use them.

This runs a throwaway loopback HTTP proxy that accepts ordinary CONNECT
requests and tunnels each one through the upstream SOCKS5 with RFC 1929 auth.
Point the browser at `http://127.0.0.1:<port>` and it reaches the same exit IP.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

BUFFER = 64 * 1024


@dataclass
class SocksTarget:
    host: str
    port: int
    user: str
    password: str

    @classmethod
    def parse(cls, proxy: str) -> "SocksTarget":
        raw = proxy if "://" in proxy else f"socks5://{proxy}"
        p = urlparse(raw)
        if not p.hostname or not p.port:
            raise ValueError(f"unparseable proxy: {proxy[:40]}")
        return cls(
            host=p.hostname,
            port=int(p.port),
            user=unquote(p.username or ""),
            password=unquote(p.password or ""),
        )


async def _socks5_connect(target: SocksTarget, dest_host: str, dest_port: int):
    reader, writer = await asyncio.open_connection(target.host, target.port)
    if target.user:
        writer.write(b"\x05\x02\x00\x02")
    else:
        writer.write(b"\x05\x01\x00")
    await writer.drain()

    version, method = await reader.readexactly(2)
    if version != 5:
        raise ConnectionError("upstream is not socks5")
    if method == 0x02:
        user = target.user.encode()
        password = target.password.encode()
        writer.write(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
        await writer.drain()
        _, status = await reader.readexactly(2)
        if status != 0:
            raise ConnectionError("socks5 auth rejected")
    elif method != 0x00:
        raise ConnectionError(f"socks5 offered no usable auth method ({method})")

    host_bytes = dest_host.encode()
    request = (b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes
               + struct.pack(">H", dest_port))
    writer.write(request)
    await writer.drain()

    version, reply, _, atyp = await reader.readexactly(4)
    if reply != 0:
        raise ConnectionError(f"socks5 connect refused ({reply})")
    if atyp == 0x01:
        await reader.readexactly(4)
    elif atyp == 0x03:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length)
    elif atyp == 0x04:
        await reader.readexactly(16)
    await reader.readexactly(2)
    return reader, writer


class SocksBridge:
    """Start once per proxy, hand `http://127.0.0.1:port` to the browser."""

    def __init__(self, proxy: str):
        self.target = SocksTarget.parse(proxy)
        self._server: asyncio.AbstractServer | None = None
        self._thread: object | None = None
        self._ready = None
        self.port = 0

    def start(self) -> str:
        import threading

        self._ready = threading.Event()
        thread = threading.Thread(target=lambda: asyncio.run(self._serve()),
                                  daemon=True, name="socks-bridge")
        thread.start()
        self._thread = thread
        if not self._ready.wait(timeout=10):
            raise RuntimeError("socks bridge did not start")
        return f"http://127.0.0.1:{self.port}"

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        assert self._ready is not None
        self._ready.set()
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return

        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        host, _, port_text = parts[1].rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        upstream_writer = None
        try:
            up_reader, upstream_writer = await asyncio.wait_for(
                _socks5_connect(self.target, host, port), timeout=20)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                self._pipe(reader, upstream_writer),
                self._pipe(up_reader, writer),
            )
        except Exception:
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            for w in (writer, upstream_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(BUFFER)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.close()
