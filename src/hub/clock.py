"""Часы приложения: подменяемый источник времени для TTL, окон rate-limit и кэшей."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Интерфейс часов. Все три метода должны сдвигаться согласованно."""

    def now(self) -> datetime:
        """Текущее время (aware, UTC)."""

    def time(self) -> float:
        """Секунды epoch (для меток времени в KeyValueStore)."""

    def monotonic(self) -> float:
        """Монотонные секунды (для истечения TTL in-memory хранилища)."""


class SystemClock:
    """Системные часы."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """Ручные часы: время меняется только через ``advance``/``set``. Для тестов и отладки."""

    def __init__(self, start: datetime | float | None = None) -> None:
        if start is None:
            self._time = time.time()
        elif isinstance(start, datetime):
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            self._time = start.timestamp()
        else:
            self._time = float(start)
        self._monotonic_offset = 0.0

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._time, tz=UTC)

    def time(self) -> float:
        return self._time

    def monotonic(self) -> float:
        return self._time + self._monotonic_offset

    def advance(self, seconds: float) -> None:
        """Сдвинуть часы вперёд на ``seconds`` секунд."""
        self._time += float(seconds)

    def set(self, when: datetime | float) -> None:
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            self._time = when.timestamp()
        else:
            self._time = float(when)


__all__ = ["Clock", "ManualClock", "SystemClock"]
