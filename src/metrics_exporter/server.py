"""A tiny HTTP server: ``/metrics``, ``/healthz``, and nothing else."""

from __future__ import annotations

import gzip
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .exporter import Exporter
from .expo import CONTENT_TYPE

log = logging.getLogger("metrics_exporter")


class Handler(BaseHTTPRequestHandler):
    server_version = "metrics-exporter"
    sys_version = ""  # do not advertise the Python version
    timeout = 10  # seconds a client may stay silent before the connection is dropped (slow-client guard)

    def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8", head: bool = False, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _route(self, head: bool):
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            body = self.server.exporter.scrape().encode("utf-8")  # type: ignore[attr-defined]
            extra = {}
            if "gzip" in self.headers.get("Accept-Encoding", "").lower():
                body, extra = gzip.compress(body, mtime=0), {"Content-Encoding": "gzip"}
            self._send(200, body, CONTENT_TYPE, head, extra)
        elif path == "/healthz":
            self._send(200, b"ok\n", head=head)
        elif path == "/":
            self._send(200, b"metrics-exporter: see /metrics\n", head=head)
        else:
            self._send(404, b"not found\n", head=head)

    def do_GET(self):
        self._route(head=False)

    def do_HEAD(self):
        self._route(head=True)

    def _not_allowed(self):
        self._send(405, b"method not allowed\n", extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


class ExporterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, exporter: Exporter):
        super().__init__(address, Handler)
        self.exporter = exporter

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, BrokenPipeError | ConnectionResetError | TimeoutError):
            log.debug("client %s went away: %s", client_address[0], type(exc).__name__)  # routine, not an error
        else:
            log.warning("unexpected error serving %s: %r", client_address[0], exc)  # one line, no traceback to stderr
