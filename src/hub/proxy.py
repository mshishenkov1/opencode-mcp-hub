"""MCP-proxy: потоковое проксирование streamable-http на upstream каталога (R-P1..R-P11).

Сессии виртуализируются (клиент видит идентификатор Hub), заголовки подставляются из каталога,
ответ отдаётся потоково без буферизации. HTTP-клиент инжектируется (``app.state.upstream_client``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from hub.catalog import EnvRef, ServerEntry
from hub.clock import Clock
from hub.crypto import sha256_hex
from hub.kv import KeyValueStore
from hub.metrics import Metrics
from hub.permissions import ToolFilter, groups_header
from hub.settings import Settings

logger = logging.getLogger("hub.proxy")

SESSION_PREFIX = "mcpsess:"
SESSION_COUNTER_PREFIX = "mcpsessn:"
TOOLS_CACHE_PREFIX = "toolscache:"
CB_PREFIX = "cb:"
SSE_PREFIX = "sse:"
RATE_MCP_PREFIX = "rl:mcp:"
RATE_WINDOW = 60.0

SESSION_HEADER = "mcp-session-id"
FORWARDED_HEADERS = (
    "accept",
    "content-type",
    "mcp-protocol-version",
    "last-event-id",
    "accept-encoding",
)
SSE_CONTENT_TYPE = "text/event-stream"

# Коды JSON-RPC ошибок Hub (R-P11)
CODE_SESSION = -32000
CODE_TOOL_FORBIDDEN = -32001
CODE_CONNECTION = -32002
CODE_RATE_LIMIT = -32003
CODE_UPSTREAM = -32004

MSG_SESSION = "Сессия не найдена, выполните initialize"
MSG_RATE_LIMIT = "Слишком много запросов, повторите позже"
MSG_UPSTREAM = "Сервер MCP временно недоступен, повторите позже"
MSG_UPSTREAM_OPEN = "Сервер MCP временно недоступен (сработала защита от перегрузки)"
MSG_NOT_CONNECTED = "Нет подключения к целевой системе — подключитесь в Hub"


class ProxyError(Exception):
    """Ошибка проксирования, отдаваемая клиенту как JSON-RPC error (R-P11)."""

    def __init__(
        self,
        *,
        status_code: int,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data or {}
        self.headers = headers or {}


@dataclass
class McpSession:
    """Виртуальная сессия клиента (запись ``mcpsess:*`` в KV, R-P4)."""

    client_session_id: str
    user_id: str
    alias: str
    connection_id: int | None
    upstream_session_id: str | None
    protocol_version: str | None
    client_info: dict[str, Any] | None
    upstream_last_used_at: float

    def to_record(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "alias": self.alias,
            "connection_id": self.connection_id,
            "upstream_session_id": self.upstream_session_id,
            "protocol_version": self.protocol_version,
            "client_info": self.client_info,
            "upstream_last_used_at": self.upstream_last_used_at,
        }


def jsonrpc_error(
    code: int, message: str, *, request_id: Any = None, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_jsonrpc(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def first_request_id(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("id")
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and "id" in item:
                return item["id"]
    return None


def jsonrpc_methods(payload: Any) -> list[tuple[Any, str, dict[str, Any]]]:
    """``(id, method, params)`` для одиночного запроса и каждого элемента batch."""
    items = payload if isinstance(payload, list) else [payload]
    result: list[tuple[Any, str, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        if not isinstance(method, str):
            continue
        params = item.get("params")
        result.append((item.get("id"), method, params if isinstance(params, dict) else {}))
    return result


def filter_tools_payload(payload: Any, tools: ToolFilter) -> Any:
    """Убрать недоступные инструменты из ответа ``tools/list`` (R-P8)."""
    if isinstance(payload, list):
        return [filter_tools_payload(item, tools) for item in payload]
    if not isinstance(payload, dict):
        return payload
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        filtered = dict(result)
        filtered["tools"] = tools.filter_tools(result["tools"])
        return {**payload, "result": filtered}
    return payload


class CircuitBreaker:
    """Выключатель на alias, состояние — в KV (R-P10)."""

    def __init__(self, kv: KeyValueStore, clock: Clock, settings: Settings) -> None:
        self.kv = kv
        self.clock = clock
        self.settings = settings

    def _key(self, alias: str) -> str:
        return CB_PREFIX + alias

    async def state(self, alias: str) -> dict[str, Any]:
        record = await self.kv.get(self._key(alias))
        if not isinstance(record, dict):
            return {"failures": 0, "open_until": 0.0}
        return record

    async def check(self, alias: str) -> float | None:
        """``None`` — можно идти на upstream; иначе секунды до следующей попытки."""
        record = await self.state(alias)
        open_until = float(record.get("open_until") or 0.0)
        now = self.clock.time()
        if open_until > now:
            return open_until - now
        return None

    async def record_success(self, alias: str) -> None:
        await self.kv.delete(self._key(alias))

    async def record_failure(self, alias: str) -> None:
        record = await self.state(alias)
        failures = int(record.get("failures") or 0) + 1
        open_until = 0.0
        if failures >= self.settings.cb_failures:
            open_until = self.clock.time() + self.settings.cb_reset
            failures = 0
        await self.kv.set(
            self._key(alias),
            {"failures": failures, "open_until": open_until},
            ttl=self.settings.cb_reset * 2,
        )


class SessionStore:
    """Виртуальные сессии MCP в KV (R-P4, R-P5)."""

    def __init__(self, kv: KeyValueStore, clock: Clock, settings: Settings) -> None:
        self.kv = kv
        self.clock = clock
        self.settings = settings

    async def create(
        self,
        *,
        user_id: str,
        alias: str,
        connection_id: int | None,
        upstream_session_id: str | None,
        protocol_version: str | None,
        client_info: dict[str, Any] | None,
    ) -> McpSession:
        session = McpSession(
            client_session_id=uuid.uuid4().hex,
            user_id=user_id,
            alias=alias,
            connection_id=connection_id,
            upstream_session_id=upstream_session_id,
            protocol_version=protocol_version,
            client_info=client_info,
            upstream_last_used_at=self.clock.time(),
        )
        await self.kv.set(
            SESSION_PREFIX + session.client_session_id,
            session.to_record(),
            ttl=self.settings.client_session_ttl,
        )
        await self.kv.incr(
            SESSION_COUNTER_PREFIX + alias, 1, ttl=self.settings.client_session_ttl
        )
        return session

    async def get(self, client_session_id: str, *, user_id: str, alias: str) -> McpSession | None:
        record = await self.kv.get(SESSION_PREFIX + client_session_id)
        if not isinstance(record, dict):
            return None
        if record.get("user_id") != user_id or record.get("alias") != alias:
            return None
        return McpSession(
            client_session_id=client_session_id,
            user_id=str(record["user_id"]),
            alias=str(record["alias"]),
            connection_id=record.get("connection_id"),
            upstream_session_id=record.get("upstream_session_id"),
            protocol_version=record.get("protocol_version"),
            client_info=record.get("client_info"),
            upstream_last_used_at=float(record.get("upstream_last_used_at") or 0.0),
        )

    async def touch(self, session: McpSession) -> None:
        session.upstream_last_used_at = self.clock.time()
        await self.kv.set(
            SESSION_PREFIX + session.client_session_id,
            session.to_record(),
            ttl=self.settings.client_session_ttl,
        )

    async def delete(self, session: McpSession) -> None:
        await self.kv.delete(SESSION_PREFIX + session.client_session_id)
        await self.kv.decr(SESSION_COUNTER_PREFIX + session.alias, 1)

    def is_idle(self, session: McpSession) -> bool:
        return (
            self.clock.time() - session.upstream_last_used_at > self.settings.upstream_idle_ttl
        )

    async def active_by_alias(self, aliases: list[str]) -> dict[str, float]:
        result: dict[str, float] = {}
        for alias in aliases:
            value = await self.kv.get(SESSION_COUNTER_PREFIX + alias)
            if isinstance(value, int | float) and value:
                result[alias] = float(value)
        return result


def resolve_header_value(value: str | EnvRef, environ: Any, access_token: str) -> str:
    """``env:VAR`` → значение переменной; ``{{access_token}}`` → токен целевой системы (R-P2)."""
    if isinstance(value, EnvRef):
        return value.get(environ) or ""
    return value.replace("{{access_token}}", access_token)


def upstream_headers(
    entry: ServerEntry,
    *,
    client_headers: httpx.Headers,
    access_token: str,
    preset: str,
    groups: list[str],
    environ: Any,
    upstream_session_id: str | None,
) -> dict[str, str]:
    """Заголовки запроса к upstream (R-P2): проброс allow-list, креды каталога, группы."""
    headers: dict[str, str] = {}
    for name in FORWARDED_HEADERS:
        value = client_headers.get(name)
        if value:
            headers[name] = value
    injected: dict[str, str] = {}
    for name, raw in (entry.model.credential_headers or {}).items():
        injected[name] = resolve_header_value(raw, environ, access_token)
    for name, raw in (entry.model.static_headers or {}).items():
        injected[name] = resolve_header_value(raw, environ, access_token)
    header_groups = groups_header(entry, preset, groups)
    if header_groups is not None:
        injected[header_groups[0]] = header_groups[1]
    lower_injected = {name.lower() for name in injected}
    headers = {k: v for k, v in headers.items() if k.lower() not in lower_injected}
    headers.update(injected)
    if upstream_session_id:
        headers["Mcp-Session-Id"] = upstream_session_id
    return headers


def tools_cache_key(alias: str, catalog_version: int, preset: str, groups: list[str]) -> str:
    digest = sha256_hex(f"{preset}|{','.join(groups)}")
    return f"{TOOLS_CACHE_PREFIX}{alias}:{catalog_version}:{digest}"


class UpstreamClient:
    """Тонкая обёртка над httpx для потоковых запросов к upstream MCP (R-P3)."""

    def __init__(self, http: Callable[[], httpx.AsyncClient], settings: Settings) -> None:
        self._http = http
        self.settings = settings

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.upstream_timeout,
            read=self.settings.upstream_timeout,
            write=self.settings.upstream_timeout,
            pool=self.settings.upstream_timeout,
        )

    async def open(
        self, method: str, url: str, *, headers: dict[str, str], content: bytes | None
    ) -> httpx.Response:
        client = self._http()
        request = client.build_request(
            method, url, headers=headers, content=content, timeout=self.timeout()
        )
        return await client.send(request, stream=True)

    async def request(
        self, method: str, url: str, *, headers: dict[str, str], content: bytes | None
    ) -> httpx.Response:
        response = await self.open(method, url, headers=headers, content=content)
        await response.aread()
        await response.aclose()
        return response


async def iter_upstream_body(response: httpx.Response) -> AsyncIterator[bytes]:
    """Куски тела upstream по мере поступления.

    Настоящий поток читается через ``aiter_raw``; ответ, тело которого уже целиком получено
    (так работает ``httpx.MockTransport`` в тестах), отдаётся одним куском.
    """
    if getattr(response, "is_stream_consumed", False):
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


async def stream_response(
    response: httpx.Response,
    *,
    on_close: Callable[[], Any] | None = None,
    transform: Callable[[bytes], bytes] | None = None,
) -> AsyncIterator[bytes]:
    """Отдать тело upstream клиенту по мере поступления (R-P3)."""
    try:
        async for chunk in iter_upstream_body(response):
            yield transform(chunk) if transform else chunk
    finally:
        await response.aclose()
        if on_close is not None:
            await on_close()


class SseFilter:
    """Потоковая фильтрация SSE: события разбираются целиком, порядок сохраняется (R-P8)."""

    def __init__(self, tools: ToolFilter) -> None:
        self.tools = tools
        self._buffer = b""

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk
        out = b""
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            out += self._filter_event(event) + b"\n\n"
        return out

    def flush(self) -> bytes:
        if not self._buffer:
            return b""
        rest, self._buffer = self._buffer, b""
        return self._filter_event(rest)

    def _filter_event(self, event: bytes) -> bytes:
        lines = event.split(b"\n")
        out: list[bytes] = []
        for line in lines:
            if not line.startswith(b"data:"):
                out.append(line)
                continue
            raw = line[len(b"data:") :].strip()
            payload = parse_jsonrpc(raw)
            if payload is None:
                out.append(line)
                continue
            filtered = filter_tools_payload(payload, self.tools)
            out.append(b"data: " + json.dumps(filtered, ensure_ascii=False).encode("utf-8"))
        return b"\n".join(out)


class SseCounter:
    """Счётчик одновременных SSE-потоков пользователя (R-P9)."""

    def __init__(self, kv: KeyValueStore, settings: Settings) -> None:
        self.kv = kv
        self.settings = settings

    async def acquire(self, user_id: str) -> bool:
        value = await self.kv.incr(SSE_PREFIX + user_id, 1, ttl=3600)
        if value > self.settings.max_sse_per_user:
            await self.kv.decr(SSE_PREFIX + user_id, 1)
            return False
        return True

    async def release(self, user_id: str) -> None:
        await self.kv.decr(SSE_PREFIX + user_id, 1)


def build_metrics_recorder(metrics: Metrics) -> Callable[[str, str, int, float], None]:
    def record(alias: str, method: str, status: int, duration: float) -> None:
        metrics.counter(
            "hub_mcp_requests_total",
            "Запросы к MCP-proxy Hub.",
            {"alias": alias, "method": method, "status": str(status)},
        )
        metrics.histogram(
            "hub_mcp_request_duration_seconds",
            "Длительность запросов к MCP-proxy Hub.",
            {"alias": alias},
            duration,
        )

    return record


__all__ = [
    "CB_PREFIX",
    "CODE_CONNECTION",
    "CODE_RATE_LIMIT",
    "CODE_SESSION",
    "CODE_TOOL_FORBIDDEN",
    "CODE_UPSTREAM",
    "MSG_NOT_CONNECTED",
    "MSG_RATE_LIMIT",
    "MSG_SESSION",
    "MSG_UPSTREAM",
    "RATE_MCP_PREFIX",
    "SESSION_PREFIX",
    "SSE_CONTENT_TYPE",
    "SSE_PREFIX",
    "TOOLS_CACHE_PREFIX",
    "CircuitBreaker",
    "McpSession",
    "ProxyError",
    "SessionStore",
    "SseCounter",
    "SseFilter",
    "UpstreamClient",
    "build_metrics_recorder",
    "filter_tools_payload",
    "first_request_id",
    "iter_upstream_body",
    "jsonrpc_error",
    "jsonrpc_methods",
    "parse_jsonrpc",
    "stream_response",
    "tools_cache_key",
    "upstream_headers",
]
