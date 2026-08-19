"""Вспомогательные функции тестов: каталоги, моки LiteLLM, JWT, прямой доступ к БД.

Все проверки выполняются против локальных моков (respx через ``httpx.MockTransport``, SQLite
``:memory:``, in-memory KeyValueStore, ``ManualClock``). Никаких обращений к внешним системам.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml
from sqlalchemy import text

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


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}
