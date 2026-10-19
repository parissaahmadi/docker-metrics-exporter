"""Prometheus text exposition format (version 0.0.4), written by hand.

Only what this exporter needs: ``gauge`` and ``counter`` families. The rules enforced here are the
ones a Prometheus server would otherwise reject or silently mangle:

* metric and label names match ``[a-zA-Z_:][a-zA-Z0-9_:]*`` / ``[a-zA-Z_][a-zA-Z0-9_]*``; labels may not
  start with ``__`` (reserved);
* a counter's name ends in ``_total``;
* one ``# HELP`` / ``# TYPE`` pair per family and all of its samples together;
* no two samples with the same name and label set in one scrape;
* label values escape ``\\``, ``"`` and newline; HELP text escapes ``\\`` and newline.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

METRIC_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
TYPES = ("gauge", "counter")
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class ExpositionError(ValueError):
    """Something that would make the scrape output invalid."""


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def format_value(value: float | int | bool) -> str:
    v = float(value)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


@dataclass
class Family:
    name: str
    type: str
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)

    def __post_init__(self):
        if not METRIC_RE.match(self.name):
            raise ExpositionError(f"invalid metric name {self.name!r}")
        if self.type not in TYPES:
            raise ExpositionError(f"{self.name}: unsupported type {self.type!r}")
        if self.type == "counter" and not self.name.endswith("_total"):
            raise ExpositionError(f"{self.name}: counter names must end in _total")

    def add(self, value: float | int | bool, **labels: str) -> Family:
        for key, val in labels.items():
            if not LABEL_RE.match(key) or key.startswith("__"):
                raise ExpositionError(f"{self.name}: invalid label name {key!r}")
            if not isinstance(val, str):
                raise ExpositionError(f"{self.name}: label {key!r} must be a string, got {type(val).__name__}")
        self.samples.append((dict(labels), float(value)))
        return self


def render(families: Iterable[Family]) -> str:
    seen_families: set[str] = set()
    lines: list[str] = []
    for fam in families:
        if fam.name in seen_families:
            raise ExpositionError(f"metric family {fam.name!r} appears twice")
        seen_families.add(fam.name)
        if not fam.samples:
            continue  # a family with no samples would be a lone HELP/TYPE; skip it
        lines.append(f"# HELP {fam.name} {escape_help(fam.help)}")
        lines.append(f"# TYPE {fam.name} {fam.type}")
        seen: set[tuple[tuple[str, str], ...]] = set()
        for labels, value in fam.samples:
            key = tuple(sorted(labels.items()))
            if key in seen:
                raise ExpositionError(f"duplicate series {fam.name}{dict(labels)}")
            seen.add(key)
            if labels:
                body = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in sorted(labels.items()))
                lines.append(f"{fam.name}{{{body}}} {format_value(value)}")
            else:
                lines.append(f"{fam.name} {format_value(value)}")
    return "\n".join(lines) + "\n" if lines else ""
