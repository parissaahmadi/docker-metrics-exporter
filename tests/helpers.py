"""Test helpers: a strict exposition parser, a fake Docker Engine API on a unix socket, a pinned clock."""

import json
import re
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)$")
LABEL_PAIR_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"(?:,|$)')


def unescape(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\":
            out.append({"\\": "\\", '"': '"', "n": "\n"}[v[i + 1]])
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def parse(text: str) -> dict:
    """Parse exposition text strictly. Returns {family: {"type", "help", "samples": {labels_tuple: value}}}.

    Fails on anything a Prometheus server would reject: samples before TYPE, a repeated TYPE, a
    duplicate series, a missing final newline, an unparseable line or value.
    """
    assert text == "" or text.endswith("\n"), "scrape must end with a newline"
    families: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name, _, help_ = line[7:].partition(" ")
            families.setdefault(name, {"type": None, "help": None, "samples": {}})["help"] = help_
        elif line.startswith("# TYPE "):
            name, _, typ = line[7:].partition(" ")
            fam = families.setdefault(name, {"type": None, "help": None, "samples": {}})
            assert fam["type"] is None, f"second TYPE line for {name}"
            fam["type"], current = typ, name
        elif line.startswith("#") or not line:
            raise AssertionError(f"unexpected line: {line!r}")
        else:
            m = SAMPLE_RE.match(line)
            assert m, f"cannot parse sample line: {line!r}"
            name, _, body, value = m.groups()
            assert name == current, f"sample {name} outside its family block (current {current})"
            labels, pos = [], 0
            body = body or ""
            while pos < len(body):
                lm = LABEL_PAIR_RE.match(body, pos)
                assert lm, f"bad label syntax in {line!r}"
                labels.append((lm.group(1), unescape(lm.group(2))))
                pos = lm.end()
            key = tuple(sorted(labels))
            fam = families[name]
            assert key not in fam["samples"], f"duplicate series {name}{key}"
            fam["samples"][key] = float(value.replace("+Inf", "inf").replace("NaN", "nan"))
    return families


def value(fams: dict, family: str, /, **labels) -> float:
    """Positional-only so a label called ``name`` (docker_container_*) cannot collide with the parameter."""
    return fams[family]["samples"][tuple(sorted(labels.items()))]


class Clock:
    def __init__(self, t: float = 1_760_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------------------- fake Docker
class FakeDocker:
    """Answers GET /containers/json on a unix socket. ``status``, ``body`` and ``delay`` are adjustable."""

    def __init__(self, containers=None):
        self.dir = tempfile.mkdtemp(dir="/tmp")  # short path: unix socket paths are limited to ~100 bytes
        self.path = str(Path(self.dir) / "d.sock")
        self.containers, self.status, self.raw_body, self.delay = containers or [], 200, None, 0.0
        self.requests: list[tuple[str, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(("GET", self.path))
                if outer.delay:
                    time.sleep(outer.delay)
                body = outer.raw_body if outer.raw_body is not None else json.dumps(outer.containers).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                outer.requests.append(("POST", self.path))
                self.send_response(405)
                self.end_headers()

            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def container(name, state="running", image="nimbus/web:1.2", cid=None, created=1_759_000_000):
    return {"Id": cid or (name * 20)[:64].ljust(64, "0"), "Names": [f"/{name}"], "Image": image,
            "State": state, "Created": created}  # fmt: skip
