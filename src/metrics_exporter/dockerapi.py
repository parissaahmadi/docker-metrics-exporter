"""Container inventory from the Docker Engine API, over its unix socket, standard library only.

Read-only: the only request made is ``GET /containers/json?all=1``. Note that mounting the socket into
a container, even with ``:ro``, still lets that container use the *whole* API (``:ro`` applies to the
socket file, not to the HTTP requests sent through it). See the README for the safer alternatives.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path

from .expo import Family

KNOWN_STATES = ("created", "running", "paused", "restarting", "removing", "exited", "dead")
MAX_BODY_BYTES = 8 * 1024 * 1024
NAME_LIMIT = 128


class DockerUnavailable(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def list_containers(socket_path: str, timeout: float = 2.0) -> list[dict]:
    if not Path(socket_path).exists():
        raise DockerUnavailable(f"docker socket not found at {socket_path}")
    conn = _UnixConnection(socket_path, timeout)
    try:
        conn.request("GET", "/containers/json?all=1", headers={"Host": "docker", "Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(MAX_BODY_BYTES + 1)
        if resp.status != 200:
            raise DockerUnavailable(f"docker API answered HTTP {resp.status}")
        if len(body) > MAX_BODY_BYTES:
            raise DockerUnavailable("docker API response is larger than the 8 MiB limit")
        data = json.loads(body)
    except (OSError, http.client.HTTPException, ValueError) as e:  # timeout, refused, bad JSON ...
        raise DockerUnavailable(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()
    if not isinstance(data, list):
        raise DockerUnavailable("docker API returned something other than a list")
    return data


def _clean(text: object, limit: int = NAME_LIMIT) -> str:
    return str(text).replace("\n", " ")[:limit]


class DockerCollector:
    def __init__(self, socket_path: str = "/var/run/docker.sock", timeout: float = 2.0, max_containers: int = 500):
        self.socket_path, self.timeout, self.max_containers = socket_path, timeout, max_containers

    def __call__(self) -> list[Family]:
        raw = list_containers(self.socket_path, self.timeout)
        rows = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            names = c.get("Names") or []
            name = _clean(str(names[0]).lstrip("/")) if names else _clean(str(c.get("Id", "?"))[:12])
            state = str(c.get("State", "")).lower()
            rows.append({
                "name": name, "image": _clean(c.get("Image", "")), "id": str(c.get("Id", ""))[:12],
                "state": state if state in KNOWN_STATES else "unknown", "created": c.get("Created"),
            })  # fmt: skip
        rows.sort(key=lambda r: r["name"])
        kept, omitted = rows[: self.max_containers], max(0, len(rows) - self.max_containers)

        by_state = Family("docker_containers", "gauge", "Containers on the host by state.")
        counts = {s: 0 for s in (*KNOWN_STATES, "unknown")}
        for r in rows:
            counts[r["state"]] += 1
        for state, n in counts.items():
            by_state.add(n, state=state)
        info = Family("docker_container_info", "gauge", "Container facts (value is always 1).")
        running = Family("docker_container_running", "gauge", "1 if the container is running.")
        created = Family("docker_container_created_timestamp_seconds", "gauge", "Container creation time, Unix seconds.")
        for r in kept:
            info.add(1, name=r["name"], image=r["image"], container_id=r["id"])
            running.add(1 if r["state"] == "running" else 0, name=r["name"])
            if isinstance(r["created"], int | float) and not isinstance(r["created"], bool):
                created.add(r["created"], name=r["name"])
        omitted_f = Family("docker_containers_omitted", "gauge", "Containers left out of per-container series by the cap.")
        omitted_f.add(omitted)
        return [by_state, info, running, created, omitted_f]
