"""MCP-proxy: потоковое проксирование streamable-http на upstream каталога (R-P1..R-P11).

Сессии виртуализируются (клиент видит идентификатор Hub), заголовки подставляются из каталога,
ответ отдаётся потоково без буферизации. HTTP-клиент инжектируется (``app.state.upstream_client``).
"""

from __future__ import annotations

import asyncio
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
TOOLS_CACHE_PREFIX = "toolscache:"
CB_PREFIX = "cb:"
CB_PROBE_SUFFIX = ":probe"  # право на пробный запрос в half-open (R-P10)
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


@dataclass(frozen=True, slots=True)
class BreakerDecision:
    """Решение выключателя по одному запросу (R-P10).

    ``retry_after is None`` — запрос идёт на upstream. ``was_open`` — выключатель был открыт или
    в half-open, то есть ключ пробы существует и по завершении запроса его нужно снять (H5-3).
    """

    retry_after: float | None
    was_open: bool


class CircuitBreaker:
    """Выключатель на alias, состояние — в KV (R-P10).

    Состояние ``cb:<alias>`` = ``{failures, open_until}``. Пока ``open_until`` в будущем — окно
    открыто, запросы на upstream не идут. По истечении окна выключатель переходит в half-open:
    право на **один** пробный запрос захватывается атомарно (``set_if_absent`` по ключу
    ``cb:<alias>:probe`` — работает и между репликами), остальные запросы в это время получают
    быстрый отказ. Успех пробы закрывает выключатель и обнуляет счётчик, ошибка — открывает окно
    снова на ``HUB_CB_RESET`` (счётчик при открытии не обнуляется, иначе провал пробы требовал бы
    заново накопить ``HUB_CB_FAILURES`` ошибок).
    """

    def __init__(self, kv: KeyValueStore, clock: Clock, settings: Settings) -> None:
        self.kv = kv
        self.clock = clock
        self.settings = settings

    def _key(self, alias: str) -> str:
        return CB_PREFIX + alias

    def _probe_key(self, alias: str) -> str:
        return CB_PREFIX + alias + CB_PROBE_SUFFIX

    def _probe_ttl(self) -> float:
        """TTL права на пробу — дольше самого долгого возможного запроса (H5-1).

        Пробный запрос живёт до ``HUB_UPSTREAM_TIMEOUT`` (обычный ответ) или до
        ``HUB_UPSTREAM_SSE_IDLE_TIMEOUT`` (SSE-поток); если бы право истекало раньше, второй
        запрос захватил бы его и отправил на лежащий upstream ещё одну пробу. Восстановление
        длинный TTL не задерживает: право снимается сразу по завершении пробы.
        """
        return (
            max(
                float(self.settings.cb_reset),
                float(self.settings.upstream_timeout),
                float(self.settings.upstream_sse_idle_timeout),
            )
            + float(self.settings.cb_probe_grace)
        )

    async def state(self, alias: str) -> dict[str, Any]:
        record = await self.kv.get(self._key(alias))
        if not isinstance(record, dict):
            return {"failures": 0, "open_until": 0.0}
        return record

    async def check(self, alias: str) -> BreakerDecision:
        """``retry_after is None`` — можно идти на upstream; иначе секунды до следующей попытки.

        В half-open проход получает только тот запрос, который захватил право на пробу; для него
        ``was_open`` истинно, и по завершении пробы вызывающий код передаёт признак в
        ``record_success`` (H5-3), чтобы в закрытом состоянии не ходить в KV за удалением.
        """
        record = await self.state(alias)
        open_until = float(record.get("open_until") or 0.0)
        if open_until <= 0.0:
            return BreakerDecision(retry_after=None, was_open=False)
        now = self.clock.time()
        if open_until > now:
            return BreakerDecision(retry_after=open_until - now, was_open=True)
        probe_ttl = self._probe_ttl()
        claimed = await self.kv.set_if_absent(
            self._probe_key(alias), {"until": now + probe_ttl}, ttl=probe_ttl
        )
        if claimed:
            return BreakerDecision(retry_after=None, was_open=True)
        # Пробный запрос уже выполняет кто-то другой — быстрый отказ до его завершения.
        held = await self.kv.get(self._probe_key(alias))
        until = float(held.get("until") or 0.0) if isinstance(held, dict) else 0.0
        return BreakerDecision(retry_after=max(until - now, 1.0), was_open=True)

    async def record_success(self, alias: str, *, was_open: bool = False) -> None:
        """Успешный ответ обнуляет счётчик ошибок и закрывает выключатель (R-P10).

        Ключ пробы удаляется только при ``was_open`` — в закрытом состоянии его не существует
        никогда, и безусловный ``delete`` был лишним обращением к KV на каждый проксированный
        запрос (H5-3). Признак берётся из записи ``cb:<alias>``, уже прочитанной в ``check()``.
        """
        if was_open:
            await self.kv.delete(self._probe_key(alias))
        await self.kv.set(
            self._key(alias),
            {"failures": 0, "open_until": 0.0},
            ttl=self.settings.cb_reset * 2,
        )

    async def record_failure(self, alias: str) -> None:
        record = await self.state(alias)
        failures = int(record.get("failures") or 0)
        open_until = float(record.get("open_until") or 0.0)
        now = self.clock.time()
        if open_until > 0.0:
            # Провал пробного запроса (или ошибка при уже открытом окне) — окно открывается снова.
            failures = max(failures, self.settings.cb_failures)
            open_until = now + self.settings.cb_reset
            await self.kv.delete(self._probe_key(alias))
        else:
            failures += 1
            open_until = now + self.settings.cb_reset if failures >= self.settings.cb_failures else 0.0
        await self.kv.set(
            self._key(alias),
            {"failures": failures, "open_until": open_until},
            ttl=self.settings.cb_reset * 2,
        )


class SessionStore:
    """Виртуальные сессии MCP в KV (R-P4, R-P5).

    Ключ — ``mcpsess:<alias>:<client_session_id>``: alias в ключе позволяет считать активные
    сессии сервера по живым записям (``count_prefix``, R-N1), без отдельного счётчика, который
    завышался бы на сессии, истёкшие по ``HUB_CLIENT_SESSION_TTL``.
    """

    def __init__(self, kv: KeyValueStore, clock: Clock, settings: Settings) -> None:
        self.kv = kv
        self.clock = clock
        self.settings = settings

    @staticmethod
    def _prefix(alias: str) -> str:
        return f"{SESSION_PREFIX}{alias}:"

    def _key(self, alias: str, client_session_id: str) -> str:
        return self._prefix(alias) + client_session_id

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
            self._key(alias, session.client_session_id),
            session.to_record(),
            ttl=self.settings.client_session_ttl,
        )
        return session

    async def get(self, client_session_id: str, *, user_id: str, alias: str) -> McpSession | None:
        record = await self.kv.get(self._key(alias, client_session_id))
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
            self._key(session.alias, session.client_session_id),
            session.to_record(),
            ttl=self.settings.client_session_ttl,
        )

    async def delete(self, session: McpSession) -> None:
        await self.kv.delete(self._key(session.alias, session.client_session_id))

    def is_idle(self, session: McpSession) -> bool:
        return (
            self.clock.time() - session.upstream_last_used_at > self.settings.upstream_idle_ttl
        )

    async def active_by_alias(self, aliases: list[str]) -> dict[str, float]:
        """Живые сессии по alias — по фактическим записям ``mcpsess:<alias>:*`` (R-N1)."""
        result: dict[str, float] = {}
        for alias in aliases:
            count = await self.kv.count_prefix(self._prefix(alias))
            if count:
                result[alias] = float(count)
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
        """Соединение и первый байт — ``HUB_UPSTREAM_TIMEOUT``; чтение тела уже установленного
        потока — ``HUB_UPSTREAM_SSE_IDLE_TIMEOUT`` (R-P3)."""
        read = max(self.settings.upstream_timeout, self.settings.upstream_sse_idle_timeout)
        return httpx.Timeout(
            connect=self.settings.upstream_timeout,
            read=read,
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
        try:
            return await asyncio.wait_for(
                client.send(request, stream=True), timeout=self.settings.upstream_timeout
            )
        except TimeoutError as exc:
            raise httpx.ReadTimeout("upstream не ответил за отведённое время", request=request) from exc

    async def request(
        self, method: str, url: str, *, headers: dict[str, str], content: bytes | None
    ) -> httpx.Response:
        response = await self.open(method, url, headers=headers, content=content)
        await response.aread()
        await response.aclose()
        return response


async def iter_upstream_body(response: httpx.Response) -> AsyncIterator[bytes]:
    """Куски тела upstream по мере поступления.

    Поток читается через ``aiter_bytes`` (content-encoding upstream снимается, поэтому наружу
    заголовок кодирования не пересылается); ответ, тело которого уже целиком получено
    (так работает ``httpx.MockTransport`` в тестах), отдаётся одним куском.
    """
    if getattr(response, "is_stream_consumed", False):
        yield response.content
        return
    async for chunk in response.aiter_bytes():
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
    "BreakerDecision",
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
