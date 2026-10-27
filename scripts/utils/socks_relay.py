#!/usr/bin/env python3
"""Unauthenticated SOCKS5 on loopback, relayed to an authenticated upstream.

Exists because Chromium-family browsers, Brave included, cannot authenticate to
a SOCKS5 proxy: Playwright 1.61.0 refuses SOCKS5 with a username and password at
launch, and Brave's own `--proxy-server=socks5://...` flag carries no credentials
at all (both MEASURED 2026-09-25, HOS-134). The browser talks plain SOCKS5 to this
relay on 127.0.0.1, and the relay opens each upstream connection through PySocks
with the slot's credentials.

Two ways in:

  * in process: `with SocksRelay(url) as relay:` then `relay.url`, as
    `scripts/linkedin-activity.py` does for the length of one scrape;
  * as a child process, for a browser that outlives the command that launched
    it (`scripts/browser.py launch --proxy-slot N`). The child reads the slot
    from `.env` itself, so the password never appears in any argv, prints one
    line `READY <port>` on stdout, then reads the browser's PID from stdin and
    exits when that process is gone, or on SIGTERM. Stdin closing before a PID
    arrives also ends it: a launch that failed leaves no relay behind.

Usage:
  python scripts/utils/socks_relay.py --slot 1
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import select
import signal
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.utils.pid_liveness import pid_is_running  # noqa: E402

log = logging.getLogger("socks-relay")

SOCKS_SCHEMES = ("socks5", "socks5h")


def proxy_scheme(url: str) -> str:
    """The slot's scheme, lower-cased; a URL with none is http."""
    return urlparse(url if "://" in url else f"http://{url}").scheme.lower()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed during the SOCKS5 handshake")
        buf += chunk
    return buf


def _socks_reply(code: int) -> bytes:
    """A SOCKS5 reply with a zero IPv4 bound address, which is all a browser reads."""
    return bytes([5, code, 0, 1]) + bytes(6)


class SocksRelay:
    """Unauthenticated SOCKS5 on loopback, relayed to an authenticated upstream.

    Scope, on purpose: one thread per connection, bound to 127.0.0.1 only,
    CONNECT only, and gone when the `with` block ends. The accept loop also
    drops any peer that is not loopback, which the bind already rules out.
    `last_error` keeps the newest upstream refusal so the caller can name it
    instead of the browser's generic network error.
    """

    def __init__(self, upstream_url: str, connect_timeout: float = 30.0):
        import socks  # PySocks; only a socks slot constructs a relay
        parsed = urlparse(upstream_url)
        try:
            port = parsed.port
        except ValueError:
            # `urlparse` quotes the unparseable piece, and in a URL whose
            # password holds an unencoded `/`, `?` or `#` that piece is part of
            # the password. Name nothing from the URL.
            port = None
        if parsed.scheme.lower() not in SOCKS_SCHEMES or not parsed.hostname or not port:
            raise ValueError("SocksRelay needs a socks5:// or socks5h:// URL with a host and a "
                             "port (percent-encode any / ? # in the password)")
        self._socks = socks
        self._upstream = (parsed.hostname, parsed.port)
        self._rdns = parsed.scheme.lower() == "socks5h"
        self._username = unquote(parsed.username) if parsed.username else None
        self._password = unquote(parsed.password) if parsed.password else None
        self._timeout = connect_timeout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._open: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._listener: socket.socket | None = None
        self.last_error: str | None = None
        self.url = ""
        self.port = 0

    def __enter__(self) -> "SocksRelay":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self.url = f"socks5://127.0.0.1:{self.port}"
        accept = threading.Thread(target=self._accept_loop, name="socks-relay", daemon=True)
        self._threads.append(accept)
        accept.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            still_open = list(self._open)
            self._open.clear()
        for sock in still_open:
            sock.close()
        for thread in list(self._threads):
            thread.join(timeout=5)
        return False

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._open.add(sock)

    def _release(self, sock: socket.socket) -> None:
        with self._lock:
            self._open.discard(sock)
        sock.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                # `__exit__` closing the listener lands here; anything else also
                # ends the relay rather than spinning on a dead socket.
                log.debug("socks relay accept loop ended: %s", exc)
                return
            if not ipaddress.ip_address(addr[0]).is_loopback:
                log.warning("socks relay refused a non-loopback client %s", addr[0])
                client.close()
                continue
            self._track(client)
            worker = threading.Thread(target=self._serve, args=(client,), daemon=True)
            # A relay tied to a browser lives for the whole session, so finished
            # workers are dropped here rather than kept until `__exit__`.
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(worker)
            worker.start()

    def _handshake(self, client: socket.socket) -> tuple[str, int] | None:
        """Read the browser's greeting and CONNECT; None once a refusal is sent."""
        version, n_methods = _recv_exact(client, 2)
        methods = _recv_exact(client, n_methods)
        if version != 5 or 0 not in methods:
            client.sendall(b"\x05\xff")
            return None
        client.sendall(b"\x05\x00")
        version, command, _reserved, atyp = _recv_exact(client, 4)
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, _recv_exact(client, 4))
        elif atyp == 3:
            host = _recv_exact(client, _recv_exact(client, 1)[0]).decode("ascii")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            client.sendall(_socks_reply(8))
            return None
        port = int.from_bytes(_recv_exact(client, 2), "big")
        if version != 5 or command != 1:
            client.sendall(_socks_reply(7))
            return None
        return host, port

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(self._timeout)
            target = self._handshake(client)
            if target is None:
                return
            upstream = self._socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            self._track(upstream)
            upstream.set_proxy(self._socks.SOCKS5, self._upstream[0], self._upstream[1],
                               rdns=self._rdns, username=self._username,
                               password=self._password)
            upstream.settimeout(self._timeout)
            try:
                upstream.connect(target)
            except self._socks.ProxyError as exc:
                self.last_error = f"upstream refused {target[0]}:{target[1]}: {exc}"
                log.warning("socks relay: %s", self.last_error)
                client.sendall(_socks_reply(1))
                return
            client.sendall(_socks_reply(0))
            self._pipe(client, upstream)
        except (OSError, ValueError) as exc:
            # ValueError: a hostname that is not ASCII fails `.decode("ascii")`.
            log.debug("socks relay connection ended: %s", exc)
        finally:
            self._release(client)
            if upstream is not None:
                self._release(upstream)

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        peer = {a: b, b: a}
        while not self._stop.is_set():
            readable, _, _ = select.select([a, b], [], [], 1.0)
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                peer[sock].sendall(data)


_HEADER_MAX = 65536


class HttpRelay(SocksRelay):
    """Unauthenticated HTTP proxy on loopback, relayed to the same SOCKS5 upstream.

    For a wrapper that hands a command the standard proxy
    variables. An http:// proxy is the one form nearly every client honours:
    the stdlib's urllib cannot speak SOCKS at all, and requests needs PySocks
    for it. `CONNECT host:port` (every https request) is tunnelled; a plain
    http request in absolute form is rewritten to origin form and sent with
    `Connection: close`, so a kept-alive client connection can never carry a
    second request to a different host down the first host's tunnel.
    Accept loop, loopback check and teardown are `SocksRelay`'s.
    """

    def __enter__(self) -> "HttpRelay":
        super().__enter__()
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > _HEADER_MAX:
                raise ValueError("request header over 64 KiB")
            chunk = client.recv(4096)
            if not chunk:
                raise ConnectionError("client closed before the request header ended")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        return head, rest

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(self._timeout)
            head, rest = self._read_head(client)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                host = host.strip("[]")
                if not host or not port_text.isdigit():
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    return
                forward = rest
            else:
                # urlsplit, not urlparse: urlparse cuts `;params` off the last
                # path segment, and the rewrite below would then drop them.
                parsed = urlsplit(target)
                if parsed.scheme.lower() != "http" or not parsed.hostname:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    return
                host, port_text = parsed.hostname, str(parsed.port or 80)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                kept = [h for h in lines[1:] if h.split(":", 1)[0].strip().lower()
                        not in ("proxy-connection", "proxy-authorization", "connection")]
                forward = ("\r\n".join([f"{method} {path} {version}", *kept, "Connection: close"])
                           .encode("latin-1") + b"\r\n\r\n" + rest)
            target_addr = (host, int(port_text))
            upstream = self._socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            self._track(upstream)
            upstream.set_proxy(self._socks.SOCKS5, self._upstream[0], self._upstream[1],
                               rdns=self._rdns, username=self._username,
                               password=self._password)
            upstream.settimeout(self._timeout)
            try:
                upstream.connect(target_addr)
            except self._socks.ProxyError as exc:
                self.last_error = f"upstream refused {host}:{port_text}: {exc}"
                log.warning("http relay: %s", self.last_error)
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            if method.upper() == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if forward:
                upstream.sendall(forward)
            self._pipe(client, upstream)
        except (OSError, ValueError) as exc:
            log.debug("http relay connection ended: %s", exc)
        finally:
            self._release(client)
            if upstream is not None:
                self._release(upstream)


def slot_url(slot: int) -> str:
    """The `DECODO_PROXY_<slot>` URL, which must be a SOCKS5 slot.

    A browser flag cannot carry credentials, so an http slot has no route
    through a relay either; it is refused rather than launched direct.
    """
    from scripts.utils.workspace import load_env
    load_env()
    url = os.environ.get(f"DECODO_PROXY_{slot}", "").strip()
    if not url:
        raise ValueError(f"DECODO_PROXY_{slot} not in .env")
    if proxy_scheme(url) not in SOCKS_SCHEMES:
        raise ValueError(f"DECODO_PROXY_{slot} is not a socks5:// or socks5h:// slot")
    return url


def _main() -> int:
    parser = argparse.ArgumentParser(description="Loopback SOCKS5 relay for one DECODO_PROXY slot.")
    parser.add_argument("--slot", type=int, required=True, choices=[1, 2, 3])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s socks-relay: %(message)s")
    try:
        relay = SocksRelay(slot_url(args.slot))
    except (ValueError, ImportError) as exc:
        print(f"ERROR {exc}", flush=True)
        return 1
    stop = threading.Event()

    def _on_sigterm(*_):
        # Raised rather than flagged, so a SIGTERM that lands while stdin is
        # still being read ends the process too; the `with` closes the relay.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)
    with relay:
        print(f"READY {relay.port}", flush=True)
        line = sys.stdin.readline().strip()
        if not line.isdigit():
            log.info("no browser PID on stdin; stopping")
            return 0
        watched = int(line)
        log.info("relay on 127.0.0.1:%d for slot %d, tied to PID %d",
                 relay.port, args.slot, watched)
        while not stop.wait(2.0):
            if not pid_is_running(watched):
                log.info("PID %d is gone; stopping", watched)
                break
    return 0


if __name__ == "__main__":
    sys.exit(_main())
