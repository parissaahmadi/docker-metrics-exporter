"""A fictional plant fleet whose readings are a pure function of (seed, device, time).

No random state: asking for the same instant twice gives the same numbers, and tests can pin the
clock. The one stateful thing is the hour meter (a counter), which accumulates between scrapes the way
a polled meter would, and can therefore never go backwards.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .expo import Family

VIBRATION_ALARM_MM_S = 7.0  # illustrative threshold for this demo, not taken from a standard


@dataclass(frozen=True)
class Device:
    device: str
    kind: str
    brand: str
    model: str
    site: str


FLEET: tuple[Device, ...] = (
    Device("VX-C01", "compressor", "Vortex", "VX-200", "hall-a"),
    Device("VX-C02", "compressor", "Vortex", "VX-200", "hall-b"),
    Device("HX-P01", "pump", "Helix", "HX-50", "hall-a"),
    Device("HX-P02", "pump", "Helix", "HX-50", "hall-b"),
    Device("IW-V01", "conveyor", "Ironwood", "IW-9", "hall-b"),
    Device("NB-F01", "fan", "Nimbus", "NB-3", "hall-a"),
)
# kind -> (typical running temperature °C, swing °C, typical vibration mm/s)
PROFILE = {
    "compressor": (78.0, 6.0, 2.8),
    "pump": (55.0, 4.0, 2.2),
    "conveyor": (46.0, 3.0, 1.6),
    "fan": (41.0, 2.5, 1.9),
}
AMBIENT_C = 22.0


def _unit(seed: int, *parts: object) -> float:
    """Deterministic number in [0, 1) from the inputs."""
    digest = hashlib.sha256("|".join(str(p) for p in (seed, *parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def reading(dev: Device, seed: int, t: float) -> dict[str, float]:
    """Temperature, vibration and running state of one device at epoch-second ``t``."""
    base_t, swing, base_v = PROFILE[dev.kind]
    phase = int(_unit(seed, dev.device, "phase") * 1000)
    running = ((int(t) // 900) + phase) % 6 != 0  # off for one 15-minute block in six
    wobble = _unit(seed, dev.device, "n", int(t) // 30) - 0.5  # new noise every 30 s
    if running:
        temp = base_t + swing * math.sin(2 * math.pi * t / 1800 + phase) + wobble * 1.2
        vib = base_v * (1 + 0.25 * math.sin(2 * math.pi * t / 600 + phase)) + wobble * 0.3
        if ((int(t) // 1200) + phase) % 9 == 0:  # a 20-minute "fault episode" every few hours
            vib *= 3.2
    else:
        temp = AMBIENT_C + abs(wobble)
        vib = 0.0
    return {"running": 1.0 if running else 0.0, "temperature": temp, "vibration": max(vib, 0.0)}


class FleetCollector:
    def __init__(self, seed: int = 7, clock: Callable[[], float] | None = None, fleet: tuple[Device, ...] = FLEET):
        import time

        self.seed, self.clock, self.fleet = seed, clock or time.time, fleet
        self._runtime = {d.device: 0.0 for d in fleet}
        self._last: tuple[float, dict[str, bool]] | None = None
        self._lock = threading.Lock()

    def __call__(self) -> list[Family]:
        now = self.clock()
        readings = {d.device: reading(d, self.seed, now) for d in self.fleet}
        with self._lock:
            if self._last is not None:
                prev_t, prev_running = self._last
                dt = max(0.0, now - prev_t)  # a clock that steps back adds nothing, never subtracts
                for name, was_running in prev_running.items():
                    if was_running:
                        self._runtime[name] += dt
            self._last = (now, {n: r["running"] == 1.0 for n, r in readings.items()})
            runtime = dict(self._runtime)

        info = Family("plant_device_info", "gauge", "Static facts about a device (value is always 1).")
        up = Family("plant_device_running", "gauge", "1 while the device is running, 0 while stopped.")
        temp = Family("plant_device_temperature_celsius", "gauge", "Housing temperature.")
        vib = Family("plant_device_vibration_rms_mm_per_s", "gauge", "Vibration velocity, RMS.")
        alarm = Family("plant_device_vibration_alarm", "gauge", f"1 when vibration exceeds {VIBRATION_ALARM_MM_S} mm/s.")
        hours = Family("plant_device_runtime_seconds_total", "counter", "Accumulated running time, sampled at scrape time.")
        count = Family("plant_fleet_devices", "gauge", "Number of devices in the simulated fleet.")
        for d in self.fleet:
            r = readings[d.device]
            info.add(1, device=d.device, kind=d.kind, brand=d.brand, model=d.model, site=d.site)
            up.add(r["running"], device=d.device)
            shown = round(r["vibration"], 3)  # the alarm is judged on the number a dashboard will show
            temp.add(round(r["temperature"], 2), device=d.device)
            vib.add(shown, device=d.device)
            alarm.add(1 if shown > VIBRATION_ALARM_MM_S else 0, device=d.device)
            hours.add(round(runtime[d.device], 3), device=d.device)
        count.add(len(self.fleet))
        return [info, up, temp, vib, alarm, hours, count]
