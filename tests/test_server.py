import gzip
import http.client
import logging
import socket
import threading
import time

import pytest

from helpers import Clock, parse, value
from metrics_exporter.exporter import Exporter
from metrics_exporter.server import ExporterServer, Handler
from metrics_exporter.simulated import FleetCollector


class Running:
    def __init__(self, exporter):
        self.srv = ExporterServer(("127.0.0.1", 0), exporter)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()

    def request(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body


def boom():
    raise RuntimeError("secret")


def server():
    return Running(Exporter({"fleet": FleetCollector(clock=Clock())}))


def test_metrics_endpoint():
    with server() as s:
        r, body = s.request("GET", "/metrics")
    assert r.status == 200 and r.getheader("Content-Type") == "text/plain; version=0.0.4; charset=utf-8"
    assert r.getheader("Cache-Control") == "no-store"
    assert value(parse(body.decode()), "plant_fleet_devices") == 6


def test_gzip_when_asked_and_identical_content():
    with server() as s:
        plain = s.request("GET", "/metrics")[1].decode()
        r, body = s.request("GET", "/metrics", {"Accept-Encoding": "gzip"})
    assert r.getheader("Content-Encoding") == "gzip"
    assert set(parse(gzip.decompress(body).decode())) == set(parse(plain))


def test_head_has_headers_and_no_body():
    with server() as s:
        r, body = s.request("HEAD", "/metrics")
    assert r.status == 200 and body == b"" and int(r.getheader("Content-Length")) > 100


def test_query_strings_are_ignored_for_routing():
    with server() as s:
        assert s.request("GET", "/metrics?debug=1")[0].status == 200
        assert s.request("GET", "/healthz?x=1")[0].status == 200


def test_health_root_and_unknown_paths():
    with server() as s:
        assert s.request("GET", "/healthz")[1] == b"ok\n"
        assert b"/metrics" in s.request("GET", "/")[1]
        for path in ("/nope", "/metrics/", "/metrics.json", "/../etc/passwd", "/%2e%2e/x"):
            assert s.request("GET", path)[0].status == 404, path


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_other_methods_are_405_with_allow(method):
    with server() as s:
        r, _ = s.request(method, "/metrics")
    assert r.status == 405 and r.getheader("Allow") == "GET, HEAD"


def test_server_header_does_not_advertise_the_python_version():
    with server() as s:
        r, _ = s.request("GET", "/healthz")
    assert "Python" not in (r.getheader("Server") or "")


def test_a_broken_collector_still_gives_a_200_scrape():
    with Running(Exporter({"bad": boom, "fleet": FleetCollector(clock=Clock())})) as s:
        r, body = s.request("GET", "/metrics")
    got = parse(body.decode())
    assert r.status == 200 and value(got, "exporter_collector_success", collector="bad") == 0 and "plant_fleet_devices" in got
    assert b"secret" not in body


def test_concurrent_scrapes_are_all_valid():
    errors, counts = [], []
    with server() as s:
        def go():
            try:
                for _ in range(10):
                    counts.append(len(parse(s.request("GET", "/metrics")[1].decode())))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    assert errors == [] and len(counts) == 80 and len(set(counts)) == 1


def test_an_idle_client_is_dropped_instead_of_holding_a_thread_forever():
    old = Handler.timeout
    Handler.timeout = 0.3
    try:
        with server() as s:
            idle = socket.create_connection(("127.0.0.1", s.port), timeout=5)  # connects, then says nothing
            started = time.monotonic()
            assert idle.recv(10) == b""  # the server closed its side
            assert time.monotonic() - started < 3
            idle.close()
            assert s.request("GET", "/healthz")[0].status == 200  # and the server still works
    finally:
        Handler.timeout = old


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_client_disconnects_are_quiet_but_real_errors_are_logged_without_a_traceback():
    log = logging.getLogger("metrics_exporter")
    rec, old_level = _Records(), log.level
    log.addHandler(rec)
    log.setLevel(logging.DEBUG)
    try:
        with server() as s:
            for exc in (BrokenPipeError(), ConnectionResetError(), TimeoutError()):
                try:
                    raise exc
                except OSError:
                    s.srv.handle_error(None, ("10.0.0.9", 1234))
            try:
                raise ValueError("boom")
            except ValueError:
                s.srv.handle_error(None, ("10.0.0.9", 1234))
    finally:
        log.removeHandler(rec)
        log.setLevel(old_level)
    levels = [r.levelno for r in rec.records]
    assert levels == [logging.DEBUG] * 3 + [logging.WARNING]
    assert all(r.exc_info is None for r in rec.records)


def test_head_sends_headers_only_even_on_the_wire():
    """http.client hides a stray body after HEAD, so read the raw bytes."""
    with server() as s:
        sock = socket.create_connection(("127.0.0.1", s.port), timeout=5)
        sock.sendall(b"HEAD /metrics HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        raw = b""
        while chunk := sock.recv(4096):
            raw += chunk
        sock.close()
    head, sep, body = raw.partition(b"\r\n\r\n")
    assert sep and head.startswith(b"HTTP/1.") and b" 200 " in head.split(b"\r\n")[0]
    assert body == b""


def test_the_shipped_idle_timeout_is_finite_and_modest():
    assert Handler.timeout is not None and 0 < Handler.timeout <= 30
