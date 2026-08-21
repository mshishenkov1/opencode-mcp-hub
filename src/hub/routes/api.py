"""``/api/*`` и ``/remote-config`` (Bearer): витрина каталога, профиль, подключения (R-A2..R-A4, R-A6)."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from hub.auth import AuthUser, authenticate, authenticate_key_or_session
from hub.broker import (
    REASON_SCOPE_UPGRADE,
    STATUS_CONNECTED,
    STATUS_NEEDS_REAUTH,
    STATUS_NOT_CONNECTED,
    ExchangeOutcome,
    TokenOrigin,
    UpstreamUnavailable,
    UserTokenRejected,
)
from hub.catalog import AuthUserToken, ServerEntry
from hub.db import Connection, UpstreamToken, to_iso
from hub.errors import HubError
from hub.permissions import (
    PRESETS,
    denied_groups,
    normalize_groups,
    preset_requires_reauth,
    unknown_groups,
)
from hub.proxy import TOOLS_CACHE_PREFIX, is_header_safe

router = APIRouter(tags=["api"])

_FALSE_VALUES = {"false", "0", "no"}


def _parse_include_deprecated(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() not in _FALSE_VALUES


async def _user_connections(request: Request, user_id: str) -> list[Connection]:
    db = request.app.state.db
    await db.init()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Connection).where(Connection.user_id == user_id).order_by(Connection.id)
            )
        ).scalars()
        return list(rows)


def _connection_view(conn: Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"status": "not_connected", "preset": None, "updated_at": None}
    return {"status": conn.status, "preset": conn.preset, "updated_at": to_iso(conn.updated_at)}


@router.get("/api/me")
async def api_me(user: Annotated[AuthUser, Depends(authenticate)]) -> JSONResponse:
    return JSONResponse(user.to_me())


@router.get("/api/catalog")
async def api_catalog(request: Request, user: Annotated[AuthUser, Depends(authenticate)]) -> JSONResponse:
    state = request.app.state
    catalog = state.catalog
    include_deprecated = _parse_include_deprecated(request.query_params.get("include_deprecated"))
    connections = {c.alias: c for c in await _user_connections(request, user.user_id)}
    servers = []
    for entry in catalog.visible_for(user.groups, include_deprecated=include_deprecated):
        view = entry.public_view(state.settings.public_url)
        view["connection"] = _connection_view(connections.get(entry.alias))
        servers.append(view)
    return JSONResponse({"version": catalog.version, "servers": servers})


async def _token_rows(request: Request, connections: list[Connection]) -> dict[int, UpstreamToken]:
    """Строки ``upstream_tokens`` подключений: происхождение токена наружу (R-U16)."""
    ids = [c.id for c in connections]
    if not ids:
        return {}
    db = request.app.state.db
    await db.init()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(UpstreamToken).where(UpstreamToken.connection_id.in_(ids))
            )
        ).scalars()
        return {row.connection_id: row for row in rows}


def _origin_view(
    entry: ServerEntry | None, connection: Connection, row: UpstreamToken | None
) -> dict[str, Any]:
    """R-U16: происхождение токена, причина и срок сессии; у OAuth-подключений — ``null``.

    Ни значение токена, ни ``issued_token_id`` наружу не отдаются (R-U17.3).
    """
    if row is None or entry is None or not entry.uses_user_token(connection.auth_method):
        return {"token_origin": None, "token_origin_reason": None, "session_expires_at": None}
    return {
        "token_origin": row.token_origin,
        "token_origin_reason": row.token_origin_reason,
        "session_expires_at": to_iso(row.submitted_expires_at),
    }


@router.get("/api/me/connections")
async def api_me_connections(
    request: Request, user: Annotated[AuthUser, Depends(authenticate)]
) -> JSONResponse:
    catalog = request.app.state.catalog
    connections = await _user_connections(request, user.user_id)
    rows = await _token_rows(request, connections)
    items = [
        {
            "alias": c.alias,
            "status": c.status,
            "preset": c.preset,
            "groups": list(c.groups or []),
            "created_at": to_iso(c.created_at),
            "updated_at": to_iso(c.updated_at),
            **_origin_view(catalog.get(c.alias), c, rows.get(c.id)),
        }
        for c in connections
    ]
    return JSONResponse(items)


def _facade_entry(request: Request, alias: str, user: AuthUser) -> Any:
    entry = request.app.state.catalog.get(alias)
    if (
        entry is None
        or entry.unconfigured
        or entry.model.mode != "facade"
        or not entry.is_visible_to(user.groups)
    ):
        raise HubError(404, "not_found", "Сервер не найден")
    return entry


def _json_object(body: bytes) -> dict[str, Any]:
    """Тело запроса как JSON-объект; иначе 400 ``invalid_request`` (R-U4)."""
    try:
        payload = json.loads(body or b"")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HubError(400, "invalid_request", "Тело запроса должно быть JSON") from exc
    if not isinstance(payload, dict):
        raise HubError(400, "invalid_request", "Ожидается JSON-объект {token, method, preset, groups}")
    return payload


def _requested_permissions(entry: ServerEntry, payload: dict[str, Any]) -> tuple[str, list[str]]:
    """``preset`` и группы запроса, проверенные по каталогу (R-B7, R-U4)."""
    preset = payload.get("preset", "readonly")
    if preset not in PRESETS:
        raise HubError(400, "invalid_request", "preset: допустимы readonly и readwrite")
    requested = payload.get("groups", [])
    if not isinstance(requested, list) or not all(isinstance(g, str) for g in requested):
        raise HubError(400, "invalid_request", "groups: ожидается массив идентификаторов групп")
    unknown = unknown_groups(entry, requested)
    if unknown:
        raise HubError(400, "invalid_request", f"Неизвестные группы: {', '.join(unknown)}")
    denied = denied_groups(entry, requested)
    if denied:
        raise HubError(400, "invalid_request", f"Группы недоступны: {', '.join(denied)}")
    return str(preset), normalize_groups(entry, str(preset), requested)


def _select_auth_method(entry: ServerEntry, payload: dict[str, Any]) -> AuthUserToken:
    """Способ подключения токеном из тела запроса (R-U4).

    Проверки идут в порядке спецификации: известность способа → доступность (409) → тип.
    """
    requested = payload.get("method")
    user_methods = entry.user_token_methods()
    if requested is not None:
        if not isinstance(requested, str):
            raise HubError(400, "invalid_request", "method: ожидается идентификатор способа")
        method = next((m for m in entry.auth_methods if m.id == requested), None)
        if method is None:
            raise HubError(400, "invalid_request", f"Неизвестный способ подключения: {requested}")
    elif len(user_methods) == 1:
        method = user_methods[0]
    elif not user_methods:
        raise HubError(400, "invalid_request", "Сервер не поддерживает подключение токеном")
    else:
        raise HubError(
            400,
            "invalid_request",
            "method: укажите способ подключения — у сервера их несколько",
        )
    if not method.available:
        raise HubError(
            409,
            "auth_method_unavailable",
            method.unavailable_reason or "Этот способ подключения сейчас недоступен",
        )
    if not isinstance(method, AuthUserToken):
        raise HubError(
            400, "invalid_request", f"Способ '{method.id}' не предполагает ввод токена"
        )
    return method


def _validated_token(method: AuthUserToken, payload: dict[str, Any]) -> str:
    """Значение токена из тела запроса: только длина, без регэкспов (R-U2, решение 63)."""
    token = payload.get("token")
    if not isinstance(token, str):
        raise HubError(400, "invalid_request", "token: обязательное поле, строка")
    field = method.field
    if len(token) < field.min_length:
        raise HubError(
            400, "invalid_request", f"token: значение короче {field.min_length} символов"
        )
    if len(token) > field.max_length:
        raise HubError(
            400, "invalid_request", f"token: значение длиннее {field.max_length} символов"
        )
    # BUG-I4-005: значение уходит в заголовок проверочного запроса (R-U2, R-U3), поэтому пригодность
    # для заголовка — такая же проверка тела, как длина, и выполняется до обращения в сеть (R-U4).
    if not is_header_safe(token):
        raise HubError(
            400,
            "invalid_request",
            "token: значение содержит символы, недопустимые в HTTP-заголовке "
            "(нужны печатные ASCII без переводов строки)",
        )
    return token


@router.post("/api/me/connections/{alias}/token")
async def api_connect_token(
    alias: str,
    request: Request,
    user: Annotated[AuthUser, Depends(authenticate_key_or_session)],
) -> JSONResponse:
    """Подключение коннектора пользовательским токеном (R-U4, R-U13).

    Проверки, не требующие сети (404, 400, 409), выполняются до обращения к целевой системе;
    само значение токена не попадает ни в журнал, ни в аудит, ни в ответ (R-U9). При объявленном
    блоке ``exchange`` присланный токен меняется на постоянный; неудача обмена подключение не
    отклоняет (R-U14) — сохраняется присланный токен с пометкой происхождения и причиной.
    """
    state = request.app.state
    entry = _facade_entry(request, alias, user)
    payload = _json_object(await request.body())
    method = _select_auth_method(entry, payload)
    token = _validated_token(method, payload)
    preset, groups = _requested_permissions(entry, payload)

    # Шаг 2 R-U13: проверка присланным токеном; её исход — единственный отказ подключения.
    try:
        account = await state.broker.verify_user_token(entry, method, token)
    except UserTokenRejected as exc:
        raise HubError(400, "token_rejected", "Целевая система не приняла токен") from exc
    except UpstreamUnavailable as exc:
        raise HubError(
            502, "upstream_unavailable", "Целевая система недоступна — повторите позже"
        ) from exc

    # Шаги 3–4 R-U13: выпуск постоянного токена и его проверка тем же блоком verify.
    if method.exchange is not None:
        outcome: ExchangeOutcome = await state.broker.exchange_user_token(
            entry, method, token, user_id=user.user_id, submitted_account=account
        )
    else:
        outcome = ExchangeOutcome(token, TokenOrigin(), account)

    # Шаг 5 R-U13: хранение. Прежний идентификатор читается до перезаписи строки (R-U15.3).
    previous = await state.broker.load_connection(user.user_id, alias)
    previous_token_id = await state.broker.issued_token_id(previous)
    connection = await state.broker.upsert_connection(
        user_id=user.user_id,
        alias=alias,
        status=STATUS_CONNECTED,
        preset=preset,
        groups=groups,
        provider_account=outcome.account,
        auth_method=method.id,
        clear_reason=True,
    )
    await state.broker.save_user_token(connection, outcome.token, origin=outcome.origin)
    await state.kv.delete_prefix(f"{TOOLS_CACHE_PREFIX}{alias}:")
    await state.db.audit(
        "connection_connected",
        user_id=user.user_id,
        alias=alias,
        details={
            "auth_method": method.id,
            "preset": preset,
            "groups": groups,
            "token_origin": outcome.origin.origin,
            "token_origin_reason": outcome.origin.reason,
        },
        ts=state.clock.now(),
    )

    session_expires_at = None
    if outcome.origin.issued and outcome.origin.issued_token_id:
        # Шаг 6 R-U13: уборка прежних выпущенных токенов; её исход на подключение не влияет.
        await state.broker.cleanup_issued_tokens(
            entry,
            method,
            user_id=user.user_id,
            access_token=outcome.token,
            issued_token_id=outcome.origin.issued_token_id,
            previous_token_id=previous_token_id,
        )
    elif method.expiry is not None:
        # Шаг 7 R-U13: срок годности присланного токена; неудача оставляет ``null`` (R-U18.4).
        expires_at = await state.broker.read_submitted_expiry(entry, method, outcome.token)
        await state.broker.store_submitted_expiry(connection, expires_at)
        session_expires_at = to_iso(expires_at)

    return JSONResponse(
        {
            "alias": alias,
            "status": STATUS_CONNECTED,
            "auth_method": method.id,
            "preset": preset,
            "groups": groups,
            "account": connection.provider_account,
            "updated_at": to_iso(connection.updated_at),
            "token_origin": outcome.origin.origin,
            "token_origin_reason": outcome.origin.reason,
            "session_expires_at": session_expires_at,
        }
    )


@router.put("/api/me/connections/{alias}/permissions")
async def api_set_permissions(
    alias: str,
    request: Request,
    user: Annotated[AuthUser, Depends(authenticate_key_or_session)],
) -> JSONResponse:
    """Смена прав подключения без переподключения (R-B7)."""
    state = request.app.state
    entry = _facade_entry(request, alias, user)
    payload = _json_object(await request.body() or b"{}")
    preset, groups = _requested_permissions(entry, payload)

    connection = await state.broker.load_connection(user.user_id, alias)
    if connection is None:
        raise HubError(404, "not_found", "Подключение не найдено")
    # R-U7: для подключений user_token расширение прав не требует повторной авторизации.
    needs_reauth = preset_requires_reauth(
        connection.preset, preset, user_token=entry.uses_user_token(connection.auth_method)
    )
    updated = await state.broker.upsert_connection(
        user_id=user.user_id,
        alias=alias,
        preset=preset,
        groups=groups,
        status=STATUS_NEEDS_REAUTH if needs_reauth else None,
        needs_reauth_reason=REASON_SCOPE_UPGRADE if needs_reauth else None,
    )
    await state.db.audit(
        "connection_permissions_changed",
        user_id=user.user_id,
        alias=alias,
        details={"preset": preset, "groups": groups, "needs_reauth": needs_reauth},
        ts=state.clock.now(),
    )
    body = {"alias": alias, "status": updated.status, "preset": preset, "groups": groups}
    if needs_reauth:
        body["message"] = REASON_SCOPE_UPGRADE
    return JSONResponse(body)


@router.delete("/api/me/connections/{alias}")
async def api_disconnect(
    alias: str,
    request: Request,
    user: Annotated[AuthUser, Depends(authenticate_key_or_session)],
) -> JSONResponse:
    """Отключение: отзыв токенов системы и всех клиентских токенов Hub (R-B8).

    R-U15.4: выпущенный Hub'ом постоянный токен отзывается до удаления строки; присланный
    токен не отзывается никогда, OAuth-``revoke_url`` для ``user_token`` не вызывается.
    """
    state = request.app.state
    entry = _facade_entry(request, alias, user)
    connection = await state.broker.load_connection(user.user_id, alias)
    if connection is None:
        raise HubError(404, "not_found", "Подключение не найдено")
    access_token = await state.broker.delete_tokens(connection, entry=entry)
    if access_token:
        # R-U5: для user_token revoke_url не вызывается никогда (решение 66).
        await state.broker.revoke(entry, access_token, auth_method=connection.auth_method)
    await state.oauth.revoke_connection_tokens(connection.id)
    await state.broker.upsert_connection(
        user_id=user.user_id,
        alias=alias,
        status=STATUS_NOT_CONNECTED,
        groups=[],
        clear_reason=True,
        clear_auth_method=True,
    )
    await state.db.audit(
        "connection_disconnected", user_id=user.user_id, alias=alias, ts=state.clock.now()
    )
    return JSONResponse({"alias": alias, "status": STATUS_NOT_CONNECTED})


@router.get("/remote-config")
async def remote_config(request: Request, user: Annotated[AuthUser, Depends(authenticate)]) -> JSONResponse:
    from hub.wellknown import build_remote_config

    connected = [c.alias for c in await _user_connections(request, user.user_id) if c.status == "connected"]
    return JSONResponse(build_remote_config(connected), headers={"Cache-Control": "private, no-store"})
