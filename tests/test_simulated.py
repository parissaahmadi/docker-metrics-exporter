import pytest

from helpers import Clock, parse, value
from metrics_exporter.expo import render
from metrics_exporter.simulated import (
    FLEET,
    PROFILE,
    VIBRATION_ALARM_MM_S,
    Device,
    FleetCollector,
    reading,
)

T0 = 1_760_000_000


def scrape(c):
    return parse(render(c()))


def test_readings_are_a_pure_function_of_seed_device_and_time():
    d = FLEET[0]
    assert reading(d, 7, T0) == reading(d, 7, T0)
    assert [reading(d, 7, T0 + i * 31) for i in range(20)] == [reading(d, 7, T0 + i * 31) for i in range(20)]


def test_a_different_seed_gives_a_different_fleet_history():
    d = FLEET[0]
    a = [reading(d, 1, T0 + i * 60) for i in range(60)]
    b = [reading(d, 2, T0 + i * 60) for i in range(60)]
    assert a != b


@pytest.mark.parametrize("dev", FLEET)
def test_values_stay_in_a_physical_range_over_two_days(dev):
    base_t, swing, base_v = PROFILE[dev.kind]
    for i in range(0, 2 * 86400, 97):
        r = reading(dev, 7, T0 + i)
        assert r["running"] in (0.0, 1.0)
        if r["running"]:
            assert base_t - swing - 2 < r["temperature"] < base_t + swing + 2
            assert 0 < r["vibration"] < base_v * 1.25 * 3.2 + 1
        else:
            assert r["vibration"] == 0.0 and 20 < r["temperature"] < 24


def test_every_device_both_runs_and_stops_and_has_a_fault_episode():
    for dev in FLEET:
        rs = [reading(dev, 7, T0 + i) for i in range(0, 86400 * 2, 60)]
        assert any(r["running"] for r in rs) and any(not r["running"] for r in rs), dev.device
    faults = sum(1 for dev in FLEET for i in range(0, 86400, 60) if reading(dev, 7, T0 + i)["vibration"] > VIBRATION_ALARM_MM_S)
    assert faults > 0


def test_scrape_is_valid_and_complete():
    got = scrape(FleetCollector(clock=Clock()))
    assert set(got) == {
        "plant_device_info", "plant_device_running", "plant_device_temperature_celsius",
        "plant_device_vibration_rms_mm_per_s", "plant_device_vibration_alarm",
        "plant_device_runtime_seconds_total", "plant_fleet_devices",
    }  # fmt: skip
    assert all(len(f["samples"]) == len(FLEET) for n, f in got.items() if n != "plant_fleet_devices")
    assert value(got, "plant_fleet_devices") == len(FLEET)
    assert got["plant_device_runtime_seconds_total"]["type"] == "counter"
    assert value(got, "plant_device_info", device="VX-C01", kind="compressor", brand="Vortex", model="VX-200", site="hall-a") == 1


def test_alarm_matches_the_threshold_exactly():
    clock, c = Clock(), None
    c = FleetCollector(clock=clock)
    for step in range(0, 86400, 600):
        clock.t = T0 + step
        got = scrape(c)
        for dev in FLEET:
            v = value(got, "plant_device_vibration_rms_mm_per_s", device=dev.device)
            a = value(got, "plant_device_vibration_alarm", device=dev.device)
            assert a == (1.0 if v > VIBRATION_ALARM_MM_S else 0.0)


def test_hour_meter_counts_only_running_time_and_never_decreases():
    dev = Device("T-1", "pump", "Helix", "HX-50", "hall-a")
    clock = Clock(T0)
    c = FleetCollector(seed=7, clock=clock, fleet=(dev,))
    expect, last, prev_value = 0.0, None, 0.0
    for step in range(0, 6 * 3600, 70):
        clock.t = T0 + step
        got = scrape(c)
        run = reading(dev, 7, clock.t)["running"] == 1.0
        if last is not None and last[1]:
            expect += clock.t - last[0]
        last = (clock.t, run)
        v = value(got, "plant_device_runtime_seconds_total", device="T-1")
        assert v >= prev_value and abs(v - expect) < 0.01
        prev_value = v
    assert 0 < prev_value < 6 * 3600  # it ran, but not the whole time


def test_clock_going_backwards_never_reduces_the_counter():
    dev = Device("T-1", "pump", "Helix", "HX-50", "hall-a")
    clock = Clock(T0)
    c = FleetCollector(seed=7, clock=clock, fleet=(dev,))
    scrape(c)
    clock.t = T0 + 120
    before = value(scrape(c), "plant_device_runtime_seconds_total", device="T-1")
    clock.t = T0 - 5000  # NTP step backwards
    after = value(scrape(c), "plant_device_runtime_seconds_total", device="T-1")
    assert after >= before


def test_alarm_boundary_is_strictly_above_the_threshold_on_the_displayed_value():
    """The scan above never lands exactly on 7.0, so pin the boundary by feeding chosen readings."""
    from metrics_exporter import simulated

    one = (Device("T-1", "pump", "Helix", "HX-50", "hall-a"),)
    real = simulated.reading
    try:
        for vibration, expected in [(6.999, 0), (7.0, 0), (7.0004, 0), (7.001, 1), (12.0, 1)]:
            simulated.reading = lambda dev, seed, t, v=vibration: {"running": 1.0, "temperature": 50.0, "vibration": v}
            got = scrape(FleetCollector(clock=Clock(), fleet=one))
            assert value(got, "plant_device_vibration_alarm", device="T-1") == expected, vibration
    finally:
        simulated.reading = real
