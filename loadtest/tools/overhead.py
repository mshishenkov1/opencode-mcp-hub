"""Добавка proxy по итогам прогона k6 (D6-08, S-01).

    k6 run --summary-export=loadtest/.seed/summary-mcp.json ... loadtest/k6/mcp.js
    python loadtest/tools/overhead.py loadtest/.seed/summary-mcp.json

Считает разницу перцентилей между трафиком через Hub (``mcp_via_hub``) и тем же
профилем запросов напрямую в мок (``mcp_direct``) и сверяет её с порогами
p50 ≤ 15 мс, p95 ≤ 50 мс. Код возврата 1, если порог превышен.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

P50_LIMIT = 15.0
P95_LIMIT = 50.0


def _values(summary: dict, name: str) -> dict:
    metrics = summary.get("metrics", summary)
    metric = metrics.get(name)
    if not isinstance(metric, dict):
        raise SystemExit(f"В итоге прогона нет метрики {name}")
    return metric.get("values", metric)


def _percentile(values: dict, *names: str) -> float:
    for name in names:
        if name in values:
            return float(values[name])
    raise SystemExit(f"В метрике нет перцентиля {names}: есть {sorted(values)}")


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "loadtest/.seed/summary-mcp.json")
    summary = json.loads(path.read_text(encoding="utf-8"))
    hub = _values(summary, "mcp_via_hub")
    direct = _values(summary, "mcp_direct")

    rows = []
    failed = False
    for label, keys, limit in (
        ("p50", ("p(50)", "med", "p50"), P50_LIMIT),
        ("p95", ("p(95)", "p95"), P95_LIMIT),
    ):
        hub_value = _percentile(hub, *keys)
        direct_value = _percentile(direct, *keys)
        overhead = hub_value - direct_value
        ok = overhead <= limit
        failed = failed or not ok
        rows.append((label, hub_value, direct_value, overhead, limit, ok))

    print(f"{'':4} | {'через Hub':>10} | {'мок':>10} | {'добавка':>10} | {'порог':>7} | итог")
    for label, hub_value, direct_value, overhead, limit, ok in rows:
        print(
            f"{label:4} | {hub_value:9.2f}м | {direct_value:9.2f}м | {overhead:9.2f}м | "
            f"{limit:6.0f}м | {'OK' if ok else 'ПРЕВЫШЕН'}"
        )

    errors = _values(summary, "http_req_failed").get("rate", 0.0)
    print(f"\nДоля ошибок HTTP: {float(errors) * 100:.4f} % (порог 0,1 %)")
    if float(errors) > 0.001:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
