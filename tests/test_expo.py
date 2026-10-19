import math
import random

import pytest
from helpers import parse, value
from metrics_exporter.expo import (
    ExpositionError,
    Family,
    escape_help,
    escape_label_value,
    format_value,
    render,
)


@pytest.mark.parametrize(
    "v,text",
    [(1, "1"), (0, "0"), (True, "1"), (False, "0"), (2.5, "2.5"), (-3.0, "-3"), (1e20, "1e+20"),
     (0.1, "0.1"), (float("inf"), "+Inf"), (float("-inf"), "-Inf"), (float("nan"), "NaN"), (123456789012, "123456789012")],
)  # fmt: skip
def test_format_value(v, text):
    assert format_value(v) == text


def test_escaping():
    assert escape_label_value('a\\b"c\nd') == 'a\\\\b\\"c\\nd'
    assert escape_help("x\\y\nz") == "x\\\\y\\nz"


def test_render_shape():
    f = Family("jobs_total", "counter", "Jobs done.").add(3, queue="a").add(5, queue="b")
    g = Family("temp_celsius", "gauge", "Temp.").add(21.5)
    assert render([f, g]) == (
        "# HELP jobs_total Jobs done.\n# TYPE jobs_total counter\n"
        'jobs_total{queue="a"} 3\njobs_total{queue="b"} 5\n'
        "# HELP temp_celsius Temp.\n# TYPE temp_celsius gauge\ntemp_celsius 21.5\n"
    )


def test_label_order_is_stable_regardless_of_insertion_order():
    a = Family("m", "gauge", "h").add(1, b="2", a="1")
    b = Family("m", "gauge", "h").add(1, a="1", b="2")
    assert render([a]) == render([b]) and 'm{a="1",b="2"} 1' in render([a])


def test_empty_family_and_empty_input_render_nothing():
    assert render([Family("m", "gauge", "h")]) == "" and render([]) == ""


@pytest.mark.parametrize("name", ["1abc", "a-b", "a b", "", "é"])
def test_invalid_metric_names(name):
    with pytest.raises(ExpositionError, match="invalid metric name"):
        Family(name, "gauge", "h")


@pytest.mark.parametrize("label", ["1a", "a-b", "__reserved", "a b", ""])
def test_invalid_label_names(label):
    with pytest.raises(ExpositionError, match="invalid label name"):
        Family("m", "gauge", "h").add(1, **{label: "x"})


def test_label_values_must_be_strings():
    with pytest.raises(ExpositionError, match="must be a string"):
        Family("m", "gauge", "h").add(1, a=5)


def test_counter_naming_and_type_checks():
    with pytest.raises(ExpositionError, match="_total"):
        Family("jobs", "counter", "h")
    with pytest.raises(ExpositionError, match="unsupported type"):
        Family("m", "histogram", "h")


def test_duplicate_series_and_duplicate_family_are_refused():
    dup = Family("m", "gauge", "h").add(1, a="x").add(2, a="x")
    with pytest.raises(ExpositionError, match="duplicate series"):
        render([dup])
    with pytest.raises(ExpositionError, match="appears twice"):
        render([Family("m", "gauge", "h").add(1), Family("m", "gauge", "h").add(1)])


def test_hostile_label_values_round_trip_through_a_strict_parser():
    rng = random.Random(1)
    alphabet = ['a', 'Z', '0', ' ', '"', '\\', '\n', '{', '}', ',', '=', '#', 'é', '\t', '\\n']
    fam = Family("m", "gauge", "help with \\ and\nnewline")
    expected = {}
    for i in range(200):
        v = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        if v in expected:
            continue
        expected[v] = i
        fam.add(i, v=v)
    parsed = parse(render([fam]))
    assert {dict(k)["v"]: val for k, val in parsed["m"]["samples"].items()} == {k: float(i) for k, i in expected.items()}


def test_special_float_values_survive():
    fam = Family("m", "gauge", "h").add(float("nan"), k="n").add(float("inf"), k="i").add(1.5e-7, k="s")
    got = parse(render([fam]))
    assert math.isnan(value(got, "m", k="n")) and value(got, "m", k="i") == math.inf and value(got, "m", k="s") == 1.5e-7
