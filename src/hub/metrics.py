"""Метрики Prometheus (текстовый формат 0.0.4) без внешних зависимостей (R-S4)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Awaitable, Callable

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


class Metrics:
    """Счётчик запросов, гистограмма латентности и gauge активных сессий входа."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._buckets = tuple(sorted(buckets))
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._hist_counts: dict[tuple[str, str], list[int]] = {}
        self._hist_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._hist_total: dict[tuple[str, str], int] = defaultdict(int)
        self._gauge_providers: dict[str, Callable[[], Awaitable[float]]] = {}
        self._gauge_help: dict[str, str] = {}

    def observe_request(self, method: str, path: str, status: int, duration: float) -> None:
        self._requests[(method, path, str(status))] += 1
        key = (method, path)
        counts = self._hist_counts.setdefault(key, [0] * len(self._buckets))
        for i, bound in enumerate(self._buckets):
            if duration <= bound:
                counts[i] += 1
        self._hist_sum[key] += duration
        self._hist_total[key] += 1

    def register_gauge(self, name: str, help_text: str, provider: Callable[[], Awaitable[float]]) -> None:
        self._gauge_providers[name] = provider
        self._gauge_help[name] = help_text

    async def render(self) -> str:
        lines: list[str] = []
        lines.append("# HELP hub_http_requests_total Число HTTP-запросов к Hub.")
        lines.append("# TYPE hub_http_requests_total counter")
        for (method, path, status), value in sorted(self._requests.items()):
            lines.append(
                "hub_http_requests_total"
                + _labels({"method": method, "path": path, "status": status})
                + f" {value}"
            )
        lines.append("# HELP hub_http_request_duration_seconds Длительность HTTP-запросов к Hub.")
        lines.append("# TYPE hub_http_request_duration_seconds histogram")
        for key in sorted(self._hist_counts):
            method, path = key
            counts = self._hist_counts[key]
            for i, bound in enumerate(self._buckets):
                le = "+Inf" if math.isinf(bound) else repr(bound)
                lines.append(
                    "hub_http_request_duration_seconds_bucket"
                    + _labels({"method": method, "path": path, "le": le})
                    + f" {counts[i]}"
                )
            lines.append(
                "hub_http_request_duration_seconds_bucket"
                + _labels({"method": method, "path": path, "le": "+Inf"})
                + f" {self._hist_total[key]}"
            )
            lines.append(
                "hub_http_request_duration_seconds_sum"
                + _labels({"method": method, "path": path})
                + f" {self._hist_sum[key]!r}"
            )
            lines.append(
                "hub_http_request_duration_seconds_count"
                + _labels({"method": method, "path": path})
                + f" {self._hist_total[key]}"
            )
        for name, provider in self._gauge_providers.items():
            gauge = float(await provider())
            lines.append(f"# HELP {name} {self._gauge_help[name]}")
            lines.append(f"# TYPE {name} gauge")
            text_value = str(int(gauge)) if gauge.is_integer() else repr(gauge)
            lines.append(f"{name} {text_value}")
        return "\n".join(lines) + "\n"


PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

__all__ = ["PROMETHEUS_CONTENT_TYPE", "Metrics"]
