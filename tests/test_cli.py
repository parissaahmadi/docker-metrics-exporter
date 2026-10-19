import io
import logging

import pytest

from helpers import FakeDocker, container, parse, value
from metrics_exporter.cli import build_exporter, build_parser, main


def scrape(*argv):
    out = io.StringIO()
    code = main(["scrape", *argv], out)
    return code, out.getvalue()


def test_scrape_prints_one_valid_scrape_and_exits_0():
    code, text = scrape()
    fams = parse(text)
    assert code == 0 and value(fams, "plant_fleet_devices") == 6
    assert value(fams, "exporter_collector_success", collector="fleet") == 1


def test_seed_changes_the_fleet_readings_but_not_its_shape():
    a, b = parse(scrape("--seed", "1")[1]), parse(scrape("--seed", "2")[1])
    assert set(a) == set(b) and a["plant_device_info"] == b["plant_device_info"]


def test_nothing_to_export_is_an_error_not_an_empty_server():
    with pytest.raises(SystemExit, match="nothing to export"):
        main(["scrape", "--no-sim"], io.StringIO())


def test_docker_flag_with_a_missing_socket_reports_failure_but_still_serves_the_fleet():
    log = logging.getLogger("metrics_exporter")
    prev = log.disabled
    log.disabled = True  # the expected warning would only clutter test output
    try:
        code, text = scrape("--docker", "--docker-socket", "/nonexistent/docker.sock")
    finally:
        log.disabled = prev
    fams = parse(text)
    assert code == 0
    assert value(fams, "exporter_collector_success", collector="docker") == 0
    assert value(fams, "exporter_collector_success", collector="fleet") == 1


def test_docker_only_mode_against_a_fake_engine():
    with FakeDocker([container("web"), container("db", state="exited")]) as fake:
        code, text = scrape("--no-sim", "--docker", "--docker-socket", fake.path)
    fams = parse(text)
    assert code == 0 and "plant_fleet_devices" not in fams
    assert value(fams, "docker_containers", state="running") == 1 and value(fams, "docker_containers", state="exited") == 1


def test_serve_defaults_to_loopback_so_nothing_is_exposed_by_accident():
    a = build_parser().parse_args(["serve"])
    assert (a.bind, a.port, a.docker, a.no_sim) == ("127.0.0.1", 9877, False, False)


def test_docker_is_opt_in():
    exporter = build_exporter(build_parser().parse_args(["scrape"]))
    assert list(exporter.collectors) == ["fleet"]
    exporter = build_exporter(build_parser().parse_args(["scrape", "--docker"]))
    assert list(exporter.collectors) == ["fleet", "docker"]
