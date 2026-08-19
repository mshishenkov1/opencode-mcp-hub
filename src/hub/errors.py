"""Исключения Hub и единый формат ошибок ``{error, message?, hint?}`` (R-A7)."""

from __future__ import annotations

from typing import Any


class ConfigError(ValueError):
    """Ошибка конфигурации (переменные окружения ``HUB_*``)."""


class CatalogError(ValueError):
    """Ошибка загрузки каталога: файл, YAML, ``$ref``, ``${VAR}``, схема."""


class HubError(Exception):
    """HTTP-ошибка Hub с единым JSON-форматом.

    ``status_code`` — HTTP-код, ``error`` — snake_case код, ``message``/``hint`` — необязательный
    русский текст, ``headers`` — дополнительные заголовки ответа.
    """

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str | None = None,
        *,
        hint: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(error if message is None else f"{error}: {message}")
        self.status_code = status_code
        self.error = error
        self.message = message
        self.hint = hint
        self.headers = headers or {}

    def to_body(self, *, cli: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if cli:
            body["status"] = "error"
        body["error"] = self.error
        if self.message is not None:
            body["message"] = self.message
        if self.hint is not None:
            body["hint"] = self.hint
        return body


def unauthorized() -> HubError:
    return HubError(
        401,
        "unauthorized",
        "Требуется ключ доступа",
        hint="выполните вход: opencode corp login",
        headers={"WWW-Authenticate": "Bearer"},
    )
