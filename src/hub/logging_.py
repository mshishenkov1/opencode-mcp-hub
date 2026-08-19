"""JSON-логи и request_id (R-S4). Секреты в логи не пишутся по построению (R-K3)."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("hub_request_id", default=None)

_STANDARD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "message",
    "asctime", "taskName",
}  # fmt: skip


class RequestIdFilter(logging.Filter):
    """Добавляет ``request_id`` из contextvar в каждую запись."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """Одна запись — один JSON-объект в строке."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or request_id_var.get()
        if rid:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_") or key == "request_id":
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int | str = logging.INFO) -> logging.Logger:
    """Настроить JSON-логи Hub. Идемпотентно (один JSON-хендлер на процесс).

    JSON-хендлер ставится на root-логгер, а не на ``hub``: записи ``hub.*`` проходят цепочку один раз
    (единственный sink процесса — нет дублей под uvicorn, чья конфигурация root-хендлеров не задаёт,
    а поздний ``logging.basicConfig`` сторонних библиотек становится no-op); ``propagate`` у ``hub``
    остаётся включённым — перехват через root (pytest caplog, агрегаторы) продолжает работать.
    """
    root = logging.getLogger()
    if not any(getattr(h, "_hub_json", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestIdFilter())
        handler._hub_json = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    logger = logging.getLogger("hub")
    logger.setLevel(level)
    if not any(isinstance(f, RequestIdFilter) for f in logger.filters):
        logger.addFilter(RequestIdFilter())
    logger.propagate = True
    return logger


__all__ = ["JsonFormatter", "RequestIdFilter", "configure_logging", "request_id_var"]
