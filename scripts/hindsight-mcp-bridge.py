#!/usr/bin/env python3
"""A local stdio MCP server that stands in front of the remote Hindsight MCP endpoint.

    hindsight-mcp-bridge.py serve     # what Claude Code spawns; stdio JSON-RPC, never exits on a network fault
    hindsight-mcp-bridge.py probe     # read-only: initialize, list the tools, close the session
    hindsight-mcp-bridge.py cache     # print where the tool catalog cache is and what it holds
    hindsight-mcp-bridge.py self-test [--only TEXT] [--live]
                                      # every scenario against a fake loopback server; --live adds a
                                      # READ-ONLY check of --url (initialize, tools/list, one recall)

The self-test is the test suite: under M050 this file gets no `tests/` entry, so
the scenarios live here, each one driving this file as a real stdio subprocess.

WHY IT EXISTS. Claude Code reaches a `type: http` MCP server directly, and when the
link drops it retries five times over about 31 s and then leaves the server failed
until someone types `/mcp`. The link here is an SSH local forward that restarts only
every 30 s, on networks where it drops dozens of times a day, so most drops outlast
the client's retries. A stdio server is a local process: it never "drops" from the
client's side. This one answers the client itself and reaches the remote endpoint
behind it, so a dead tunnel costs the calls made during the outage and nothing else.

TWO LIFECYCLES, AND THEY ARE NOT THE SAME CONVERSATION. Toward Claude Code this is
its own MCP server: it answers `initialize` from a fixed profile (tools only,
`listChanged: true`, its own `serverInfo`, a protocol version it implements), and it
never replays an upstream initialize as if it were the server's. Toward Hindsight it
is an MCP client over Streamable HTTP, with its own session, connected lazily and
recovered in a background loop with backoff and jitter. One upstream initialization
runs at a time; every session carries a generation number, and a reply that belongs
to an attempt the bridge has already abandoned is dropped rather than delivered.

WHAT A FAILED CALL SAYS, because a retry is where a memory gets written twice.

- not executed: the connection was refused, the header file could not be read, or
  the server refused the request before running it (401, 403, a redirect, another
  4xx). Retrying is safe.
- session invalid: HTTP 404 with a JSON-RPC error body on a request that carried a
  session id. The server rejected it at the session check, before dispatch, so the
  bridge re-initializes once (one initialization shared by every call that saw it)
  and replays the request once. This is the ONLY automatic replay for writes but one.
- outcome unknown: the request may have reached the server and no final answer came
  back (the stream broke, the deadline passed, a 5xx). Never replayed. A read-only
  tool says it is safe to ask again; any other tool says it may have been executed.
- the one exception: `retain` or `sync_retain` carrying a `document_id`, with
  `update_mode` absent or "replace", replaces that document on this server
  (Hindsight 0.10.2, api/retain.mdx, as read by HELM on 2026-09-30), so it is
  replayed once after an outcome-unknown failure, as soon as the link is back
  within the call's deadline. MEASURED 2026-09-30 on the live endpoint: `retain`
  also takes `update_mode`, a free string whose other values were not read, so
  any value but "replace" is treated as not idempotent. That narrowing is this
  script's own inference, not a documented fact.

The read-only set below is a list, not the tools' `readOnlyHint`: an annotation is
the server describing itself, and whether a replay is safe is not a thing to take on
its word. MEASURED 2026-09-30: the 15 names below are exactly the tools that carry
`readOnlyHint` today except `reflect`, which is left out because it runs a model and
nothing read here says it stores nothing. A tool missing from the list is treated as
a write, so a new tool fails safe.

WHAT IT DOES NOT DO, and why each is safe here.

- No GET/SSE stream. Hindsight's MCP tools send no server-initiated messages: HELM
  grepped `mcp_tools.py` and `api/mcp.py` of v0.10.2 on 2026-09-30 for
  report_progress, notifications, ctx.log/sample/elicit and found none. Progress and
  notifications that arrive on a POST's own stream are still relayed, and a request
  the server sends there is answered "method not found" so it never waits on us.
  An upstream `notifications/tools/list_changed` triggers a catalog refresh.
- No prompts or resources. MEASURED 2026-09-30: the server advertises both and
  lists none, so advertising tools only loses nothing today.
- No per-call schema check against a changed catalog. A call for a tool the live
  catalog no longer has is refused; a changed schema under the same name is left to
  the server, which validates arguments itself.
- No rollback on cancel. `notifications/cancelled` is forwarded to the mapped
  upstream request and its HTTP stream is aborted; what already ran stays run.

THE SERVER'S TWO MODES. Stateful (today: it issues `Mcp-Session-Id`) and stateless
(no session header on the initialize answer): the bridge sends the session header
only when it was given one, so both work without configuration.

TIMEOUTS. Connect 5 s. Initialize and `tools/list` 20 s each, so a link that accepts
and then stays silent fails the handshake quickly. A tool call has a deadline
(default 600 s, because `reflect` has a 120 s model timeout on the server and can
take longer end to end). While a call's stream is open, silence longer than the idle
timeout (default 60 s) counts as a dead link, but only once this process has seen a
keepalive comment on some stream: the MCP SDK's SSE responses send one every 15 s
(read in sse-starlette's `DEFAULT_PING_INTERVAL`, a local copy, not the server's), and
a server that sends none would otherwise have its long quiet calls cut. Without
keepalives the SSH tunnel's own end, which closes the forwarded socket within about
45 s of a dead link, is what ends the wait.

CATALOG CACHE. The last full `tools/list` answer is kept at the runtime address
`runtime:hindsight-mcp-bridge/cache`, one file per endpoint, holding the endpoint,
the tools and a timestamp and never a header, written through a temp file and
`os.replace`, so several bridge processes can share it. It is served only as the
last known list; after every recovery the bridge re-reads the full catalog (all
pages), publishes it in one step, and sends `notifications/tools/list_changed` when
it differs, compared without regard to order. No cache, or a corrupt one: an empty
catalog, the bridge stays initialized, and recovery fills it.

STDOUT CARRIES ONLY PROTOCOL. At start the real stdout is duplicated for the writer
and fd 1 is pointed at stderr, so a stray print from anywhere lands in the log. One
lock serializes whole messages; each is one line, flushed. The bearer key and memory
payloads are never logged: only methods, ids, statuses and timings.

Settings: `--url` / HINDSIGHT_MCP_BRIDGE_URL (default http://127.0.0.1:8888/mcp/main/),
`--headers-file` / HINDSIGHT_MCP_BRIDGE_HEADERS (default ~/.config/hindsight/mcp-headers.json,
a JSON object of header names to values, re-read on every connect),
`--cache-file` / HINDSIGHT_MCP_BRIDGE_CACHE (overrides the resolved address),
HINDSIGHT_MCP_BRIDGE_DEBUG=1 for per-message log lines.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import itertools
import json
import os
import random
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BRIDGE_NAME = "hindsight-mcp-bridge"
BRIDGE_VERSION = "1.0.0"
# Newest first. MEASURED 2026-09-30: the live server echoes 2024-11-05, 2025-06-18
# and 2025-11-25 when asked for each; 2025-03-26 is in the same family and differs
# from its neighbours in nothing this bridge touches.
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_URL = "http://127.0.0.1:8888/mcp/main/"
DEFAULT_HEADERS_FILE = "~/.config/hindsight/mcp-headers.json"

READ_ONLY_TOOLS = frozenset({
    "recall", "list_mental_models", "get_mental_model", "list_directives",
    "list_memories", "get_memory", "list_documents", "get_document",
    "list_operations", "get_operation", "list_tags", "get_bank",
    "get_knowledge_base_tree", "search_knowledge_base", "get_knowledge_page",
})
REPLACING_RETAIN_TOOLS = frozenset({"retain", "sync_retain"})

# Headers the bridge owns on the upstream leg; a header file may not set them.
_RESERVED_HEADERS = frozenset({
    "content-type", "accept", "mcp-session-id", "mcp-protocol-version",
    "content-length", "host", "transfer-encoding", "connection",
})
_MAX_BODY = 256 * 1024 * 1024
_JITTER = random.SystemRandom()  # backoff jitter and temp names; not a secret, but no reason for a weaker source
_MAX_PAGES = 100

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_NOT_EXECUTED = -32001
ERR_OUTCOME_UNKNOWN = -32002
ERR_BUSY = -32003


# ---------------------------------------------------------------- logging, output

_DEBUG = os.environ.get("HINDSIGHT_MCP_BRIDGE_DEBUG") == "1"


def log(msg: str) -> None:
    """One line to stderr. Never pass a header value or a payload in here."""
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"
        sys.stderr.write(f"{stamp} {BRIDGE_NAME}[{os.getpid()}] {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass  # stderr itself is gone; there is nowhere left to report to


def debug(msg: str) -> None:
    if _DEBUG:
        log(msg)


class Output:
    """The one writer of protocol messages: whole lines, one at a time, flushed."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = threading.Event()

    def send(self, message: dict) -> bool:
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self.broken.is_set():
                return False
            try:
                self._stream.write(data)
                self._stream.flush()
                return True
            except (OSError, ValueError) as exc:
                log(f"stdout is closed ({type(exc).__name__}); shutting down")
                self.broken.set()
                return False


# ---------------------------------------------------------------- upstream errors

class UpstreamError(Exception):
    """kind: not_sent | session_invalid | ambiguous | auth | rejected | cancelled.

    `link_down` marks a failure of the connection itself, which is what tells the
    recovery loop the backend is gone, as opposed to a server that answered badly.
    """

    def __init__(self, kind: str, detail: str, *, link_down: bool = False) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.link_down = link_down


class Abort:
    """Lets another thread cut one HTTP exchange: cancellation and shutdown use it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self.fired = False

    def attach(self, sock: socket.socket | None) -> None:
        with self._lock:
            self._sock = sock
            if self.fired and sock is not None:
                _shutdown_socket(sock)

    def fire(self) -> None:
        with self._lock:
            self.fired = True
            if self._sock is not None:
                _shutdown_socket(self._sock)


def _shutdown_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError as exc:
        debug(f"socket shutdown: {type(exc).__name__}")


def load_headers(path: str) -> dict[str, str]:
    """The header file, validated. Raises UpstreamError(not_sent) on any defect,
    naming the defect and never the value."""
    p = Path(path).expanduser()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError("not_sent", f"header file unreadable ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not data:
        raise UpstreamError("not_sent", "header file is not a JSON object of headers")
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key \
                or any(c in key for c in ":\r\n \t") or "\r" in value or "\n" in value:
            raise UpstreamError("not_sent", "header file holds a malformed header")
        if key.lower() in _RESERVED_HEADERS:
            log(f"header file sets {key}, which the bridge owns; ignored")
            continue
        out[key] = value
    return out


class Endpoint:
    def __init__(self, url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the endpoint URL must be http(s)://host[:port]/path")
        if parts.username or parts.password:
            raise ValueError("the endpoint URL may not carry credentials; use the header file")
        self.url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    def connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)


# ---------------------------------------------------------------- one HTTP exchange

def _iter_sse(resp, sock, deadline: float, idle_s: float | None, on_keepalive):
    """Yield parsed JSON-RPC messages from an event stream as they arrive.

    Reads line by line, so nothing waits for the end of the body. Each read may
    block at most until the deadline, or the idle limit when one applies."""
    data_lines: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpstreamError("ambiguous", "deadline passed while the call was running")
        wait = remaining if idle_s is None else min(remaining, idle_s)
        try:
            sock.settimeout(wait)
        except OSError as exc:
            raise UpstreamError("ambiguous", f"stream socket unusable ({type(exc).__name__})", link_down=True) from None
        try:
            raw = resp.readline(_MAX_BODY)
        except (TimeoutError, socket.timeout):
            if deadline - time.monotonic() <= 0:
                raise UpstreamError("ambiguous", "deadline passed while the call was running") from None
            raise UpstreamError("ambiguous", f"no bytes for {wait:.0f} s on an open stream", link_down=True) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise UpstreamError("ambiguous", f"stream broke ({type(exc).__name__})", link_down=True) from None
        if not raw:
            if data_lines:
                yield _parse_event("\n".join(data_lines))
            raise UpstreamError("ambiguous", "stream ended before the answer", link_down=True)
        if not raw.endswith(b"\n") and len(raw) >= _MAX_BODY:
            raise UpstreamError("ambiguous", "event larger than the bridge accepts")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                message = _parse_event("\n".join(data_lines))
                data_lines = []
                if message is not None:
                    yield message
            continue
        if line.startswith(":"):
            on_keepalive()
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)


def _parse_event(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log("upstream sent an event that is not JSON; skipped")
        return None


class Upstream:
    """The client side: session, generation, single-flight initialization, recovery."""

    def __init__(self, endpoint: Endpoint, headers_file: str, *, connect_s: float,
                 init_s: float, idle_s: float) -> None:
        self.endpoint = endpoint
        self.headers_file = headers_file
        self.connect_s = connect_s
        self.init_s = init_s
        self.idle_s = idle_s
        self._cond = threading.Condition()
        self._ids = itertools.count(1)
        self.session_id: str | None = None
        self.version: str | None = None
        self.generation = 0
        self.ready = False
        self._initializing = False
        self.last_error = "not connected yet"
        self.last_kind = "not_sent"
        self.keepalives_seen = False
        self.proposed_version = SUPPORTED_VERSIONS[0]
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.on_ready = None          # callback(tools) after a successful (re)connect
        self.on_notification = None   # callback(message) for upstream notifications
        self.aborts: set[Abort] = set()
        self._aborts_lock = threading.Lock()

    # -- ids and state

    def next_id(self) -> int:
        return next(self._ids)

    def snapshot(self) -> tuple[bool, int]:
        with self._cond:
            return self.ready, self.generation

    def mark_down(self, generation: int, reason: str) -> None:
        with self._cond:
            if generation == self.generation and self.ready:
                self.ready = False
                self.last_error = reason
                log(f"backend down (generation {generation}): {reason}")
        self.wake.set()

    def invalidate_session(self, generation: int) -> None:
        with self._cond:
            if generation == self.generation and self.session_id is not None:
                log(f"session of generation {generation} rejected by the server; re-initializing")
                self.session_id = None
                self.ready = False
        self.wake.set()

    def wait_ready(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._cond:
            while not self.ready and not self.stop.is_set():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 1.0))
            return self.ready

    def ensure_ready(self) -> int:
        """Return the generation of a ready backend, connecting now if needed.

        Single-flight: while one thread initializes, the others wait for its
        result instead of opening sessions of their own."""
        end = time.monotonic() + self.connect_s + 4 * self.init_s + 1
        waited = False
        with self._cond:
            while True:
                if self.ready:
                    return self.generation
                if self.stop.is_set():
                    raise UpstreamError("not_sent", "bridge is shutting down")
                if not self._initializing:
                    if waited:
                        # Somebody else's attempt just failed: share its result
                        # instead of starting a second one straight away.
                        raise UpstreamError("auth" if self.last_kind == "auth" else "not_sent", self.last_error)
                    self._initializing = True
                    break
                left = end - time.monotonic()
                if left <= 0:
                    raise UpstreamError("not_sent", "backend initialization is still running")
                waited = True
                self._cond.wait(left)
        tools = None
        try:
            tools = self._connect()
        except UpstreamError as exc:
            with self._cond:
                self.last_error = exc.detail
                self.last_kind = exc.kind
            raise
        finally:
            with self._cond:
                self._initializing = False
                self._cond.notify_all()
        if self.on_ready is not None and tools is not None:
            self.on_ready(tools)
        with self._cond:
            return self.generation

    def _connect(self) -> list:
        """Revive the old session with a ping if there is one, else initialize.
        Then read the whole catalog. Raises UpstreamError; sets ready on success."""
        with self._cond:
            session, version, gen = self.session_id, self.version, self.generation
        if session is not None:
            try:
                self._exchange({"jsonrpc": "2.0", "id": self.next_id(), "method": "ping"},
                               session, version, time.monotonic() + self.init_s, None)
            except UpstreamError as exc:
                if exc.kind != "session_invalid":
                    raise
                session = None
                with self._cond:
                    if self.generation == gen:
                        self.session_id = None
        if session is None:
            session, version = self._initialize()
            with self._cond:
                self.generation += 1
                self.session_id = session
                self.version = version
                gen = self.generation
            log(f"connected: generation {gen}, protocol {version}, "
                f"{'stateful' if session else 'stateless'} server")
        tools = self._list_tools(session, version)
        with self._cond:
            if self.generation == gen:
                self.ready = True
                self.last_error = ""
                self._cond.notify_all()
        return tools

    def _initialize(self) -> tuple[str | None, str]:
        req = {"jsonrpc": "2.0", "id": self.next_id(), "method": "initialize", "params": {
            "protocolVersion": self.proposed_version,
            "capabilities": {},
            "clientInfo": {"name": BRIDGE_NAME, "version": BRIDGE_VERSION},
        }}
        reply, headers = self._exchange(req, None, None, time.monotonic() + self.init_s, None,
                                        want_headers=True)
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(result, dict):
            raise UpstreamError("rejected", "server refused initialize")
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise UpstreamError("rejected", f"server speaks protocol {version!r}, which the bridge does not implement")
        session = headers.get("mcp-session-id") or None
        self._exchange({"jsonrpc": "2.0", "method": "notifications/initialized"}, session, version,
                       time.monotonic() + self.init_s, None)
        return session, version

    def _list_tools(self, session: str | None, version: str | None) -> list:
        tools: list = []
        cursor = None
        seen: set = set()
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            reply, _h = self._exchange({"jsonrpc": "2.0", "id": self.next_id(), "method": "tools/list",
                                        "params": params}, session, version,
                                       time.monotonic() + self.init_s, None, want_headers=True)
            result = reply.get("result") if isinstance(reply, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise UpstreamError("rejected", "server answered tools/list without a tool list")
            tools.extend(t for t in result["tools"] if isinstance(t, dict) and isinstance(t.get("name"), str))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if cursor in seen:
                raise UpstreamError("rejected", "tools/list repeated a cursor")
            seen.add(cursor)
        raise UpstreamError("rejected", f"tools/list ran past {_MAX_PAGES} pages")

    # -- the request path used by calls

    def request(self, message: dict, generation: int, deadline: float, abort: Abort,
                on_message) -> dict:
        with self._cond:
            if generation != self.generation or not self.ready:
                raise UpstreamError("not_sent", "backend changed before the request was sent")
            session, version = self.session_id, self.version
        return self._exchange(message, session, version, deadline, abort, on_message=on_message,
                              call=True)

    def notify(self, message: dict, timeout: float) -> None:
        """Best-effort notification or response to the server; failures are logged."""
        with self._cond:
            session, version, ready = self.session_id, self.version, self.ready
        if not ready:
            return
        try:
            self._exchange(message, session, version, time.monotonic() + timeout, None)
        except UpstreamError as exc:
            log(f"could not deliver {message.get('method', 'a response')} upstream: {exc.kind}")

    def close_session(self, timeout: float) -> None:
        """Best-effort DELETE of the upstream session on shutdown. MEASURED
        2026-09-30: a DELETE on the live endpoint once hung for 10 s, so this is
        bounded and never retried."""
        with self._cond:
            session = self.session_id
        if not session:
            return
        try:
            headers = load_headers(self.headers_file)
            conn = self.endpoint.connection(timeout)
            headers["Mcp-Session-Id"] = session
            conn.request("DELETE", self.endpoint.path, headers=headers)
            conn.getresponse().read(4096)
            conn.close()
        except (UpstreamError, OSError, http.client.HTTPException) as exc:
            log(f"session close skipped: {type(exc).__name__}")

    def _exchange(self, message: dict, session: str | None, version: str | None, deadline: float,
                  abort: Abort | None, *, on_message=None, want_headers: bool = False, call: bool = False):
        headers = load_headers(self.headers_file)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        if session:
            headers["Mcp-Session-Id"] = session
        if version:
            headers["MCP-Protocol-Version"] = version
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        is_request = "id" in message and "method" in message
        own = abort or Abort()
        with self._aborts_lock:
            self.aborts.add(own)
        conn = self.endpoint.connection(self.connect_s)
        try:
            if own.fired or self.stop.is_set():
                raise UpstreamError("cancelled", "cancelled before sending")
            try:
                conn.connect()
            except OSError as exc:
                raise UpstreamError("not_sent", f"cannot connect to the endpoint ({type(exc).__name__})",
                                    link_down=True) from None
            sock = conn.sock
            own.attach(sock)
            # A call may wait for its headers until its deadline: a server in JSON
            # response mode sends nothing until the tool has finished.
            first = deadline - time.monotonic() if call else min(self.init_s, deadline - time.monotonic())
            try:
                sock.settimeout(max(0.05, first))
                conn.request("POST", self.endpoint.path, body=body, headers=headers)
                resp = conn.getresponse()
            except (TimeoutError, socket.timeout):
                raise UpstreamError("ambiguous", "no answer to the request", link_down=True) from None
            except (OSError, http.client.HTTPException) as exc:
                if own.fired:
                    raise UpstreamError("cancelled", "cancelled while sending") from None
                raise UpstreamError("ambiguous", f"request broke ({type(exc).__name__})", link_down=True) from None
            return self._read_response(resp, sock, message, session, deadline, own, on_message,
                                       want_headers, is_request, call)
        except UpstreamError as exc:
            if own.fired and exc.kind != "cancelled":
                raise UpstreamError("cancelled", "cancelled") from None
            raise
        finally:
            with self._aborts_lock:
                self.aborts.discard(own)
            try:
                conn.close()
            except OSError as exc:
                debug(f"connection close: {type(exc).__name__}")

    def _read_response(self, resp, sock, message, session, deadline, own, on_message,
                       want_headers, is_request, call):
        status = resp.status
        rheaders = {k.lower(): v for k, v in resp.getheaders()}
        ctype = rheaders.get("content-type", "").split(";")[0].strip().lower()
        if status == 202:
            resp.read(4096)
            if is_request:
                raise UpstreamError("ambiguous", "server accepted a request without answering it")
            return (None, rheaders) if want_headers else None
        if 300 <= status < 400:
            raise UpstreamError("rejected", f"redirect {status} not followed")
        if status in (401, 403):
            raise UpstreamError("auth", f"server refused the credentials ({status})")
        if status == 404:
            text = self._read_small(resp)
            if session and _is_jsonrpc_error(text):
                raise UpstreamError("session_invalid", "server does not know the session")
            raise UpstreamError("rejected", "endpoint not found (404)")
        if status >= 500:
            self._read_small(resp)
            raise UpstreamError("ambiguous", f"server error {status}")
        if status != 200:
            self._read_small(resp)
            raise UpstreamError("rejected", f"server refused the request ({status})")
        if not is_request:
            resp.read(4096)
            return (None, rheaders) if want_headers else None
        want_id = message["id"]
        if ctype == "text/event-stream":
            idle = self.idle_s if (call and self.keepalives_seen) else None
            if not call:
                idle = None
            for msg in _iter_sse(resp, sock, deadline, idle, self._saw_keepalive):
                final = self._route(msg, want_id, on_message)
                if final is not None:
                    return (final, rheaders) if want_headers else final
            raise UpstreamError("ambiguous", "stream ended before the answer", link_down=True)
        if ctype == "application/json":
            try:
                sock.settimeout(max(0.05, deadline - time.monotonic()))
                raw = resp.read(_MAX_BODY)
            except (TimeoutError, socket.timeout):
                raise UpstreamError("ambiguous", "deadline passed while reading the answer", link_down=True) from None
            except (OSError, http.client.HTTPException) as exc:
                raise UpstreamError("ambiguous", f"answer broke ({type(exc).__name__})", link_down=True) from None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise UpstreamError("ambiguous", "answer is not JSON") from None
            for msg in parsed if isinstance(parsed, list) else [parsed]:
                final = self._route(msg, want_id, on_message)
                if final is not None:
                    return (final, rheaders) if want_headers else final
            raise UpstreamError("ambiguous", "answer did not carry the response")
        raise UpstreamError("ambiguous", f"unexpected content type {ctype or 'none'}")

    def _route(self, msg, want_id, on_message):
        """Return msg when it is the answer we are waiting for; route anything else."""
        if not isinstance(msg, dict):
            return None
        if "method" not in msg and ("result" in msg or "error" in msg):
            if msg.get("id") == want_id:
                return msg
            debug("dropped a response for an id this exchange does not own")
            return None
        method = msg.get("method")
        if not isinstance(method, str):
            return None
        if "id" in msg:
            # A server-to-client request. The bridge advertises no client
            # capabilities, so answer it rather than leave the server waiting.
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": ERR_METHOD_NOT_FOUND, "message": f"{method} is not supported by the bridge"}}
            threading.Thread(target=self.notify, args=(reply, self.init_s), daemon=True).start()
            return None
        if method == "notifications/tools/list_changed":
            self.wake.set()
            if self.on_notification is not None:
                self.on_notification(msg)
            return None
        if on_message is not None:
            on_message(msg)
        return None

    @staticmethod
    def _read_small(resp) -> str:
        try:
            return resp.read(65536).decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            debug(f"error body unreadable: {type(exc).__name__}")
            return ""

    def _saw_keepalive(self) -> None:
        if not self.keepalives_seen:
            self.keepalives_seen = True
            debug("keepalives seen on an upstream stream; idle limit armed")

    def abort_all(self) -> None:
        with self._aborts_lock:
            aborts = list(self.aborts)
        for a in aborts:
            a.fire()


def _is_jsonrpc_error(text: str) -> bool:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("error"), dict)


# ---------------------------------------------------------------- the tool catalog

def fingerprint(tools: list) -> str:
    ordered = sorted(tools, key=lambda t: t.get("name", ""))
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def default_cache_file(url: str) -> Path | None:
    """The runtime address for this endpoint's cache, or None if it cannot be resolved."""
    try:
        from scripts.utils.data_path import data_path
        base = data_path("runtime:hindsight-mcp-bridge/cache")
    except Exception as exc:  # noqa: BLE001 - any resolver failure means "no cache", and is logged
        log(f"cache address unresolved ({type(exc).__name__}); running without a cache")
        return None
    return Path(base) / f"tools-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}.json"


class Catalog:
    def __init__(self, cache_file: Path | None, endpoint_url: str) -> None:
        self.cache_file = cache_file
        self.endpoint_url = endpoint_url
        self._lock = threading.Lock()
        self.tools: list = []
        self.fp = fingerprint([])
        self.live = False
        self.saved_at: str | None = None

    def load(self) -> None:
        if self.cache_file is None:
            return
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log("no tool catalog cache yet; starting with an empty catalog")
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log(f"tool catalog cache unreadable ({type(exc).__name__}); starting with an empty catalog")
            return
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list) or data.get("endpoint") != self.endpoint_url \
                or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools):
            log("tool catalog cache is for another endpoint or malformed; ignored")
            return
        with self._lock:
            self.tools = tools
            self.fp = fingerprint(tools)
            self.saved_at = data.get("saved_at")
        log(f"serving {len(tools)} tools from the cache written {self.saved_at} (last known, not live)")

    def names(self) -> set:
        with self._lock:
            return {t["name"] for t in self.tools}

    def current(self) -> list:
        with self._lock:
            return list(self.tools)

    def publish(self, tools: list) -> bool:
        """Swap in a live catalog. True when it differs from what was published."""
        new_fp = fingerprint(tools)
        with self._lock:
            changed = new_fp != self.fp
            self.tools = list(tools)
            self.fp = new_fp
            self.live = True
        self._save(tools, new_fp)
        return changed

    def _save(self, tools: list, fp: str) -> None:
        if self.cache_file is None:
            return
        try:
            old = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if isinstance(old, dict) and old.get("endpoint") == self.endpoint_url \
                    and isinstance(old.get("tools"), list) and fingerprint(old["tools"]) == fp:
                return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            debug(f"cache on disk not comparable ({type(exc).__name__}); rewriting it")
        payload = {"endpoint": self.endpoint_url,
                   "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "tools": tools}
        tmp = self.cache_file.with_name(
            f".{self.cache_file.name}.{os.getpid()}.{threading.get_ident()}.{_JITTER.getrandbits(32):08x}.tmp")
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.cache_file)
        except OSError as exc:
            log(f"tool catalog cache not written ({type(exc).__name__})")
            try:
                tmp.unlink()
            except OSError as unlink_exc:
                debug(f"temp cache not removed ({type(unlink_exc).__name__}); never created or already moved")


# ---------------------------------------------------------------- the bridge

class Pending:
    def __init__(self, fid, key: str, tool: str | None) -> None:
        self.fid = fid
        self.key = key
        self.tool = tool
        self.upstream_id: int | None = None
        self.abort = Abort()
        self.done = False
        self.cancelled = False


class Bridge:
    def __init__(self, upstream: Upstream, catalog: Catalog, out: Output, *, call_s: float,
                 replay_wait_s: float, workers: int, max_inflight: int) -> None:
        self.upstream = upstream
        self.catalog = catalog
        self.out = out
        self.call_s = call_s
        self.replay_wait_s = replay_wait_s
        self.max_inflight = max_inflight
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="call")
        self._lock = threading.Lock()
        self.inflight: dict[str, Pending] = {}
        self.client_initialized = False
        self._pending_list_changed = False
        self._recovery: threading.Thread | None = None
        self.stop = upstream.stop
        upstream.on_ready = self._on_upstream_ready
        upstream.on_notification = self._on_upstream_list_changed

    # -- frontend input

    def handle_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.out.send({"jsonrpc": "2.0", "id": None, "error": {"code": ERR_PARSE, "message": "parse error"}})
            return
        if isinstance(msg, list):
            self.out.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": ERR_INVALID_REQUEST, "message": "batches are not supported"}})
            return
        if not isinstance(msg, dict):
            self.out.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": ERR_INVALID_REQUEST, "message": "not a JSON-RPC message"}})
            return
        method = msg.get("method")
        has_id = "id" in msg
        if not isinstance(method, str):
            if has_id and ("result" in msg or "error" in msg):
                debug("ignored a client response: the bridge sends no requests to the client")
                return
            if has_id:
                self._error(msg.get("id"), ERR_INVALID_REQUEST, "no method")
            return
        debug(f"<- {method}" + (f" id={msg.get('id')!r}" if has_id else ""))
        if not has_id:
            self._notification(method, msg.get("params"))
            return
        fid = msg["id"]
        if not isinstance(fid, (str, int)) or isinstance(fid, bool):
            self._error(None, ERR_INVALID_REQUEST, "id must be a string or an integer")
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "initialize":
            self._initialize(fid, params)
        elif method == "ping":
            self.out.send({"jsonrpc": "2.0", "id": fid, "result": {}})
        elif method == "tools/list":
            if params.get("cursor"):
                self._error(fid, ERR_INVALID_PARAMS, "unknown cursor: the bridge returns the whole list in one page")
            else:
                self.out.send({"jsonrpc": "2.0", "id": fid, "result": {"tools": self.catalog.current()}})
        elif method == "tools/call":
            self._submit_call(fid, params)
        else:
            self._error(fid, ERR_METHOD_NOT_FOUND, f"{method} is not supported by the bridge")

    def _notification(self, method: str, params) -> None:
        if method == "notifications/initialized":
            with self._lock:
                self.client_initialized = True
                flush = self._pending_list_changed
                self._pending_list_changed = False
            if flush:
                self.out.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        elif method == "notifications/cancelled" and isinstance(params, dict):
            self._cancel(params.get("requestId"), params.get("reason"))

    def _initialize(self, fid, params: dict) -> None:
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        self.upstream.proposed_version = version
        self.out.send({"jsonrpc": "2.0", "id": fid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": BRIDGE_NAME, "version": BRIDGE_VERSION},
            "instructions": ("Local bridge to the Hindsight memory server. When the link is down a call "
                             "fails with a message saying whether it was executed."),
        }})
        self.start_recovery()

    # -- calls

    def _submit_call(self, fid, params: dict) -> None:
        key = json.dumps(fid)
        name = params.get("name")
        if not isinstance(name, str):
            self._error(fid, ERR_INVALID_PARAMS, "tools/call needs a tool name")
            return
        with self._lock:
            if key in self.inflight:
                duplicate = True
            else:
                duplicate = False
                busy = len(self.inflight) >= self.max_inflight
                if not busy:
                    pending = Pending(fid, key, name)
                    self.inflight[key] = pending
        if duplicate:
            self._error(fid, ERR_INVALID_REQUEST, "a request with this id is already running")
            return
        if busy:
            self._error(fid, ERR_BUSY, "bridge busy: too many calls in flight; not executed")
            return
        try:
            self.pool.submit(self._guarded_call, pending, params)
        except RuntimeError:
            self._finish(pending, error=(ERR_NOT_EXECUTED, "bridge is shutting down; not executed"))

    def _guarded_call(self, pending: Pending, params: dict) -> None:
        try:
            self._call(pending, params)
        except Exception as exc:  # noqa: BLE001 - a worker must answer, whatever broke
            log(f"call {pending.tool} failed inside the bridge: {type(exc).__name__}")
            self._finish(pending, error=(ERR_OUTCOME_UNKNOWN,
                                         f"bridge fault ({type(exc).__name__}); {self._unknown_advice(pending.tool)}"))

    def _call(self, p: Pending, params: dict) -> None:
        deadline = time.monotonic() + self.call_s
        tool = p.tool
        if self.catalog.live and tool not in self.catalog.names():
            self._finish(p, error=(ERR_INVALID_PARAMS, f"the server no longer offers the tool {tool}"))
            return
        replayed = False
        while True:
            if p.cancelled:
                return
            try:
                gen = self.upstream.ensure_ready()
            except UpstreamError as exc:
                self._finish(p, error=(ERR_NOT_EXECUTED,
                                       f"memory server unreachable ({exc.detail}); not executed, safe to retry"))
                return
            uid = self.upstream.next_id()
            with self._lock:
                if p.cancelled:
                    return
                p.upstream_id = uid
            forward = {"jsonrpc": "2.0", "id": uid, "method": "tools/call", "params": params}
            started = time.monotonic()
            try:
                reply = self.upstream.request(forward, gen, deadline, p.abort, self._relay)
            except UpstreamError as exc:
                debug(f"call {tool} upstream id {uid}: {exc.kind} after {time.monotonic() - started:.1f} s")
                if p.cancelled or exc.kind == "cancelled":
                    return
                if exc.link_down:
                    self.upstream.mark_down(gen, exc.detail)
                if exc.kind == "session_invalid" and not replayed:
                    replayed = True
                    self.upstream.invalidate_session(gen)
                    continue
                if exc.kind == "not_sent":
                    self._finish(p, error=(ERR_NOT_EXECUTED, f"{exc.detail}; not executed, safe to retry"))
                    return
                if exc.kind == "ambiguous" and not replayed and replaces_document(tool, params):
                    left = deadline - time.monotonic()
                    if left > 0 and self.upstream.wait_ready(min(left, self.replay_wait_s)):
                        replayed = True
                        log(f"replaying {tool} with a document_id after an unknown outcome")
                        continue
                if exc.kind == "ambiguous":
                    self._finish(p, error=(ERR_OUTCOME_UNKNOWN, f"{exc.detail}; {self._unknown_advice(tool)}"))
                    return
                self._finish(p, error=(ERR_NOT_EXECUTED, f"{exc.detail}; not executed"))
                return
            debug(f"call {tool} upstream id {uid}: answered in {time.monotonic() - started:.1f} s")
            self._finish(p, reply=reply)
            return

    @staticmethod
    def _unknown_advice(tool: str | None) -> str:
        if tool in READ_ONLY_TOOLS:
            return "no answer arrived; this tool only reads, so asking again is safe"
        return "OUTCOME UNKNOWN: it may have been executed; check before retrying"

    def _finish(self, p: Pending, *, reply: dict | None = None, error: tuple | None = None) -> None:
        """Deliver the one terminal answer for p, or nothing if it already has one."""
        with self._lock:
            if p.done or p.cancelled:
                return
            p.done = True
            self.inflight.pop(p.key, None)
        if error is not None:
            self._error(p.fid, error[0], error[1])
            return
        out = {"jsonrpc": "2.0", "id": p.fid}
        if isinstance(reply.get("error"), dict):
            out["error"] = reply["error"]
        else:
            out["result"] = reply.get("result", {})
        self.out.send(out)

    def _cancel(self, request_id, reason) -> None:
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return
        with self._lock:
            p = self.inflight.pop(json.dumps(request_id), None)
            if p is None or p.done:
                return
            p.cancelled = True
            uid = p.upstream_id
        log(f"call {p.tool} cancelled by the client; forwarding (no rollback is implied)")

        def forward():
            if uid is not None:
                note = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": uid}}
                if isinstance(reason, str):
                    note["params"]["reason"] = reason
                self.upstream.notify(note, 5.0)
            p.abort.fire()

        threading.Thread(target=self._guarded(forward), daemon=True).start()

    def _relay(self, msg: dict) -> None:
        """A notification that arrived on a call's stream (progress, log)."""
        if self.client_initialized and isinstance(msg.get("method"), str) and "id" not in msg:
            self.out.send(msg)

    # -- catalog and recovery

    def _on_upstream_ready(self, tools: list) -> None:
        changed = self.catalog.publish(tools)
        log(f"catalog: {len(tools)} tools live" + (", changed" if changed else ""))
        if changed:
            self._list_changed()

    def _on_upstream_list_changed(self, _msg) -> None:
        def refresh():
            ready, gen = self.upstream.snapshot()
            if ready:
                self.upstream.mark_down(gen, "server says its tool list changed")
        threading.Thread(target=self._guarded(refresh), daemon=True).start()

    def _list_changed(self) -> None:
        with self._lock:
            if not self.client_initialized:
                self._pending_list_changed = True
                return
        self.out.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def start_recovery(self) -> None:
        with self._lock:
            if self._recovery is not None:
                return
            self._recovery = threading.Thread(target=self._guarded(self._recovery_loop), name="recovery", daemon=True)
        self._recovery.start()

    def _recovery_loop(self) -> None:
        failures = 0
        while not self.stop.is_set():
            ready, _gen = self.upstream.snapshot()
            if ready:
                self.upstream.wake.wait(300)
                self.upstream.wake.clear()
                continue
            try:
                self.upstream.ensure_ready()
                failures = 0
                continue
            except UpstreamError as exc:
                failures += 1
                cap = 300.0 if exc.kind in ("auth", "rejected") or "header file" in exc.detail else 30.0
                delay = min(cap, 2.0 ** min(failures - 1, 10)) * _JITTER.uniform(0.5, 1.0)
                if failures == 1 or failures % 20 == 0:
                    log(f"backend unavailable ({exc.detail}); attempt {failures}, next in {delay:.1f} s")
            self.upstream.wake.clear()
            self.upstream.wake.wait(delay)

    @staticmethod
    def _guarded(fn):
        def run():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - a background thread must never kill the bridge silently
                log(f"background task {getattr(fn, '__name__', 'task')} failed: {type(exc).__name__}: {exc}")
        return run

    def _error(self, fid, code: int, message: str) -> None:
        self.out.send({"jsonrpc": "2.0", "id": fid, "error": {"code": code, "message": message}})

    def shutdown(self) -> None:
        self.stop.set()
        self.upstream.wake.set()
        with self._lock:
            pending = list(self.inflight.values())
            self.inflight.clear()
        for p in pending:
            p.cancelled = True
            p.abort.fire()
        self.upstream.abort_all()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.upstream.close_session(2.0)


def replaces_document(tool: str | None, params: dict) -> bool:
    """True only for a retain that replaces a named document, the one idempotent write."""
    if tool not in REPLACING_RETAIN_TOOLS:
        return False
    args = params.get("arguments")
    if not isinstance(args, dict):
        return False
    doc = args.get("document_id")
    mode = args.get("update_mode")
    return isinstance(doc, str) and doc != "" and (mode is None or mode == "replace")


# ---------------------------------------------------------------- self-test
#
# Every scenario drives the REAL entry point: this file started as a subprocess in
# `serve` mode, talking stdio, against a fake Streamable-HTTP MCP server on a
# loopback port owned by the self-test. Nothing leaves the machine; the one live
# check (`self-test --live`) is read-only: initialize, tools/list, one recall.
# Every stdout line of every bridge is checked to be exactly one JSON-RPC message,
# and stderr is checked for the invented key, the invented payload marker and any
# traceback. Scenarios run in parallel, each on its own ports and temp directory.

_ST_KEY = "Bearer st-key-7f3a"
_ST_MARKER = "st-payload-91c2"


def _st_tool(name: str) -> dict:
    return {"name": name, "description": f"{name} tool", "inputSchema": {"type": "object", "properties": {}}}


_ST_TOOLS = [_st_tool(n) for n in ("recall", "retain", "sync_retain", "update_memory", "slow_tool", "fast_tool")]


class _StState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: set = set()
        self.next_sid = 0
        self.stateless = False
        self.pages = [list(_ST_TOOLS)]
        self.status_override: dict = {}
        self.fail_init = 0
        self.init_count = 0
        self.init_versions: list = []
        self.executions: list = []
        self.behaviour: dict = {}
        self.cancels: list = []
        self.client_responses: list = []
        self.deletes = 0
        self.headers_seen: list = []
        self.elsewhere_hits = 0
        self.stream_closed = threading.Event()
        self.cancel_seen = threading.Event()

    def restart(self) -> None:
        with self.lock:
            self.sessions.clear()


def _st_handler(state: _StState):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            return

        def _json(self, status, obj=None, extra=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _sse_start(self, extra=None):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True

        def _event(self, obj):
            self.wfile.write(b"event: message\r\ndata: " + json.dumps(obj).encode() + b"\r\n\r\n")
            self.wfile.flush()

        def _drop(self):
            self.close_connection = True
            _shutdown_socket(self.connection)

        def do_DELETE(self):
            with state.lock:
                state.deletes += 1
                state.sessions.discard(self.headers.get("Mcp-Session-Id"))
            self._json(200)

        def do_POST(self):
            if self.path.startswith("/elsewhere"):
                with state.lock:
                    state.elsewhere_hits += 1
                self._json(200, {"jsonrpc": "2.0", "id": 0, "result": {}})
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with state.lock:
                state.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
            if self.headers.get("Authorization") != _ST_KEY:
                self._json(401, {"error": "unauthorized"})
                return
            method = body.get("method")
            if method is None:
                with state.lock:
                    state.client_responses.append(body)
                self._json(202)
                return
            name = (body.get("params") or {}).get("name") if method == "tools/call" else method
            status = state.status_override.get(name)
            if status in (307, 308):
                self._json(status, None, {"Location": "/elsewhere"})
                return
            if status in (401, 403):
                self._json(status, {"error": "no"})
                return
            if method == "initialize":
                with state.lock:
                    state.init_count += 1
                    state.init_versions.append(body["params"]["protocolVersion"])
                    drop = state.fail_init > 0
                    if drop:
                        state.fail_init -= 1
                if drop:
                    self._drop()
                    return
                extra = {}
                if not state.stateless:
                    with state.lock:
                        state.next_sid += 1
                        sid = f"sid-{state.next_sid}"
                        state.sessions.add(sid)
                    extra["Mcp-Session-Id"] = sid
                self._sse_start(extra)
                self._event({"jsonrpc": "2.0", "id": body["id"], "result": {
                    "protocolVersion": body["params"]["protocolVersion"],
                    "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "fake", "version": "0"}}})
                return
            if not state.stateless:
                with state.lock:
                    known = self.headers.get("Mcp-Session-Id") in state.sessions
                if not known:
                    self._json(404, {"jsonrpc": "2.0", "id": "server-error",
                                     "error": {"code": -32600, "message": "Session not found"}})
                    return
            if "id" not in body:
                if method == "notifications/cancelled":
                    with state.lock:
                        state.cancels.append(body["params"])
                    state.cancel_seen.set()
                    time.sleep(0.3)  # hold the 202 so an answer can overtake it
                self._json(202)
                return
            rid = body["id"]
            if method == "ping":
                self._sse_start()
                self._event({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "tools/list":
                cursor = (body.get("params") or {}).get("cursor")
                idx = int(cursor) if cursor else 0
                result = {"tools": state.pages[idx]}
                if idx + 1 < len(state.pages):
                    result["nextCursor"] = str(idx + 1)
                self._sse_start()
                self._event({"jsonrpc": "2.0", "id": rid, "result": result})
            elif method == "tools/call":
                self._call(rid, name, body["params"].get("arguments") or {})
            else:
                self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})

        def _call(self, rid, name, args):
            with state.lock:
                state.executions.append((name, args))
                beh = state.behaviour.get(name, "ok")
                count = sum(1 for n, _ in state.executions if n == name)
            if beh == "drop_after_exec" or (beh == "drop_after_exec_once" and count == 1):
                self._drop()
                return
            if beh == "plain404":
                body = b"Not Found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if beh == "http500":
                self._json(500, {"error": "boom"})
                return
            if beh == "html":
                body = b"<html>oops</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._sse_start()
            try:
                if beh.startswith(("slow:", "silent:")):
                    end = time.monotonic() + float(beh.split(":")[1])
                    while time.monotonic() < end:
                        time.sleep(0.2)
                        if beh.startswith("slow:"):
                            self.wfile.write(b": ping - keepalive\r\n\r\n")
                            self.wfile.flush()
                elif beh == "answer_on_cancel":
                    state.cancel_seen.wait(10)
                elif beh == "hang":
                    time.sleep(15)
                    return
                elif beh == "drop_mid_stream":
                    self.wfile.write(b": ping\r\n\r\n")
                    self.wfile.flush()
                    self._drop()
                    return
                elif beh == "stray_then_ok":
                    self._event({"jsonrpc": "2.0", "id": 987654, "result": {"content": [], "isError": False}})
                elif beh == "bad_event_then_ok":
                    self.wfile.write(b"event: message\r\ndata: {not json\r\n\r\n")
                elif beh == "progress_then_ok":
                    self._event({"jsonrpc": "2.0", "method": "notifications/progress",
                                 "params": {"progressToken": "tok", "progress": 1}})
                elif beh == "server_request_then_ok":
                    self._event({"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}})
                elif beh == "list_changed_then_ok":
                    self._event({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                self._event({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"{name} ok"}], "isError": False}})
            except OSError:
                state.stream_closed.set()

    return Handler


def _st_http_server_class():
    import http.server

    class Quiet(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            # The scenarios cut connections on purpose; a reset seen by the
            # fake server is the scenario working, not a finding.
            debug(f"fake server: {sys.exc_info()[0].__name__} from a cut connection")

    return Quiet


_StHTTPServer = _st_http_server_class()


class _StServer:
    def __init__(self, port: int = 0) -> None:
        self.state = _StState()
        self.httpd = _StHTTPServer(("127.0.0.1", port), _st_handler(self.state))
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp/main/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _StBlackhole:
    """Accepts connections and never answers: up locally, dead beyond."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/mcp/main/"
        self.conns: list = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)

    def stop(self) -> None:
        self.sock.close()
        for c in self.conns:
            c.close()


def _st_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _st_wait(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


class _StBridge:
    """The bridge as Claude Code runs it: a child process on stdio."""

    def __init__(self, url: str, tmp: Path, *, headers: str | None = None, cache: Path | None = None,
                 args: tuple = (), headers_path: Path | None = None) -> None:
        import subprocess
        tmp.mkdir(parents=True, exist_ok=True)
        hf = headers_path or tmp / "headers.json"
        if headers_path is None:
            hf.write_text(headers if headers is not None else json.dumps({"Authorization": _ST_KEY}))
            hf.chmod(0o600)
        self.cache = cache or (tmp / "cache.json")
        extra = list(args)
        for k, v in (("--connect-timeout", "1"), ("--init-timeout", "2"), ("--idle-timeout", "1"),
                     ("--call-timeout", "10"), ("--replay-wait", "8")):
            if k not in extra:
                extra += [k, v]
        env = dict(os.environ, HINDSIGHT_MCP_BRIDGE_DEBUG="1")
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--url", url, "--headers-file", str(hf),
             "--cache-file", str(self.cache), *extra, "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.lines: list = []
        self.messages: list = []
        self.stderr: list = []
        self._ids = itertools.count(1000)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line)
            try:
                self.messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # recorded in self.lines; clean() reports it

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.decode("utf-8", "replace"))

    def send(self, obj) -> None:
        self.send_raw(json.dumps(obj).encode() + b"\n")

    def send_raw(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def response(self, rid, timeout: float = 10.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in list(self.messages):
                if m.get("id") == rid and ("result" in m or "error" in m):
                    return m
            time.sleep(0.01)
        raise AssertionError(f"no response for id {rid!r}; bridge log tail:\n{''.join(self.stderr)[-1500:]}")

    def request(self, method: str, params: dict | None = None, rid=None, timeout: float = 10.0) -> dict:
        rid = next(self._ids) if rid is None else rid
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self.response(rid, timeout)

    def call(self, name: str, arguments: dict | None = None, timeout: float = 15.0) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, None, timeout)

    def start_call(self, name: str, arguments: dict | None = None) -> int:
        rid = next(self._ids)
        self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}})
        return rid

    def initialize(self, version: str = "2025-06-18") -> dict:
        r = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                        "clientInfo": {"name": "self-test", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def tool_names(self) -> list:
        return [t["name"] for t in self.request("tools/list", {})["result"]["tools"]]

    def notes(self, method: str) -> list:
        return [m for m in self.messages if m.get("method") == method]

    def close(self) -> None:
        import subprocess
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError as exc:
                debug(f"self-test stdin close: {type(exc).__name__}")
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        time.sleep(0.05)

    def clean(self) -> None:
        for line in self.lines:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                raise AssertionError(f"stdout carried a non-JSON line: {line[:120]!r}") from None
            assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0", f"not JSON-RPC: {line[:120]!r}"
            assert line.endswith(b"\n") and line.count(b"\n") == 1, "a message spans lines"
        err = "".join(self.stderr)
        assert "st-key-7f3a" not in err, "the key reached the log"
        assert _ST_MARKER not in err, "a payload reached the log"
        assert "Traceback" not in err, f"traceback in the log:\n{err[-1500:]}"


class _StHarness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bridges: list = []
        self.servers: list = []
        self.holes: list = []

    def server(self, port: int = 0) -> _StServer:
        s = _StServer(port)
        self.servers.append(s)
        return s

    def blackhole(self) -> _StBlackhole:
        h = _StBlackhole()
        self.holes.append(h)
        return h

    def bridge(self, url: str, sub: str = "b", **kw) -> _StBridge:
        b = _StBridge(url, self.tmp / sub, **kw)
        self.bridges.append(b)
        return b

    def finish(self) -> None:
        for b in self.bridges:
            b.close()
        for s in self.servers:
            s.stop()
        for h in self.holes:
            h.stop()
        for b in self.bridges:
            b.clean()


def _st_connected(b: _StBridge, n: int = len(_ST_TOOLS), timeout: float = 10.0) -> None:
    assert _st_wait(lambda: len(b.tool_names()) == n, timeout), f"catalog never reached {n}: {b.tool_names()}"
    # The catalog is served before its fsync'd save returns, and list_changed follows the save;
    # the "tools live" line is logged between the two, so wait for it before reading either.
    assert _st_wait(lambda: any(f"catalog: {n} tools live" in line for line in b.stderr), timeout), \
        "catalog never saved"


def _st_ready(h: _StHarness, **kw) -> tuple[_StServer, _StBridge]:
    srv = h.server()
    b = h.bridge(srv.url, **kw)
    b.initialize()
    _st_connected(b)
    return srv, b


_ST_SCENARIOS: list = []


def _scenario(name: str, *cases):
    """Register fn once, or once per case (the case is passed as the second argument)."""
    def deco(fn):
        if cases:
            for case in cases:
                _ST_SCENARIOS.append((f"{name}[{case[0] if isinstance(case, tuple) else case}]",
                                      lambda h, c=case, f=fn: f(h, c)))
        else:
            _ST_SCENARIOS.append((name, fn))
        return fn
    return deco


@_scenario("initialize is the bridge's own; versions negotiated and pinned upstream")
def _st_initialize(h):
    srv = h.server()
    b = h.bridge(srv.url)
    r = b.initialize("2025-06-18")["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert r["capabilities"] == {"tools": {"listChanged": True}}
    assert r["serverInfo"]["name"] == BRIDGE_NAME
    _st_connected(b)
    assert srv.state.init_versions == ["2025-06-18"], srv.state.init_versions
    later = [x for x in srv.state.headers_seen if x.get("mcp-session-id")]
    assert later and all(x.get("mcp-protocol-version") == "2025-06-18" for x in later)
    assert all("text/event-stream" in x["accept"] and x["content-type"] == "application/json"
               for x in srv.state.headers_seen)
    b2 = h.bridge(srv.url, sub="b2")
    assert b2.initialize("2099-01-01")["result"]["protocolVersion"] == SUPPORTED_VERSIONS[0]


@_scenario("refused at start: calls fail fast as not executed, then tools appear by themselves")
def _st_refused(h):
    port = _st_free_port()
    b = h.bridge(f"http://127.0.0.1:{port}/mcp/main/")
    b.initialize()
    assert b.tool_names() == []
    t0 = time.monotonic()
    r = b.call("fast_tool")
    assert r["error"]["code"] == ERR_NOT_EXECUTED and "safe to retry" in r["error"]["message"], r
    assert time.monotonic() - t0 < 3
    srv = h.server(port)
    assert _st_wait(lambda: b.notes("notifications/tools/list_changed"), 15), "no list_changed after recovery"
    assert sorted(b.tool_names()) == sorted(t["name"] for t in _ST_TOOLS)
    assert b.call("fast_tool")["result"]["content"][0]["text"] == "fast_tool ok"
    assert srv.state.executions == [("fast_tool", {})]


@_scenario("blackholed link: calls fail as not executed within the handshake bound, ping stays live")
def _st_blackhole(h):
    hole = h.blackhole()
    b = h.bridge(hole.url)
    b.initialize()
    rid = b.start_call("fast_tool")
    t0 = time.monotonic()
    assert "result" in b.request("ping", timeout=1)
    assert time.monotonic() - t0 < 1
    assert b.response(rid, timeout=15)["error"]["code"] == ERR_NOT_EXECUTED


@_scenario("stateless server: no session header is ever sent")
def _st_stateless(h):
    srv = h.server()
    srv.state.stateless = True
    b = h.bridge(srv.url)
    b.initialize()
    _st_connected(b)
    assert b.call("fast_tool")["result"]["isError"] is False
    assert not any(x.get("mcp-session-id") for x in srv.state.headers_seen)


@_scenario("drop after the server executed",
           ("retain-no-doc", "retain", {"content": _ST_MARKER}, 1, "unknown"),
           ("update_memory", "update_memory", {"memory_id": "m1"}, 1, "unknown"),
           ("retain-doc-append", "retain", {"content": _ST_MARKER, "document_id": "d1", "update_mode": "append"}, 1, "unknown"),
           ("recall", "recall", {"query": _ST_MARKER}, 1, "reads"),
           ("retain-doc", "retain", {"content": _ST_MARKER, "document_id": "d1"}, 2, "ok"),
           ("sync_retain-doc-replace", "sync_retain", {"content": _ST_MARKER, "document_id": "d2", "update_mode": "replace"}, 2, "ok"))
def _st_drop(h, case):
    _label, name, args, runs, expect = case
    srv = h.server()
    srv.state.behaviour[name] = "drop_after_exec_once"
    b = h.bridge(srv.url)
    b.initialize()
    _st_connected(b)
    r = b.call(name, args)
    count = lambda: len([e for e in srv.state.executions if e[0] == name])  # noqa: E731
    assert count() == runs, f"{count()} executions, expected {runs}"
    if expect == "ok":
        assert r["result"]["content"][0]["text"] == f"{name} ok", r
    elif expect == "reads":
        assert r["error"]["code"] == ERR_OUTCOME_UNKNOWN and "only reads" in r["error"]["message"], r
    else:
        assert r["error"]["code"] == ERR_OUTCOME_UNKNOWN and "may have been executed" in r["error"]["message"], r
    time.sleep(0.5)
    assert count() == runs


@_scenario("other ambiguous failures never replay a write", "http500", "drop_mid_stream", "html")
def _st_ambiguous(h, beh):
    srv = h.server()
    srv.state.behaviour["update_memory"] = beh
    b = h.bridge(srv.url)
    b.initialize()
    _st_connected(b)
    r = b.call("update_memory", {"memory_id": "m1"})
    assert r["error"]["code"] == ERR_OUTCOME_UNKNOWN, r
    time.sleep(0.5)
    assert len(srv.state.executions) == 1


@_scenario("404 storm: six concurrent calls, one re-initialization, each executed once")
def _st_storm(h):
    srv, b = _st_ready(h)
    assert srv.state.init_count == 1
    srv.state.restart()
    rids = [b.start_call("fast_tool") for _ in range(6)]
    for rid in rids:
        assert b.response(rid)["result"]["isError"] is False
    assert srv.state.init_count == 2, srv.state.init_count
    assert len(srv.state.executions) == 6


@_scenario("flap during re-init: backs off, no spin, recovers")
def _st_flap(h):
    srv, b = _st_ready(h)
    srv.state.fail_init = 3
    srv.state.restart()
    assert b.call("fast_tool")["error"]["code"] == ERR_NOT_EXECUTED
    time.sleep(1.2)
    assert srv.state.init_count <= 4, f"{srv.state.init_count} initialize attempts in about a second"
    assert _st_wait(lambda: srv.state.init_count >= 5, 20)
    assert _st_wait(lambda: "result" in b.call("fast_tool"), 10)


@_scenario("a long call blocks neither ping nor other calls")
def _st_concurrency(h):
    srv, b = _st_ready(h)
    srv.state.behaviour["slow_tool"] = "slow:2"
    slow = b.start_call("slow_tool")
    time.sleep(0.2)
    t0 = time.monotonic()
    assert "result" in b.request("ping", timeout=1)
    fast = b.call("fast_tool", timeout=2)
    assert "result" in fast and time.monotonic() - t0 < 1.5
    assert "result" in b.response(slow, timeout=10)
    order = [m.get("id") for m in b.messages if m.get("id") in (slow, fast["id"])]
    assert order == [fast["id"], slow], order


@_scenario("cancellation is forwarded with the upstream id, the stream is cut, no answer follows")
def _st_cancel(h):
    srv, b = _st_ready(h)
    srv.state.behaviour["slow_tool"] = "slow:4"
    rid = b.start_call("slow_tool")
    assert _st_wait(lambda: srv.state.executions, 5)
    b.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rid, "reason": "user"}})
    assert _st_wait(lambda: srv.state.cancels, 5), "cancel never reached the server"
    up = srv.state.cancels[0]
    assert isinstance(up["requestId"], int) and up["requestId"] != rid and up["reason"] == "user", up
    assert _st_wait(srv.state.stream_closed.is_set, 5), "the upstream stream was not cut"
    time.sleep(0.5)
    assert not [m for m in b.messages if m.get("id") == rid]
    assert "result" in b.call("fast_tool")


@_scenario("an answer that overtakes a cancel is not delivered")
def _st_cancel_race(h):
    srv, b = _st_ready(h)
    srv.state.behaviour["slow_tool"] = "answer_on_cancel"
    rid = b.start_call("slow_tool")
    assert _st_wait(lambda: srv.state.executions, 5)
    b.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rid}})
    assert _st_wait(lambda: srv.state.cancels, 5)
    time.sleep(1.0)
    assert not [m for m in b.messages if m.get("id") == rid], "an answer followed the cancel"
    assert "result" in b.call("fast_tool")


@_scenario("a 404 without a JSON-RPC body is a wrong path, not a lost session: no re-init, no replay")
def _st_plain404(h):
    srv, b = _st_ready(h)
    srv.state.behaviour["update_memory"] = "plain404"
    r = b.call("update_memory", {"memory_id": "m1"})
    assert r["error"]["code"] == ERR_NOT_EXECUTED and "not found" in r["error"]["message"], r
    assert srv.state.init_count == 1, srv.state.init_count
    assert len(srv.state.executions) == 1


@_scenario("late answer after the deadline is dropped: one terminal answer per request")
def _st_late(h):
    srv, b = _st_ready(h, args=("--call-timeout", "1"))
    srv.state.behaviour["slow_tool"] = "silent:2.5"
    r = b.call("slow_tool")
    assert r["error"]["code"] == ERR_OUTCOME_UNKNOWN, r
    time.sleep(3)
    assert len([m for m in b.messages if m.get("id") == r["id"]]) == 1


@_scenario("a stray response for a foreign id and a malformed event are skipped")
def _st_stray(h):
    srv, b = _st_ready(h)
    srv.state.behaviour.update(fast_tool="stray_then_ok", recall="bad_event_then_ok")
    assert b.call("fast_tool")["result"]["content"][0]["text"] == "fast_tool ok"
    assert b.call("recall")["result"]["content"][0]["text"] == "recall ok"
    assert not [m for m in b.messages if m.get("id") == 987654]


@_scenario("keepalives arm the idle limit: a quiet link then fails fast, a pinging long call survives")
def _st_idle(h):
    srv, b = _st_ready(h, args=("--idle-timeout", "1", "--call-timeout", "20"))
    srv.state.behaviour.update(slow_tool="slow:2", fast_tool="hang")
    assert "result" in b.call("slow_tool")
    t0 = time.monotonic()
    r = b.call("fast_tool", timeout=20)
    assert r["error"]["code"] == ERR_OUTCOME_UNKNOWN and time.monotonic() - t0 < 5, r


@_scenario("progress is relayed; a server request on the stream is answered 'not supported'")
def _st_progress(h):
    srv, b = _st_ready(h)
    srv.state.behaviour.update(fast_tool="progress_then_ok", recall="server_request_then_ok")
    assert "result" in b.call("fast_tool")
    assert b.notes("notifications/progress")[0]["params"]["progressToken"] == "tok"
    assert "result" in b.call("recall")
    assert _st_wait(lambda: srv.state.client_responses, 5)
    got = srv.state.client_responses[0]
    assert got["id"] == "srv-1" and got["error"]["code"] == ERR_METHOD_NOT_FOUND, got


@_scenario("catalog: pagination, reorder without notice, change with notice, removed tool refused")
def _st_catalog(h):
    srv = h.server()
    srv.state.pages = [_ST_TOOLS[:3], _ST_TOOLS[3:]]
    b = h.bridge(srv.url)
    b.initialize()
    _st_connected(b)
    listed = b.request("tools/list", {})["result"]
    assert "nextCursor" not in listed and len(listed["tools"]) == len(_ST_TOOLS)
    assert b.request("tools/list", {"cursor": "1"})["error"]["code"] == ERR_INVALID_PARAMS
    before = len(b.notes("notifications/tools/list_changed"))
    srv.state.pages = [list(reversed(_ST_TOOLS))]
    srv.state.restart()
    assert "result" in b.call("fast_tool")
    time.sleep(0.5)
    assert len(b.notes("notifications/tools/list_changed")) == before, "a reorder was announced as a change"
    srv.state.pages = [_ST_TOOLS + [_st_tool("new_tool")]]
    srv.state.behaviour["fast_tool"] = "list_changed_then_ok"
    assert "result" in b.call("fast_tool")
    assert _st_wait(lambda: len(b.notes("notifications/tools/list_changed")) == before + 1, 10)
    assert "new_tool" in b.tool_names()
    srv.state.pages = [_ST_TOOLS[1:] + [_st_tool("new_tool")]]
    srv.state.restart()
    b.call("fast_tool")
    assert _st_wait(lambda: "recall" not in b.tool_names(), 10)
    r = b.call("recall")
    assert r["error"]["code"] == ERR_INVALID_PARAMS and "no longer offers" in r["error"]["message"], r


@_scenario("three bridges share one cache; it is atomic, keyless, 0600, served when down, per endpoint")
def _st_cache_shared(h):
    srv = h.server()
    cache = h.tmp / "shared" / "cache.json"
    procs = [h.bridge(srv.url, sub=f"p{i}", cache=cache) for i in range(3)]
    for p in procs:
        p.initialize()
    for p in procs:
        _st_connected(p)
    text = cache.read_text()
    data = json.loads(text)
    assert data["endpoint"] == srv.url and len(data["tools"]) == len(_ST_TOOLS)
    assert "st-key" not in text
    assert cache.stat().st_mode & 0o777 == 0o600, oct(cache.stat().st_mode)
    assert not [f for f in cache.parent.iterdir() if f.name.endswith(".tmp")]
    url = srv.url
    srv.stop()
    h.servers.remove(srv)
    cold = h.bridge(url, sub="cold", cache=cache)
    cold.initialize()
    assert sorted(cold.tool_names()) == sorted(t["name"] for t in _ST_TOOLS)
    assert cold.call("fast_tool")["error"]["code"] == ERR_NOT_EXECUTED
    other = h.bridge(f"http://127.0.0.1:{_st_free_port()}/mcp/other/", sub="other", cache=cache)
    other.initialize()
    assert other.tool_names() == []


@_scenario("corrupt or foreign cache: empty catalog, still initialized, repaired on connect",
           "truncated", "list", "wrong-shape", "empty")
def _st_cache_corrupt(h, kind):
    content = {"truncated": "{not json", "list": "[]", "empty": "",
               "wrong-shape": json.dumps({"endpoint": "x", "tools": "no"})}[kind]
    srv = h.server()
    cache = h.tmp / "c.json"
    cache.write_text(content)
    b = h.bridge(srv.url, cache=cache)
    assert b.initialize()["result"]["serverInfo"]["name"] == BRIDGE_NAME
    _st_connected(b)
    assert len(json.loads(cache.read_text())["tools"]) == len(_ST_TOOLS)


@_scenario("401/403: clear message, no reconnect storm", 401, 403)
def _st_auth(h, status):
    srv = h.server()
    srv.state.status_override["initialize"] = status
    b = h.bridge(srv.url)
    b.initialize()
    r = b.call("fast_tool")
    assert r["error"]["code"] == ERR_NOT_EXECUTED and "credentials" in r["error"]["message"], r
    n = len(srv.state.headers_seen)
    time.sleep(3)
    assert len(srv.state.headers_seen) - n <= 6, f"{len(srv.state.headers_seen) - n} attempts in 3 s"


@_scenario("malformed header file: not executed, nothing sent", "not-json", "list", "crlf", "empty")
def _st_headers(h, kind):
    content = {"not-json": "{not json", "list": "[]", "empty": "",
               "crlf": json.dumps({"Authorization": "Bearer a\r\nX-Evil: 1"})}[kind]
    srv = h.server()
    b = h.bridge(srv.url, headers=content)
    b.initialize()
    r = b.call("fast_tool")
    assert r["error"]["code"] == ERR_NOT_EXECUTED and "header file" in r["error"]["message"], r
    assert not srv.state.headers_seen


@_scenario("a redirect is refused and never followed with the key")
def _st_redirect(h):
    srv, b = _st_ready(h)
    srv.state.status_override["fast_tool"] = 307
    r = b.call("fast_tool")
    assert r["error"]["code"] == ERR_NOT_EXECUTED and "redirect" in r["error"]["message"], r
    assert srv.state.elsewhere_hits == 0


@_scenario("malformed input never breaks the protocol")
def _st_malformed(h):
    srv, b = _st_ready(h)
    for raw in (b"garbage\n", b"[1,2]\n", b"\"str\"\n", b"\xff\xfe\n"):
        b.send_raw(raw)
    b.send({"jsonrpc": "2.0", "id": 5, "result": {}})
    b.send({"jsonrpc": "2.0", "method": "notifications/unknown"})
    assert b.request("prompts/list", {})["error"]["code"] == ERR_METHOD_NOT_FOUND
    assert b.request("tools/call", {"arguments": {}})["error"]["code"] == ERR_INVALID_PARAMS
    srv.state.behaviour["slow_tool"] = "slow:1"
    rid = b.start_call("slow_tool")
    b.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": "fast_tool"}})
    assert _st_wait(lambda: len([m for m in b.messages if m.get("id") == rid]) == 2, 10)
    answers = [m for m in b.messages if m.get("id") == rid]
    assert sorted("result" in m for m in answers) == [False, True], answers
    errors = [m["error"]["code"] for m in b.messages if m.get("id") is None and "error" in m]
    assert errors == [ERR_PARSE, ERR_INVALID_REQUEST, ERR_INVALID_REQUEST, ERR_PARSE], errors
    assert "result" in b.request("ping")


@_scenario("exit is clean and closes the session", "stdin-eof", "sigterm", "sigterm-mid-call")
def _st_exit(h, how):
    srv, b = _st_ready(h)
    srv.state.behaviour["slow_tool"] = "slow:20"
    if how == "sigterm-mid-call":
        b.start_call("slow_tool")
        assert _st_wait(lambda: srv.state.executions, 5)
    t0 = time.monotonic()
    if how == "stdin-eof":
        b.proc.stdin.close()
    else:
        b.proc.send_signal(signal.SIGTERM)
    assert b.proc.wait(8) == 0
    assert time.monotonic() - t0 < 6
    assert _st_wait(lambda: srv.state.deletes == 1, 3), f"{srv.state.deletes} DELETEs"


@_scenario("with debug logging on, neither the key nor a payload reaches stderr")
def _st_nolog(h):
    srv, b = _st_ready(h)
    srv.state.behaviour["retain"] = "drop_after_exec"
    b.call("retain", {"content": _ST_MARKER})
    b.call("recall", {"query": _ST_MARKER})
    # the check itself runs in clean(), over every bridge of every scenario


def _st_live(url: str, headers_file: str) -> None:
    """READ-ONLY against the real endpoint: initialize, tools/list, one recall."""
    import tempfile
    from scripts.utils.data_path import data_write_dir
    with tempfile.TemporaryDirectory(prefix="hindsight-mcp-bridge-live-",
                                     dir=data_write_dir("runtime:scratch/tmp")) as tmp:
        b = _StBridge(url, Path(tmp), headers_path=Path(headers_file).expanduser(),
                      args=("--init-timeout", "20", "--connect-timeout", "5", "--call-timeout", "120"))
        try:
            assert b.initialize()["result"]["serverInfo"]["name"] == BRIDGE_NAME
            assert _st_wait(lambda: b.notes("notifications/tools/list_changed"), 60), "never connected"
            names = b.tool_names()
            r = b.call("recall", {"query": "hindsight mcp bridge", "max_tokens": 500}, timeout=120)
            assert "result" in r and r["result"].get("isError") is not True, r.get("error")
            print(json.dumps({"live_tools": len(names), "recall": "ok"}))
        finally:
            b.close()
        b.stderr = [line for line in b.stderr if "Traceback" in line]
        for line in b.lines:
            json.loads(line)


def cmd_self_test(args) -> int:
    import tempfile
    import traceback
    from scripts.utils.data_path import data_write_dir
    chosen = [(n, f) for n, f in _ST_SCENARIOS if not args.only or args.only in n]
    if not chosen:
        print("no scenario matches", file=sys.stderr)
        return 2
    results: dict = {}

    def run(item):
        name, fn = item
        with tempfile.TemporaryDirectory(prefix="hindsight-mcp-bridge-st-",
                                         dir=data_write_dir("runtime:scratch/tmp")) as tmp:
            h = _StHarness(Path(tmp))
            started = time.monotonic()
            try:
                try:
                    fn(h)
                finally:
                    h.finish()
                results[name] = (True, "", time.monotonic() - started)
            except AssertionError as exc:
                results[name] = (False, str(exc) or traceback.format_exc(limit=3), time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - a crashed scenario is a failed scenario, reported
                results[name] = (False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}",
                                 time.monotonic() - started)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(run, chosen))
    failed = 0
    for name, _fn in chosen:
        ok, why, secs = results[name]
        print(f"{'PASS' if ok else 'FAIL'} {secs:5.1f}s  {name}")
        if not ok:
            failed += 1
            print("      " + why.strip().replace("\n", "\n      "))
    print(f"{len(chosen) - failed} passed, {failed} failed, {len(chosen)} scenarios")
    if args.live:
        try:
            _st_live(args.url, args.headers_file)
            print("PASS live read-only check")
        except (AssertionError, OSError, KeyError) as exc:
            print(f"FAIL live read-only check: {type(exc).__name__}: {exc}")
            failed += 1
    return 1 if failed else 0


# ---------------------------------------------------------------- entry points

def _settings(args) -> tuple[Endpoint, str, Path | None]:
    endpoint = Endpoint(args.url)
    cache = Path(args.cache_file).expanduser() if args.cache_file else default_cache_file(endpoint.url)
    return endpoint, args.headers_file, cache


def cmd_serve(args) -> int:
    # Keep the real stdout for protocol only; everything else written to fd 1 goes to stderr.
    out_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    out = Output(os.fdopen(out_fd, "wb"))
    try:
        endpoint, headers_file, cache = _settings(args)
    except ValueError as exc:
        log(f"cannot start: {exc}")
        return 2
    upstream = Upstream(endpoint, headers_file, connect_s=args.connect_timeout,
                        init_s=args.init_timeout, idle_s=args.idle_timeout)
    catalog = Catalog(cache, endpoint.url)
    catalog.load()
    bridge = Bridge(upstream, catalog, out, call_s=args.call_timeout, replay_wait_s=args.replay_wait,
                    workers=args.workers, max_inflight=args.max_inflight)
    stop = threading.Event()

    def on_signal(signum, _frame):
        log(f"signal {signum}; shutting down")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    def reader():
        stdin = sys.stdin.buffer
        while not stop.is_set():
            try:
                line = stdin.readline()
            except (OSError, ValueError) as exc:
                log(f"stdin failed ({type(exc).__name__}); shutting down")
                break
            if not line:
                log("stdin closed; shutting down")
                break
            try:
                bridge.handle_line(line)
            except Exception as exc:  # noqa: BLE001 - one bad message must not end the session
                log(f"message handling failed: {type(exc).__name__}: {exc}")
        stop.set()

    threading.Thread(target=reader, name="stdin", daemon=True).start()
    log(f"started for {endpoint.url}")
    while not stop.wait(0.5):
        if out.broken.is_set():
            stop.set()
    bridge.shutdown()
    log("stopped")
    sys.stderr.flush()
    os._exit(0)  # worker threads may be blocked on a socket; nothing is left to flush


def cmd_probe(args) -> int:
    """Read-only check of the endpoint: initialize, the whole tool list, close."""
    try:
        endpoint, headers_file, _cache = _settings(args)
    except ValueError as exc:
        print(f"cannot probe: {exc}", file=sys.stderr)
        return 2
    upstream = Upstream(endpoint, headers_file, connect_s=args.connect_timeout,
                        init_s=args.init_timeout, idle_s=args.idle_timeout)
    seen: list = []
    upstream.on_ready = seen.append
    started = time.monotonic()
    try:
        upstream.ensure_ready()
    except UpstreamError as exc:
        print(f"{endpoint.url}: {exc.kind}: {exc.detail}", file=sys.stderr)
        return 1
    tools = seen[0] if seen else []
    print(json.dumps({"endpoint": endpoint.url, "protocol": upstream.version,
                      "mode": "stateful" if upstream.session_id else "stateless",
                      "tools": len(tools), "seconds": round(time.monotonic() - started, 2)}))
    upstream.close_session(5.0)
    return 0


def cmd_cache(args) -> int:
    try:
        endpoint, _h, cache = _settings(args)
    except ValueError as exc:
        print(f"cannot resolve: {exc}", file=sys.stderr)
        return 2
    if cache is None:
        print("no cache address could be resolved", file=sys.stderr)
        return 1
    catalog = Catalog(cache, endpoint.url)
    catalog.load()
    print(json.dumps({"cache_file": str(cache), "exists": cache.exists(), "saved_at": catalog.saved_at,
                      "tools": sorted(catalog.names())}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=BRIDGE_NAME, description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("HINDSIGHT_MCP_BRIDGE_URL", DEFAULT_URL))
    ap.add_argument("--headers-file", default=os.environ.get("HINDSIGHT_MCP_BRIDGE_HEADERS", DEFAULT_HEADERS_FILE))
    ap.add_argument("--cache-file", default=os.environ.get("HINDSIGHT_MCP_BRIDGE_CACHE"))
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--init-timeout", type=float, default=20.0)
    ap.add_argument("--idle-timeout", type=float, default=60.0)
    ap.add_argument("--call-timeout", type=float, default=600.0)
    ap.add_argument("--replay-wait", type=float, default=90.0,
                    help="how long a replayable retain waits for the link to return")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-inflight", type=int, default=64)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("probe").set_defaults(fn=cmd_probe)
    sub.add_parser("cache").set_defaults(fn=cmd_cache)
    st = sub.add_parser("self-test", help="every scenario against a fake loopback server; no network")
    st.add_argument("--only", help="run only scenarios whose name contains this text")
    st.add_argument("--jobs", type=int, default=8)
    st.add_argument("--live", action="store_true",
                    help="also run the READ-ONLY live check (initialize, tools/list, one recall) on --url")
    st.set_defaults(fn=cmd_self_test)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return (getattr(args, "fn", None) or cmd_serve)(args)


if __name__ == "__main__":
    sys.exit(main())
