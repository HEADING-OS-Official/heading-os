"""A loopback JSON HTTP server for the offline --self-test scenarios of the Hindsight scripts.

    with serve(answer) as api:      # api = "http://127.0.0.1:<port>"
        ...

`answer(method, path, query, body)` returns `(status, json_object)`: `path` is the URL path,
`query` the parsed query string (`urllib.parse.parse_qs`), `body` the decoded JSON request body
or None. Each script keeps its own invented bank; only this wrapper is shared. It binds
127.0.0.1 on a free port, serves GET and POST, logs nothing, and shuts down on exit.
"""
from __future__ import annotations

import contextlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator

Answer = Callable[[str, str, dict, object], tuple[int, object]]


@contextlib.contextmanager
def serve(answer: Answer) -> Iterator[str]:
    class H(BaseHTTPRequestHandler):
        def _do(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            body = json.loads(raw) if raw else None
            u = urllib.parse.urlsplit(self.path)
            code, out = answer(method, u.path, urllib.parse.parse_qs(u.query), body)
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._do("GET")

        def do_POST(self):  # noqa: N802
            self._do("POST")

        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
