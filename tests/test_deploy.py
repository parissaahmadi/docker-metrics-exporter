"""The deployment files must agree with the code: every metric, label, port and path they mention has to exist.

None of this needs Docker. It is what keeps a dashboard from silently showing "No data" after a rename.
"""

import json
import re
from pathlib import Path

import yaml

from helpers import FakeDocker, container, parse
from metrics_exporter.dockerapi import DockerCollector
from metrics_exporter.exporter import Exporter
from metrics_exporter.simulated import FleetCollector

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
METRIC_TOKEN = re.compile(r"\b((?:plant|docker|exporter)_[a-z0-9_]+|site:[a-z0-9_:]+)")
SELECTOR = re.compile(r"([a-zA-Z_:][\w:]*)\{([^}]*)\}")
MATCHER_KEY = re.compile(r'([a-zA-Z_]\w*)\s*(?:=~|!~|!=|=)')


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def emitted() -> dict[str, set[str]]:
    """{metric family -> label names it really carries}, from a real scrape of both collectors."""
    with FakeDocker([container("web"), container("db", state="exited")]) as fake:
        exp = Exporter({"fleet": FleetCollector(seed=1), "docker": DockerCollector(fake.path)})
        fams = parse(exp.scrape())
    out = {name: {k for labels in f["samples"] for k, _ in labels} for name, f in fams.items()}
    out["up"] = {"job", "instance"}  # added by Prometheus itself
    return out


def recorded() -> dict[str, set[str]]:
    rules = load_yaml(DEPLOY / "prometheus" / "rules.yml")
    return {r["record"]: {"site"} for g in rules["groups"] for r in g["rules"] if "record" in r}


def dashboard() -> dict:
    return json.loads((DEPLOY / "grafana" / "dashboards" / "plant.json").read_text(encoding="utf-8"))


def all_exprs() -> list[tuple[str, str]]:
    exprs = [(f"dashboard panel {p['title']!r}", t["expr"]) for p in dashboard()["panels"] for t in p.get("targets", [])]
    rules = load_yaml(DEPLOY / "prometheus" / "rules.yml")
    exprs += [(f"rule {r.get('alert') or r.get('record')}", r["expr"]) for g in rules["groups"] for r in g["rules"]]
    return exprs


# ----------------------------------------------------------------- metrics and labels
def test_every_metric_in_dashboard_and_rules_is_really_exported():
    known = set(emitted()) | set(recorded())
    seen = set()
    for where, expr in all_exprs():
        for token in METRIC_TOKEN.findall(expr):
            seen.add(token)
            assert token in known, f"{where} uses {token}, which the exporter does not produce"
    assert {"plant_device_running", "docker_containers", "site:plant_device_running:sum"} <= seen  # the check is not vacuous


def test_label_selectors_only_use_labels_the_metric_has():
    labels = emitted() | recorded()
    checked = 0
    for where, expr in all_exprs():
        for metric, body in SELECTOR.findall(expr):
            for key in MATCHER_KEY.findall(body):
                assert key in labels[metric], f"{where}: {metric} has no label {key!r} (has {sorted(labels[metric])})"
                checked += 1
    assert checked >= 2  # the dashboard and rules use two label matchers today (state=, job=); keep this non-vacuous


def test_alert_annotations_only_use_labels_the_alert_can_have():
    labels = emitted() | recorded()
    rules = load_yaml(DEPLOY / "prometheus" / "rules.yml")
    checked = 0
    for g in rules["groups"]:
        for r in g["rules"]:
            if "alert" not in r:
                continue
            available = set().union(*(labels[m] for m in METRIC_TOKEN.findall(r["expr"]) + re.findall(r"\bup\b", r["expr"])))
            for key in re.findall(r"\$labels\.(\w+)", r["annotations"]["summary"]):
                assert key in available, f"{r['alert']}: annotation uses $labels.{key}, the series has {sorted(available)}"
                checked += 1
    assert checked >= 4


def test_recording_rule_join_labels_exist_on_both_sides():
    labels = emitted()
    assert "device" in labels["plant_device_running"] and {"device", "site"} <= labels["plant_device_info"]


def test_the_alert_threshold_in_rules_matches_the_code():
    from metrics_exporter.simulated import VIBRATION_ALARM_MM_S

    exprs = dict(all_exprs())
    assert "plant_device_vibration_alarm == 1" in exprs["rule DeviceVibrationHigh"]  # the threshold lives in the code, once
    assert VIBRATION_ALARM_MM_S == 7.0


# ----------------------------------------------------------------- dashboard
def test_dashboard_structure_and_datasource_wiring():
    d = dashboard()
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids)) and d["uid"] and d["panels"]
    ds = load_yaml(DEPLOY / "grafana" / "provisioning" / "datasources" / "prometheus.yml")["datasources"][0]
    for p in d["panels"]:
        assert p["datasource"] == {"type": "prometheus", "uid": ds["uid"]}
        for t in p["targets"]:
            assert t["expr"].strip() and t.get("refId")
            assert t["datasource"]["uid"] == ds["uid"] if "datasource" in t else True


def test_grafana_provider_path_matches_the_dashboard_mount():
    prov = load_yaml(DEPLOY / "grafana" / "provisioning" / "dashboards" / "plant.yml")["providers"][0]
    compose = load_yaml(ROOT / "compose.yaml")
    mounts = [v.split(":")[1] for v in compose["services"]["grafana"]["volumes"] if v.startswith("./deploy/grafana/dashboards")]
    assert mounts == [prov["options"]["path"]]
    assert (DEPLOY / "grafana" / "dashboards" / "plant.json").exists()


# ----------------------------------------------------------------- prometheus config
def test_scrape_targets_are_compose_services_on_the_exposed_port():
    compose = load_yaml(ROOT / "compose.yaml")["services"]
    prom = load_yaml(DEPLOY / "prometheus" / "prometheus.yml")
    for job in prom["scrape_configs"]:
        for target in job["static_configs"][0]["targets"]:
            host, port = target.split(":")
            assert host in compose, f"{target}: no compose service called {host}"
            assert port in compose[host]["expose"], f"{target}: service does not expose {port}"


def test_rule_file_is_mounted_where_prometheus_looks_for_it():
    prom = load_yaml(DEPLOY / "prometheus" / "prometheus.yml")
    compose = load_yaml(ROOT / "compose.yaml")["services"]["prometheus"]
    mounted = {v.split(":")[1] for v in compose["volumes"]}
    assert set(prom["rule_files"]) <= mounted


def test_alert_rules_are_well_formed():
    rules = load_yaml(DEPLOY / "prometheus" / "rules.yml")
    alerts = [r for g in rules["groups"] for r in g["rules"] if "alert" in r]
    assert len(alerts) >= 5
    for r in alerts:
        assert r["expr"].strip() and r["for"] and r["labels"]["severity"] in {"warning", "critical"}
        assert r["annotations"]["summary"]
    assert len({r["alert"] for r in alerts}) == len(alerts)


# ----------------------------------------------------------------- compose and Dockerfile
def test_compose_hardening_of_the_exporter_services():
    services = load_yaml(ROOT / "compose.yaml")["services"]
    for name in ("exporter", "exporter-docker"):
        s = services[name]
        assert s["read_only"] is True and s["cap_drop"] == ["ALL"] and "no-new-privileges:true" in s["security_opt"]
        assert str(s["user"]).split(":")[0] not in {"0", "root"}
        assert "ports" not in s, "exporters are reachable only on the compose network"


def test_published_ports_are_loopback_only():
    for name, s in load_yaml(ROOT / "compose.yaml")["services"].items():
        for port in s.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} on all interfaces"


def test_docker_socket_is_mounted_only_in_the_opt_in_profile():
    services = load_yaml(ROOT / "compose.yaml")["services"]
    for name, s in services.items():
        mounts_socket = any("docker.sock" in v for v in s.get("volumes", []))
        assert mounts_socket == (name == "exporter-docker")
    assert services["exporter-docker"]["profiles"] == ["docker"]


def test_images_are_pinned_and_secrets_are_required_not_defaulted():
    services = load_yaml(ROOT / "compose.yaml")["services"]
    for name in ("prometheus", "grafana"):
        image = services[name]["image"]
        assert re.search(r":v?\d+\.\d+", image) and "latest" not in image, image
    assert services["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "admin:admin" not in text and "GF_SECURITY_ADMIN_USER" not in text


def test_dockerfile_runs_as_a_non_root_user_and_copies_files_that_exist():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER\s+10001\s*$", text, re.M)
    compose = load_yaml(ROOT / "compose.yaml")["services"]
    assert str(compose["exporter"]["user"]).startswith("10001")
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for copied in re.findall(r"^COPY\s+(?!--)(.+?)\s+\./?\S*\s*$", text, re.M):
        for name in copied.split():
            assert (ROOT / name).exists(), f"Dockerfile copies {name}, which does not exist"
            assert name not in ignore, f"{name} is copied by the Dockerfile but excluded by .dockerignore"
    assert "EXPOSE 9877" in text


def test_healthcheck_hits_a_route_the_server_really_has():
    services = load_yaml(ROOT / "compose.yaml")["services"]
    probe = " ".join(services["exporter"]["healthcheck"]["test"])
    assert "127.0.0.1:9877/healthz" in probe
