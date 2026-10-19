import json

import pytest
from helpers import FakeDocker, container, parse, value
from metrics_exporter import dockerapi
from metrics_exporter.dockerapi import DockerCollector, DockerUnavailable, list_containers
from metrics_exporter.expo import render


def scrape(path, **kw):
    return parse(render(DockerCollector(path, **kw)()))


def test_reads_only_the_container_list_with_a_get():
    with FakeDocker([container("web")]) as d:
        scrape(d.path)
        assert d.requests == [("GET", "/containers/json?all=1")]


def test_inventory_metrics():
    cs = [container("web", "running", "nimbus/web:1.2"), container("db", "exited", "quanta/db:5"), container("job", "paused")]
    with FakeDocker(cs) as d:
        got = scrape(d.path)
    assert value(got, "docker_containers", state="running") == 1
    assert value(got, "docker_containers", state="exited") == 1
    assert value(got, "docker_containers", state="paused") == 1
    assert value(got, "docker_containers", state="dead") == 0  # known states are always present, even at 0
    assert value(got, "docker_container_running", name="web") == 1
    assert value(got, "docker_container_running", name="db") == 0
    assert any(dict(k)["name"] == "db" and dict(k)["image"] == "quanta/db:5" and len(dict(k)["container_id"]) == 12
               for k in got["docker_container_info"]["samples"])  # fmt: skip
    assert value(got, "docker_container_created_timestamp_seconds", name="web") == 1_759_000_000
    assert value(got, "docker_containers_omitted") == 0


def test_an_empty_host_still_reports_zero_counts():
    with FakeDocker([]) as d:
        got = scrape(d.path)
    assert all(v == 0 for v in got["docker_containers"]["samples"].values()) and "docker_container_info" not in got


def test_unknown_states_are_folded_into_one_label_value():
    with FakeDocker([container("a", "weird-new-state"), container("b", "")]) as d:
        got = scrape(d.path)
    assert value(got, "docker_containers", state="unknown") == 2
    assert not any("weird" in dict(k)["state"] for k in got["docker_containers"]["samples"])  # bounded cardinality


def test_per_container_series_are_capped_and_the_omission_is_visible():
    cs = [container(f"c{i:03d}") for i in range(30)]
    with FakeDocker(cs) as d:
        got = scrape(d.path, max_containers=10)
    assert len(got["docker_container_running"]["samples"]) == 10
    assert value(got, "docker_containers_omitted") == 20
    assert value(got, "docker_containers", state="running") == 30  # the totals still count everything


def test_hostile_names_are_sanitised_and_still_parse():
    nasty = container('x"y\\z\nq', image="reg/img\nwith-newline:" + "t" * 400)
    with FakeDocker([nasty, {"Id": "abc123def456789", "Names": [], "State": "running", "Image": "i"}, "not-a-dict"]) as d:
        got = scrape(d.path)
    names = {dict(k)["name"] for k in got["docker_container_running"]["samples"]}
    assert 'x"y\\z q' in names and "abc123def456" in names
    assert all(len(dict(k)["image"]) <= 128 for k in got["docker_container_info"]["samples"])


def test_missing_created_is_skipped_not_fatal():
    c = container("web")
    c["Created"] = "yesterday"
    with FakeDocker([c]) as d:
        got = scrape(d.path)
    assert "docker_container_created_timestamp_seconds" not in got and value(got, "docker_container_running", name="web") == 1


def test_missing_socket():
    with pytest.raises(DockerUnavailable, match="not found"):
        list_containers("/tmp/definitely-not-here.sock")


@pytest.mark.parametrize("status", [400, 403, 500])
def test_non_200_answers(status):
    with FakeDocker([]) as d:
        d.status = status
        with pytest.raises(DockerUnavailable, match=f"HTTP {status}"):
            list_containers(d.path)


@pytest.mark.parametrize("body", [b"{not json", b'{"a": 1}', b'"text"', b""])
def test_bad_bodies(body):
    with FakeDocker([]) as d:
        d.raw_body = body
        with pytest.raises(DockerUnavailable):
            list_containers(d.path)


def test_oversized_body_is_refused():
    old = dockerapi.MAX_BODY_BYTES
    dockerapi.MAX_BODY_BYTES = 1000
    try:
        with FakeDocker([]) as d:
            d.raw_body = json.dumps([container(f"c{i}") for i in range(50)]).encode()
            with pytest.raises(DockerUnavailable, match="larger than"):
                list_containers(d.path)
    finally:
        dockerapi.MAX_BODY_BYTES = old


def test_a_slow_daemon_times_out_instead_of_hanging_the_scrape():
    with FakeDocker([container("web")]) as d:
        d.delay = 1.5
        with pytest.raises(DockerUnavailable):
            list_containers(d.path, timeout=0.2)
