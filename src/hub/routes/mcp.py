"""``POST|GET|DELETE /mcp/{alias}`` — MCP-proxy Hub (R-P1..R-P11)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from hub.broker import (
    REASON_REFRESH_FAILED,
    REASON_TOKEN_REJECTED,
    STATUS_CONNECTED,
    STATUS_NEEDS_REAUTH,
    NeedsReauth,
    ServerUnconfigured,
)
from hub.catalog import ServerEntry
from hub.oauth import OAuthError, TokenClaims
from hub.permissions import ToolFilter, tool_filter
from hub.proxy import (
    CODE_CONNECTION,
    CODE_RATE_LIMIT,
    CODE_SESSION,
    CODE_TOOL_FORBIDDEN,
    CODE_UPSTREAM,
    MSG_NOT_CONNECTED,
    MSG_RATE_LIMIT,
    MSG_SESSION,
    MSG_UPSTREAM,
    MSG_UPSTREAM_OPEN,
    RATE_MCP_PREFIX,
    RATE_WINDOW,
    SESSION_HEADER,
    SSE_CONTENT_TYPE,
    McpSession,
    ProxyError,
    SseFilter,
    filter_tools_payload,
    first_request_id,
    iter_upstream_body,
    jsonrpc_error,
    jsonrpc_methods,
    parse_jsonrpc,
    tools_cache_key,
    upstream_headers,
)

router = APIRouter(tags=["mcp"])
logger = logging.getLogger("hub.mcp")

REASON_NOT_CONNECTED = "not_connected"
REASON_NEEDS_REAUTH = "needs_reauth"
MSG_NEEDS_REAUTH = "Подключение требует повторной авторизации в целевой системе"


@dataclass
class ProxyContext:
    """Всё, что нужно для проксирования одного запроса."""

    request: Request
    entry: ServerEntry
    claims: TokenClaims
    state_record: dict[str, Any]
    tools: ToolFilter
    body: bytes = b""
    payload: Any = None
    request_id: Any = None
    jsonrpc: bool = False
    breaker_was_open: bool = False
    started: float = field(default_factory=time.perf_counter)

    @property
    def alias(self) -> str:
        return self.entry.alias

    @property
    def user_id(self) -> str:
        return self.claims.subject

    @property
    def connection_id(self) -> int | None:
        cid = self.state_record.get("connection_id")
        return int(cid) if isinstance(cid, int) else None


def _hint_url(request: Request, alias: str) -> str:
    return f"{request.app.state.settings.public_url}/ui/servers/{alias}"


def _unauthorized(request: Request, alias: str, *, error: str = "invalid_token") -> JSONResponse:
    oauth = request.app.state.oauth
    return JSONResponse(
        {
            "error": "unauthorized",
            "message": "Требуется действующий токен доступа Hub",
            "hint": "пройдите авторизацию MCP-клиента заново",
        },
        status_code=401,
        headers={"WWW-Authenticate": oauth.www_authenticate(alias, error=error)},
    )


def _not_found_json() -> JSONResponse:
    return JSONResponse({"error": "not_found", "message": "Ресурс не найден"}, status_code=404)


def _proxy_error_response(request: Request, alias: str, exc: ProxyError, ctx_jsonrpc: bool,
                          request_id: Any) -> Response:
    """Ошибка Hub на /mcp/{alias}: JSON-RPC для тела JSON-RPC, иначе ``{error, message, hint_url}``."""
    if ctx_jsonrpc:
        body: Any = jsonrpc_error(exc.code, exc.message, request_id=request_id, data=exc.data)
    else:
        body = {
            "error": _ERROR_NAMES.get(exc.code, "error"),
            "message": exc.message,
            "hint_url": exc.data.get("hint_url", _hint_url(request, alias)),
        }
        # Пояснительные поля (reason, retry_after) сохраняются и в форме без JSON-RPC (R-P11).
        for key, value in exc.data.items():
            if key != "hint_url":
                body.setdefault(key, value)
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


_ERROR_NAMES = {
    CODE_SESSION: "session_not_found",
    CODE_TOOL_FORBIDDEN: "forbidden_tool",
    CODE_CONNECTION: "not_connected",
    CODE_RATE_LIMIT: "rate_limited",
    CODE_UPSTREAM: "upstream_unavailable",
}


def _facade_entry(request: Request, alias: str) -> ServerEntry | None:
    entry = request.app.state.catalog.get(alias)
    if entry is None or entry.unconfigured or entry.model.mode != "facade":
        return None
    return entry


async def _read_body(request: Request, limit: int) -> bytes:
    """Тело с ограничением ``HUB_MAX_BODY_BYTES`` (R-P9): превышение → ``ProxyError`` 413."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ProxyError(status_code=413, code=CODE_UPSTREAM, message="Тело запроса слишком велико")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ProxyError(
                status_code=413, code=CODE_UPSTREAM, message="Тело запроса слишком велико"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _payload_too_large() -> JSONResponse:
    return JSONResponse(
        {"error": "payload_too_large", "message": "Тело запроса слишком велико"}, status_code=413
    )


async def _prepare(request: Request, alias: str, *, read_body: bool) -> ProxyContext | Response:
    """Порядок проверок R-P1: alias → токен → лимит тела → rate-limit → подключение."""
    state = request.app.state
    entry = _facade_entry(request, alias)
    if entry is None:
        return _not_found_json()

    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return _unauthorized(request, alias)
    try:
        claims = await state.oauth.verify_access_token(token.strip(), alias=alias)
    except OAuthError as exc:
        if exc.status_code == 403:
            return JSONResponse(
                {"error": "forbidden", "message": "Токен выдан для другого сервера"},
                status_code=403,
            )
        return _unauthorized(request, alias)

    body = b""
    payload: Any = None
    if read_body:
        try:
            body = await _read_body(request, state.settings.max_body_bytes)
        except ProxyError:
            return _payload_too_large()
        payload = parse_jsonrpc(body) if body else None

    request_id = first_request_id(payload)
    is_jsonrpc = payload is not None

    allowed, retry_after = await state.kv.rate_limit_hit(
        f"{RATE_MCP_PREFIX}{claims.subject}:{alias}",
        state.clock.time(),
        RATE_WINDOW,
        state.settings.rate_limit_mcp,
    )
    if not allowed:
        seconds = max(1, math.ceil(retry_after))
        limited = ProxyError(
            status_code=429,
            code=CODE_RATE_LIMIT,
            message=MSG_RATE_LIMIT,
            data={"retry_after": seconds, "hint_url": _hint_url(request, alias)},
            headers={"Retry-After": str(seconds)},
        )
        return _proxy_error_response(request, alias, limited, is_jsonrpc, request_id)

    record = await state.broker.connection_state(claims.subject, alias)
    if (
        record is not None
        and claims.connection_id is not None
        and record.get("connection_id") != claims.connection_id
    ):
        return _unauthorized(request, alias)
    if record is None or record.get("status") != STATUS_CONNECTED:
        reason = (
            REASON_NEEDS_REAUTH
            if record is not None and record.get("status") == STATUS_NEEDS_REAUTH
            else REASON_NOT_CONNECTED
        )
        message = MSG_NEEDS_REAUTH if reason == REASON_NEEDS_REAUTH else MSG_NOT_CONNECTED
        not_connected = ProxyError(
            status_code=200,
            code=CODE_CONNECTION,
            message=message,
            data={"reason": reason, "hint_url": _hint_url(request, alias)},
        )
        return _proxy_error_response(request, alias, not_connected, is_jsonrpc, request_id)

    tools = tool_filter(
        entry, str(record.get("preset") or "readonly"), list(record.get("groups") or [])
    )
    return ProxyContext(
        request=request,
        entry=entry,
        claims=claims,
        state_record=record,
        tools=tools,
        body=body,
        payload=payload,
        request_id=request_id,
        jsonrpc=is_jsonrpc,
    )


async def _check_breaker(ctx: ProxyContext) -> None:
    state = ctx.request.app.state
    decision = await state.breaker.check(ctx.alias)
    if decision.retry_after is None:
        # Признак «выключатель был открыт» несёт запрос-проба: только он снимает право на пробу
        # по успешному завершению (H5-3), в закрытом состоянии ключа пробы нет.
        ctx.breaker_was_open = decision.was_open
        return
    seconds = max(1, math.ceil(decision.retry_after))
    state.metrics.counter(
        "hub_upstream_errors_total",
        "Ошибки upstream MCP.",
        {"alias": ctx.alias, "kind": "circuit_open"},
    )
    raise ProxyError(
        status_code=503,
        code=CODE_UPSTREAM,
        message=MSG_UPSTREAM_OPEN,
        data={"hint_url": _hint_url(ctx.request, ctx.alias), "reason": "upstream_unavailable"},
        headers={"Retry-After": str(seconds)},
    )


async def _access_token(ctx: ProxyContext, *, force_refresh: bool = False) -> str:
    """Токен целевой системы: сначала из KV-кэша подключения (без обращения к БД, R-O12)."""
    state = ctx.request.app.state
    if not force_refresh:
        cached = await state.broker.cached_access_token(ctx.state_record)
        if cached:
            return cached
    connection = await state.broker.load_connection(ctx.user_id, ctx.alias)
    if connection is None:
        raise ProxyError(
            status_code=200,
            code=CODE_CONNECTION,
            message=MSG_NOT_CONNECTED,
            data={"reason": REASON_NOT_CONNECTED, "hint_url": _hint_url(ctx.request, ctx.alias)},
        )
    try:
        return await state.broker.access_token(connection, force_refresh=force_refresh)
    except (NeedsReauth, ServerUnconfigured) as exc:
        raise ProxyError(
            status_code=200,
            code=CODE_CONNECTION,
            message=MSG_NEEDS_REAUTH,
            data={"reason": REASON_NEEDS_REAUTH, "hint_url": _hint_url(ctx.request, ctx.alias)},
        ) from exc


def _headers_for(ctx: ProxyContext, access_token: str, upstream_session_id: str | None) -> dict[str, str]:
    return upstream_headers(
        ctx.entry,
        client_headers=httpx.Headers(ctx.request.headers.raw),
        access_token=access_token,
        preset=str(ctx.state_record.get("preset") or "readonly"),
        groups=list(ctx.state_record.get("groups") or []),
        environ=ctx.request.app.state.catalog_env,
        upstream_session_id=upstream_session_id,
    )


def _upstream_error(ctx: ProxyContext, kind: str) -> ProxyError:
    ctx.request.app.state.metrics.counter(
        "hub_upstream_errors_total", "Ошибки upstream MCP.", {"alias": ctx.alias, "kind": kind}
    )
    return ProxyError(
        status_code=502,
        code=CODE_UPSTREAM,
        message=MSG_UPSTREAM,
        data={"hint_url": _hint_url(ctx.request, ctx.alias), "reason": "upstream_unavailable"},
    )


async def _open_upstream(
    ctx: ProxyContext, method: str, headers: dict[str, str], content: bytes | None
) -> httpx.Response:
    state = ctx.request.app.state
    url = ctx.entry.model.upstream_url or ""
    try:
        return await state.upstream.open(method, url, headers=headers, content=content)
    except httpx.TimeoutException as exc:
        await state.breaker.record_failure(ctx.alias)
        logger.info("upstream_timeout", extra={"alias": ctx.alias})
        raise _upstream_error(ctx, "timeout") from exc
    except httpx.HTTPError as exc:
        await state.breaker.record_failure(ctx.alias)
        logger.info("upstream_network_error", extra={"alias": ctx.alias})
        raise _upstream_error(ctx, "network") from exc


async def _recreate_upstream_session(ctx: ProxyContext, session: McpSession, access_token: str) -> None:
    """Прозрачное пересоздание upstream-сессии (R-P5)."""
    state = ctx.request.app.state
    old_session_id = session.upstream_session_id
    if old_session_id:
        try:
            await state.upstream.request(
                "DELETE",
                ctx.entry.model.upstream_url or "",
                headers=_headers_for(ctx, access_token, old_session_id),
                content=None,
            )
        except httpx.HTTPError:
            logger.debug("upstream_delete_failed", extra={"alias": ctx.alias})
    init_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": f"hub-reinit-{uuid.uuid4().hex}",
            "method": "initialize",
            "params": {
                "protocolVersion": session.protocol_version or "2025-06-18",
                "capabilities": {},
                "clientInfo": session.client_info or {"name": "opencode-mcp-hub", "version": "1"},
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = _headers_for(ctx, access_token, None)
    headers["content-type"] = "application/json"
    headers.setdefault("accept", "application/json, text/event-stream")
    response = await _open_upstream(ctx, "POST", headers, init_body)
    await response.aread()
    await response.aclose()
    if response.status_code >= 400:
        await state.breaker.record_failure(ctx.alias)
        raise _upstream_error(ctx, "http_5xx" if response.status_code >= 500 else "network")
    new_session_id = response.headers.get(SESSION_HEADER)
    session.upstream_session_id = new_session_id
    notify = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, ensure_ascii=False
    ).encode("utf-8")
    notify_headers = _headers_for(ctx, access_token, new_session_id)
    notify_headers["content-type"] = "application/json"
    notify_headers.setdefault("accept", "application/json, text/event-stream")
    try:
        await state.upstream.request(
            "POST", ctx.entry.model.upstream_url or "", headers=notify_headers, content=notify
        )
    except httpx.HTTPError:
        logger.debug("upstream_notify_failed", extra={"alias": ctx.alias})
    await state.sessions.touch(session)
    logger.info(
        "upstream_session_recreated", extra={"alias": ctx.alias, "session": session.client_session_id}
    )


async def _send(
    ctx: ProxyContext, method: str, content: bytes | None, session: McpSession | None
) -> httpx.Response:
    """Запрос к upstream с однократным повтором после обновления токена или пересоздания сессии."""
    state = ctx.request.app.state
    await _check_breaker(ctx)
    access_token = await _access_token(ctx)
    token_refreshed = False
    session_recreated = False
    if session is not None and session.upstream_session_id and state.sessions.is_idle(session):
        await _recreate_upstream_session(ctx, session, access_token)
        session_recreated = True
    while True:
        headers = _headers_for(
            ctx, access_token, session.upstream_session_id if session else None
        )
        response = await _open_upstream(ctx, method, headers, content)
        status = response.status_code
        # R-U6: у подключения user_token обновлять нечего — 401 сразу переводит в needs_reauth,
        # без force_refresh и без повтора запроса.
        user_token = ctx.entry.uses_user_token(ctx.state_record.get("auth_method"))
        if status == 401 and not token_refreshed and not user_token:
            await response.aclose()
            token_refreshed = True
            access_token = await _access_token(ctx, force_refresh=True)
            continue
        if status == 401:
            await response.aclose()
            connection = await state.broker.load_connection(ctx.user_id, ctx.alias)
            if connection is not None:
                await state.broker.mark_needs_reauth(
                    connection, REASON_TOKEN_REJECTED if user_token else REASON_REFRESH_FAILED
                )
            raise ProxyError(
                status_code=200,
                code=CODE_CONNECTION,
                message=MSG_NEEDS_REAUTH,
                data={"reason": REASON_NEEDS_REAUTH, "hint_url": _hint_url(ctx.request, ctx.alias)},
            )
        if status == 404 and session is not None and session.upstream_session_id:
            await response.aclose()
            if session_recreated:
                await state.breaker.record_failure(ctx.alias)
                raise _upstream_error(ctx, "network")
            session_recreated = True
            await _recreate_upstream_session(ctx, session, access_token)
            continue
        if status >= 500:
            await response.aclose()
            await state.breaker.record_failure(ctx.alias)
            raise _upstream_error(ctx, "http_5xx")
        await state.breaker.record_success(ctx.alias, was_open=ctx.breaker_was_open)
        if session is not None:
            await state.sessions.touch(session)
        return response


def _passthrough_headers(response: httpx.Response, client_session_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    cache_control = response.headers.get("cache-control")
    if cache_control:
        headers["Cache-Control"] = cache_control
    if client_session_id:
        headers["Mcp-Session-Id"] = client_session_id
    return headers


async def _stream_to_client(
    ctx: ProxyContext, response: httpx.Response, client_session_id: str | None
) -> Response:
    """SSE-ответ upstream отдаётся клиенту потоково, с фильтрацией инструментов (R-P3, R-P8)."""
    state = ctx.request.app.state
    if not await state.sse_counter.acquire(ctx.user_id):
        await response.aclose()
        raise ProxyError(
            status_code=429,
            code=CODE_RATE_LIMIT,
            message=MSG_RATE_LIMIT,
            data={"reason": "too_many_streams", "hint_url": _hint_url(ctx.request, ctx.alias)},
            headers={"Retry-After": "1"},
        )
    sse_filter = SseFilter(ctx.tools)
    user_id = ctx.user_id
    alias = ctx.alias

    idle_timeout = state.settings.upstream_sse_idle_timeout

    async def iterator() -> Any:
        chunks = iter_upstream_body(response).__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), timeout=idle_timeout)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    # R-P3: бездействие внутри установленного потока — поток закрывается.
                    logger.info("upstream_timeout", extra={"alias": alias, "phase": "sse_idle"})
                    break
                out = sse_filter.feed(chunk)
                if out:
                    yield out
            tail = sse_filter.flush()
            if tail:
                yield tail
        finally:
            await response.aclose()
            await state.sse_counter.release(user_id)

    return StreamingResponse(
        iterator(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", SSE_CONTENT_TYPE),
        headers=_passthrough_headers(response, client_session_id),
    )


def _record_metrics(ctx: ProxyContext, method: str, status: int) -> None:
    state = ctx.request.app.state
    state.metrics.counter(
        "hub_mcp_requests_total",
        "Запросы к MCP-proxy Hub.",
        {"alias": ctx.alias, "method": method, "status": str(status)},
    )
    state.metrics.histogram(
        "hub_mcp_request_duration_seconds",
        "Длительность запросов к MCP-proxy Hub.",
        {"alias": ctx.alias},
        time.perf_counter() - ctx.started,
    )


def _rpc_method(ctx: ProxyContext, default: str) -> str:
    methods = jsonrpc_methods(ctx.payload)
    return methods[0][1] if methods else default


async def _resolve_session(ctx: ProxyContext) -> McpSession | None:
    """Найти виртуальную сессию по клиентскому ``Mcp-Session-Id`` (R-P4)."""
    client_session_id = ctx.request.headers.get(SESSION_HEADER)
    if not client_session_id:
        return None
    session = await ctx.request.app.state.sessions.get(
        client_session_id, user_id=ctx.user_id, alias=ctx.alias
    )
    if session is None:
        raise ProxyError(
            status_code=404,
            code=CODE_SESSION,
            message=MSG_SESSION,
            data={"hint_url": _hint_url(ctx.request, ctx.alias)},
        )
    return session


def _check_tools(ctx: ProxyContext) -> None:
    """``tools/call`` скрытого инструмента отклоняется без обращения к upstream (R-P8)."""
    for _request_id, method, params in jsonrpc_methods(ctx.payload):
        if method != "tools/call":
            continue
        name = params.get("name")
        if not isinstance(name, str) or ctx.tools.allows(name):
            continue
        raise ProxyError(
            status_code=200,
            code=CODE_TOOL_FORBIDDEN,
            message=f"Инструмент {name} недоступен с текущими правами",
            data={"tool": name, "hint_url": _hint_url(ctx.request, ctx.alias)},
        )


def _cacheable_tools_list(ctx: ProxyContext) -> bool:
    if not isinstance(ctx.payload, dict):
        return False
    if ctx.payload.get("method") != "tools/list":
        return False
    params = ctx.payload.get("params")
    return not (isinstance(params, dict) and params.get("cursor"))


def _forbidden_tool_id(ctx: ProxyContext) -> Any:
    for request_id, method, params in jsonrpc_methods(ctx.payload):
        if method == "tools/call":
            name = params.get("name")
            if isinstance(name, str) and not ctx.tools.allows(name):
                return request_id
    return ctx.request_id


@router.post("/mcp/{alias}")
async def mcp_post(alias: str, request: Request) -> Response:
    prepared = await _prepare(request, alias, read_body=True)
    if isinstance(prepared, Response):
        return prepared
    ctx = prepared
    state = request.app.state
    method_label = _rpc_method(ctx, "POST")
    try:
        session = await _resolve_session(ctx)
        _check_tools(ctx)

        cache_key = None
        if _cacheable_tools_list(ctx):
            cache_key = tools_cache_key(
                ctx.alias,
                state.catalog.version,
                str(ctx.state_record.get("preset") or "readonly"),
                list(ctx.state_record.get("groups") or []),
            )
            cached = await state.kv.get(cache_key)
            if isinstance(cached, dict):
                body = filter_tools_payload({**cached, "id": ctx.request_id}, ctx.tools)
                _record_metrics(ctx, method_label, 200)
                headers = (
                    {"Mcp-Session-Id": session.client_session_id} if session is not None else {}
                )
                return JSONResponse(body, headers=headers)

        response = await _send(ctx, "POST", ctx.body, session)
        client_session_id = session.client_session_id if session else None
        if session is None:
            new_session_id = response.headers.get(SESSION_HEADER)
            if new_session_id:
                params = jsonrpc_methods(ctx.payload)
                init = next((p for _, m, p in params if m == "initialize"), {})
                created = await state.sessions.create(
                    user_id=ctx.user_id,
                    alias=ctx.alias,
                    connection_id=ctx.connection_id,
                    upstream_session_id=new_session_id,
                    protocol_version=str(init.get("protocolVersion") or "") or None,
                    client_info=init.get("clientInfo")
                    if isinstance(init.get("clientInfo"), dict)
                    else None,
                )
                client_session_id = created.client_session_id

        content_type = response.headers.get("content-type", "")
        if content_type.startswith(SSE_CONTENT_TYPE):
            _record_metrics(ctx, method_label, response.status_code)
            return await _stream_to_client(ctx, response, client_session_id)

        raw = await response.aread()
        await response.aclose()
        payload = parse_jsonrpc(raw)
        if cache_key and response.status_code == 200 and isinstance(payload, dict):
            await state.kv.set(cache_key, payload, ttl=state.settings.tools_cache_ttl)
        _record_metrics(ctx, method_label, response.status_code)
        headers = _passthrough_headers(response, client_session_id)
        if payload is None:
            return Response(
                content=raw,
                status_code=response.status_code,
                media_type=content_type or "application/json",
                headers=headers,
            )
        return JSONResponse(
            filter_tools_payload(payload, ctx.tools),
            status_code=response.status_code,
            headers=headers,
        )
    except ProxyError as exc:
        request_id = _forbidden_tool_id(ctx) if exc.code == CODE_TOOL_FORBIDDEN else ctx.request_id
        _record_metrics(ctx, method_label, exc.status_code)
        return _proxy_error_response(request, alias, exc, ctx.jsonrpc, request_id)


@router.get("/mcp/{alias}")
async def mcp_get(alias: str, request: Request) -> Response:
    prepared = await _prepare(request, alias, read_body=False)
    if isinstance(prepared, Response):
        return prepared
    ctx = prepared
    try:
        session = await _resolve_session(ctx)
        response = await _send(ctx, "GET", None, session)
        client_session_id = session.client_session_id if session else None
        content_type = response.headers.get("content-type", "")
        if content_type.startswith(SSE_CONTENT_TYPE):
            _record_metrics(ctx, "GET", response.status_code)
            return await _stream_to_client(ctx, response, client_session_id)
        raw = await response.aread()
        await response.aclose()
        _record_metrics(ctx, "GET", response.status_code)
        return Response(
            content=raw,
            status_code=response.status_code,
            media_type=content_type or "application/json",
            headers=_passthrough_headers(response, client_session_id),
        )
    except ProxyError as exc:
        _record_metrics(ctx, "GET", exc.status_code)
        return _proxy_error_response(request, alias, exc, False, None)


@router.delete("/mcp/{alias}")
async def mcp_delete(alias: str, request: Request) -> Response:
    prepared = await _prepare(request, alias, read_body=False)
    if isinstance(prepared, Response):
        return prepared
    ctx = prepared
    state = request.app.state
    try:
        session = await _resolve_session(ctx)
        if session is None:
            raise ProxyError(
                status_code=404,
                code=CODE_SESSION,
                message=MSG_SESSION,
                data={"hint_url": _hint_url(request, alias)},
            )
        access_token = await _access_token(ctx)
        status_code = 204
        try:
            response = await state.upstream.request(
                "DELETE",
                ctx.entry.model.upstream_url or "",
                headers=_headers_for(ctx, access_token, session.upstream_session_id),
                content=None,
            )
            status_code = response.status_code if response.status_code < 400 else 204
        except httpx.HTTPError:
            logger.info("upstream_delete_failed", extra={"alias": ctx.alias})
        await state.sessions.delete(session)
        _record_metrics(ctx, "DELETE", status_code)
        return Response(status_code=status_code)
    except ProxyError as exc:
        _record_metrics(ctx, "DELETE", exc.status_code)
        return _proxy_error_response(request, alias, exc, False, None)


__all__ = ["router"]
