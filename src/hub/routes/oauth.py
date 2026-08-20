"""Эндпоинты Hub как authorization server и подключения целевых систем (R-O*, R-B2, R-W3)."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import uuid
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from hub.broker import STATUS_CONNECTED, ServerUnconfigured, UpstreamAuthFailed
from hub.catalog import ServerEntry
from hub.crypto import random_token
from hub.db import Consent, to_naive_utc
from hub.oauth import OAuthError, redirect_uri_matches
from hub.permissions import normalize_groups
from hub.web import current_session, group_definitions, html_error
from hub.websession import CSRF_FIELD, CSRF_HEADER, check_csrf

router = APIRouter(tags=["oauth"])
logger = logging.getLogger("hub.oauth.routes")

TX_PREFIX = "oauthtx:"
STATE_PREFIX = "oauthstate:"
RATE_REGISTER_PREFIX = "rl:register:"
RATE_TOKEN_PREFIX = "rl:token:"
RATE_WINDOW = 60.0
MAX_IP_LEN = 45  # максимальная длина текстового представления IPv6 (H5-2)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

MODE_MCP = "mcp"
MODE_CONNECT = "connect"


def _parse_forwarded_ip(value: str) -> str | None:
    """Левый элемент ``X-Forwarded-For`` — только если это корректный IP-адрес (H5-2).

    Типовой ingress (``proxy_add_x_forwarded_for``) заголовок не перезаписывает, а дополняет,
    поэтому левое значение задаёт клиент: без проверки в ключи ``rl:register:<ip>`` и
    ``rl:token:<client_id>:<ip>`` попадали бы произвольные строки — и обход лимита регистрации,
    и неограниченная кардинальность/длина ключей KV. Отбрасываются скобки IPv6 и суффикс
    ``:<port>``; значение длиннее максимального текстового представления IPv6 не разбирается.
    """
    if not value or len(value) > MAX_IP_LEN:
        return None
    if value.startswith("["):
        host, sep, _ = value[1:].partition("]")
        if not sep:
            return None
    elif value.count(":") == 1:
        host = value.rpartition(":")[0]
    else:
        host = value
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _client_ip(request: Request) -> str:
    """IP для ключей rate-limit: адрес соединения; ``X-Forwarded-For`` — только при
    ``HUB_TRUST_PROXY=true`` (за ingress иначе все запросы приходят с одного адреса и лимит
    становится общим на весь Hub; недоверенный заголовок клиент подделывает сам)."""
    if request.app.state.settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            parsed = _parse_forwarded_ip(first)
            if parsed is not None:
                return parsed
            # Само значение в лог не идёт (R-T4, R-K3) — только заголовок и его длина.
            logger.warning(
                "forwarded_for_rejected",
                extra={"header": "X-Forwarded-For", "value_length": len(first)},
            )
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _oauth_json(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers=dict(NO_STORE))


def _error_response(exc: OAuthError) -> JSONResponse:
    return _oauth_json(exc.body(), status_code=exc.status_code)


def _facade_entry(request: Request, alias: str) -> ServerEntry | None:
    entry = request.app.state.catalog.get(alias)
    if entry is None or entry.unconfigured or entry.model.mode != "facade":
        return None
    return entry


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not_found", "message": "Ресурс не найден"}, status_code=404)


# ---------------------------------------------------------------------------
# Метаданные (R-O1, R-O2)
# ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(request: Request) -> Response:
    state = request.app.state
    return JSONResponse(
        state.oauth.as_metadata(state.catalog), headers={"Cache-Control": "public, max-age=300"}
    )


@router.get("/.well-known/oauth-authorization-server/mcp/{alias}")
async def as_metadata_for_alias(alias: str, request: Request) -> Response:
    state = request.app.state
    if _facade_entry(request, alias) is None:
        return _not_found()
    return JSONResponse(
        state.oauth.as_metadata(state.catalog), headers={"Cache-Control": "public, max-age=300"}
    )


@router.get("/.well-known/oauth-protected-resource/mcp/{alias}")
async def resource_metadata(alias: str, request: Request) -> Response:
    entry = _facade_entry(request, alias)
    if entry is None:
        return _not_found()
    return JSONResponse(
        request.app.state.oauth.resource_metadata(entry),
        headers={"Cache-Control": "public, max-age=300"},
    )


# ---------------------------------------------------------------------------
# Динамическая регистрация (R-O3)
# ---------------------------------------------------------------------------


@router.post("/oauth/register")
async def register(request: Request) -> Response:
    state = request.app.state
    allowed, retry_after = await state.kv.rate_limit_hit(
        RATE_REGISTER_PREFIX + _client_ip(request),
        state.clock.time(),
        RATE_WINDOW,
        state.settings.rate_limit_register,
    )
    if not allowed:
        return JSONResponse(
            {"error": "rate_limited", "error_description": "Слишком много регистраций, повторите позже"},
            status_code=429,
            headers={**NO_STORE, "Retry-After": str(max(1, math.ceil(retry_after)))},
        )
    raw = await request.body()
    try:
        payload = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error_response(
            OAuthError(400, "invalid_client_metadata", "Тело запроса должно быть JSON-объектом")
        )
    try:
        body = await state.oauth.register_client(payload, ip=_client_ip(request))
    except OAuthError as exc:
        return _error_response(exc)
    return _oauth_json(body, status_code=201)


# ---------------------------------------------------------------------------
# Транзакции авторизации
# ---------------------------------------------------------------------------


async def _save_tx(request: Request, tx_id: str, tx: dict[str, Any]) -> None:
    state = request.app.state
    await state.kv.set(TX_PREFIX + tx_id, tx, ttl=state.settings.oauth_tx_ttl)


async def _load_tx(request: Request, tx_id: str | None) -> dict[str, Any] | None:
    if not tx_id:
        return None
    tx = await request.app.state.kv.get(TX_PREFIX + tx_id)
    return tx if isinstance(tx, dict) else None


def _redirect_error(
    redirect_uri: str, error: str, description: str, state_value: str | None
) -> RedirectResponse:
    params = {"error": error, "error_description": description}
    if state_value is not None:
        params["state"] = state_value
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


def _alias_from_resource(public_url: str, resource: str) -> str | None:
    prefix = f"{public_url}/mcp/"
    normalized = resource.rstrip("/")
    if not normalized.startswith(prefix):
        return None
    alias = normalized[len(prefix) :]
    return alias or None


async def _start_provider_oauth(
    request: Request, entry: ServerEntry, tx_id: str, tx: dict[str, Any]
) -> Response:
    """Запустить OAuth целевой системы в рамках транзакции (R-B2)."""
    state = request.app.state
    broker = state.broker
    provider_state = random_token()
    verifier = random_token()
    tx["provider_state"] = provider_state
    tx["provider_verifier"] = verifier
    tx["step"] = "provider"
    try:
        provider = broker.provider(entry)
        # R-U1/R-U4: способом, помеченным available: false, подключиться нельзя.
        if not provider.available:
            return html_error(
                state.templates,
                error="auth_method_unavailable",
                message=provider.unavailable_reason or "Этот способ подключения сейчас недоступен",
                status_code=409,
            )
        url = broker.authorize_url(
            entry, preset=tx["preset"], state=provider_state, code_verifier=verifier
        )
    except ServerUnconfigured as exc:
        logger.warning("provider_unconfigured", extra={"alias": entry.alias, "reason": str(exc)})
        return html_error(
            state.templates,
            error="server_unconfigured",
            message="Сервер не настроен: обратитесь к администратору Hub",
            status_code=503,
        )
    await _save_tx(request, tx_id, tx)
    await state.kv.set(
        STATE_PREFIX + provider_state, {"tx": tx_id}, ttl=state.settings.oauth_tx_ttl
    )
    return RedirectResponse(url, status_code=302)


async def _consent_page(request: Request, entry: ServerEntry, tx_id: str, tx: dict[str, Any]) -> Response:
    state = request.app.state
    info = await current_session(request)
    always, groups = group_definitions(entry)
    conn = await state.broker.load_connection(tx["user_id"], entry.alias)
    selected = set(conn.groups or []) if conn else set()
    return state.templates.page(
        "consent.html",
        server={"title": entry.model.title, "description": entry.model.description},
        client_name=tx.get("client_name") or tx["client_id"],
        scope=tx["scope"],
        preset=tx["preset"],
        tx=tx_id,
        csrf_token=info.csrf_token if info else "",
        always_groups=always,
        groups=groups,
        selected=selected,
    )


async def _issue_code_and_redirect(
    request: Request, entry: ServerEntry, tx_id: str, tx: dict[str, Any]
) -> Response:
    """Выдать код авторизации и вернуть клиента на его ``redirect_uri`` (R-O7)."""
    state = request.app.state
    conn = await state.broker.load_connection(tx["user_id"], entry.alias)
    code = await state.oauth.issue_code(
        client_id=tx["client_id"],
        user_id=tx["user_id"],
        alias=entry.alias,
        connection_id=conn.id if conn else None,
        redirect_uri=tx["redirect_uri"],
        code_challenge=tx["code_challenge"],
        scope=tx["scope"],
        resource=tx.get("resource"),
    )
    await state.kv.delete(TX_PREFIX + tx_id)
    params = {"code": code}
    if tx.get("state") is not None:
        params["state"] = tx["state"]
    separator = "&" if "?" in tx["redirect_uri"] else "?"
    return RedirectResponse(f"{tx['redirect_uri']}{separator}{urlencode(params)}", status_code=302)


async def _remembered_consent(request: Request, tx: dict[str, Any]) -> bool:
    """``HUB_CONSENT=remember``: согласие того же клиента на тот же scope уже дано (R-O6)."""
    state = request.app.state
    if state.settings.consent != "remember":
        return False
    await state.db.init()
    async with state.db.session() as session:
        row = (
            await session.execute(
                select(Consent)
                .where(
                    Consent.user_id == tx["user_id"],
                    Consent.client_id == tx["client_id"],
                    Consent.alias == tx["alias"],
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    return row is not None and row.scope == tx["scope"]


async def _continue_authorize(
    request: Request, entry: ServerEntry, tx_id: str, tx: dict[str, Any]
) -> Response:
    """Шаги после входа в веб: подключение → экран прав → код (R-O6)."""
    state = request.app.state
    conn = await state.broker.load_connection(tx["user_id"], entry.alias)
    if conn is None or conn.status != STATUS_CONNECTED:
        return await _start_provider_oauth(request, entry, tx_id, tx)
    if tx.get("mode") == MODE_CONNECT:
        await state.kv.delete(TX_PREFIX + tx_id)
        return RedirectResponse(f"/ui/servers/{entry.alias}", status_code=302)
    if await _remembered_consent(request, tx):
        return await _issue_code_and_redirect(request, entry, tx_id, tx)
    tx["step"] = "consent"
    await _save_tx(request, tx_id, tx)
    return await _consent_page(request, entry, tx_id, tx)


# ---------------------------------------------------------------------------
# /oauth/authorize (R-O4..R-O7)
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize")
async def authorize(request: Request) -> Response:
    state = request.app.state
    params = request.query_params
    templates = state.templates

    client = await state.oauth.get_client(params.get("client_id"))
    if client is None:
        return html_error(
            templates,
            error="invalid_client",
            message="Клиент не зарегистрирован в Hub",
        )
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri or not redirect_uri_matches(list(client.redirect_uris or []), redirect_uri):
        return html_error(
            templates,
            error="invalid_redirect_uri",
            message="Адрес возврата не зарегистрирован для этого клиента",
        )

    state_value = params.get("state")
    if params.get("response_type") != "code":
        return _redirect_error(
            redirect_uri, "unsupported_response_type", "Поддерживается только response_type=code",
            state_value,
        )
    code_challenge = params.get("code_challenge")
    if not code_challenge or params.get("code_challenge_method", "S256") != "S256":
        return _redirect_error(
            redirect_uri, "invalid_request", "Требуется PKCE с code_challenge_method=S256", state_value
        )

    resource = params.get("resource")
    scope = params.get("scope")
    alias_from_resource = None
    if resource:
        alias_from_resource = _alias_from_resource(state.settings.public_url, resource)
        if alias_from_resource is None or _facade_entry(request, alias_from_resource) is None:
            return _redirect_error(
                redirect_uri, "invalid_target", "Ресурс не обслуживается этим Hub", state_value
            )
    alias_from_scope = None
    if scope:
        alias_from_scope = scope.split(":")[0] if ":" in scope else None
        if not alias_from_scope or _facade_entry(request, alias_from_scope) is None:
            return _redirect_error(
                redirect_uri, "invalid_scope", "Запрошен неизвестный scope", state_value
            )
    if alias_from_resource and alias_from_scope and alias_from_resource != alias_from_scope:
        return _redirect_error(
            redirect_uri, "invalid_request", "resource и scope указывают на разные серверы", state_value
        )
    alias = alias_from_resource or alias_from_scope
    if alias is None:
        return _redirect_error(
            redirect_uri, "invalid_request", "Не указан ни resource, ни scope", state_value
        )
    entry = _facade_entry(request, alias)
    assert entry is not None
    final_scope = scope or f"{alias}:readonly"
    if final_scope not in (f"{alias}:readonly", f"{alias}:readwrite"):
        return _redirect_error(
            redirect_uri, "invalid_scope", "Запрошен неизвестный scope", state_value
        )

    info = await current_session(request)
    if info is None:
        target = str(request.url.path)
        query = request.url.query
        next_url = f"{target}?{query}" if query else target
        from urllib.parse import quote

        return RedirectResponse(f"/auth/login?next={quote(next_url, safe='')}", status_code=302)

    preset = "readwrite" if final_scope.endswith(":readwrite") else "readonly"
    tx_id = uuid.uuid4().hex
    tx = {
        "client_id": client.client_id,
        "client_name": client.client_name,
        "redirect_uri": redirect_uri,
        "state": state_value,
        "code_challenge": code_challenge,
        "scope": final_scope,
        "resource": resource,
        "alias": alias,
        "preset": preset,
        "user_id": info.user_id,
        "mode": MODE_MCP,
        "step": "start",
    }
    await _save_tx(request, tx_id, tx)
    return await _continue_authorize(request, entry, tx_id, tx)


@router.get("/oauth/connect/{alias}")
async def connect(alias: str, request: Request) -> Response:
    """«Переподключить» со страницы Hub: OAuth целевой системы без клиента MCP (R-B5, R-W5)."""
    state = request.app.state
    info = await current_session(request)
    if info is None:
        from hub.web import login_redirect

        return login_redirect(f"/oauth/connect/{alias}")
    entry = _facade_entry(request, alias)
    if entry is None:
        return html_error(
            state.templates, error="not_found", message="Сервер не найден", status_code=404
        )
    conn = await state.broker.load_connection(info.user_id, alias)
    preset = (conn.preset if conn else None) or "readonly"
    tx_id = uuid.uuid4().hex
    tx = {
        "client_id": "",
        "client_name": None,
        "redirect_uri": None,
        "state": None,
        "code_challenge": None,
        "scope": f"{alias}:{preset}",
        "resource": None,
        "alias": alias,
        "preset": preset,
        "user_id": info.user_id,
        "mode": MODE_CONNECT,
        "step": "start",
    }
    await _save_tx(request, tx_id, tx)
    return await _start_provider_oauth(request, entry, tx_id, tx)


# ---------------------------------------------------------------------------
# Экран прав (R-W3)
# ---------------------------------------------------------------------------


@router.post("/oauth/consent")
async def consent(request: Request) -> Response:
    state = request.app.state
    info = await current_session(request)
    form = await request.form()
    tx_id = str(form.get("tx") or "")
    tx = await _load_tx(request, tx_id)
    if tx is None:
        return html_error(
            state.templates,
            error="invalid_transaction",
            message="Сессия авторизации истекла, начните заново",
        )
    if info is None:
        return html_error(
            state.templates,
            error="forbidden",
            message="Требуется вход в Hub",
            status_code=403,
        )
    provided = request.headers.get(CSRF_HEADER) or form.get(CSRF_FIELD)
    check_csrf(info, str(provided) if provided is not None else None)
    if tx["user_id"] != info.user_id:
        return html_error(
            state.templates,
            error="forbidden",
            message="Транзакция принадлежит другому пользователю",
            status_code=403,
        )
    entry = _facade_entry(request, tx["alias"])
    if entry is None:
        return html_error(
            state.templates, error="not_found", message="Сервер не найден", status_code=404
        )
    if str(form.get("action") or "") == "deny":
        await state.kv.delete(TX_PREFIX + tx_id)
        return _redirect_error(
            tx["redirect_uri"], "access_denied", "Пользователь отклонил запрос", tx.get("state")
        )

    preset = str(form.get("preset") or "readonly")
    if preset not in ("readonly", "readwrite"):
        preset = "readonly"
    groups = normalize_groups(entry, preset, [str(g) for g in form.getlist("groups")])
    conn = await state.broker.load_connection(tx["user_id"], entry.alias)
    granted_preset = (conn.preset if conn else None) or "readonly"
    tx["preset"] = preset
    tx["groups"] = groups
    if preset == "readwrite" and granted_preset != "readwrite":
        # Нужны более широкие права целевой системы — повторный OAuth системы (R-B7).
        # После возврата флоу продолжается с шага «подключение есть» (_continue_authorize):
        # при HUB_CONSENT=always экран прав показывается ещё раз (R-O6.3), при remember с тем
        # же scope код выдаётся сразу.
        return await _start_provider_oauth(request, entry, tx_id, tx)

    await state.broker.upsert_connection(
        user_id=tx["user_id"], alias=entry.alias, preset=preset, groups=groups
    )
    await _remember_consent(request, tx, preset, groups)
    return await _issue_code_and_redirect(request, entry, tx_id, tx)


async def _remember_consent(
    request: Request, tx: dict[str, Any], preset: str, groups: list[str]
) -> None:
    state = request.app.state
    if not tx.get("client_id"):
        return
    await state.db.init()
    now = to_naive_utc(state.clock.now())
    async with state.db.session() as session, session.begin():
        row = (
            await session.execute(
                select(Consent)
                .where(
                    Consent.user_id == tx["user_id"],
                    Consent.client_id == tx["client_id"],
                    Consent.alias == tx["alias"],
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                Consent(
                    user_id=tx["user_id"],
                    client_id=tx["client_id"],
                    alias=tx["alias"],
                    scope=tx["scope"],
                    preset=preset,
                    groups=list(groups),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.scope = tx["scope"]
            row.preset = preset
            row.groups = list(groups)
            row.updated_at = now


# ---------------------------------------------------------------------------
# Возврат из целевой системы (R-B2, R-B3)
# ---------------------------------------------------------------------------


@router.get("/oauth/callback/{alias}")
async def provider_callback(alias: str, request: Request) -> Response:
    state = request.app.state
    templates = state.templates
    params = request.query_params
    provider_state = params.get("state")
    record = await state.kv.get(STATE_PREFIX + provider_state) if provider_state else None
    if not isinstance(record, dict):
        return html_error(
            templates, error="invalid_state", message="Сессия авторизации истекла, начните заново"
        )
    await state.kv.delete(STATE_PREFIX + str(provider_state))
    tx_id = str(record.get("tx") or "")
    tx = await _load_tx(request, tx_id)
    if tx is None or tx.get("alias") != alias:
        return html_error(
            templates, error="invalid_state", message="Сессия авторизации истекла, начните заново"
        )
    info = await current_session(request)
    if info is None or info.user_id != tx["user_id"]:
        return html_error(
            templates, error="invalid_state", message="Сессия авторизации принадлежит другому пользователю"
        )
    entry = _facade_entry(request, alias)
    if entry is None:
        return html_error(templates, error="not_found", message="Сервер не найден", status_code=404)

    if params.get("error"):
        await state.kv.delete(TX_PREFIX + tx_id)
        if tx.get("mode") == MODE_MCP and tx.get("redirect_uri"):
            return _redirect_error(
                tx["redirect_uri"], "access_denied", "Доступ в целевой системе не предоставлен",
                tx.get("state"),
            )
        return html_error(
            templates,
            error="access_denied",
            message="Целевая система отклонила запрос доступа",
            retry_url=f"/oauth/connect/{alias}",
        )
    code = params.get("code")
    if not code:
        return html_error(
            templates, error="invalid_request", message="Целевая система не передала код"
        )
    try:
        tokens = await state.broker.exchange_code(
            entry, code=code, code_verifier=tx.get("provider_verifier")
        )
    except (UpstreamAuthFailed, ServerUnconfigured) as exc:
        logger.warning("upstream_auth_failed", extra={"alias": alias, "reason": str(exc)})
        return html_error(
            templates,
            error="upstream_auth_failed",
            message="Не удалось получить доступ в целевой системе, попробуйте ещё раз",
            status_code=502,
            retry_url=f"/oauth/connect/{alias}",
        )
    # R-U8/решение 70: способ подключения фиксируется и для OAuth-подключений.
    auth_method_id = entry.model.auth.id if entry.model.auth is not None else None
    if auth_method_id is None:
        try:
            auth_method_id = state.broker.provider(entry).id
        except ServerUnconfigured:  # pragma: no cover - обмен кода уже прошёл через провайдера
            auth_method_id = None
    connection = await state.broker.upsert_connection(
        user_id=tx["user_id"],
        alias=alias,
        status=STATUS_CONNECTED,
        preset=tx["preset"],
        groups=list(tx.get("groups") or []),
        clear_reason=True,
        provider_account=tokens.account,
        auth_method=auth_method_id,
    )
    await state.broker.save_tokens(connection, tokens)
    await state.db.audit(
        "connection_connected",
        user_id=tx["user_id"],
        alias=alias,
        details={
            "auth_method": auth_method_id,
            "preset": tx["preset"],
            "groups": list(tx.get("groups") or []),
        },
        ts=state.clock.now(),
    )
    if tx.get("mode") == MODE_CONNECT:
        await state.kv.delete(TX_PREFIX + tx_id)
        return RedirectResponse(f"/ui/servers/{alias}", status_code=302)
    return await _continue_authorize(request, entry, tx_id, tx)


# ---------------------------------------------------------------------------
# /oauth/token и /oauth/revoke (R-O8, R-O10, R-O11)
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(request: Request) -> Response:
    state = request.app.state
    form = await request.form()
    client_id = str(form.get("client_id") or "")
    allowed, retry_after = await state.kv.rate_limit_hit(
        f"{RATE_TOKEN_PREFIX}{client_id}:{_client_ip(request)}",
        state.clock.time(),
        RATE_WINDOW,
        state.settings.rate_limit_token,
    )
    if not allowed:
        return JSONResponse(
            {"error": "rate_limited", "error_description": "Слишком много запросов, повторите позже"},
            status_code=429,
            headers={**NO_STORE, "Retry-After": str(max(1, math.ceil(retry_after)))},
        )
    grant_type = str(form.get("grant_type") or "")
    if not grant_type:
        return _error_response(
            OAuthError(400, "invalid_request", "Не указан обязательный параметр grant_type")
        )
    if grant_type not in ("authorization_code", "refresh_token"):
        return _error_response(
            OAuthError(400, "unsupported_grant_type", f"Грант {grant_type} не поддерживается")
        )
    client = await state.oauth.get_client(client_id)
    if client is None:
        return _error_response(OAuthError(401, "invalid_client", "Клиент не зарегистрирован"))
    try:
        if grant_type == "authorization_code":
            code = form.get("code")
            if not code:
                raise OAuthError(400, "invalid_request", "Не указан обязательный параметр code")
            body = await state.oauth.exchange_code(
                code=str(code),
                client_id=client_id,
                redirect_uri=str(form.get("redirect_uri")) if form.get("redirect_uri") else None,
                code_verifier=str(form.get("code_verifier")) if form.get("code_verifier") else None,
            )
        else:
            refresh_token = form.get("refresh_token")
            if not refresh_token:
                raise OAuthError(
                    400, "invalid_request", "Не указан обязательный параметр refresh_token"
                )
            body = await state.oauth.refresh_tokens(
                refresh_token=str(refresh_token),
                client_id=client_id,
                scope=str(form.get("scope")) if form.get("scope") else None,
            )
    except OAuthError as exc:
        return _error_response(exc)
    return _oauth_json(body)


@router.post("/oauth/revoke")
async def revoke(request: Request) -> Response:
    state = request.app.state
    form = await request.form()
    raw = form.get("token")
    if not raw:
        return _error_response(
            OAuthError(400, "invalid_request", "Не указан обязательный параметр token")
        )
    revoked = await state.oauth.revoke_token(str(raw))
    if revoked:
        await state.db.audit(
            "oauth_token_revoked",
            details={"client_id": str(form.get("client_id") or "") or None},
            ts=state.clock.now(),
        )
    return _oauth_json({})


__all__ = ["router"]
