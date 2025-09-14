from __future__ import annotations

"""Lightweight in-memory metrics with Prometheus text exposition.

We avoid external deps; counters and summaries only.
"""

from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Tuple


LabelKey = Tuple[Tuple[str, str], ...]  # sorted tuple of (k,v)


def _labels_key(labels: Dict[str, str] | None) -> LabelKey:
    return tuple(sorted((labels or {}).items()))


@dataclass
class _Summary:
    count: float = 0.0
    sum: float = 0.0


class Metrics:
    def __init__(self) -> None:
        self._counters: Dict[str, Dict[LabelKey, float]] = {}
        self._summaries: Dict[str, Dict[LabelKey, _Summary]] = {}
        self._lock = Lock()

    def inc(self, name: str, *, labels: Dict[str, str] | None = None, value: float = 1.0) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = _labels_key(labels)
            series[key] = series.get(key, 0.0) + value

    def observe(self, name: str, value: float, *, labels: Dict[str, str] | None = None) -> None:
        with self._lock:
            series = self._summaries.setdefault(name, {})
            key = _labels_key(labels)
            s = series.get(key)
            if s is None:
                s = _Summary()
                series[key] = s
            s.count += 1.0
            s.sum += float(value)

    def to_prometheus(self) -> str:
        lines: list[str] = []
        # Counters
        for name, series in self._counters.items():
            lines.append(f"# TYPE {name} counter")
            for labels, value in series.items():
                label_str = "".join(
                    [
                        "{" + ",".join([f'{k}="{v}"' for k, v in labels]) + "}"
                        if labels
                        else ""
                    ]
                )
                lines.append(f"{name}{label_str} {value}")
        # Summaries (export as _count and _sum)
        for name, series in self._summaries.items():
            lines.append(f"# TYPE {name} summary")
            for labels, s in series.items():
                label_str = (
                    "{" + ",".join([f'{k}="{v}"' for k, v in labels]) + "}"
                    if labels
                    else ""
                )
                lines.append(f"{name}_count{label_str} {s.count}")
                lines.append(f"{name}_sum{label_str} {s.sum}")
        return "\n".join(lines) + "\n"


metrics = Metrics()

