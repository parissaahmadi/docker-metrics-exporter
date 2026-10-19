from helpers import Clock, FakeDocker, container, parse, value
from metrics_exporter.dockerapi import DockerCollector
from metrics_exporter.expo import Family
from metrics_exporter.exporter import Exporter
from metrics_exporter.simulated import FleetCollector


def ok():
    return [Family("good_metric", "gauge", "h").add(1)]


def boom():
    raise RuntimeError("secret detail: /srv/private/path")


def test_self_metrics_are_always_present():
    got = parse(Exporter({"a": ok}).scrape())
    assert value(got, "exporter_collector_success", collector="a") == 1
    assert value(got, "exporter_collector_duration_seconds", collector="a") >= 0
    assert [dict(k)["version"] for k in got["exporter_build_info"]["samples"]] == ["0.1.0"]


def test_a_failing_collector_does_not_take_the_others_down_and_leaks_nothing():
    text = Exporter({"bad": boom, "a": ok}).scrape()
    got = parse(text)
    assert value(got, "exporter_collector_success", collector="bad") == 0
    assert value(got, "exporter_collector_success", collector="a") == 1
    assert value(got, "good_metric") == 1
    assert "secret" not in text and "private" not in text


def test_a_collector_producing_invalid_output_is_dropped_whole():
    def dup():
        return [Family("m", "gauge", "h").add(1, a="x").add(2, a="x"), Family("other", "gauge", "h").add(1)]

    got = parse(Exporter({"dup": dup, "a": ok}).scrape())
    assert "m" not in got and "other" not in got  # no partial output from the broken collector
    assert value(got, "exporter_collector_success", collector="dup") == 0


def test_series_limit_per_collector():
    def many():
        f = Family("big", "gauge", "h")
        for i in range(101):
            f.add(i, i=str(i))
        return [f]

    got = parse(Exporter({"many": many}, max_series_per_collector=100).scrape())
    assert "big" not in got and value(got, "exporter_collector_success", collector="many") == 0
    got = parse(Exporter({"many": many}, max_series_per_collector=101).scrape())
    assert len(got["big"]["samples"]) == 101


def test_two_collectors_cannot_claim_the_same_metric_name():
    got = parse(Exporter({"first": ok, "second": ok}).scrape())
    assert value(got, "exporter_collector_success", collector="first") == 1
    assert value(got, "exporter_collector_success", collector="second") == 0
    assert len(got["good_metric"]["samples"]) == 1


def test_repeated_scrapes_are_each_valid_and_the_counter_only_grows():
    clock = Clock()
    ex = Exporter({"fleet": FleetCollector(clock=clock)})
    last = {}
    for step in range(30):
        clock.t += 45
        got = parse(ex.scrape())
        now = got["plant_device_runtime_seconds_total"]["samples"]
        assert all(now[k] >= last.get(k, 0) for k in now)
        last = now


def test_fleet_and_docker_together_and_docker_down_leaves_the_fleet_up():
    with FakeDocker([container("web")]) as d:
        both = parse(Exporter({"fleet": FleetCollector(clock=Clock()), "docker": DockerCollector(d.path)}).scrape())
    assert value(both, "exporter_collector_success", collector="fleet") == 1
    assert value(both, "exporter_collector_success", collector="docker") == 1
    down = parse(Exporter({"fleet": FleetCollector(clock=Clock()), "docker": DockerCollector("/tmp/none.sock")}).scrape())
    assert value(down, "exporter_collector_success", collector="docker") == 0
    assert value(down, "exporter_collector_success", collector="fleet") == 1
    assert "docker_containers" not in down and "plant_fleet_devices" in down
