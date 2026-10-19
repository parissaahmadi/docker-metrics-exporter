"""Mutation check: apply one deliberate bug at a time to a COPY of the project; the suite must fail each time.

Usage:  python mutate8.py        (RUNNER="uv run pytest -q -x" python mutate8.py to choose the test command)
Source, deployment files and tests are all in scope: a dashboard that quietly points at a renamed metric,
or a compose file that quietly publishes a port on every interface, is as much a bug as a wrong number.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
RUNNER = os.environ.get("RUNNER", f"{sys.executable} -m pytest -q -x").split()
TIMEOUT = int(os.environ.get("MUTATION_TIMEOUT", "90"))  # seconds per mutant
COPY = ["src", "tests", "deploy", "pyproject.toml", "compose.yaml", "Dockerfile", ".dockerignore", "README.md"]
S = "src/metrics_exporter/"

# (file relative to the project root, [(old, new), ...], what the bug is)
MUTATIONS = [
    # --- exposition format
    (S + "expo.py", [('.replace("\\\\", "\\\\\\\\").replace(\'"\', \'\\\\"\').replace("\\n", "\\\\n")', '.replace("\\\\", "\\\\\\\\").replace("\\n", "\\\\n")')], "double quotes in label values not escaped"),
    (S + "expo.py", [('return text.replace("\\\\", "\\\\\\\\").replace("\\n", "\\\\n")', "return text")], "HELP text not escaped"),
    (S + "expo.py", [('if self.type == "counter" and not self.name.endswith("_total"):', "if False:")], "counters may lack the _total suffix"),
    (S + "expo.py", [("            if key in seen:\n", "            if False:\n")], "duplicate series allowed"),
    (S + "expo.py", [("        if fam.name in seen_families:", "        if False:")], "duplicate metric families allowed"),
    (S + "expo.py", [('if not LABEL_RE.match(key) or key.startswith("__"):', "if not LABEL_RE.match(key):")], "reserved __ label names allowed"),
    (S + "expo.py", [("for k, v in sorted(labels.items()))", "for k, v in labels.items())")], "label order depends on insertion order"),
    (S + "expo.py", [('return "+Inf" if v > 0 else "-Inf"', "return str(v)")], "infinity rendered as 'inf'"),
    (S + "expo.py", [('return "\\n".join(lines) + "\\n" if lines else ""', 'return "\\n".join(lines) if lines else ""')], "scrape lacks the final newline"),
    (S + "expo.py", [("            if not isinstance(val, str):", "            if False:")], "non-string label values accepted"),
    # --- exporter
    (S + "exporter.py", [("if total > self.max_series:", "if False:")], "series cap per collector removed"),
    (S + "exporter.py", [("            if clash:", "            if False:")], "two collectors may claim one metric name"),
    (S + "exporter.py", [("            except Exception as e:  # one source", "            except KeyError as e:  # one source")], "one failing collector breaks the whole scrape"),
    (S + "exporter.py", [("success.add(1 if ok else 0, collector=name)", "success.add(1, collector=name)")], "failed collectors reported as healthy"),
    # --- server
    (S + "server.py", [('if "gzip" in self.headers.get("Accept-Encoding", "").lower():', "if True:")], "always gzip, even for clients that did not ask"),
    (S + "server.py", [("        if not head:\n            self.wfile.write(body)", "        if True:\n            self.wfile.write(body)")], "HEAD responses carry a body"),
    (S + "server.py", [("    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed\n", "")], "other methods not answered with 405"),
    (S + "server.py", [('path = self.path.split("?", 1)[0]', "path = self.path")], "query strings break routing"),
    (S + "server.py", [("    timeout = 10 ", "    timeout = None ")], "idle clients hold a thread forever"),
    (S + "server.py", [('self.send_header("Cache-Control", "no-store")', "pass")], "responses may be cached"),
    (S + "server.py", [('    sys_version = ""', "    pass")], "Server header advertises the Python version"),
    (S + "server.py", [("if isinstance(exc, BrokenPipeError | ConnectionResetError | TimeoutError):", "if False:")], "routine client disconnects logged as errors"),
    # --- docker collector
    (S + "dockerapi.py", [("        if len(body) > MAX_BODY_BYTES:", "        if False:")], "oversized Docker responses accepted"),
    (S + "dockerapi.py", [("        if resp.status != 200:", "        if False:")], "non-200 Docker answers treated as data"),
    (S + "dockerapi.py", [("kept, omitted = rows[: self.max_containers], max(0, len(rows) - self.max_containers)", "kept, omitted = rows, 0")], "per-container series cap removed"),
    (S + "dockerapi.py", [('"state": state if state in KNOWN_STATES else "unknown"', '"state": state')], "arbitrary container states become label values"),
    (S + "dockerapi.py", [('str(text).replace("\\n", " ")[:limit]', "str(text)")], "container names/images not sanitised or truncated"),
    (S + "dockerapi.py", [('conn.request("GET", "/containers/json?all=1"', 'conn.request("POST", "/containers/json?all=1"')], "uses a write method on the Docker API"),
    # --- simulator
    (S + "simulated.py", [("dt = max(0.0, now - prev_t)", "dt = now - prev_t")], "hour meter can go backwards"),
    (S + "simulated.py", [("                    if was_running:", "                    if True:")], "hour meter counts stopped time"),
    (S + "simulated.py", [('alarm.add(1 if shown > VIBRATION_ALARM_MM_S else 0', 'alarm.add(1 if shown >= VIBRATION_ALARM_MM_S else 0')], "alarm threshold off by one (>= instead of >)"),
    # --- CLI
    (S + "cli.py", [('    s.add_argument("--bind", default="127.0.0.1"', '    s.add_argument("--bind", default="0.0.0.0"')], "server listens on every interface by default"),
    (S + "cli.py", [("    if not collectors:\n        raise SystemExit", "    if False:\n        raise SystemExit")], "an exporter with nothing to export starts anyway"),
    # --- deployment files
    ("deploy/prometheus/rules.yml", [("plant_device_vibration_alarm == 1", "plant_device_vibration_alarms == 1")], "alert on a metric name the exporter does not produce"),
    ("deploy/prometheus/rules.yml", [("{{ $labels.collector }}", "{{ $labels.colector }}")], "alert annotation uses a label the series lacks"),
    ("deploy/prometheus/prometheus.yml", [('targets: ["exporter:9877"]', 'targets: ["exporter:9878"]')], "Prometheus scrapes the wrong port"),
    ("deploy/grafana/dashboards/plant.json", [('"expr": "plant_device_temperature_celsius"', '"expr": "plant_device_temp_celsius"')], "dashboard panel points at a renamed metric"),
    ("compose.yaml", [('ports: ["127.0.0.1:9090:9090"]', 'ports: ["9090:9090"]')], "Prometheus published on every interface"),
    ("compose.yaml", [("    read_only: true\n", "", )], "exporter container filesystem writable"),
    ("compose.yaml", [("image: prom/prometheus:v3.5.0", "image: prom/prometheus:latest")], "unpinned Prometheus image"),
    ("compose.yaml", [("GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD}", "GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}")], "Grafana admin password silently defaults to 'admin'"),
    ("compose.yaml", [('    expose: ["9877"]\n    healthcheck:', '    expose: ["9877"]\n    volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]\n    healthcheck:')], "Docker socket mounted into the default exporter"),
    ("Dockerfile", [("USER 10001\n", "USER root\n")], "image runs as root"),
]


def run(tree: Path) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run(RUNNER, cwd=tree, env=env, capture_output=True, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        return 124  # a hang is a failure too: a mutated server that never answers is caught


def main() -> int:
    survived = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (rel, edits, why) in enumerate(MUTATIONS, 1):
            tree = Path(tmp) / f"m{i}"
            for name in COPY:
                src = ROOT / name
                if src.is_dir():
                    shutil.copytree(src, tree / name)
                else:
                    tree.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, tree / name)
            target = tree / rel
            text = target.read_text()
            for old, new in edits:
                if old not in text:
                    print(f"{i:2} SETUP ERROR  pattern not found in {rel}: {old[:60]!r}")
                    return 2
                text = text.replace(old, new, 1)
            target.write_text(text)
            killed = run(tree) != 0
            print(f"{i:2} {'killed  ' if killed else 'SURVIVED'} {Path(rel).name:16} {why}")
            if not killed:
                survived.append(why)
    print(f"\n{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
