"""Runs the collectors for one scrape and keeps one broken source from breaking the rest."""

from __future__ import annotations

import logging
import platform
import time
from collections.abc import Callable

from . import __version__
from .expo import ExpositionError, Family, render

log = logging.getLogger("metrics_exporter")

Collector = Callable[[], list[Family]]


class Exporter:
    def __init__(self, collectors: dict[str, Collector], max_series_per_collector: int = 5000):
        self.collectors = collectors
        self.max_series = max_series_per_collector

    def scrape(self) -> str:
        chunks: list[str] = []
        success = Family("exporter_collector_success", "gauge", "1 if the collector worked in this scrape, else 0.")
        duration = Family("exporter_collector_duration_seconds", "gauge", "Time the collector took in this scrape.")
        seen: set[str] = set()
        for name, collect in self.collectors.items():
            started = time.perf_counter()
            ok = False
            try:
                families = collect()
                total = sum(len(f.samples) for f in families)
                if total > self.max_series:
                    raise ExpositionError(f"{total} series exceeds the limit of {self.max_series}")
                clash = {f.name for f in families} & seen
                if clash:
                    raise ExpositionError(f"metric name(s) already used by another collector: {sorted(clash)}")
                text = render(families)  # validates names, labels and duplicates
                seen |= {f.name for f in families}
                chunks.append(text)
                ok = True
            except Exception as e:  # one source failing must not take the others down
                log.warning("collector %r failed: %s: %s", name, type(e).__name__, e)  # detail stays in the log
            success.add(1 if ok else 0, collector=name)
            duration.add(round(time.perf_counter() - started, 6), collector=name)
        build = Family("exporter_build_info", "gauge", "Exporter version (value is always 1).")
        build.add(1, version=__version__, python=platform.python_version())
        chunks.append(render([success, duration, build]))
        return "".join(chunks)
