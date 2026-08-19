"""Вспомогательные функции тестов: каталоги, моки LiteLLM, JWT, прямой доступ к БД.

Все проверки выполняются против локальных моков (respx через ``httpx.MockTransport``, SQLite
``:memory:``, in-memory KeyValueStore, ``ManualClock``). Никаких обращений к внешним системам.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import hmac
import io
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml
from sqlalchemy import text

from hub.clock import Clock
from hub.kv import InMemoryKeyValueStore
from hub.logging_ import JsonFormatter

LITELLM_URL = "https://litellm.test"
PUBLIC_URL = "https://hub.test"
FERNET_KEY = base64.urlsafe_b64encode(b"\x01" * 32).decode("ascii")  # 44 символа, 32 байта
POLL_SECRET_HEADER = "X-Hub-Poll-Secret"

# ---------------------------------------------------------------------------
# Каталоги
# ---------------------------------------------------------------------------


def native_server(alias: str = "tag", **overrides: Any) -> dict[str, Any]:
    server: dict[str, Any] = {
        "alias": alias,
        "title": "ТЭГ (Mattermost)",
        "description": "Сообщения и каналы корпоративного мессенджера.",
        "owner": "Мирослав Шишенков",
        "contact": "https://tag.test",
        "docs_url": "https://docs.test/tag",
        "status": "ga",
        "audience": ["all"],
        "mode": "native",
        "mcp_url": "https://tag.test/mcp",
        "permission_model": {
            "kind": "consent",
            "presets": {
                "readonly": {"write_mode": "readonly"},
                "readwrite": {"write_mode": "confirm"},
            },
        },
    }
    server.update(overrides)
    return server


def facade_server(alias: str = "gitlab", **overrides: Any) -> dict[str, Any]:
    server: dict[str, Any] = {
        "alias": alias,
        "title": "GitLab",
        "description": "Репозитории, merge requests, issues GitLab.",
        "owner": "AI Lab",
        "contact": "https://portal.test",
        "docs_url": "https://portal.test/docs/gitlab",
        "status": "ga",
        "audience": ["all"],
        "mode": "facade",
        "upstream_url": "https://mcp-gitlab.internal.test/mcp",
        "auth": {
            "type": "oauth2",
            "authorize_url": "https://gitlab.test/oauth/authorize",
            "token_url": "https://gitlab.test/oauth/token",
            "revoke_url": "https://gitlab.test/oauth/revoke",
            "client_id": "hub-client-id",
            "client_secret": "env:GL_SECRET",
            "pkce": True,
            "scopes": {"readonly": ["read_api"], "readwrite": ["api"]},
        },
        "credential_headers": {"Authorization": "Bearer {{access_token}}"},
        "static_headers": {"X-Static": "static-value"},
        "permission_model": {
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": ["core"],
            "groups": [
                {"id": "code_review", "title": "Code review", "preset": "readonly"},
                {"id": "repo_write", "title": "Запись", "preset": "readwrite"},
                {"id": "admin", "title": "Админ", "preset": "none"},
            ],
        },
    }
    server.update(overrides)
    return server


def catalog_doc(
    servers: list[dict[str, Any]] | None = None, version: int = 1, **extra: Any
) -> dict[str, Any]:
    doc: dict[str, Any] = {"version": version, "servers": copy.deepcopy(servers or [])}
    doc.update(extra)
    return doc


def write_catalog(path: Path, document: dict[str, Any] | str) -> Path:
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return path


def default_catalog() -> dict[str, Any]:
    """Каталог по умолчанию: facade 'gitlab' + native 'tag' (порядок как в файле)."""
    return catalog_doc([facade_server("gitlab"), native_server("tag")])


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(claims: dict[str, Any] | None = None, *, header: dict[str, Any] | None = None) -> str:
    """JWT с произвольными claims и фиктивной подписью (подпись Hub не проверяет)."""
    header = header or {"alg": "HS256", "typ": "JWT"}
    claims = claims or {}
    return ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(claims, separators=(",", ":")).encode()),
            _b64url(b"signature-not-verified"),
        ]
    )


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Моки LiteLLM (respx)
# ---------------------------------------------------------------------------


def make_litellm_router() -> respx.MockRouter:
    return respx.MockRouter(base_url=LITELLM_URL, assert_all_called=False, assert_all_mocked=True)


def litellm_http_client(router: respx.MockRouter) -> httpx.AsyncClient:
    """httpx-клиент, весь трафик которого уходит в respx-роутер (без сети)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(router.handler))


def start_body(
    login_id: str = "ll-1",
    poll_secret: str = "ll-secret",
    user_code: str = "ABCD-1234",
    expires_in: int | None = 600,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "login_id": login_id,
        "poll_secret": poll_secret,
        "user_code": user_code,
    }
    if expires_in is not None:
        body["expires_in"] = expires_in
    return body


def mock_start(
    router: respx.MockRouter, body: dict[str, Any] | None = None, status: int = 200
) -> respx.Route:
    return router.post("/sso/cli/start").respond(
        status, json=body if body is not None else start_body()
    )


def mock_poll(
    router: respx.MockRouter,
    body: dict[str, Any] | None = None,
    *,
    login_id: str = "ll-1",
    team_id: str | None = None,
    status: int = 200,
) -> respx.Route:
    """Мок ``GET /sso/cli/poll/{login_id}``. Маршрут с ``team_id`` регистрируется как более
    специфичный: вызывающий код должен регистрировать его ДО маршрута без ``team_id``."""
    kwargs: dict[str, Any] = {}
    if team_id is not None:
        kwargs["params"] = {"team_id": team_id}
    return router.get(f"/sso/cli/poll/{login_id}", **kwargs).respond(status, json=body)


def mock_key_generate(
    router: respx.MockRouter, key: str | None = "sk-test-1", status: int = 200
) -> respx.Route:
    body: dict[str, Any] = {"key": key} if key is not None else {}
    return router.post("/key/generate").respond(status, json=body)


def teams_body(*teams: tuple[str, str], with_details: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "ready",
        "requires_team_selection": True,
        "teams": [t for t, _ in teams],
    }
    if with_details:
        body["team_details"] = [{"team_id": t, "team_alias": a} for t, a in teams]
    return body


def ready_body(
    jwt: str,
    *,
    user_id: str | None = "u1",
    team_id: str | None = "t1",
    teams: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "ready", "key": jwt}
    if user_id is not None:
        body["user_id"] = user_id
    if team_id is not None:
        body["team_id"] = team_id
    body["teams"] = teams if teams is not None else ([team_id] if team_id else [])
    return body


# ---------------------------------------------------------------------------
# БД (прямой доступ через SQL по схеме spec.md §6)
# ---------------------------------------------------------------------------


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


async def fetch_rows(app: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    db = app.state.db
    await db.init()
    async with db.session() as session:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]


async def execute(app: Any, sql: str, **params: Any) -> None:
    db = app.state.db
    await db.init()
    async with db.session() as session, session.begin():
        await session.execute(text(sql), params)


async def insert_user(
    app: Any,
    user_id: str = "u1",
    email: str | None = "u1@corp.test",
    groups: list[str] | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    await execute(
        app,
        "INSERT INTO users (user_id, email, groups, created_at, updated_at) "
        "VALUES (:user_id, :email, :groups, :created_at, :updated_at)",
        user_id=user_id,
        email=email,
        groups=json.dumps(groups if groups is not None else ["all"]),
        created_at=_dt(now),
        updated_at=_dt(now),
    )


async def insert_key(
    app: Any,
    key: str,
    user_id: str = "u1",
    *,
    key_kind: str = "persistent",
    key_alias: str = "opencode-u1-20260101-1200",
    client: str | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    await execute(
        app,
        "INSERT INTO api_keys (key_sha256, user_id, key_kind, key_alias, client, created_at, expires_at) "
        "VALUES (:sha, :user_id, :kind, :alias, :client, :created_at, :expires_at)",
        sha=sha256_hex(key),
        user_id=user_id,
        kind=key_kind,
        alias=key_alias,
        client=client,
        created_at=_dt(created_at or datetime.now(UTC)),
        expires_at=_dt(expires_at),
    )


async def insert_connection(
    app: Any,
    user_id: str,
    alias: str,
    *,
    status: str = "connected",
    preset: str | None = "readonly",
    groups: list[str] | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    await execute(
        app,
        "INSERT INTO connections (user_id, alias, status, preset, groups, created_at, updated_at) "
        "VALUES (:user_id, :alias, :status, :preset, :groups, :created_at, :updated_at)",
        user_id=user_id,
        alias=alias,
        status=status,
        preset=preset,
        groups=json.dumps(groups or []),
        created_at=_dt(now),
        updated_at=_dt(now),
    )


async def seed_user_with_key(
    app: Any, key: str, user_id: str = "u1", email: str | None = "u1@corp.test", **key_kwargs: Any
) -> None:
    await insert_user(app, user_id, email)
    await insert_key(app, key, user_id, **key_kwargs)


async def audit_rows(app: Any, action: str | None = None) -> list[dict[str, Any]]:
    rows = await fetch_rows(
        app, "SELECT id, ts, user_id, action, alias, details FROM audit_log ORDER BY id"
    )
    for row in rows:
        if isinstance(row["details"], str):
            row["details"] = json.loads(row["details"])
    if action is not None:
        rows = [r for r in rows if r["action"] == action]
    return rows


async def dump_all_tables(app: Any) -> str:
    """Все строки всех таблиц как одна строка (для поиска утечек секретов)."""
    chunks: list[str] = []
    for table in ("users", "api_keys", "connections", "audit_log"):
        rows = await fetch_rows(app, f"SELECT * FROM {table}")
        chunks.append(json.dumps(rows, default=str, ensure_ascii=False))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Логи
# ---------------------------------------------------------------------------


def record_text(record: Any) -> str:
    """Полный текст записи лога: сообщение + все дополнительные атрибуты (``extra``)."""
    extras = {k: v for k, v in record.__dict__.items() if not k.startswith("_")}
    return record.getMessage() + " " + json.dumps(extras, default=str, ensure_ascii=False)


class JsonLogLines:
    """Строки, которые JSON-хендлер Hub записал бы в stderr (R-S4), разобранные как JSON."""

    def __init__(self, buffer: io.StringIO) -> None:
        self._buffer = buffer

    def raw(self) -> list[str]:
        return [ln for ln in self._buffer.getvalue().splitlines() if ln.strip()]

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(ln) for ln in self.raw()]

    def find(self, message: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r.get("message") == message]


def hub_json_handlers() -> list[logging.StreamHandler[Any]]:
    """Хендлеры root-логгера с JSON-форматтером Hub (ожидается ровно один на процесс)."""
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter)
    ]


@contextmanager
def capture_json_logs() -> Iterator[JsonLogLines]:
    """Перехватить вывод JSON-хендлера Hub (подмена потока stderr на буфер) на время блока."""
    handlers = hub_json_handlers()
    assert len(handlers) == 1, f"ожидается ровно один JSON-хендлер Hub на root-логгере: {handlers}"
    handler = handlers[0]
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        yield JsonLogLines(buffer)
    finally:
        handler.setStream(previous)


# ---------------------------------------------------------------------------
# KeyValueStore: запись с журналом ключей (для проверки формата ключей §6)
# ---------------------------------------------------------------------------


class RecordingKeyValueStore(InMemoryKeyValueStore):
    """In-memory KeyValueStore, запоминающий все ключи, под которыми что-либо записывалось."""

    def __init__(self, clock: Clock | None = None) -> None:
        super().__init__(clock)
        self.written_keys: list[str] = []

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self.written_keys.append(key)
        await super().set(key, value, ttl)

    async def rate_limit_hit(self, key: str, now: float, window: float, limit: int) -> tuple[bool, float]:
        self.written_keys.append(key)
        return await super().rate_limit_hit(key, now, window, limit)


async def kv_session(app: Any, login_id: str) -> dict[str, Any] | None:
    """Запись сессии входа ``login:<login_id>`` из KeyValueStore приложения (spec §6)."""
    value = await app.state.kv.get(f"login:{login_id}")
    assert value is None or isinstance(value, dict)
    return value


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ===========================================================================
# I-3: моки внешних систем (AS целевых систем, upstream MCP, OIDC) — §16 R-N4.
# Всё, что уходит наружу, перехватывается ``httpx.MockTransport``; любой запрос
# к неизвестному адресу — AssertionError (тесты не ходят в сеть, AC-146).
# ===========================================================================

GITLAB_UPSTREAM = "https://mcp-gitlab.internal.test/mcp"
JIRA_UPSTREAM = "https://mcp-jira.internal.test/mcp"
GITLAB_AS = "https://gitlab.test"
JIRA_AS = "https://jira.test"
OIDC_ISSUER = "https://kc.test/realms/corp"

GL_SECRET = "gl-secret"
GL_STATIC = "st-1"
JIRA_SECRET = "jira-secret"
CATALOG_ENV = {"GL_SECRET": GL_SECRET, "GL_STATIC": GL_STATIC, "JIRA_SECRET": JIRA_SECRET}

UPSTREAM_ACCESS = "ups-access-1"
UPSTREAM_REFRESH = "ups-refresh-1"

LOOPBACK_REDIRECT = "http://127.0.0.1:19876/mcp/oauth/callback"
OTHER_REDIRECT = "http://127.0.0.1:20000/cb"

SESSION_HEADER = "Mcp-Session-Id"
SSE_MEDIA_TYPE = "text/event-stream"


@dataclass
class RecordedRequest:
    """Запрос, полученный моком внешней системы."""

    method: str
    url: str
    headers: dict[str, str]
    content: bytes

    @property
    def json_body(self) -> Any:
        try:
            return json.loads(self.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    @property
    def form(self) -> dict[str, str]:
        from urllib.parse import parse_qsl

        return dict(parse_qsl(self.content.decode("utf-8")))

    @property
    def query(self) -> dict[str, str]:
        from urllib.parse import parse_qsl, urlsplit

        return dict(parse_qsl(urlsplit(self.url).query))

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def _record(request: httpx.Request) -> RecordedRequest:
    return RecordedRequest(
        method=request.method,
        url=str(request.url),
        headers={k.lower(): v for k, v in request.headers.items()},
        content=request.content,
    )


def sse_body(*events: str) -> bytes:
    """Тело SSE из готовых событий (каждое завершается пустой строкой)."""
    return "".join(f"{e}\n\n" for e in events).encode("utf-8")


def sse_event(payload: Any, *, event: str = "message") -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}"


class MockUpstream:
    """Мок upstream MCP (streamable-http): initialize / tools/list / tools/call, SSE, 401/404/5xx."""

    def __init__(self, url: str = GITLAB_UPSTREAM, *, prefix: str = "up") -> None:
        self.url = url
        self.prefix = prefix
        self.requests: list[RecordedRequest] = []
        self.queue: list[Any] = []
        self.sessions: set[str] = set()
        self.session_seq = 0
        self.tools: list[dict[str, Any]] = [
            {"name": "list_mrs", "description": "Список merge requests"},
            {"name": "create_merge_request", "description": "Создать MR"},
            {"name": "admin_labels", "description": "Управление метками"},
        ]
        self.sse_tools_list = False
        self.issue_session_on_initialize = True

    # --- управление сценарием ------------------------------------------------

    def push(self, response: Any) -> None:
        """Добавить в очередь ответ (``httpx.Response``), исключение или функцию."""
        self.queue.append(response)

    def push_many(self, response: Any, times: int) -> None:
        for _ in range(times):
            self.queue.append(response)

    def reset(self) -> None:
        self.requests.clear()
        self.queue.clear()

    @property
    def calls(self) -> int:
        return len(self.requests)

    def last(self) -> RecordedRequest:
        assert self.requests, "upstream не получил ни одного запроса"
        return self.requests[-1]

    def matches(self, request: httpx.Request) -> bool:
        return str(request.url).split("?")[0] == self.url

    # --- обработка -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        recorded = _record(request)
        self.requests.append(recorded)
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                return item(recorded)
            return item
        return self.default(recorded)

    def default(self, recorded: RecordedRequest) -> httpx.Response:
        session_id = recorded.header(SESSION_HEADER)
        if recorded.method == "DELETE":
            self.sessions.discard(session_id or "")
            return httpx.Response(204)
        if session_id and session_id not in self.sessions:
            return httpx.Response(404, json={"error": "session_not_found"})
        if recorded.method == "GET":
            return httpx.Response(405, json={"error": "method_not_allowed"})
        payload = recorded.json_body
        if isinstance(payload, list):
            return httpx.Response(
                200, json=[self._result(item) for item in payload if item.get("id") is not None]
            )
        if not isinstance(payload, dict):
            return httpx.Response(400, json={"error": "bad_request"})
        method = payload.get("method")
        headers: dict[str, str] = {}
        if method == "initialize" and self.issue_session_on_initialize:
            self.session_seq += 1
            new_session = f"{self.prefix}-{self.session_seq}"
            self.sessions.add(new_session)
            headers[SESSION_HEADER] = new_session
        if method == "notifications/initialized":
            return httpx.Response(202, headers=headers)
        body = self._result(payload)
        if method == "tools/list" and self.sse_tools_list:
            return httpx.Response(
                200,
                headers={**headers, "content-type": SSE_MEDIA_TYPE},
                content=sse_body(sse_event(body)),
            )
        return httpx.Response(200, json=body, headers=headers)

    def _result(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = payload.get("method")
        request_id = payload.get("id")
        if method == "initialize":
            result: Any = {
                "protocolVersion": (payload.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-upstream", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": copy.deepcopy(self.tools)}
        elif method == "tools/call":
            name = (payload.get("params") or {}).get("name")
            result = {"content": [{"type": "text", "text": f"вызван {name}"}], "isError": False}
        else:
            result = {"ok": True}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


class MockProviderAS:
    """Мок authorization server целевой системы: token / revoke (authorize — только редирект)."""

    def __init__(
        self,
        base_url: str = GITLAB_AS,
        *,
        access_token: str = UPSTREAM_ACCESS,
        refresh_token: str | None = UPSTREAM_REFRESH,
        expires_in: int | None = 7200,
        scope: str = "read_api read_user read_repository",
    ) -> None:
        self.base_url = base_url
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.scope = scope
        self.token_requests: list[RecordedRequest] = []
        self.revoke_requests: list[RecordedRequest] = []
        self.queue: list[Any] = []
        self.seq = 0
        self.rotate_refresh = True

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/oauth/token"

    @property
    def revoke_url(self) -> str:
        return f"{self.base_url}/oauth/revoke"

    @property
    def authorize_url(self) -> str:
        return f"{self.base_url}/oauth/authorize"

    def push(self, response: Any) -> None:
        self.queue.append(response)

    def matches(self, request: httpx.Request) -> bool:
        return str(request.url).split("?")[0] in (self.token_url, self.revoke_url)

    def handle(self, request: httpx.Request) -> httpx.Response:
        recorded = _record(request)
        url = str(request.url).split("?")[0]
        if url == self.revoke_url:
            self.revoke_requests.append(recorded)
            return httpx.Response(200, json={})
        self.token_requests.append(recorded)
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                return item(recorded)
            return item
        return self.token_response(recorded)

    def token_response(self, recorded: RecordedRequest) -> httpx.Response:
        self.seq += 1
        grant = recorded.form.get("grant_type")
        body: dict[str, Any] = {
            "access_token": self.access_token if self.seq == 1 else f"{self.access_token}-{self.seq}",
            "token_type": "Bearer",
            "scope": self.scope,
        }
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        if self.refresh_token is not None:
            if grant == "refresh_token" and self.rotate_refresh:
                body["refresh_token"] = f"{self.refresh_token[:-1]}{self.seq + 1}"
            elif grant != "refresh_token":
                body["refresh_token"] = self.refresh_token
        return httpx.Response(200, json=body)


_OIDC_KEY: Any = None
_OIDC_WRONG_KEY: Any = None


def raw_jws(header: dict[str, Any], claims: dict[str, Any], secret: bytes | None) -> str:
    """Собрать JWS вручную: ``secret is None`` → подписи нет (``alg: none``), иначе HMAC-SHA256."""
    parts = [
        _b64url(json.dumps(part, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        for part in (header, claims)
    ]
    signing_input = ".".join(parts).encode("ascii")
    if secret is None:
        return ".".join([*parts, ""])
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return ".".join([*parts, _b64url(signature)])


def oidc_keys() -> tuple[Any, Any]:
    """Пара RSA-ключей мока OIDC (генерируются один раз на процесс)."""
    global _OIDC_KEY, _OIDC_WRONG_KEY
    if _OIDC_KEY is None:
        from joserfc.jwk import RSAKey

        _OIDC_KEY = RSAKey.generate_key(2048, auto_kid=True)
        _OIDC_WRONG_KEY = RSAKey.generate_key(2048, auto_kid=True)
    return _OIDC_KEY, _OIDC_WRONG_KEY


class MockOIDC:
    """Мок провайдера OIDC: discovery, JWKS, token с подписанным ``id_token``."""

    def __init__(self, issuer: str = OIDC_ISSUER, *, client_id: str = "opencode-mcp-hub") -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.requests: list[RecordedRequest] = []
        self.next_id_token: str | None = None
        self.token_status = 200
        self.metadata_status = 200
        self.jwks_status = 200
        self.extra_token_body: dict[str, Any] = {}

    @property
    def jwks_requests(self) -> list[RecordedRequest]:
        """Запросы к JWKS издателя (AC-154: при запрещённом ``alg`` их быть не должно)."""
        return [r for r in self.requests if str(r.url).split("?")[0] == self.jwks_url]

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    @property
    def authorize_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/auth"

    @property
    def token_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"

    def jwks(self) -> dict[str, Any]:
        from joserfc.jwk import KeySet

        key, _ = oidc_keys()
        return KeySet([key]).as_dict(private=False)

    def make_id_token(
        self,
        *,
        nonce: str,
        subject: str = "u1-sub",
        username: str | None = "u1",
        email: str | None = "u1@corp",
        issuer: str | None = None,
        audience: str | None = None,
        expires_at: float | None = None,
        wrong_key: bool = False,
        alg: str = "RS256",
        now: float | None = None,
    ) -> str:
        from joserfc import jwt as jose_jwt

        key, other = oidc_keys()
        signing = other if wrong_key else key
        issued = float(now if now is not None else 0.0)
        claims: dict[str, Any] = {
            "iss": issuer if issuer is not None else self.issuer,
            "sub": subject,
            "aud": audience if audience is not None else self.client_id,
            "exp": int(expires_at if expires_at is not None else issued + 300),
            "iat": int(issued),
            "nonce": nonce,
        }
        if username is not None:
            claims["preferred_username"] = username
        if email is not None:
            claims["email"] = email
        if alg != "RS256":
            # HS256 — HMAC по значению открытого ключа из JWKS (классическая algorithm confusion),
            # none — вовсе без подписи. joserfc такие токены не выпускает, собираем JWS вручную.
            secret = None if alg == "none" else self.jwks()["keys"][0]["n"].encode("ascii")
            return raw_jws({"alg": alg, "kid": signing.kid}, claims, secret)
        return jose_jwt.encode({"alg": "RS256", "kid": signing.kid}, claims, signing)

    def matches(self, request: httpx.Request) -> bool:
        return str(request.url).split("?")[0] in (
            self.discovery_url,
            self.jwks_url,
            self.token_url,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        recorded = _record(request)
        self.requests.append(recorded)
        url = str(request.url).split("?")[0]
        if url == self.discovery_url:
            if self.metadata_status != 200:
                return httpx.Response(self.metadata_status, json={"error": "unavailable"})
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": self.authorize_url,
                    "token_endpoint": self.token_url,
                    "jwks_uri": self.jwks_url,
                },
            )
        if url == self.jwks_url:
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status, json={"error": "unavailable"})
            return httpx.Response(200, json=self.jwks())
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant"})
        body: dict[str, Any] = {"access_token": "kc-access", "token_type": "Bearer"}
        if self.next_id_token is not None:
            body["id_token"] = self.next_id_token
        body.update(self.extra_token_body)
        return httpx.Response(200, json=body)


class MockNetwork:
    """Единая точка перехвата исходящих HTTP-запросов Hub (кроме LiteLLM)."""

    def __init__(self) -> None:
        self.upstreams: dict[str, MockUpstream] = {
            "gitlab": MockUpstream(GITLAB_UPSTREAM, prefix="up"),
            "jira": MockUpstream(JIRA_UPSTREAM, prefix="jira"),
        }
        self.providers: dict[str, MockProviderAS] = {
            "gitlab": MockProviderAS(GITLAB_AS),
            "jira": MockProviderAS(
                JIRA_AS,
                access_token="jira-access-1",
                refresh_token="jira-refresh-1",
                scope="read:jira",
            ),
        }
        self.oidc = MockOIDC()
        self.unmatched: list[str] = []

    @property
    def upstream(self) -> MockUpstream:
        return self.upstreams["gitlab"]

    @property
    def provider(self) -> MockProviderAS:
        return self.providers["gitlab"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        for mock in (*self.upstreams.values(), *self.providers.values(), self.oidc):
            if mock.matches(request):
                return mock.handle(request)
        self.unmatched.append(f"{request.method} {request.url}")
        raise AssertionError(f"тест обратился к неизвестному адресу: {request.method} {request.url}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# ---------------------------------------------------------------------------
# Каталоги I-3
# ---------------------------------------------------------------------------


def gitlab_facade(**overrides: Any) -> dict[str, Any]:
    """facade 'gitlab' ревизии 2: scopes по пресетам, группы прав, tool_filter (AC-115, AC-122)."""
    server = facade_server(
        "gitlab",
        upstream_url=GITLAB_UPSTREAM,
        auth={
            "type": "oauth2",
            "authorize_url": f"{GITLAB_AS}/oauth/authorize",
            "token_url": f"{GITLAB_AS}/oauth/token",
            "revoke_url": f"{GITLAB_AS}/oauth/revoke",
            "client_id": "hub-client-id",
            "client_secret": "env:GL_SECRET",
            "pkce": True,
            "scopes": {
                "readonly": ["read_api", "read_user", "read_repository"],
                "readwrite": ["api", "read_user"],
            },
        },
        credential_headers={"Authorization": "Bearer {{access_token}}"},
        static_headers={"X-Static": "env:GL_STATIC"},
        permission_model={
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": ["core"],
            "groups": [
                {"id": "code_review", "title": "Code review", "preset": "readonly"},
                {"id": "devops", "title": "DevOps", "preset": "readonly"},
                {"id": "users", "title": "Пользователи", "preset": "readonly"},
                {
                    "id": "repo_write",
                    "title": "Запись в репозиторий",
                    "preset": "readwrite",
                    "tools": ["create_*"],
                },
                {"id": "admin", "title": "Администрирование", "preset": "none"},
            ],
            "tool_filter": {"allow": ["*"], "deny": ["admin_*"]},
        },
    )
    server.update(overrides)
    return server


def jira_facade(**overrides: Any) -> dict[str, Any]:
    server = facade_server(
        "jira",
        title="Jira",
        description="Задачи и проекты Jira.",
        docs_url="https://portal.test/docs/jira",
        upstream_url=JIRA_UPSTREAM,
        auth={
            "type": "oauth2",
            "authorize_url": f"{JIRA_AS}/oauth/authorize",
            "token_url": f"{JIRA_AS}/oauth/token",
            "revoke_url": f"{JIRA_AS}/oauth/revoke",
            "client_id": "hub-jira-id",
            "client_secret": "env:JIRA_SECRET",
            "pkce": True,
            "scopes": {"readonly": ["read:jira"], "readwrite": ["write:jira"]},
        },
        # Как в боевом catalog.yaml: креды уходят в собственном заголовке, Authorization
        # каталогом не задаётся — клиентский Authorization обязан быть удалён проксёй (R-P2).
        credential_headers={
            "X-Atlassian-Jira-Personal-Token": "{{access_token}}",
            "X-Atlassian-Jira-Url": "https://jira.test",
        },
        static_headers={},
        permission_model={
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": [],
            "groups": [{"id": "issues", "title": "Задачи", "preset": "readonly"}],
        },
    )
    server.update(overrides)
    return server


def unconfigured_facade(alias: str = "u") -> dict[str, Any]:
    """beta-facade без заданной ``${VAR}`` → состояние unconfigured (R-C3)."""
    return facade_server(
        alias,
        title="Незаданный сервер",
        status="beta",
        upstream_url="${U_UPSTREAM_URL}",
        auth={
            "type": "oauth2",
            "authorize_url": "https://u.test/oauth/authorize",
            "token_url": "https://u.test/oauth/token",
            "client_id": "${U_CLIENT_ID}",
            "client_secret": "env:U_SECRET",
            "pkce": True,
            "scopes": {"readonly": ["r"], "readwrite": ["w"]},
        },
    )


def restricted_facade(alias: str = "b") -> dict[str, Any]:
    return facade_server(
        alias,
        title="Внутренний сервер",
        audience=["devs"],
        upstream_url="https://mcp-b.internal.test/mcp",
        auth={
            "type": "oauth2",
            "authorize_url": "https://b.test/oauth/authorize",
            "token_url": "https://b.test/oauth/token",
            "client_id": "b-id",
            "client_secret": "env:B_SECRET",
            "pkce": True,
            "scopes": {"readonly": ["r"], "readwrite": ["w"]},
        },
    )


def i3_catalog(*, extra: bool = False, version: int = 1) -> dict[str, Any]:
    """Каталог ревизии 2: facade 'gitlab' и 'jira', native 'tag' (+ 'u'/'b' при ``extra``)."""
    servers = [gitlab_facade(), jira_facade(), native_server("tag")]
    if extra:
        servers += [unconfigured_facade("u"), restricted_facade("b")]
    return catalog_doc(servers, version=version)


# ---------------------------------------------------------------------------
# Потоковый вызов ASGI-приложения (httpx.ASGITransport буферизует тело целиком)
# ---------------------------------------------------------------------------


class AsgiResponseStream:
    """Ответ ASGI-приложения, читаемый по мере поступления кусков тела."""

    def __init__(self, status_code: int, headers: dict[str, str], queue: Any, task: Any) -> None:
        self.status_code = status_code
        self.headers = headers
        self._queue = queue
        self._task = task
        self.chunks: list[bytes] = []

    async def next_chunk(self, timeout: float = 2.0) -> bytes | None:
        import asyncio

        chunk = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if chunk is not None:
            self.chunks.append(chunk)
        return chunk

    async def read_all(self, timeout: float = 2.0) -> bytes:
        while True:
            chunk = await self.next_chunk(timeout=timeout)
            if chunk is None:
                break
        return b"".join(self.chunks)


@contextlib.asynccontextmanager
async def asgi_stream(
    app: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
    query: str = "",
) -> Any:
    """Выполнить запрос к ASGI-приложению, читая тело ответа потоково.

    ``httpx.ASGITransport`` дожидается завершения приложения и склеивает тело — для проверок
    потоковости (AC-116) и одновременных SSE-потоков (AC-126) нужен прямой вызов ASGI.
    """
    import asyncio

    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "headers": raw_headers,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "server": ("hub.test", 443),
        "client": ("127.0.0.1", 5555),
        "root_path": "",
        "state": {},
    }
    queue: Any = asyncio.Queue()
    started: Any = asyncio.get_running_loop().create_future()
    disconnected = asyncio.Event()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": content, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            if not started.done():
                started.set_result(
                    (
                        int(message["status"]),
                        {
                            k.decode("latin-1").lower(): v.decode("latin-1")
                            for k, v in message.get("headers", [])
                        },
                    )
                )
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await queue.put(body)
            if not message.get("more_body", False):
                await queue.put(None)

    task = asyncio.create_task(app(scope, receive, send))

    async def _guard() -> None:
        with contextlib.suppress(BaseException):  # ошибка всплывёт при чтении задачи ниже
            await task
        if not started.done():
            started.set_exception(RuntimeError("приложение не начало ответ"))
        await queue.put(None)

    guard = asyncio.create_task(_guard())
    try:
        status, response_headers = await asyncio.wait_for(started, timeout=5.0)
        stream = AsgiResponseStream(status, response_headers, queue, task)
        stream.disconnect = disconnected.set  # type: ignore[attr-defined]
        yield stream
    finally:
        disconnected.set()
        for pending in (task, guard):
            if not pending.done():
                pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await guard


# ---------------------------------------------------------------------------
# Сценарные помощники I-3 (регистрация клиента, вход, подключение, токены)
# ---------------------------------------------------------------------------

HIDDEN_INPUT_RE = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', re.IGNORECASE
)
META_ERROR_RE = re.compile(r'<meta name="hub-error" content="([^"]*)"')


def set_cookie_header(response: Any, name: str) -> str:
    """Строка ``Set-Cookie`` именно этой cookie (в ответе их несколько — AC-131).

    Склеенный ``headers["set-cookie"]`` для проверки атрибутов не годится: атрибут соседней
    cookie удовлетворял бы проверку.
    """
    for raw in response.headers.get_list("set-cookie"):
        if raw.split("=", 1)[0].strip() == name:
            return raw
    raise AssertionError(f"в ответе нет Set-Cookie {name}: {response.headers.get_list('set-cookie')}")


def cookie_attributes(raw: str) -> dict[str, str]:
    """Атрибуты одной строки ``Set-Cookie`` в нижнем регистре (флаги → ``""``)."""
    attrs: dict[str, str] = {}
    for chunk in raw.split(";")[1:]:
        key, _, value = chunk.strip().partition("=")
        attrs[key.strip().lower()] = value.strip()
    return attrs


def hidden_inputs(html: str) -> dict[str, str]:
    return dict(HIDDEN_INPUT_RE.findall(html))


def html_error_code(html: str) -> str | None:
    m = META_ERROR_RE.search(html)
    return m.group(1) if m else None


def query_of(url: str) -> dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit

    return dict(parse_qsl(urlsplit(url).query))


def pkce_pair(verifier: str = "test-code-verifier-0123456789abcdef") -> tuple[str, str]:
    from hub.crypto import code_challenge_s256

    return verifier, code_challenge_s256(verifier)


def jsonrpc_body(method: str, params: dict[str, Any] | None = None, request_id: Any = 1) -> bytes:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "opencode", "version": "1.17.9"},
}


def mcp_headers(
    token: str, *, session_id: str | None = None, accept: str = "application/json, text/event-stream"
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


async def ensure_user(hub: Any, user_id: str = "u1", email: str | None = "u1@corp.test") -> None:
    rows = await fetch_rows(hub.app, "SELECT user_id FROM users WHERE user_id = :uid", uid=user_id)
    if not rows:
        await insert_user(hub.app, user_id, email)


async def web_login(hub: Any, user_id: str = "u1", auth_method: str = "litellm") -> str:
    """Создать веб-сессию пользователя и положить cookie в клиент; вернуть CSRF-токен."""
    await ensure_user(hub, user_id)
    token, csrf = await hub.app.state.web_sessions.create(user_id, auth_method)
    domain = hub.base_url.split("//", 1)[-1].split("/", 1)[0]
    hub.client.cookies.set("hub_session", token, domain=domain, path="/")
    hub.client.cookies.set("hub_csrf", csrf, domain=domain, path="/")
    return csrf


def web_logout(hub: Any) -> None:
    hub.client.cookies.clear()


async def register_client(
    hub: Any,
    *,
    redirect_uris: list[str] | None = None,
    client_name: str | None = "OpenCode",
    **extra: Any,
) -> str:
    payload: dict[str, Any] = {
        "redirect_uris": redirect_uris if redirect_uris is not None else [LOOPBACK_REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if client_name is not None:
        payload["client_name"] = client_name
    payload.update(extra)
    response = await hub.client.post("/oauth/register", json=payload)
    assert response.status_code == 201, response.text
    return str(response.json()["client_id"])


def authorize_params(
    client_id: str,
    *,
    redirect_uri: str = LOOPBACK_REDIRECT,
    challenge: str | None = None,
    state: str | None = "state-1",
    resource: str | None = "https://hub.test/mcp/gitlab",
    scope: str | None = None,
    response_type: str | None = "code",
    method: str | None = "S256",
) -> dict[str, str]:
    params: dict[str, str] = {"client_id": client_id}
    if redirect_uri is not None:
        params["redirect_uri"] = redirect_uri
    if response_type is not None:
        params["response_type"] = response_type
    if challenge is not None:
        params["code_challenge"] = challenge
    if method is not None:
        params["code_challenge_method"] = method
    if state is not None:
        params["state"] = state
    if resource is not None:
        params["resource"] = resource
    if scope is not None:
        params["scope"] = scope
    return params


async def provider_callback(hub: Any, location: str, *, alias: str = "gitlab", code: str = "prov-code") -> Any:
    """Отработать редирект на AS целевой системы: вернуться на ``/oauth/callback/{alias}``."""
    state_value = query_of(location)["state"]
    return await hub.client.get(
        f"/oauth/callback/{alias}", params={"code": code, "state": state_value}
    )


async def submit_consent(
    hub: Any, html: str, *, preset: str = "readonly", groups: list[str] | None = None,
    action: str = "allow", csrf: str | None = None,
) -> Any:
    fields = hidden_inputs(html)
    data: dict[str, Any] = {
        "tx": fields.get("tx", ""),
        "action": action,
        "preset": preset,
        "groups": list(groups if groups is not None else ["code_review"]),
    }
    token = csrf if csrf is not None else fields.get("csrf_token", "")
    if token is not None:
        data["csrf_token"] = token
    return await hub.client.post("/oauth/consent", data=data)


async def authorize_to_code(
    hub: Any,
    client_id: str,
    *,
    challenge: str,
    alias: str = "gitlab",
    redirect_uri: str = LOOPBACK_REDIRECT,
    preset: str = "readonly",
    groups: list[str] | None = None,
    state: str | None = "state-1",
    scope: str | None = None,
    resource: str | None = "https://hub.test/mcp/gitlab",
    provider_code: str = "prov-code",
) -> str:
    """Полный путь ``/oauth/authorize`` → OAuth системы → экран прав → код авторизации."""
    response = await hub.client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id,
            redirect_uri=redirect_uri,
            challenge=challenge,
            state=state,
            resource=resource,
            scope=scope,
        ),
    )
    if response.status_code == 302 and response.headers["location"].startswith("http"):
        location = response.headers["location"]
        if location.startswith(redirect_uri):
            return query_of(location)["code"]
        response = await provider_callback(hub, location, alias=alias, code=provider_code)
    if response.status_code == 302:
        return query_of(response.headers["location"])["code"]
    assert response.status_code == 200, response.text
    response = await submit_consent(hub, response.text, preset=preset, groups=groups)
    assert response.status_code == 302, response.text
    return query_of(response.headers["location"])["code"]


async def exchange_code(
    hub: Any,
    *,
    code: str,
    client_id: str,
    verifier: str | None,
    redirect_uri: str | None = LOOPBACK_REDIRECT,
) -> Any:
    data = {"grant_type": "authorization_code", "code": code, "client_id": client_id}
    if verifier is not None:
        data["code_verifier"] = verifier
    if redirect_uri is not None:
        data["redirect_uri"] = redirect_uri
    return await hub.client.post("/oauth/token", data=data)


async def refresh_grant(hub: Any, *, refresh_token: str, client_id: str, **extra: str) -> Any:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    data.update(extra)
    return await hub.client.post("/oauth/token", data=data)


async def seed_connection(
    hub: Any,
    *,
    user_id: str = "u1",
    alias: str = "gitlab",
    preset: str = "readonly",
    groups: tuple[str, ...] | list[str] = ("code_review",),
    status: str = "connected",
    access_token: str = UPSTREAM_ACCESS,
    refresh_token: str | None = UPSTREAM_REFRESH,
    expires_in: int | None = 7200,
    with_tokens: bool = True,
) -> Any:
    """Готовое подключение пользователя к целевой системе (без прохождения OAuth)."""
    from hub.broker import UpstreamTokens

    await ensure_user(hub, user_id)
    conn = await hub.app.state.broker.upsert_connection(
        user_id=user_id, alias=alias, status=status, preset=preset, groups=list(groups)
    )
    if with_tokens:
        await hub.app.state.broker.save_tokens(
            conn,
            UpstreamTokens(
                access_token=access_token, refresh_token=refresh_token, expires_in=expires_in
            ),
        )
    return conn


async def register_client_directly(hub: Any, *, redirect_uri: str = LOOPBACK_REDIRECT) -> str:
    """Зарегистрировать клиента в обход HTTP (не расходует окно rate-limit регистраций)."""
    body = await hub.app.state.oauth.register_client(
        {"redirect_uris": [redirect_uri], "client_name": "test-client"}, ip="127.0.0.1"
    )
    return str(body["client_id"])


async def issue_hub_tokens(
    hub: Any,
    *,
    user_id: str = "u1",
    alias: str = "gitlab",
    connection_id: int | None = None,
    client_id: str | None = None,
    scope: str | None = None,
    chain_id: str | None = None,
) -> dict[str, Any]:
    from hub.crypto import random_token

    if client_id is None:
        client_id = await register_client_directly(hub)
    return await hub.app.state.oauth.issue_tokens(
        client_id=client_id,
        user_id=user_id,
        alias=alias,
        connection_id=connection_id,
        scope=scope or f"{alias}:readonly",
        chain_id=chain_id or random_token(),
    )


async def connected_client(
    hub: Any,
    *,
    user_id: str = "u1",
    alias: str = "gitlab",
    preset: str = "readonly",
    groups: tuple[str, ...] | list[str] = ("code_review",),
    client_id: str | None = None,
    **conn_kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """Подключение + выданная пара токенов Hub для него (клиент регистрируется, если не задан)."""
    conn = await seed_connection(
        hub, user_id=user_id, alias=alias, preset=preset, groups=groups, **conn_kwargs
    )
    tokens = await issue_hub_tokens(
        hub,
        user_id=user_id,
        alias=alias,
        connection_id=conn.id,
        scope=f"{alias}:{preset}",
        client_id=client_id,
    )
    tokens["client_id"] = (
        await fetch_rows(
            hub.app,
            "SELECT client_id FROM refresh_tokens WHERE token_sha256 = :d",
            d=sha256_hex(tokens["refresh_token"]),
        )
    )[0]["client_id"]
    return conn, tokens


WEB_LOGIN_POLL_RE = re.compile(r"/auth/login/poll/([0-9a-fA-F-]+)")


async def litellm_web_login(
    hub: Any,
    *,
    next_url: str = "/ui/connections",
    user_id: str = "u1",
    email: str | None = "u1@corp.test",
    key: str = "sk-web-1",
) -> Any:
    """Полный вход в веб через CLI-SSO LiteLLM (HUB_WEB_AUTH=litellm): страница → опрос → cookie."""
    mock_start(hub.litellm, start_body())
    page = await hub.client.get("/auth/login", params={"next": next_url})
    assert page.status_code == 200, page.text
    match = WEB_LOGIN_POLL_RE.search(page.text)
    assert match, page.text
    login_id = match.group(1)
    claims: dict[str, Any] = {"sub": user_id, "exp": int(hub.clock.time()) + 3600}
    if email is not None:
        claims["email"] = email
    mock_poll(hub.litellm, ready_body(make_jwt(claims), user_id=user_id))
    mock_key_generate(hub.litellm, key)
    return await hub.client.get(f"/auth/login/poll/{login_id}")


async def add_key(hub: Any, key: str, user_id: str = "u1", **kwargs: Any) -> None:
    """Ключ LiteLLM для уже существующего (или создаваемого) пользователя."""
    await ensure_user(hub, user_id)
    await insert_key(hub.app, key, user_id, **kwargs)


def parse_db_datetime(value: Any) -> datetime:
    """Значение DATETIME из «сырого» SQL (SQLite отдаёт строку) → aware UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


B64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def tamper_signature(token: str, position: int = -1) -> str:
    """Изменить значащий символ подписи JWT так, чтобы изменились сами байты подписи.

    ``position`` — индекс символа подписи (по умолчанию последний). В 43-символьной
    base64url-подписи HS256 два младших бита последнего символа не значимы, поэтому «соседняя»
    буква даёт ту же подпись; сдвиг на 4 позиции алфавита меняет байт при любом положении.
    """
    head, _, signature = token.rpartition(".")
    index = B64URL_ALPHABET.index(signature[position])
    replacement = B64URL_ALPHABET[(index + 4) % 64]
    chars = list(signature)
    chars[position] = replacement
    return f"{head}.{''.join(chars)}"


def signature_bytes(token: str) -> bytes:
    """Декодированные байты подписи JWT (для проверки, что подпись действительно изменилась)."""
    signature = token.rpartition(".")[2]
    return base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
