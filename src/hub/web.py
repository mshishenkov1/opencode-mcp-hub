"""Веб-интерфейс Hub: общие представления страниц и вспомогательные функции (R-W1..R-W6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from hub.catalog import PermissionHeaderGroups, ServerEntry
from hub.db import (
    TOKEN_ORIGIN_ISSUED,
    TOKEN_ORIGIN_REASON_POLICY_DENIED,
    TOKEN_ORIGIN_REASON_TOKEN_UNUSABLE,
    TOKEN_ORIGIN_REASON_UPSTREAM_UNAVAILABLE,
    TOKEN_ORIGIN_SUBMITTED,
)
from hub.templating import Templates
from hub.websession import WebSessionInfo, session_token

LOGIN_PATH = "/auth/login"
DEFAULT_NEXT = "/ui/connections"

STATUS_TITLES = {
    "not_connected": "Не подключён",
    "connected": "Подключён",
    "needs_reauth": "Нужна повторная авторизация",
}
PRESET_TITLES = {"readonly": "Только чтение", "readwrite": "Чтение и запись"}

# R-U16: пользователь должен видеть, отвалится ли коннектор при выходе из мессенджера.
ISSUED_NOTICE = "Подключено постоянным токеном: выход из мессенджера подключение не разорвёт"
SUBMITTED_NOTICE = (
    "Подключено присланным токеном сессии: подключение прервётся при выходе из мессенджера, "
    "токен придётся ввести заново"
)
# R-U18.3: показанный срок — верхняя граница, поэтому формулировка «не позднее» обязательна.
SUBMITTED_UNTIL = "а также не позднее"
ORIGIN_REASON_TEXTS = {
    TOKEN_ORIGIN_REASON_POLICY_DENIED: "целевая система не разрешает выпуск личных токенов",
    TOKEN_ORIGIN_REASON_UPSTREAM_UNAVAILABLE: (
        "целевая система была недоступна, попробуйте переподключиться позже"
    ),
    TOKEN_ORIGIN_REASON_TOKEN_UNUSABLE: "выданный постоянный токен оказался непригоден",
}
EXCHANGE_HINT = (
    "Hub получит у целевой системы постоянный токен, а присланный не сохранит: "
    "подключение переживёт выход из мессенджера"
)


def format_moment(value: datetime | None) -> str | None:
    """Дата и время в UTC для страниц; ``None`` — срок неизвестен и не додумывается (R-U18)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def token_origin_notice(
    origin: str | None, reason: str | None, expires_at: datetime | None
) -> dict[str, Any] | None:
    """Текст о постоянстве подключения (R-U16); ``None`` — показывать нечего.

    У способа без блока ``exchange`` причина пуста: Hub не знает, какой токен прислал
    пользователь, и не вправе называть подключение временным — вид страницы прежний.
    """
    if origin == TOKEN_ORIGIN_ISSUED:
        return {"kind": TOKEN_ORIGIN_ISSUED, "text": ISSUED_NOTICE, "reason_text": None}
    if origin == TOKEN_ORIGIN_SUBMITTED and reason:
        text = SUBMITTED_NOTICE
        moment = format_moment(expires_at)
        if moment:
            text = f"{text}, {SUBMITTED_UNTIL} {moment}"
        return {
            "kind": TOKEN_ORIGIN_SUBMITTED,
            "text": text,
            "reason_text": ORIGIN_REASON_TEXTS.get(reason),
        }
    return None


def status_title(status: str | None) -> str:
    return STATUS_TITLES.get(status or "not_connected", "Не подключён")


def preset_title(preset: str | None) -> str | None:
    if not preset:
        return None
    return PRESET_TITLES.get(preset, preset)


def safe_next(value: str | None) -> str:
    """Только относительный путь внутри Hub; внешний/абсолютный ``next`` → ``/ui/connections`` (R-W1)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return DEFAULT_NEXT
    return value


def login_redirect(next_url: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"{LOGIN_PATH}?next={quote(next_url, safe='')}", status_code=302)


async def current_session(request: Request) -> WebSessionInfo | None:
    service = request.app.state.web_sessions
    return await service.load(session_token(request))  # type: ignore[no-any-return]


def group_definitions(entry: ServerEntry) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``(always-группы, выбираемые группы)`` каталога; группы ``preset: none`` не показываются (R-W3)."""
    model = entry.model.permission_model
    if not isinstance(model, PermissionHeaderGroups):
        return [], []
    by_id = {g.id: g for g in model.groups}
    always = [
        {"id": gid, "title": by_id[gid].title if gid in by_id else gid} for gid in model.always
    ]
    selectable = [
        {"id": g.id, "title": g.title, "preset": g.preset}
        for g in model.groups
        if g.preset != "none" and g.id not in set(model.always)
    ]
    return always, selectable


def group_titles(entry: ServerEntry, groups: list[str]) -> list[str]:
    model = entry.model.permission_model
    if not isinstance(model, PermissionHeaderGroups):
        return list(groups)
    by_id = {g.id: g.title for g in model.groups}
    return [by_id.get(gid, gid) for gid in groups]


def html_error(
    templates: Templates,
    *,
    error: str,
    message: str,
    status_code: int = 400,
    hint: str | None = None,
    retry_url: str | None = None,
) -> Response:
    """Страница ошибки с машиночитаемым кодом в ``<meta name="hub-error">`` (R-O4)."""
    return templates.page(
        "error.html",
        status_code=status_code,
        error=error,
        message=message,
        hint=hint,
        retry_url=retry_url,
    )


def server_public_view(entry: ServerEntry, public_url: str) -> dict[str, Any]:
    view = entry.public_view(public_url)
    view.pop("permission_model", None)
    return view


__all__ = [
    "DEFAULT_NEXT",
    "EXCHANGE_HINT",
    "ISSUED_NOTICE",
    "LOGIN_PATH",
    "ORIGIN_REASON_TEXTS",
    "PRESET_TITLES",
    "STATUS_TITLES",
    "SUBMITTED_NOTICE",
    "current_session",
    "format_moment",
    "group_definitions",
    "group_titles",
    "html_error",
    "login_redirect",
    "preset_title",
    "safe_next",
    "server_public_view",
    "status_title",
    "token_origin_notice",
]
