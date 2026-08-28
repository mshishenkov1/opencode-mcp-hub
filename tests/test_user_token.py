"""Подключение коннектора пользовательским токеном (R-U1..R-U10): AC-169..AC-194.

Все проверки идут против локальных моков: целевая система (адрес проверки токена и upstream MCP)
поднята в ``MockNetwork``, БД — SQLite ``:memory:``, KV — in-memory. Обращений в сеть нет.
"""

from __future__ import annotations

import json
import logging
import types
import typing
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from hub.catalog import _EXCHANGE_VERBATIM_SKIP as _B252_ACTUAL_EXCHANGE_SKIP
from hub.catalog import _UPSTREAM_VERBATIM_SKIP as _B252_ACTUAL_UPSTREAM_SKIP
from hub.catalog import _VERIFY_VERBATIM_SKIP as _B252_ACTUAL_VERIFY_SKIP
from hub.catalog import (
    TokenExchange,
    TokenExchangeRevoke,
    TokenVerify,
    parse_catalog,
)
from hub.errors import CatalogError
from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    JIRA_AS,
    TAG_ENV,
    TAG_SPEC_VERIFY_URL,
    TAG_TOOLS,
    TAG_UPSTREAM,
    VERIFY_URL,
    add_key,
    audit_rows,
    bearer,
    capture_json_logs,
    catalog_doc,
    connect_with_token,
    dump_database,
    dump_kv,
    exchange_block,
    facade_server,
    fetch_rows,
    gitlab_facade,
    i3_catalog,
    issue_hub_tokens,
    jsonrpc_body,
    mcp_headers,
    native_server,
    oauth_method,
    record_text,
    restricted_facade,
    seed_connection,
    tag_spec_server,
    unconfigured_facade,
    user_token_facade,
    user_token_method,
    web_login,
)

CODE_CONNECTION = -32002
CODE_TOOL_FORBIDDEN = -32001

TOKEN = "usr-tok-1"
SECOND_TOKEN = "usr-tok-2"


# --- вспомогательное -------------------------------------------------------


async def _hub(
    make_hub: HubFactory,
    *,
    servers: list[dict[str, Any]] | None = None,
    catalog: Any = None,
    env: dict[str, str] | None = None,
    **overrides: Any,
) -> Hub:
    document = catalog if catalog is not None else catalog_doc(servers or [user_token_facade()])
    return await make_hub(
        catalog=document,
        env=TAG_ENV if env is None else env,
        base_url="https://hub.test",
        **overrides,
    )


async def _user(hub: Hub, key: str = "sk-ok", user_id: str = "u1") -> None:
    """Пользователь и его ключ LiteLLM (пользователь может быть уже создан веб-входом)."""
    await add_key(hub, key, user_id)


async def _connect(
    hub: Hub, *, alias: str = "tag", token: str = TOKEN, key: str = "sk-ok", **fields: Any
) -> httpx.Response:
    response = await connect_with_token(hub, alias=alias, token=token, key=key, **fields)
    assert response.status_code == 200, response.text
    return response


async def _connection_row(hub: Hub, *, user_id: str = "u1", alias: str = "tag") -> dict[str, Any]:
    rows = await fetch_rows(
        hub.app,
        "SELECT id, status, preset, revision, auth_method, provider_account "
        "FROM connections WHERE user_id = :u AND alias = :a",
        u=user_id,
        a=alias,
    )
    assert rows, f"подключения {user_id}/{alias} нет"
    return rows[0]


async def _token_rows(hub: Hub, connection_id: int) -> list[dict[str, Any]]:
    return await fetch_rows(
        hub.app,
        "SELECT access_token_enc, refresh_token_enc, expires_at, token_type, scopes "
        "FROM upstream_tokens WHERE connection_id = :cid",
        cid=connection_id,
    )


async def _mcp_tokens(
    hub: Hub, *, user_id: str = "u1", alias: str = "tag", preset: str = "readonly"
) -> dict[str, Any]:
    """Пара токенов Hub для уже существующего подключения (клиент MCP)."""
    row = await _connection_row(hub, user_id=user_id, alias=alias)
    return await issue_hub_tokens(
        hub, user_id=user_id, alias=alias, connection_id=row["id"], scope=f"{alias}:{preset}"
    )


async def _mcp_call(
    hub: Hub, token: str, *, alias: str = "tag", method: str = "tools/list", params: Any = None
) -> httpx.Response:
    return await hub.post(
        f"/mcp/{alias}",
        content=jsonrpc_body(method, params),
        headers=mcp_headers(token),
    )


def _expect_error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == code, body
    return body


# --- AC-169 ----------------------------------------------------------------


@pytest.mark.ac("AC-169")
async def test_catalog_accepts_auth_methods_list(make_hub: HubFactory) -> None:
    """Список способов подключения загружается, витрина показывает оба (R-U1)."""
    hub = await _hub(make_hub)
    await _user(hub)
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    server = {s["alias"]: s for s in catalog["servers"]}["tag"]
    methods = server["auth_methods"]
    assert [m["id"] for m in methods] == ["corp_oauth", "session_token"]
    assert [m["type"] for m in methods] == ["oauth2", "user_token"]
    assert [m["available"] for m in methods] == [False, True]
    # auth_kind — тип первого доступного способа: corp_oauth недоступен, значит user_token.
    assert server["auth_kind"] == "user_token"


@pytest.mark.ac("AC-169")
async def test_catalog_with_legacy_auth_field_is_unchanged(make_hub: HubFactory) -> None:
    """Сервер с прежним полем ``auth`` валиден и ведёт себя как один способ oauth2 (R-U1)."""
    hub = await _hub(make_hub, catalog=i3_catalog(), env=CATALOG_ENV)
    await _user(hub)
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in catalog["servers"]] == ["gitlab", "jira", "tag"]
    for server in catalog["servers"]:
        assert "auth_methods" not in server, server["alias"]
        assert server["auth_kind"] == "oauth2", server["alias"]


# --- AC-170 ----------------------------------------------------------------


def _invalid_auth_methods_cases() -> list[tuple[str, dict[str, Any], tuple[str, ...]]]:
    both = user_token_facade()
    both["auth"] = facade_server("x")["auth"]
    duplicate = user_token_facade(
        methods=[user_token_method("session_token"), user_token_method("session_token")]
    )
    unknown_type = user_token_method()
    unknown_type["type"] = "pat"
    extra_field = user_token_method()
    extra_field["mask"] = "^tok-"
    return [
        ("auth и auth_methods вместе", both, ("servers[0].auth_methods",)),
        ("пустой список", user_token_facade(methods=[]), ("servers[0].auth_methods",)),
        ("дубль id", duplicate, ("servers[0].auth_methods", "session_token")),
        (
            "неизвестный type",
            user_token_facade(methods=[unknown_type]),
            ("servers[0].auth_methods[0].type",),
        ),
        (
            "лишнее поле",
            user_token_facade(methods=[extra_field]),
            ("servers[0].auth_methods[0].mask",),
        ),
    ]


@pytest.mark.ac("AC-170")
@pytest.mark.parametrize(
    ("title", "server", "fragments"),
    _invalid_auth_methods_cases(),
    ids=[case[0] for case in _invalid_auth_methods_cases()],
)
async def test_invalid_auth_methods_break_start(
    make_hub: HubFactory, title: str, server: dict[str, Any], fragments: tuple[str, ...]
) -> None:
    """Некорректный ``auth_methods`` роняет старт с путём к полю (R-U1, R-C1)."""
    with pytest.raises(Exception) as excinfo:
        await _hub(make_hub, servers=[server])
    message = str(excinfo.value)
    for fragment in fragments:
        assert fragment in message, f"{title}: ожидалось {fragment!r} в сообщении {message!r}"


# --- AC-171 ----------------------------------------------------------------


def _user_token_requirement_cases() -> list[tuple[str, dict[str, Any], str]]:
    native = native_server("nat")
    native["auth_methods"] = [user_token_method()]
    no_field = user_token_method()
    del no_field["field"]
    no_verify = user_token_method()
    del no_verify["verify"]
    no_headers = user_token_method()
    del no_headers["verify"]["headers"]
    return [
        ("native", native, "servers[0].auth_methods[0].type"),
        ("без field", user_token_facade(methods=[no_field]), "servers[0].auth_methods[0].field"),
        ("без verify", user_token_facade(methods=[no_verify]), "servers[0].auth_methods[0].verify"),
        (
            "verify без headers",
            user_token_facade(methods=[no_headers]),
            "servers[0].auth_methods[0].verify.headers",
        ),
    ]


@pytest.mark.ac("AC-171")
@pytest.mark.parametrize(
    ("title", "server", "fragment"),
    _user_token_requirement_cases(),
    ids=[case[0] for case in _user_token_requirement_cases()],
)
async def test_user_token_requires_facade_field_and_verify(
    make_hub: HubFactory, title: str, server: dict[str, Any], fragment: str
) -> None:
    """``type: user_token`` требует ``mode: facade``, ``field`` и ``verify`` с ``headers`` (R-U1)."""
    with pytest.raises(Exception) as excinfo:
        await _hub(make_hub, servers=[server])
    assert fragment in str(excinfo.value), f"{title}: {excinfo.value}"


# --- AC-194 ----------------------------------------------------------------


@pytest.mark.ac("AC-194")
async def test_unset_var_inside_unavailable_method_keeps_server_visible(
    make_hub: HubFactory,
) -> None:
    """Незаданная ``${VAR}`` внутри недоступного способа не делает сервер unconfigured (R-U1)."""
    server = user_token_facade(
        status="beta",
        methods=[
            oauth_method(client_id="${TAG_OAUTH_CLIENT_ID}", client_secret="env:TAG_OAUTH_SECRET"),
            user_token_method(),
        ],
    )
    hub = await _hub(make_hub, servers=[server], env={"TAG_MCP_URL": TAG_UPSTREAM})
    await _user(hub)
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in catalog["servers"]] == ["tag"]
    assert "${" not in (await hub.get("/api/catalog", headers=bearer("sk-ok"))).text
    # ... и подключиться доступным способом можно
    await _connect(hub)
    assert (await _connection_row(hub))["status"] == "connected"


@pytest.mark.ac("AC-194")
async def test_unset_var_outside_unavailable_method_still_hides_server(
    make_hub: HubFactory,
) -> None:
    """R-C2 в остальном не изменён: незаданная переменная в другом поле beta-сервера скрывает его."""
    server = user_token_facade(
        status="beta",
        upstream_url="${TAG_MCP_URL}",
        methods=[
            oauth_method(client_id="${TAG_OAUTH_CLIENT_ID}", client_secret="env:TAG_OAUTH_SECRET"),
            user_token_method(),
        ],
    )
    hub = await _hub(make_hub, servers=[server], env={})
    await _user(hub)
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert catalog["servers"] == []
    response = await connect_with_token(hub, alias="tag", token=TOKEN)
    _expect_error(response, 404, "not_found")


# --- AC-172 ----------------------------------------------------------------


@pytest.mark.ac("AC-172")
async def test_saved_user_token_is_injected_and_client_header_dropped(
    make_hub: HubFactory,
) -> None:
    """Сохранённый токен уходит на upstream, заголовок клиента не пробрасывается (R-U2, R-P2)."""
    hub = await _hub(make_hub)
    await _user(hub)
    await _connect(hub, token=TOKEN)
    tokens = await _mcp_tokens(hub)
    hub_token = tokens["access_token"]

    response = await hub.post(
        "/mcp/tag",
        content=jsonrpc_body("tools/list"),
        headers={
            "Authorization": f"Bearer {hub_token}",
            "Cookie": "a=b",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text

    assert hub.net is not None
    sent = hub.net.upstreams["tag"].last()
    assert sent.header("authorization") == f"Bearer {TOKEN}"
    assert sent.header("cookie") is None
    leaked = [name for name, value in sent.headers.items() if hub_token in value]
    assert leaked == [], f"access-токен Hub ушёл на upstream в заголовках {leaked}"


# --- AC-173 ----------------------------------------------------------------


@pytest.mark.ac("AC-173")
async def test_successful_verify_creates_connection_with_account(make_hub: HubFactory) -> None:
    """Проверка токена у целевой системы → подключение и сохранённый ``account`` (R-U3, R-U4)."""
    hub = await _hub(make_hub)
    await _user(hub)
    assert hub.net is not None
    hub.net.verify.account = "m.ivanov"

    response = await _connect(hub, token=TOKEN)
    body = response.json()
    assert body["status"] == "connected"
    assert body["auth_method"] == "session_token"
    assert body["account"] == "m.ivanov"

    verify = hub.net.verify
    assert verify.calls == 1
    assert verify.last().method == "GET"
    assert verify.last().url == VERIFY_URL
    assert verify.last().header("authorization") == f"Bearer {TOKEN}"

    row = await _connection_row(hub)
    assert row["status"] == "connected"
    assert row["auth_method"] == "session_token"
    assert row["provider_account"] == "m.ivanov"
    tokens = await _token_rows(hub, row["id"])
    assert len(tokens) == 1
    assert hub.app.state.cipher.decrypt(tokens[0]["access_token_enc"]) == TOKEN
    assert tokens[0]["refresh_token_enc"] is None
    assert tokens[0]["expires_at"] is None
    assert tokens[0]["token_type"] == "Bearer"


# --- AC-174 ----------------------------------------------------------------


@pytest.mark.ac("AC-174")
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_is_not_saved(make_hub: HubFactory, status: int) -> None:
    """401/403 на проверке → 400 ``token_rejected``, токен не сохранён (R-U3, R-U4)."""
    hub = await _hub(make_hub)
    await _user(hub)
    assert hub.net is not None
    hub.net.verify.push(httpx.Response(status, json={"error": "unauthorized"}))

    response = await connect_with_token(hub, alias="tag", token="bad-token")
    _expect_error(response, 400, "token_rejected")

    assert hub.net.verify.calls == 1
    rows = await fetch_rows(hub.app, "SELECT id, status FROM connections")
    assert [r["status"] for r in rows] != ["connected"]
    assert await fetch_rows(hub.app, "SELECT connection_id FROM upstream_tokens") == []


# --- AC-175 ----------------------------------------------------------------


@pytest.mark.ac("AC-175")
@pytest.mark.parametrize(
    ("title", "outcome"),
    [
        ("500", httpx.Response(500, json={"error": "boom"})),
        ("сетевая ошибка", httpx.ConnectError("connection refused")),
        ("таймаут", httpx.ReadTimeout("timed out")),
    ],
    ids=["500", "network", "timeout"],
)
async def test_unavailable_upstream_keeps_previous_connection(
    make_hub: HubFactory, title: str, outcome: Any
) -> None:
    """Недоступность целевой системы на проверке не разрушает прежнее подключение (R-U3, R-U4)."""
    hub = await _hub(make_hub)
    await _user(hub)
    await _connect(hub, token=TOKEN)
    before = await _connection_row(hub)
    assert hub.net is not None
    hub.net.verify.push(outcome)

    response = await connect_with_token(hub, alias="tag", token=SECOND_TOKEN)
    _expect_error(response, 502, "upstream_unavailable")

    after = await _connection_row(hub)
    assert after["status"] == "connected"
    assert after["revision"] == before["revision"]
    rows = await _token_rows(hub, after["id"])
    assert len(rows) == 1
    assert hub.app.state.cipher.decrypt(rows[0]["access_token_enc"]) == TOKEN

    tokens = await _mcp_tokens(hub)
    assert (await _mcp_call(hub, tokens["access_token"])).status_code == 200
    assert hub.net.upstreams["tag"].last().header("authorization") == f"Bearer {TOKEN}"


# --- AC-176 ----------------------------------------------------------------


@pytest.mark.ac("AC-176")
async def test_success_response_shape_has_no_token(make_hub: HubFactory) -> None:
    """Успешный ответ — договорённые поля §22, без значения токена (R-U4, R-U9)."""
    hub = await _hub(make_hub)
    await _user(hub)
    response = await _connect(hub, token=TOKEN, preset="readonly")
    body = response.json()
    assert set(body) == {
        "alias",
        "status",
        "auth_method",
        "preset",
        "groups",
        "account",
        "updated_at",
        # §28 ревизии 4: ответ дополнен происхождением токена и сроком сессии (R-U16).
        "token_origin",
        "token_origin_reason",
        "session_expires_at",
    }
    # Способ без блока exchange: обмен не предусмотрен, предупреждать не о чем (R-U14.1).
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] is None
    assert body["session_expires_at"] is None
    assert body["alias"] == "tag"
    assert body["preset"] == "readonly"
    assert body["groups"] == []
    assert body["updated_at"]
    assert TOKEN not in response.text
    assert all(TOKEN not in str(value) for value in body.values())

    connected = await audit_rows(hub.app, "connection_connected")
    assert len(connected) == 1
    assert connected[0]["details"]["auth_method"] == "session_token"
    assert TOKEN not in str(connected[0])


# --- AC-177 ----------------------------------------------------------------


@pytest.mark.ac("AC-177")
async def test_invalid_body_is_rejected_without_touching_upstream(make_hub: HubFactory) -> None:
    """Невалидное тело — 400 ``invalid_request`` до обращения к целевой системе (R-U4)."""
    hub = await _hub(make_hub)
    await _user(hub)
    assert hub.net is not None

    raw_cases = ["не json".encode(), b"[1, 2]"]
    json_cases: list[Any] = [
        {},
        {"token": ""},
        {"token": "t" * 4097},
        {"token": "ok", "preset": "admin"},
        {"token": "ok", "groups": "core"},
    ]
    for raw in raw_cases:
        response = await hub.post(
            "/api/me/connections/tag/token",
            content=raw,
            headers={**bearer("sk-ok"), "Content-Type": "application/json"},
        )
        body = _expect_error(response, 400, "invalid_request")
        assert body["message"]
    for payload in json_cases:
        response = await connect_with_token(hub, alias="tag", body=payload)
        body = _expect_error(response, 400, "invalid_request")
        assert body["message"], payload

    assert hub.net.verify.calls == 0, "негативы дошли до целевой системы"
    assert await fetch_rows(hub.app, "SELECT id FROM connections") == []
    assert await fetch_rows(hub.app, "SELECT connection_id FROM upstream_tokens") == []


# --- AC-178 ----------------------------------------------------------------


@pytest.mark.ac("AC-178")
async def test_token_endpoint_visibility_and_authentication(make_hub: HubFactory) -> None:
    """Подключение токеном — только видимый facade-сервер и только с аутентификацией (R-U4)."""
    hub = await _hub(
        make_hub,
        servers=[
            native_server("nat"),
            unconfigured_facade("unc"),
            restricted_facade("closed"),
            user_token_facade("ok"),
        ],
        env={**TAG_ENV, "B_SECRET": "b-secret"},
    )
    await _user(hub)
    assert hub.net is not None

    for alias in ("missing", "nat", "unc", "closed"):
        response = await connect_with_token(hub, alias=alias, token=TOKEN)
        _expect_error(response, 404, "not_found")

    anonymous = await hub.post("/api/me/connections/ok/token", json={"token": TOKEN})
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error"] == "unauthorized"

    assert hub.net.verify.calls == 0
    assert await fetch_rows(hub.app, "SELECT id FROM connections") == []


# --- AC-179 ----------------------------------------------------------------


@pytest.mark.ac("AC-179")
async def test_auth_method_selection_rules(make_hub: HubFactory) -> None:
    """Способ подключения: обязательность, известность, тип и доступность (R-U4)."""
    multi = user_token_facade(
        "multi",
        methods=[
            user_token_method("a", title="Способ A"),
            user_token_method("b", title="Способ B"),
            oauth_method("corp"),
        ],
    )
    single = user_token_facade("single", methods=[user_token_method()])
    hub = await _hub(make_hub, servers=[multi, single])
    await _user(hub)
    assert hub.net is not None

    without = await connect_with_token(hub, alias="multi", token=TOKEN)
    body = _expect_error(without, 400, "invalid_request")
    assert "способ" in body["message"].lower()

    unknown = await connect_with_token(hub, alias="multi", token=TOKEN, method="nope")
    _expect_error(unknown, 400, "invalid_request")

    unavailable = await connect_with_token(hub, alias="multi", token=TOKEN, method="corp")
    body = _expect_error(unavailable, 409, "auth_method_unavailable")
    assert body["message"] == "OAuth-приложение ТЭГ ещё не выдано администраторами"

    assert hub.net.verify.calls == 0, "выбор способа проверяется до обращения к целевой системе"
    assert await fetch_rows(hub.app, "SELECT id FROM connections") == []

    ok = await _connect(hub, alias="single", token=TOKEN)
    assert ok.json()["auth_method"] == "session_token"


# --- AC-180 ----------------------------------------------------------------


@pytest.mark.ac("AC-180")
async def test_reconnect_replaces_token_without_leaving_the_old_one(
    make_hub: HubFactory,
) -> None:
    """Повторное подключение заменяет токен: истории значений нет (R-U4)."""
    hub = await _hub(make_hub)
    await _user(hub)
    await _connect(hub, token=TOKEN)
    before = await _connection_row(hub)

    await _connect(hub, token=SECOND_TOKEN)
    after = await _connection_row(hub)
    assert after["id"] == before["id"]
    assert after["revision"] > before["revision"]

    rows = await _token_rows(hub, after["id"])
    assert len(rows) == 1
    assert hub.app.state.cipher.decrypt(rows[0]["access_token_enc"]) == SECOND_TOKEN

    tokens = await _mcp_tokens(hub)
    assert (await _mcp_call(hub, tokens["access_token"])).status_code == 200
    assert hub.net is not None
    assert hub.net.upstreams["tag"].last().header("authorization") == f"Bearer {SECOND_TOKEN}"

    assert TOKEN not in await dump_database(hub.app)
    assert TOKEN not in dump_kv(hub.app)


# --- AC-181 ----------------------------------------------------------------


@pytest.mark.ac("AC-181")
async def test_disconnect_erases_token_and_never_calls_revoke_url(
    make_hub: HubFactory,
) -> None:
    """Отключение стирает токен и не обращается к ``revoke_url`` (R-U5, решение 66)."""
    hub = await _hub(
        make_hub,
        servers=[user_token_facade(methods=[oauth_method(as_base=JIRA_AS), user_token_method()])],
    )
    await _user(hub)
    await _connect(hub, token=TOKEN)
    connection_id = (await _connection_row(hub))["id"]
    tokens = await _mcp_tokens(hub)
    assert hub.net is not None
    revoke_mock = hub.net.providers["jira"]

    response = await hub.client.delete("/api/me/connections/tag", headers=bearer("sk-ok"))
    assert response.status_code == 200, response.text
    assert response.json() == {"alias": "tag", "status": "not_connected"}

    row = await _connection_row(hub)
    assert row["status"] == "not_connected"
    assert row["auth_method"] is None
    assert await _token_rows(hub, connection_id) == []
    assert revoke_mock.revoke_requests == [], "личный токен пользователя отправлен на revoke_url"
    assert revoke_mock.token_requests == []

    call = await _mcp_call(hub, tokens["access_token"])
    if call.status_code == 200:
        error = call.json()["error"]
        assert error["code"] == CODE_CONNECTION
        assert error["data"]["reason"] == "not_connected"
    else:
        assert call.status_code == 401, call.text
    assert hub.net.upstreams["tag"].calls == 0

    config = await hub.get("/remote-config", headers=bearer("sk-ok"))
    assert config.status_code == 200, config.text
    assert "tag" not in str(config.json())
    assert len(await audit_rows(hub.app, "connection_disconnected")) == 1


# --- AC-182 ----------------------------------------------------------------


@pytest.mark.ac("AC-182")
async def test_upstream_401_moves_user_token_connection_to_needs_reauth(
    make_hub: HubFactory,
) -> None:
    """401 от upstream → ``needs_reauth`` без обновления; новый токен восстанавливает (R-U6)."""
    hub = await _hub(
        make_hub,
        servers=[user_token_facade(methods=[oauth_method(as_base=JIRA_AS), user_token_method()])],
    )
    await _user(hub)
    await _connect(hub, token=TOKEN)
    tokens = await _mcp_tokens(hub)
    assert hub.net is not None
    upstream = hub.net.upstreams["tag"]
    upstream.push(httpx.Response(401, json={"error": "unauthorized"}))

    response = await _mcp_call(hub, tokens["access_token"])
    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["code"] == CODE_CONNECTION
    assert error["data"]["reason"] == "needs_reauth"
    assert error["data"]["hint_url"] == "https://hub.test/ui/servers/tag"

    assert upstream.calls == 1, "запрос к upstream повторён после 401"
    assert hub.net.providers["jira"].token_requests == [], "была попытка обновить токен"
    assert (await _connection_row(hub))["status"] == "needs_reauth"
    assert len(await audit_rows(hub.app, "connection_needs_reauth")) == 1

    await _connect(hub, token=SECOND_TOKEN)
    assert (await _connection_row(hub))["status"] == "connected"

    again = await _mcp_call(hub, tokens["access_token"])
    assert again.status_code == 200, again.text
    assert "result" in again.json()
    assert upstream.last().header("authorization") == f"Bearer {SECOND_TOKEN}"


# --- AC-183 ----------------------------------------------------------------


@pytest.mark.ac("AC-183")
async def test_background_refresh_never_picks_user_token_connections(
    make_hub: HubFactory,
) -> None:
    """Фоновое обновление выбирает только OAuth-подключение (R-U6, R-B4)."""
    hub = await _hub(
        make_hub,
        servers=[
            gitlab_facade(),
            user_token_facade(methods=[oauth_method(as_base=JIRA_AS), user_token_method()]),
        ],
        token_refresh_lead=300,
    )
    await _user(hub)
    await _connect(hub, token=TOKEN)
    tag_row = await _connection_row(hub)
    before = await _token_rows(hub, tag_row["id"])
    await seed_connection(hub, alias="gitlab", expires_in=200)

    assert await hub.app.state.token_refresher.run_once() == 1

    assert hub.net is not None
    assert len(hub.net.providers["gitlab"].token_requests) == 1
    assert hub.net.providers["gitlab"].token_requests[0].form["grant_type"] == "refresh_token"
    assert hub.net.providers["jira"].token_requests == [], "обновлялось подключение user_token"
    after = await _connection_row(hub)
    assert after["status"] == "connected"
    assert await _token_rows(hub, tag_row["id"]) == before


# --- AC-184 ----------------------------------------------------------------


@pytest.mark.ac("AC-184")
async def test_permission_upgrade_for_user_token_keeps_connection(make_hub: HubFactory) -> None:
    """readonly → readwrite для user_token не требует повторной авторизации (R-U7, AC-111)."""
    hub = await _hub(make_hub, servers=[gitlab_facade(), user_token_facade()])
    await _user(hub)
    await _connect(hub, token=TOKEN, preset="readonly")
    tokens = await _mcp_tokens(hub, preset="readonly")
    await seed_connection(hub, alias="gitlab", preset="readonly")
    assert hub.net is not None
    hub.net.upstreams["tag"].tools = [dict(tool) for tool in TAG_TOOLS]

    upgraded = await hub.client.put(
        "/api/me/connections/tag/permissions",
        json={"preset": "readwrite"},
        headers=bearer("sk-ok"),
    )
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["status"] == "connected"
    assert (await _connection_row(hub))["status"] == "connected"

    listed = await _mcp_call(hub, tokens["access_token"])
    assert listed.status_code == 200, listed.text
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert names == [tool["name"] for tool in TAG_TOOLS]

    # Для OAuth-подключения поведение AC-111 прежнее.
    oauth = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readwrite", "groups": ["repo_write"]},
        headers=bearer("sk-ok"),
    )
    assert oauth.status_code == 200, oauth.text
    assert oauth.json()["status"] == "needs_reauth"
    assert oauth.json()["message"]


# --- AC-185 ----------------------------------------------------------------


@pytest.mark.ac("AC-185")
async def test_catalog_publishes_methods_without_secrets(make_hub: HubFactory) -> None:
    """Витрина отдаёт способы по границе типа: у oauth2 — ничего сверх прежнего, у доступного
    user_token — verify и блок upstream карточки (R-U8, R-U8.1)."""
    hub = await _hub(make_hub, servers=[user_token_facade(), native_server("plain")])
    await _user(hub)
    response = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert response.status_code == 200, response.text
    servers = {s["alias"]: s for s in response.json()["servers"]}

    methods = {m["id"]: m for m in servers["tag"]["auth_methods"]}
    assert set(methods) == {"corp_oauth", "session_token"}
    # R-U16 (ревизия 4): к публичному виду способа добавлен признак выпуска постоянного токена;
    # у способа без блока exchange он false, сам блок наружу не отдаётся.
    # R-U8.1 п. 1: состав oauth2 не меняется ни на один ключ — независимо от available.
    assert set(methods["corp_oauth"]) == {
        "id",
        "title",
        "type",
        "available",
        "unavailable_reason",
        "issues_permanent_token",
    }
    assert methods["corp_oauth"]["issues_permanent_token"] is False
    assert methods["session_token"]["issues_permanent_token"] is False
    assert methods["corp_oauth"]["available"] is False
    assert methods["corp_oauth"]["unavailable_reason"]
    assert "field" not in methods["corp_oauth"]
    field = methods["session_token"]["field"]
    assert {"label", "hint", "docs_url", "secret", "min_length", "max_length"} <= set(field)
    assert field["secret"] is True

    # R-U8.1 п. 2: у доступного user_token публикуется verify дословно — адрес проверки и шаблон
    # заголовка отныне часть публичного представления способа, а не секрет.
    assert set(methods["session_token"]) == {
        "id",
        "title",
        "type",
        "available",
        "unavailable_reason",
        "issues_permanent_token",
        "field",
        "verify",
    }
    assert methods["session_token"]["verify"] == {
        "url": VERIFY_URL,
        "method": "GET",
        "headers": {"Authorization": "Bearer {{access_token}}"},
        "expect_status": None,
        "account_field": "username",
        "require_account": False,
    }
    # У способа не объявлен exchange в каталоге — блока нет (независимо от границы публикации).
    assert "exchange" not in methods["session_token"]

    # R-U8.1 п. 4: блок upstream карточки — адрес целевого сервера и шаблоны заголовков, потому
    # что у неё есть доступный способ user_token.
    assert servers["tag"]["upstream"] == {
        "url": TAG_UPSTREAM,
        "credential_headers": {"Authorization": "Bearer {{access_token}}"},
    }

    forbidden_keys = {
        "authorize_url",
        "token_url",
        "revoke_url",
        "scopes",
        "client_id",
        "client_secret",
    }
    assert not (_all_keys(response.json()) & forbidden_keys)
    for forbidden_value in ("tag-client-id", "env:GL_SECRET", "GL_SECRET"):
        assert forbidden_value not in response.text
    # У сервера без auth_methods ни ключа auth_methods, ни ключа upstream в публичном
    # представлении нет вовсе (R-U8.1 п. 1, п. 4; AC-22 не изменился).
    assert "auth_methods" not in servers["plain"]
    assert "upstream" not in servers["plain"]


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


# --- AC-252 ------------------------------------------------------------------
# Ревизия 4.4, R-U8.1 п. 3, 6-9: непубликуемое значение снимает весь блок, а не часть, и
# оставляет ровно один след WARNING на блок при каждой загрузке каталога.
#
# Дополнено по reports/review-rev44-1.json (findings 1 и 2, must_fix — вердикт request_changes):
# ни одна карточка не доводила непубликуемые/публикуемые static_headers до опубликованного
# upstream (finding 1: инъекция I11 — снять 'static_headers' из проверки дословности в
# _publication — была зелёной, хотя выпускала ${VAR} наружу), и ни одна карточка не несла двух
# способов user_token, один из которых «грязный», другой чистый (finding 2: инъекция I12 —
# raw_list[::-1] — тоже была зелёной, хотя путала соответствие сырых способов разобранным и
# публиковала verify грязного способа с развёрнутым секретом). Карточки 'clean' и 'statichdr'
# закрывают finding 1, карточка 'twoways' — finding 2.
#
# Дополнено по reports/review-rev44-2.json (findings 1 и 2, оба blocker — вердикт request_changes):
# (а) три набора перечня R-U8.1 п. 7 — exchange.headers, exchange.body и exchange.revoke.body —
# не имели собственного негативного прогона: инъекции I13/I14/I15 (добавление ('headers',),
# ('body',), ('revoke','body') в _EXCHANGE_VERBATIM_SKIP) давали зелёный прогон, хотя каждая
# выпускала секрет наружу. Карточки 'exchhdr', 'exchbody' и 'revokebody' закрывают ровно эти три
# набора — каждая кладёт непубликуемое значение единственно в свой набор, остальные части
# exchange (и verify, и upstream) остаются дословными, чтобы снятие не «расползлось». Для тел
# годится только ${VAR} — env:VAR вне headers отвергает схема (AC-15, R-U8.1 п. 6).
# (б) новая инъекция N5 (`_is_verbatim` сведена к `value.startswith("${")`) выпускала секрет из
# credential_headers, static_headers и verify.headers сразу, потому что во всех прежних карточках
# непубликуемое значение занимало строку целиком — форма «Bearer ${VAR}» (подстановка внутри
# строки) не была покрыта ничем, хотя это самая частая запись боевого каталога. Карточка 'midstr'
# кладёт ${VAR} внутрь строки в начале, в середине, в конце и дважды в одной строке — по одному
# разу на каждый из трёх наборов. Инфиксная форма ссылки `env:VAR` не нужна: `ENV_REF_RE` требует
# точного совпадения строки целиком (R-U8.1 п. 6, «является ссылкой env:VAR» — а не «содержит»),
# поэтому `env:` внутри большей строки — обычный литерал, а не ссылка, и утечки не образует.

# Четыре адреса — исключение из дословности (R-U8.1 п. 6): подставляются из ${VAR}.
_B252_VERIFY_URL = "https://tag-direct.test/api/v4/users/me"
_B252_EXCHANGE_URL = "https://tag-direct.test/api/v4/users/me/tokens"
_B252_REVOKE_URL = "https://tag-direct.test/api/v4/users/tokens/revoke"
_B252_MCP_URL = "https://mcp-tag-direct.test/mcp"
# Значение переменной, подставляемой в заголовок 'subst' — маркер утечки, а не адрес.
_B252_GW_HEADER_VALUE = "secret-value"
# Значение переменной, подставляемой в static_headers карточки 'statichdr' (finding 1): маркер
# утечки, уникальная подстрока в пределах этого теста.
_B252_STATIC_LEAK_VALUE = "static-header-leak-marker-4Q"
# Значение переменной, подставляемой в verify.headers «грязного» способа карточки 'twoways'
# (finding 2): маркер утечки, уникальная подстрока в пределах этого теста.
_B252_METHOD_LEAK_VALUE = "method-independence-leak-marker-7Z"

# rev44-2, blocker 1: по одному маркеру на каждый из трёх непокрытых наборов перечня п. 7.
# 'exchhdr' — ссылка env:VAR (годится, набор не тело); имени переменной достаточно как маркера,
# значение никогда не читается и не подставляется (как GW_KEY/TAG_TOKEN выше).
_B252_EXCHHDR_LEAK_NAME = "B252_EXCHHDR_LEAK"
_B252_EXCHBODY_LEAK_VALUE = "exchange-body-leak-marker-K3"
_B252_REVOKEBODY_LEAK_VALUE = "exchange-revoke-body-leak-marker-Q9"

# rev44-2, blocker 2: подстановка ${VAR} внутри большей строки — в начале, в середине, в конце и
# дважды в одной строке — по одному разу на credential_headers, static_headers и verify.headers.
_B252_MIDSTR_START_VALUE = "midstr-start-leak-marker-A1"
_B252_MIDSTR_D1_VALUE = "midstr-double-leak-marker-B2"
_B252_MIDSTR_D2_VALUE = "midstr-double-leak-marker-C3"
_B252_MIDSTR_CRED_VALUE = "midstr-cred-leak-marker-D4"
_B252_MIDSTR_STATIC_VALUE = "midstr-static-leak-marker-E5"

# rev44-3, minor (положительный прогон п. 5): подстановка ${VAR} внутри exchange.list — значения
# exchange.list наружу не идут никогда (R-U15.3), поэтому они не имеют права снять блок exchange.
_B252_LISTEX_LEAK_VALUE = "listex-not-a-leak-marker-L7"

_B252_ENV = {
    "B252_VERIFY_URL": _B252_VERIFY_URL,
    "B252_EXCHANGE_URL": _B252_EXCHANGE_URL,
    "B252_REVOKE_URL": _B252_REVOKE_URL,
    "B252_MCP_URL": _B252_MCP_URL,
    "GW_HEADER": _B252_GW_HEADER_VALUE,
    "B252_STATIC_LEAK": _B252_STATIC_LEAK_VALUE,
    "B252_METHOD_LEAK": _B252_METHOD_LEAK_VALUE,
    "B252_EXCHBODY_LEAK": _B252_EXCHBODY_LEAK_VALUE,
    "B252_REVOKEBODY_LEAK": _B252_REVOKEBODY_LEAK_VALUE,
    "B252_MIDSTR_START": _B252_MIDSTR_START_VALUE,
    "B252_MIDSTR_D1": _B252_MIDSTR_D1_VALUE,
    "B252_MIDSTR_D2": _B252_MIDSTR_D2_VALUE,
    "B252_MIDSTR_CRED": _B252_MIDSTR_CRED_VALUE,
    "B252_MIDSTR_STATIC": _B252_MIDSTR_STATIC_VALUE,
    "B252_LISTEX_LEAK": _B252_LISTEX_LEAK_VALUE,
}

# Все маркеры-секреты этого теста — используются и для тела ответа, и для полного текста записи
# журнала (getMessage() + extra=, см. record_text в tests/support.py и finding 4 отчёта).
_B252_LEAK_MARKERS = (
    "env:",
    "TAG_TOKEN",
    "GW_KEY",
    _B252_GW_HEADER_VALUE,
    _B252_STATIC_LEAK_VALUE,
    "B252_STATIC_LEAK",
    _B252_METHOD_LEAK_VALUE,
    "B252_METHOD_LEAK",
    _B252_EXCHHDR_LEAK_NAME,
    _B252_EXCHBODY_LEAK_VALUE,
    "B252_EXCHBODY_LEAK",
    _B252_REVOKEBODY_LEAK_VALUE,
    "B252_REVOKEBODY_LEAK",
    _B252_MIDSTR_START_VALUE,
    _B252_MIDSTR_D1_VALUE,
    _B252_MIDSTR_D2_VALUE,
    _B252_MIDSTR_CRED_VALUE,
    _B252_MIDSTR_STATIC_VALUE,
    _B252_LISTEX_LEAK_VALUE,
)


def _b252_method() -> dict[str, Any]:
    """Способ ``session_token`` с verify и exchange, все адреса — через ``${VAR}`` (дословно
    во всём остальном): опорная точка, от которой каждая карточка отклоняется на одно значение."""
    method = user_token_method("session_token")
    method["verify"]["url"] = "${B252_VERIFY_URL}"
    method["exchange"] = exchange_block(
        url="${B252_EXCHANGE_URL}", list_url=None, revoke_url="${B252_REVOKE_URL}"
    )
    return method


def _b252_clean_card(alias: str) -> dict[str, Any]:
    """Эталонная карточка «clean»: все значения дословны (адреса — через ``${VAR}``), все блоки
    публикуются, ``static_headers`` непусты и доходят до опубликованного ``upstream`` (единственное
    место в наборе, где это происходит). Отдельная функция — не только карточка исходного прогона
    (``alias='clean'``), но и опорная точка сторожа перечня (§34.5): он ходит по опубликованному
    представлению ровно этой карточки, а не по списку, зашитому в код."""
    return user_token_facade(
        alias,
        methods=[_b252_method()],
        upstream_url="${B252_MCP_URL}",
        static_headers={"X-Static": "static-header-literal-value"},
    )


@pytest.mark.ac("AC-252")
async def test_publication_boundary_drops_whole_block_and_leaves_a_trace(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Граница держится целиком: непубликуемое значение снимает весь блок (не часть), соседние
    блоки и способ остаются на месте, секрет не попадает в тело ответа, а каждая загрузка
    каталога оставляет ровно десять записей WARNING (R-U8.1 п. 3, 6-9).

    'clean' и 'statichdr' закрывают finding 1 отчёта reports/review-rev44-1.json: 'clean' несёт
    непустые дословные static_headers, доходящие до опубликованного upstream (единственное место
    в наборе, где это происходит), а у 'statichdr' единственное непубликуемое значение лежит
    именно в static_headers (credential_headers сами по себе дословны) — весь upstream обязан
    быть снят. 'twoways' закрывает finding 2: два доступных способа user_token на одной
    карточке, один «грязный» (${VAR} в verify.headers), другой чистый — снятие блока у грязного
    не должно задевать чистый и не должно перепутать, какому способу какой блок принадлежит.

    'exchhdr', 'exchbody' и 'revokebody' закрывают finding 1 отчёта reports/review-rev44-2.json
    (blocker): три набора перечня R-U8.1 п. 7 — exchange.headers, exchange.body и
    exchange.revoke.body — не имели собственного негативного прогона, и вывод любого из них из
    проверки дословности (инъекции I13/I14/I15) выпускал секрет наружу при зелёном прогоне.
    Каждая из трёх карточек кладёт непубликуемое значение единственно в свой набор — остальные
    части exchange остаются дословными, а весь exchange (вместе с вложенным revoke) обязан быть
    снят целиком. 'midstr' закрывает finding 2 (blocker): во всех прежних карточках непубликуемое
    значение занимало строку целиком, и инъекция N5 (проверка '${' сведена к началу строки)
    выпускала секрет из credential_headers, static_headers и verify.headers сразу. 'midstr'
    кладёт ${VAR} внутрь строки — в начале, в середине, в конце и дважды в одной строке — по
    одному разу на каждый из трёх наборов.

    Дополнено по reports/review-rev44-3.json (minor, задание §34.5): 'listex' — положительный
    прогон п. 5. Утверждение «значения exchange.list на публикацию exchange не влияют» до этого
    прогона не имело, ни при каком изменении. Карточка кладёт ${VAR} внутрь exchange.list.headers
    при дословных headers, body и revoke — exchange обязан остаться опубликованным целиком, без
    ключа 'list', и записи следа на эту карточку быть не должно ни одной.
    """
    clean = _b252_clean_card("clean")

    envverify_method = _b252_method()
    envverify_method["verify"]["headers"] = {
        "Authorization": "Bearer {{access_token}}",
        "X-Extra": "env:TAG_TOKEN",
    }
    envverify = user_token_facade(
        "envverify", methods=[envverify_method], upstream_url="${B252_MCP_URL}"
    )

    envcred = user_token_facade(
        "envcred",
        methods=[_b252_method()],
        upstream_url="${B252_MCP_URL}",
        credential_headers={"Authorization": "Bearer {{access_token}}", "X-Key": "env:GW_KEY"},
        static_headers={"X-Static": "static-literal-value"},
    )

    subst_method = _b252_method()
    subst_method["exchange"]["revoke"]["headers"] = {
        "Authorization": "Bearer {{access_token}}",
        "X-Revoke-Extra": "${GW_HEADER}",
    }
    subst = user_token_facade("subst", methods=[subst_method], upstream_url="${B252_MCP_URL}")

    # 'statichdr' (finding 1, blocker): credential_headers дословны, а static_headers несёт
    # подстановку ${VAR} — единственное непубликуемое значение во всём блоке upstream карточки.
    # Правило «только целиком» (R-U8.1 п. 7) обязано снять upstream без остатка, независимо от
    # того, что credential_headers сами по себе публикуемы.
    statichdr = user_token_facade(
        "statichdr",
        methods=[_b252_method()],
        upstream_url="${B252_MCP_URL}",
        credential_headers={"Authorization": "Bearer {{access_token}}"},
        static_headers={"X-Static-Secret": "${B252_STATIC_LEAK}"},
    )

    closed_method = _b252_method()
    closed_method["available"] = False
    closed_method["unavailable_reason"] = "Способ отключён администратором"
    closed = user_token_facade("closed", methods=[closed_method])

    # 'twoways' (finding 2, major): два доступных способа user_token — 'dirty_token' со ссылкой
    # ${VAR} в verify.headers и 'clean_token' целиком дословный. Соответствие сырых способов
    # (raw_list) разобранным (model.auth_methods) не должно путаться местами: снятие блока у
    # dirty_token не имеет права ни задеть clean_token, ни оставить блок dirty_token нетронутым.
    dirty_method = _b252_method()
    dirty_method["id"] = "dirty_token"
    dirty_method["verify"]["headers"] = {
        "Authorization": "Bearer {{access_token}}",
        "X-Dirty": "${B252_METHOD_LEAK}",
    }
    clean_method = _b252_method()
    clean_method["id"] = "clean_token"
    twoways = user_token_facade(
        "twoways", methods=[dirty_method, clean_method], upstream_url="${B252_MCP_URL}"
    )

    # 'exchhdr' (rev44-2, blocker 1, набор 1/3): единственное непубликуемое значение — ссылка
    # env:VAR в exchange.headers; exchange.body, revoke целиком и verify/upstream остаются
    # дословными. Правило «только целиком» (R-U8.1 п. 7) обязано снять exchange вместе с
    # вложенным revoke без остатка.
    exchhdr_method = _b252_method()
    exchhdr_method["exchange"]["headers"] = {
        "Authorization": "Bearer {{access_token}}",
        "X-Exch-Extra": f"env:{_B252_EXCHHDR_LEAK_NAME}",
    }
    exchhdr = user_token_facade(
        "exchhdr", methods=[exchhdr_method], upstream_url="${B252_MCP_URL}"
    )

    # 'exchbody' (rev44-2, blocker 1, набор 2/3): единственное непубликуемое значение — ${VAR} в
    # exchange.body (env:VAR в теле схема отвергает, AC-15 — годится только подстановка).
    exchbody_method = _b252_method()
    exchbody_method["exchange"]["body"] = {
        "description": "{{token_description}}",
        "note": "${B252_EXCHBODY_LEAK}",
    }
    exchbody = user_token_facade(
        "exchbody", methods=[exchbody_method], upstream_url="${B252_MCP_URL}"
    )

    # 'revokebody' (rev44-2, blocker 1, набор 3/3): единственное непубликуемое значение — ${VAR}
    # в exchange.revoke.body; headers обмена и отзыва дословны, exchange.body дословен.
    revokebody_method = _b252_method()
    revokebody_method["exchange"]["revoke"]["body"] = {
        "token_id": "{{token_id}}",
        "note": "${B252_REVOKEBODY_LEAK}",
    }
    revokebody = user_token_facade(
        "revokebody", methods=[revokebody_method], upstream_url="${B252_MCP_URL}"
    )

    # 'midstr' (rev44-2, blocker 2): подстановка ${VAR} внутри большей строки, а не строкой
    # целиком — форма «Bearer ${VAR}», самая частая запись боевого каталога. По одному разу на
    # credential_headers (в конце строки), static_headers (в середине) и дважды на
    # verify.headers одного способа (в начале строки и с двумя подстановками в одной строке).
    midstr_method = _b252_method()
    midstr_method["verify"]["headers"] = {
        "Authorization": "Bearer {{access_token}}",
        "X-Start": "${B252_MIDSTR_START}-tail",
        "X-Double": "${B252_MIDSTR_D1}-and-${B252_MIDSTR_D2}",
    }
    midstr = user_token_facade(
        "midstr",
        methods=[midstr_method],
        upstream_url="${B252_MCP_URL}",
        credential_headers={"Authorization": "Bearer ${B252_MIDSTR_CRED}"},
        static_headers={"X-Static-Mid": "prefix-${B252_MIDSTR_STATIC}-suffix"},
    )

    # 'listex' (rev44-3, minor — положительный прогон п. 5): подстановка ${VAR} внутри
    # exchange.list, headers, body и revoke дословны. Значения exchange.list наружу не идут
    # никогда (R-U15.3), поэтому непубликуемое значение внутри него не имеет права снять exchange —
    # ссылка env:VAR здесь не годится: вне разрешённых полей её отвергает схема (AC-15), и прогон
    # покраснел бы ошибкой загрузки, а не тем, ради чего написан (§34.5).
    listex_method = _b252_method()
    listex_method["exchange"]["list"] = {
        "url": _B252_EXCHANGE_URL,
        "method": "GET",
        "headers": {
            "Authorization": "Bearer {{access_token}}",
            "X-List-Extra": "${B252_LISTEX_LEAK}",
        },
        "id_field": "id",
        "description_field": "description",
    }
    listex = user_token_facade(
        "listex", methods=[listex_method], upstream_url="${B252_MCP_URL}"
    )

    caplog.clear()
    hub = await _hub(
        make_hub,
        servers=[
            clean,
            envverify,
            envcred,
            subst,
            statichdr,
            closed,
            twoways,
            exchhdr,
            exchbody,
            revokebody,
            midstr,
            listex,
        ],
        env=_B252_ENV,
        admin_token="adm",
    )
    await _user(hub)

    def _assert_boundary(response: httpx.Response) -> None:
        assert response.status_code == 200, response.text
        servers = {s["alias"]: s for s in response.json()["servers"]}
        assert set(servers) == {
            "clean",
            "envverify",
            "envcred",
            "subst",
            "statichdr",
            "closed",
            "twoways",
            "exchhdr",
            "exchbody",
            "revokebody",
            "midstr",
            "listex",
        }

        # 'clean': verify, exchange (с revoke) и upstream опубликованы дословно, все четыре
        # адреса подставлены, а непустые static_headers дошли до upstream дословно (finding 1,
        # часть «а» — единственная карточка набора, где это происходит).
        method = servers["clean"]["auth_methods"][0]
        assert method["verify"] == {
            "url": _B252_VERIFY_URL,
            "method": "GET",
            "headers": {"Authorization": "Bearer {{access_token}}"},
            "expect_status": None,
            "account_field": "username",
            "require_account": False,
        }
        assert method["exchange"]["url"] == _B252_EXCHANGE_URL
        assert method["exchange"]["revoke"]["url"] == _B252_REVOKE_URL
        assert "list" not in method["exchange"]
        assert servers["clean"]["upstream"] == {
            "url": _B252_MCP_URL,
            "credential_headers": {"Authorization": "Bearer {{access_token}}"},
            "static_headers": {"X-Static": "static-header-literal-value"},
        }

        # 'envverify': блока verify нет вовсе; exchange и upstream на месте, способ сохранил
        # available, id, title, field и issues_permanent_token.
        method = servers["envverify"]["auth_methods"][0]
        assert "verify" not in method
        assert method["exchange"]["url"] == _B252_EXCHANGE_URL
        assert servers["envverify"]["upstream"]["url"] == _B252_MCP_URL
        assert method["id"] == "session_token"
        assert method["title"]
        assert method["available"] is True
        assert method["field"]["label"]
        assert method["issues_permanent_token"] is True

        # 'envcred': блока upstream нет целиком — вместе со static_headers, которые сами по себе
        # публикуемы, — а verify и exchange способа на месте.
        assert "upstream" not in servers["envcred"]
        method = servers["envcred"]["auth_methods"][0]
        assert "verify" in method
        assert "exchange" in method

        # 'subst': блока exchange нет целиком, вместе с вложенным revoke; verify и upstream
        # на месте. Признак issues_permanent_token не зависит от публикации блока (R-U16).
        method = servers["subst"]["auth_methods"][0]
        assert "exchange" not in method
        assert "verify" in method
        assert servers["subst"]["upstream"]["url"] == _B252_MCP_URL
        assert method["issues_permanent_token"] is True

        # 'statichdr' (finding 1, blocker): единственное непубликуемое значение — ${VAR} в
        # static_headers; credential_headers сами по себе дословны и публикуемы, но правило
        # «только целиком» (R-U8.1 п. 7) снимает upstream карточки без остатка. verify и
        # exchange способа этим не задеты — снятие блока не «расползается» на соседние блоки.
        assert "upstream" not in servers["statichdr"]
        method = servers["statichdr"]["auth_methods"][0]
        assert "verify" in method
        assert "exchange" in method

        # 'closed': ни verify, ни exchange, ни upstream у карточки; id, title, available,
        # unavailable_reason, field и issues_permanent_token у способа прежние.
        method = servers["closed"]["auth_methods"][0]
        assert "verify" not in method
        assert "exchange" not in method
        assert "upstream" not in servers["closed"]
        assert method["id"] == "session_token"
        assert method["title"]
        assert method["available"] is False
        assert method["unavailable_reason"] == "Способ отключён администратором"
        assert method["field"]["label"]
        assert method["issues_permanent_token"] is True

        # 'twoways' (finding 2, major): у 'dirty_token' блока verify нет, у 'clean_token' он
        # опубликован дословно и совпадает с образцом byte-в-byte; снятие блока у одного способа
        # не задевает ни другой способ карточки, ни блок upstream (R-U8.1 п. 7) — соответствие
        # сырых способов разобранным не перепутано местами.
        methods = {m["id"]: m for m in servers["twoways"]["auth_methods"]}
        assert set(methods) == {"dirty_token", "clean_token"}
        assert "verify" not in methods["dirty_token"]
        assert methods["dirty_token"]["available"] is True
        assert methods["dirty_token"]["exchange"]["url"] == _B252_EXCHANGE_URL
        assert methods["clean_token"]["verify"] == {
            "url": _B252_VERIFY_URL,
            "method": "GET",
            "headers": {"Authorization": "Bearer {{access_token}}"},
            "expect_status": None,
            "account_field": "username",
            "require_account": False,
        }
        assert methods["clean_token"]["exchange"]["url"] == _B252_EXCHANGE_URL
        assert servers["twoways"]["upstream"] == {
            "url": _B252_MCP_URL,
            "credential_headers": {"Authorization": "Bearer {{access_token}}"},
        }

        # 'exchhdr' (rev44-2, blocker 1, набор 1/3): единственное непубликуемое значение — ссылка
        # env:VAR в exchange.headers; verify и upstream не задеты, exchange снят целиком вместе с
        # вложенным revoke.
        method = servers["exchhdr"]["auth_methods"][0]
        assert "exchange" not in method
        assert method["verify"]["url"] == _B252_VERIFY_URL
        assert servers["exchhdr"]["upstream"]["url"] == _B252_MCP_URL

        # 'exchbody' (rev44-2, blocker 1, набор 2/3): единственное непубликуемое значение — ${VAR}
        # в exchange.body; verify и upstream не задеты, exchange снят целиком вместе с вложенным
        # revoke.
        method = servers["exchbody"]["auth_methods"][0]
        assert "exchange" not in method
        assert method["verify"]["url"] == _B252_VERIFY_URL
        assert servers["exchbody"]["upstream"]["url"] == _B252_MCP_URL

        # 'revokebody' (rev44-2, blocker 1, набор 3/3): единственное непубликуемое значение —
        # ${VAR} в exchange.revoke.body; верхний exchange.headers и exchange.body сами по себе
        # дословны, но правило «только целиком» снимает exchange вместе с вложенным revoke без
        # остатка. verify и upstream не задеты.
        method = servers["revokebody"]["auth_methods"][0]
        assert "exchange" not in method
        assert method["verify"]["url"] == _B252_VERIFY_URL
        assert servers["revokebody"]["upstream"]["url"] == _B252_MCP_URL

        # 'midstr' (rev44-2, blocker 2): ${VAR} внутри строки (не строкой целиком) в
        # credential_headers (в конце), static_headers (в середине) и дважды в verify.headers
        # (в начале и с двумя подстановками) — снимает и verify, и upstream целиком; exchange
        # способа не задет ни одной из этих подстановок.
        assert "upstream" not in servers["midstr"]
        method = servers["midstr"]["auth_methods"][0]
        assert "verify" not in method
        assert method["exchange"]["url"] == _B252_EXCHANGE_URL
        assert method["exchange"]["revoke"]["url"] == _B252_REVOKE_URL

        # 'listex' (rev44-3, minor, п. 5): подстановка ${VAR} лежит только внутри exchange.list —
        # блока, который наружу не идёт никогда (R-U15.3). exchange обязан быть опубликован и
        # сверен с образцом целиком (byte-в-byte), ключа 'list' в нём нет; verify и upstream не
        # задеты. Ни одной записи следа на эту карточку быть не должно — послабление в эту сторону
        # (наружу пропускается блок, потерявший бы прямой режим по любому другому набору) здесь не
        # проверяется, но и не отменяется: exchange.list просто вне области действия п. 7.
        method = servers["listex"]["auth_methods"][0]
        assert method["exchange"] == {
            "url": _B252_EXCHANGE_URL,
            "method": "POST",
            "headers": {"Authorization": "Bearer {{access_token}}"},
            "body": {"description": "{{token_description}}"},
            "expect_status": 200,
            "token_field": "token",
            "token_id_field": "id",
            "description": "OpenCode Hub",
            "revoke": {
                "url": _B252_REVOKE_URL,
                "method": "POST",
                "headers": {"Authorization": "Bearer {{access_token}}"},
                "body": {"token_id": "{{token_id}}"},
                "expect_status": 200,
            },
        }
        assert "list" not in method["exchange"]
        assert "verify" in method
        assert servers["listex"]["upstream"]["url"] == _B252_MCP_URL

        # Ни ссылки env:, ни имени переменной, ни развёрнутого секрета нет нигде в теле ответа.
        text = response.text
        for leaked in _B252_LEAK_MARKERS:
            assert leaked not in text, f"утечка '{leaked}' в /api/catalog"

    def _warnings() -> list[logging.LogRecord]:
        return [
            r for r in caplog.records if r.name == "hub.catalog" and r.levelno == logging.WARNING
        ]

    def _assert_warnings(warnings: list[logging.LogRecord]) -> None:
        assert len(warnings) == 10, [w.getMessage() for w in warnings]
        seen = set()
        for record in warnings:
            message = record.getMessage()
            if "'envverify'" in message and "verify" in message:
                seen.add(("envverify", "verify"))
            elif "'envcred'" in message and "upstream" in message:
                seen.add(("envcred", "upstream"))
            elif "'subst'" in message and "exchange" in message:
                seen.add(("subst", "exchange"))
            elif "'statichdr'" in message and "upstream" in message:
                seen.add(("statichdr", "upstream"))
            elif "'twoways'" in message and "verify" in message:
                seen.add(("twoways", "verify"))
            elif "'exchhdr'" in message and "exchange" in message:
                seen.add(("exchhdr", "exchange"))
            elif "'exchbody'" in message and "exchange" in message:
                seen.add(("exchbody", "exchange"))
            elif "'revokebody'" in message and "exchange" in message:
                seen.add(("revokebody", "exchange"))
            elif "'midstr'" in message and "verify" in message:
                seen.add(("midstr", "verify"))
            elif "'midstr'" in message and "upstream" in message:
                seen.add(("midstr", "upstream"))
            else:
                pytest.fail(f"неожиданная запись WARNING: {message}")
            # finding 4 отчёта: след проверяется не только по getMessage(), но и по полному
            # тексту записи с учётом extra= — JsonFormatter сериализует в лог-строку любые
            # нестандартные атрибуты записи (src/hub/logging_.py), а getMessage() их не видит.
            full_text = record_text(record)
            for leaked in _B252_LEAK_MARKERS:
                assert leaked not in full_text, f"утечка '{leaked}' в записи журнала: {full_text}"
        assert seen == {
            ("envverify", "verify"),
            ("envcred", "upstream"),
            ("subst", "exchange"),
            ("statichdr", "upstream"),
            ("twoways", "verify"),
            ("exchhdr", "exchange"),
            ("exchbody", "exchange"),
            ("revokebody", "exchange"),
            ("midstr", "verify"),
            ("midstr", "upstream"),
        }
        assert not any("'clean'" in w.getMessage() for w in warnings)
        assert not any("'closed'" in w.getMessage() for w in warnings)
        assert not any("'twoways'" in w.getMessage() and "exchange" in w.getMessage() for w in warnings)
        assert not any("clean_token" in w.getMessage() for w in warnings)
        assert not any("'exchhdr'" in w.getMessage() and "verify" in w.getMessage() for w in warnings)
        assert not any("'exchbody'" in w.getMessage() and "verify" in w.getMessage() for w in warnings)
        assert not any("'revokebody'" in w.getMessage() and "verify" in w.getMessage() for w in warnings)
        assert not any("'midstr'" in w.getMessage() and "exchange" in w.getMessage() for w in warnings)
        assert not any("'listex'" in w.getMessage() for w in warnings)

    # --- первая загрузка: старт приложения (уже произошёл в _hub выше) ---
    _assert_warnings(_warnings())

    first = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    _assert_boundary(first)

    # --- вторая загрузка: POST /admin/catalog/reload — тот же файл, те же десять следов ---
    caplog.clear()
    reload = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert reload.status_code == 200, reload.text
    _assert_warnings(_warnings())

    second = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    _assert_boundary(second)


# rev44-3, задание §34.5, приоритет высокий — «сторож перечня»: четыре пробела подряд
# (static_headers, три набора exchange, подстановка внутри строки, четыре описательных значения)
# были одной ошибкой, повторённой четырежды, — перечень публикуемых значений вёлся вручную, в
# тексте правила и в тексте прогона. Ниже — прогон, порождающий случаи из ОПУБЛИКОВАННОГО
# представления, а не из перечня, зашитого в код.

_B252_WATCH_MARKER = "b252-watch-leak-9f3ac2"

# R-U8.1 п. 6: четыре адреса — единственное исключение из дословности. Записаны здесь литералом,
# а не выведены структурно из кода: появление нового такого исключения обязано стать видимой
# строкой в диффе этого множества, а не проскочить мимо прогона молча (§34.5).
_B252_ADDRESS_EXCEPTIONS: frozenset[tuple[str, tuple[str, ...]]] = frozenset(
    {
        ("verify", ("url",)),
        ("exchange", ("url",)),
        ("exchange", ("revoke", "url")),
        ("upstream", ("url",)),
    }
)


# --- §34.5, сверка 1: множества пропуска против литерала (ревью reports/review-rev44-4.json,
# major) ----------------------------------------------------------------------------------------
# Литерал того, что проверка дословности (``_all_verbatim``) пропускает СЕГОДНЯ, записан здесь
# руками — выводить его из кода нельзя ни в какой форме, иначе сверка проверяла бы код им же самим
# и проходила бы всегда. Равенство, а не включение: и новый путь пропуска, и исчезнувший обязаны
# стать видимой строкой в диффе этих множеств. Опровергается инъекцией N23 ревью: путь
# ``('headers', 'X-Request-Id')`` в множестве ``verify`` — обход выше его не ловит (эталонная
# карточка такого заголовка не задаёт), а эта сверка обязана покраснеть.
_B252_EXPECTED_VERIFY_SKIP: frozenset[tuple[str, ...]] = frozenset({("url",)})
_B252_EXPECTED_EXCHANGE_SKIP: frozenset[tuple[str, ...]] = frozenset(
    {("url",), ("revoke", "url"), ("list",)}
)
# Единая константа кода обслуживает оба набора карточки (``credential_headers``, ``static_headers``,
# см. ``_publication``) — пустое множество пропуска у обоих (§34.5).
_B252_EXPECTED_UPSTREAM_SKIP: frozenset[tuple[str, ...]] = frozenset()


# --- §34.5, сверка 2: состав публикуемых полей против состава обхода (то же ревью, major) -------


def _b252_strip_optional(annotation: Any) -> Any:
    """Снять ``| None`` с аннотации поля, если это ровно один непустой вариант объединения."""
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        rest = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(rest) == 1:
            return rest[0]
    return annotation


def _b252_unwrap_annotated(annotation: Any) -> Any:
    """Снять ``Annotated[X, ...]`` до ``X`` (значения заголовков — ``Annotated[str | EnvRef, ...]``)."""
    if typing.get_origin(annotation) is typing.Annotated:
        return typing.get_args(annotation)[0]
    return annotation


def _b252_type_carries_string(annotation: Any) -> bool:
    """Тип способен нести строку: сам ``str``, либо объединение, где ``str`` — один из вариантов
    (нужно для значений словарей вроде ``HeaderValue = str | EnvRef``)."""
    annotation = _b252_unwrap_annotated(_b252_strip_optional(annotation))
    if annotation is str:
        return True
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        return any(_b252_type_carries_string(a) for a in typing.get_args(annotation))
    return False


def _b252_field_carries_string(annotation: Any) -> bool:
    """Поле модели способно нести строку наружу (§34.5, сверка 2): аннотация после снятия ``| None``
    — это ``str``, ``Literal[...]`` из строк, ``dict`` со строковым значением, или вложенная модель
    (её имя учитывается как поле — рекурсия по её собственным полям делается отдельным вызовом для
    соответствующего вложенного блока, а не здесь)."""
    annotation = _b252_strip_optional(annotation)
    if annotation is str:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return all(isinstance(v, str) for v in typing.get_args(annotation))
    if origin is dict:
        _, value_t = typing.get_args(annotation)
        return _b252_type_carries_string(value_t)
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _b252_string_fields(model_cls: type[BaseModel]) -> set[str]:
    """Имена полей модели, способных нести строку наружу (§34.5, сверка 2)."""
    return {
        name
        for name, info in model_cls.model_fields.items()
        if _b252_field_carries_string(info.annotation)
    }


def _b252_literal_fields(model_cls: type[BaseModel]) -> set[str]:
    """Имена полей модели с аннотацией ``Literal[...]`` из строк — единственная причина, по которой
    схема может отвергнуть подставленное значение у этих моделей (R-U8.1 п. 7). §34.5, minor:
    причина выводится из аннотации поля, а не из имени сегмента ``method`` (ручной суррогат)."""
    names: set[str] = set()
    for name, info in model_cls.model_fields.items():
        annotation = _b252_strip_optional(info.annotation)
        if typing.get_origin(annotation) is typing.Literal and all(
            isinstance(v, str) for v in typing.get_args(annotation)
        ):
            names.add(name)
    return names


def _b252_is_literal_string_path(block: str, path: tuple[str, ...]) -> bool:
    """Путь ведёт к строковому полю с ``Literal[...]``-аннотацией — второй валидный исход обхода
    (отказ схемы) вместо снятия блока. Заменяет прежний суррогат по имени сегмента ``method``
    выводом из тех же аннотаций, что читает сверка 2 (§34.5, minor)."""
    if block == "verify" and len(path) == 1:
        return path[0] in _b252_literal_fields(TokenVerify)
    if block == "exchange":
        if len(path) == 1:
            return path[0] in _b252_literal_fields(TokenExchange)
        if len(path) == 2 and path[0] == "revoke":
            return path[1] in _b252_literal_fields(TokenExchangeRevoke)
    return False


# Исключение сверки 2 — сегодня ровно одно, с причиной (R-U15.3, п. 5): ``exchange.list`` наружу не
# идёт никогда, поэтому в опубликованном представлении его нет и путь обхода для него не строится,
# хотя модель ``TokenExchange`` несёт поле ``list`` (вложенная модель, способная нести строку).
_B252_FIELD_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset({("exchange", "list")})


def _b252_walk_string_paths(node: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Пути всех строковых **значений** узла — имена ключей не берутся (по ним подстановка не
    выполняется, R-U8.1 п. 6). Обходит словари и списки рекурсивно."""
    if isinstance(node, dict):
        paths: list[tuple[str, ...]] = []
        for key, value in node.items():
            paths.extend(_b252_walk_string_paths(value, (*prefix, str(key))))
        return paths
    if isinstance(node, list):
        paths = []
        for i, value in enumerate(node):
            paths.extend(_b252_walk_string_paths(value, (*prefix, str(i))))
        return paths
    if isinstance(node, str):
        return [prefix]
    return []


def _b252_set_watch(raw_server: dict[str, Any], block: str, path: tuple[str, ...], value: str) -> None:
    """Поставить ``value`` (строку, содержащую ``${B252_WATCH}``) ровно в одном месте сырой
    карточки — по пути, снятому с опубликованного представления. Соответствие путей прямое,
    единственное исключение — опубликованный ``upstream.url`` отвечает полю ``upstream_url``
    карточки (R-U8.1 п. 6)."""
    if block == "upstream":
        if path == ("url",):
            raw_server["upstream_url"] = value
            return
        target: Any = raw_server[path[0]]
        for key in path[1:-1]:
            target = target[key]
        target[path[-1]] = value
        return
    target = raw_server["auth_methods"][0][block]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.ac("AC-252")
async def test_publication_boundary_watches_every_published_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сторож перечня (rev44-3, задание §34.5, приоритет высокий): прогон порождает случаи
    непосредственно из ОПУБЛИКОВАННОГО представления карточки 'clean' (``_b252_clean_card``), а не
    из перечня, зашитого в текст правила R-U8.1 п. 7 или в код прежних прогонов, — закрывает класс
    дефекта, а не очередной его экземпляр. Четыре пробела подряд не находили один и тот же дефект:
    ``static_headers`` (finding 1, rev44-1), три набора ``exchange`` и подстановка внутри строки
    (blocker 1/2, rev44-2), четыре описательных значения — ``verify.account_field``,
    ``exchange.description``, ``exchange.token_field``, ``exchange.token_id_field`` (major,
    инъекция N16, rev44-3). Каждый раз пробел закрывался новой карточкой на конкретное значение;
    этот прогон новых карточек на новое значение не требует.

    Алгоритм: у эталонной карточки 'clean' разбираются опубликованные ``verify``, ``exchange``
    (вместе с вложенным ``revoke``) и ``upstream``; для каждого пути строкового значения в них (имя
    ключа не берётся, п. 6) строится вариант карточки, где ровно в этом месте сырых данных стоит
    ``${B252_WATCH}``, и карточка загружается. Четыре адреса — исключение из дословности (п. 6) и
    записаны здесь литералом (``_B252_ADDRESS_EXCEPTIONS``) с ОБРАТНЫМ ожиданием: блок публикуется,
    а значение — маркер, развёрнутый подстановкой (иначе новое исключение стало бы видимой строкой
    в диффе, а не тихой правкой этого множества). Для всякого другого пути ожидание одно: блок,
    которому путь принадлежит, отсутствует целиком, маркера нет нигде в опубликованном
    представлении, и на загрузку приходится ровно одна запись WARNING про этот блок (п. 7, п. 9).
    Отказ схемы на подстановку (поля с перечислением — ``method`` у ``verify``, ``exchange`` и
    ``revoke``, ограниченные тремя литералами каждое) засчитывается как второй валидный исход:
    маркер наружу и тогда не вышел — прогон обязан принимать оба исхода, иначе он покраснеет на
    верной реализации (§34.5).

    Что это ловит, чего не ловили прежние инъекции по отдельности: новое публикуемое строковое
    значение в блоках verify/exchange/upstream попадает в обход автоматически — правку кода,
    добавляющую путь в ``_VERIFY_VERBATIM_SKIP``/``_EXCHANGE_VERBATIM_SKIP`` (сужение первого
    рубежа), этот прогон обнаруживает без единой новой карточки.

    Честная граница обхода (найдена ревью ``reports/review-rev44-4.json``, N23 и N22b): обход ходит
    по значениям ОДНОГО экземпляра эталонной карточки, поэтому видит только пути, которые этот
    экземпляр порождает. Путь, которого карточка не задаёт, для обхода не существует — ни новый
    путь пропуска (N23: ``('headers', 'X-Request-Id')`` в ``_VERIFY_VERBATIM_SKIP``), ни новое
    публикуемое поле модели, которого карточка не задаёт (N22b). Обе инъекции дают полностью
    зелёный сьют при подтверждённой оракулом утечке — воспроизведено этим review и закрыто ниже
    двумя сверками состава (§34.5, задание того же ревью, major), которые обход не заменяют, а
    дополняют:

    Сверка 1 — множества пропуска против литерала, записанного в этой функции руками
    (``_B252_EXPECTED_*_SKIP``), а не выведенного из кода. Равенство, не включение: и новый путь
    пропуска, и исчезнувший обязаны стать видимой строкой в диффе. Ловит N23 — обход его не видит.

    Сверка 2 — состав полей моделей ``TokenVerify``/``TokenExchange``/``TokenExchangeRevoke``,
    способных нести строку (``str``, ``Literal[...]`` из строк, ``dict`` со строковым значением,
    вложенная модель), сверенный с составом первых сегментов путей обхода. Исключение — одно,
    с причиной: ``exchange.list`` наружу не идёт никогда (п. 5). У ``upstream`` модели нет —
    сверяется состав ОПУБЛИКОВАННЫХ ключей с тройкой п. 4; эта половина слабее двух других и ловит
    только безусловно публикуемый новый ключ (незакрытый вход, названный в спеке честно). Ловит
    N22b без правки множеств пропуска (поле модели есть — пути обхода нет).

    Обе сверки нужны вместе: N22b (поле в модели + путь в множестве пропуска) трогает и то, и
    другое, поэтому её одной недостаточно как доказательства — доказательство даёт разделённая
    пара (поле без кортежа / кортеж без поля, см. проверку выше и N23).

    §34.5, minor: причина отказа схемы (``method_paths``) выводится из аннотации поля
    (``Literal[...]`` из строк — ``_b252_is_literal_string_path``), а не из имени сегмента
    ``method`` — тот же приём и то же место, что сверка 2.
    """
    baseline = parse_catalog(catalog_doc([_b252_clean_card("watchbase")]), env=_B252_ENV)
    entry = baseline.get("watchbase")
    assert entry is not None
    view = entry.public_view("https://hub.test")
    method_view = view["auth_methods"][0]
    assert "verify" in method_view
    assert "exchange" in method_view
    assert "upstream" in view

    paths: list[tuple[str, tuple[str, ...]]] = []
    for block, node in (
        ("verify", method_view["verify"]),
        ("exchange", method_view["exchange"]),
        ("upstream", view["upstream"]),
    ):
        for path in _b252_walk_string_paths(node):
            paths.append((block, path))

    exceptions = [p for p in paths if p in _B252_ADDRESS_EXCEPTIONS]
    others = [p for p in paths if p not in _B252_ADDRESS_EXCEPTIONS]
    # Сверка самого перечня (rev44-3): опубликованное представление 'clean' сегодня несёт ровно
    # четыре адреса, три поля method (verify.method, exchange.method, exchange.revoke.method —
    # тоже строки; п. 7 не требует им собственного прогона по указанной в правиле причине —
    # множество значений схемой ограничено тремя литералами, но из ОБХОДА представления они не
    # исключаются: у них просто есть второй валидный исход, отказ загрузки) и одиннадцать прочих
    # строковых значений — семь наборов заголовков/тел плюс четыре описательных значения, ровно то
    # число, что называет R-U8.1 п. 7. Итого четырнадцать путей вне четырёх адресов. Разойдётся
    # число — разошлось само представление 'clean', и это тоже обязано быть видно, а не проглочено
    # молча.
    assert set(exceptions) == _B252_ADDRESS_EXCEPTIONS, exceptions
    # §34.5, minor: причина отказа схемы выводится из аннотации поля (``Literal[...]`` из строк),
    # а не из имени сегмента ``method`` — тот же приём, что читает сверка 2 ниже.
    method_paths = [p for p in others if _b252_is_literal_string_path(p[0], p[1])]
    assert len(method_paths) == 3, method_paths
    assert len(others) == 14, others

    # --- §34.5, сверка 1: множества пропуска против литерала (задание ревью rev44-4, major) -----
    # Равенство с литералом, записанным руками (см. определение выше этой функции), а не с чем-то
    # выведенным из кода. Опровергается инъекцией N23: путь ('headers', 'X-Request-Id'), добавленный
    # в множество пропуска ``verify``, — обход выше молчит (эталонная карточка такой заголовок не
    # задаёт), а эта сверка обязана покраснеть.
    assert _B252_ACTUAL_VERIFY_SKIP == _B252_EXPECTED_VERIFY_SKIP, _B252_ACTUAL_VERIFY_SKIP
    assert _B252_ACTUAL_EXCHANGE_SKIP == _B252_EXPECTED_EXCHANGE_SKIP, _B252_ACTUAL_EXCHANGE_SKIP
    assert _B252_ACTUAL_UPSTREAM_SKIP == _B252_EXPECTED_UPSTREAM_SKIP, _B252_ACTUAL_UPSTREAM_SKIP

    # --- §34.5, сверка 2: состав публикуемых полей против состава обхода (то же ревью, major) ----
    # Свести пути обхода (полные, включая четыре адреса-исключения — поле, публикующее адрес,
    # тоже часть состава модели) к первым сегментам на блок и сверить с моделями. Для ``revoke`` —
    # вторые сегменты у путей, начинающихся с ``revoke``.
    verify_path_names = {path[0] for block, path in paths if block == "verify"}
    exchange_path_names = {path[0] for block, path in paths if block == "exchange"}
    revoke_path_names = {
        path[1] for block, path in paths if block == "exchange" and path[0] == "revoke"
    }

    verify_field_names = _b252_string_fields(TokenVerify)
    exchange_field_names = _b252_string_fields(TokenExchange) - {
        field for exc_block, field in _B252_FIELD_EXCEPTIONS if exc_block == "exchange"
    }
    revoke_field_names = _b252_string_fields(TokenExchangeRevoke)

    # Опровергается необязательным публикуемым строковым полем модели (например,
    # ``verify.account_prefix``), добавленным БЕЗ правки множеств пропуска (N22b без второй
    # половины инъекции, ревью rev44-4): поле модели есть, пути обхода нет — красный здесь, тогда
    # как сверка 1 выше на этой же инъекции остаётся зелёной (множества пропуска не менялись).
    assert verify_field_names == verify_path_names, (verify_field_names, verify_path_names)
    assert exchange_field_names == exchange_path_names, (exchange_field_names, exchange_path_names)
    assert revoke_field_names == revoke_path_names, (revoke_field_names, revoke_path_names)

    # У блока ``upstream`` модели нет — сверяется состав ОПУБЛИКОВАННЫХ ключей с литеральной тройкой
    # R-U8.1 п. 4. Половина слабее двух предыдущих сверок, и §34.5 это не скрывает: она ловит новый
    # ключ, публикуемый безусловно, но не ловит ключ, отдаваемый только при заданном значении —
    # незакрытый вход, названный в спеке прямо (нет модели, по которой его увидеть до публикации).
    assert set(view["upstream"].keys()) == {
        "url",
        "credential_headers",
        "static_headers",
    }, view["upstream"]

    env = dict(_B252_ENV)
    env["B252_WATCH"] = _B252_WATCH_MARKER

    for i, (block, path) in enumerate(exceptions):
        # Alias — короткий и заведомо валидный (R-C1, ``^[a-z][a-z0-9-]{0,31}$`` — имена полей вида
        # ``account_field`` подчёркивание не проходят): путь и блок идут в сообщение об ошибке, а
        # не в имя карточки.
        alias = f"watchx{i}"
        raw = _b252_clean_card(alias)
        _b252_set_watch(raw, block, path, "${B252_WATCH}")
        caplog.clear()
        parsed = parse_catalog(catalog_doc([raw]), env=env)
        entry = parsed.get(alias)
        assert entry is not None, f"{alias}: адрес — исключение п. 6, карточка обязана загрузиться"
        view = entry.public_view("https://hub.test")
        node: Any = (
            view["auth_methods"][0]["verify"]
            if block == "verify"
            else view["auth_methods"][0]["exchange"]
            if block == "exchange"
            else view["upstream"]
        )
        for key in path:
            node = node[key]
        assert node == _B252_WATCH_MARKER, (block, path, node)

    schema_rejected: list[tuple[str, tuple[str, ...]]] = []
    for i, (block, path) in enumerate(others):
        # Alias — короткий и заведомо валидный (см. пояснение у карточек-исключений выше).
        alias = f"watch{i}"
        raw = _b252_clean_card(alias)
        # Маркер — ВНУТРИ большей строки, не строкой целиком (форма «Bearer ${VAR}», самая частая
        # запись боевого каталога, и регрессия N5 отчёта reports/review-rev44-2.json: `_is_verbatim`,
        # суженная до ``value.startswith("${")``, такую подстановку не ловит). Подстановка строкой
        # целиком этот случай не проверяет — на нём N5 не отличима от верной реализации.
        _b252_set_watch(raw, block, path, "lit-${B252_WATCH}-tail")
        caplog.clear()
        try:
            parsed = parse_catalog(catalog_doc([raw]), env=env)
        except CatalogError:
            # Схема отвергла подставленное значение (перечень — 'method' у verify/exchange/revoke):
            # маркер наружу не вышел, отказ загрузки — второй валидный исход (§34.5). Отказ по
            # любой другой причине (например, собственная ошибка построения карточки в этом
            # прогоне) прогон обязан отличить, а не молча засчитать as принятый исход — отсюда
            # сверка ``schema_rejected == method_paths`` ниже.
            schema_rejected.append((block, path))
            continue
        entry = parsed.get(alias)
        assert entry is not None, alias
        view = entry.public_view("https://hub.test")
        method_view = view["auth_methods"][0]
        if block == "upstream":
            assert "upstream" not in view, (block, path, view)
        else:
            assert block not in method_view, (block, path, method_view)
        body_text = json.dumps(view, ensure_ascii=False)
        assert _B252_WATCH_MARKER not in body_text, (block, path, body_text)

        warnings = [
            r for r in caplog.records if r.name == "hub.catalog" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, (block, path, [w.getMessage() for w in warnings])
        message = warnings[0].getMessage()
        assert f"'{alias}'" in message, (block, path, message)
        assert block in message, (block, path, message)
        assert _B252_WATCH_MARKER not in message, (block, path, message)

    # Отказ схемы обязан объясняться ровно тремя полями method (перечень — литерал, а не догадка):
    # отказ по любой другой причине сигналил бы о собственной ошибке этого прогона (а не о втором
    # валидном исходе R-U8.1 п. 7) и не имеет права быть проглочен молча веткой ``except`` выше.
    assert set(schema_rejected) == set(method_paths), schema_rejected


# --- AC-186 ----------------------------------------------------------------


@pytest.mark.ac("AC-186")
async def test_server_page_shows_method_choice_only_for_several_methods(
    make_hub: HubFactory,
) -> None:
    """Выбор способа появляется только когда способов больше одного (R-U8)."""
    hub = await _hub(
        make_hub,
        servers=[user_token_facade("multi"), user_token_facade("single", methods=[user_token_method()])],
    )
    await web_login(hub)

    multi = await hub.get("/ui/servers/multi")
    assert multi.status_code == 200, multi.text
    assert "Корпоративная авторизация ТЭГ" in multi.text
    assert "Токен сессии ТЭГ" in multi.text
    assert 'name="method"' in multi.text
    fieldset = multi.text.split('id="auth-methods"', 1)[1].split("</fieldset>", 1)[0]
    corp = fieldset.split('value="corp_oauth"', 1)[1].split("</label>", 1)[0]
    assert "disabled" in corp
    assert "OAuth-приложение ТЭГ ещё не выдано администраторами" in fieldset

    single = await hub.get("/ui/servers/single")
    assert single.status_code == 200, single.text
    assert 'name="method"' not in single.text
    assert 'id="connect-token"' in single.text
    assert 'name="token"' in single.text


# --- AC-187 ----------------------------------------------------------------


def _token_input(html: str) -> str:
    """Тег поля ввода токена целиком (по атрибуту ``name="token"``)."""
    assert 'name="token"' in html, "на странице нет поля ввода токена"
    head, _, tail = html.partition('name="token"')
    return head.rpartition("<input")[2] + 'name="token"' + tail.partition(">")[0]


async def _force_needs_reauth(hub: Hub, tokens: dict[str, Any], *, alias: str = "tag") -> None:
    """Довести подключение до ``needs_reauth`` штатным путём: 401 от целевой системы (R-U6)."""
    assert hub.net is not None
    hub.net.upstreams[alias].push(httpx.Response(401, json={"error": "unauthorized"}))
    response = await _mcp_call(hub, tokens["access_token"], alias=alias)
    assert response.json()["error"]["data"]["reason"] == "needs_reauth", response.text


@pytest.mark.ac("AC-187")
async def test_token_form_and_states_never_show_the_token(make_hub: HubFactory) -> None:
    """Форма ввода и три состояния подключения по каталогу, без значения токена (R-U8, R-U9)."""
    hub = await _hub(make_hub)
    await web_login(hub)
    await _user(hub)
    assert hub.net is not None
    hub.net.verify.account = "m.ivanov"

    not_connected = await hub.get("/ui/servers/tag")
    assert not_connected.status_code == 200, not_connected.text

    await _connect(hub, token=TOKEN)
    connected = await hub.get("/ui/servers/tag")
    assert connected.status_code == 200, connected.text
    assert "Подключён" in connected.text
    assert "Токен сессии ТЭГ" in connected.text
    assert "m.ivanov" in connected.text
    assert "Заменить токен" in connected.text
    assert "Отключить" in connected.text

    tokens = await _mcp_tokens(hub)
    await _force_needs_reauth(hub, tokens)
    reauth = await hub.get("/ui/servers/tag")
    assert reauth.status_code == 200, reauth.text
    assert "больше не действует" in reauth.text

    for page in (not_connected, connected, reauth):
        html = page.text
        field = _token_input(html)
        assert 'type="password"' in field, field
        assert 'value=""' in field, field
        assert "Токен сессии ТЭГ" in html
        assert "ТЭГ → Профиль → Настройки безопасности → Личные токены доступа" in html
        assert "https://docs.test/tag#токен" in html
        assert "Где взять токен" in html
        assert TOKEN not in html


# --- AC-188 ----------------------------------------------------------------


def _fragments(value: str, length: int = 8) -> list[str]:
    return [value[i : i + length] for i in range(len(value) - length + 1)]


@pytest.mark.ac("AC-188")
async def test_user_token_never_appears_in_logs_or_audit(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Токен не попадает ни в журнал любого уровня, ни в аудит (R-U9)."""
    secret = "usr-tok-SECRET-1"
    hub = await _hub(make_hub)
    await _user(hub)
    # Перехват логов всех уровней: root мало — configure_logging внутри create_app
    # вернул логгер ``hub`` на INFO, и DEBUG-утечка тестом бы не ловилась.
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="hub")
    with capture_json_logs() as json_logs:
        await _connect(hub, token=secret)
        tokens = await _mcp_tokens(hub)
        assert (await _mcp_call(hub, tokens["access_token"])).status_code == 200
        changed = await hub.client.put(
            "/api/me/connections/tag/permissions",
            json={"preset": "readwrite"},
            headers=bearer("sk-ok"),
        )
        assert changed.status_code == 200, changed.text
        await _force_needs_reauth(hub, tokens)
        dropped = await hub.client.delete("/api/me/connections/tag", headers=bearer("sk-ok"))
        assert dropped.status_code == 200, dropped.text

    logged = "\n".join(
        [record_text(record) for record in caplog.records] + json_logs.raw()
    )
    assert logged, "журнал пуст — проверка вырождена"
    leaked = [fragment for fragment in _fragments(secret) if fragment in logged]
    assert leaked == [], f"фрагменты токена в журнале: {leaked}"

    rows = await audit_rows(hub.app)
    dumped = json.dumps(rows, default=str, ensure_ascii=False)
    assert [f for f in _fragments(secret) if f in dumped] == []
    actions = [row["action"] for row in rows]
    assert "connection_connected" in actions
    assert "connection_needs_reauth" in actions
    assert "connection_disconnected" in actions
    connected = next(r for r in rows if r["action"] == "connection_connected")
    assert connected["details"]["auth_method"] == "session_token"


# --- AC-189 ----------------------------------------------------------------


@pytest.mark.ac("AC-189")
async def test_user_token_is_stored_encrypted_only(make_hub: HubFactory) -> None:
    """Токен не отдаётся наружу и хранится только шифртекстом — в БД и в KV (R-U9, R-B9)."""
    secret = "usr-tok-SECRET-2"
    hub = await _hub(make_hub)
    await web_login(hub)
    await _user(hub)
    connect = await _connect(hub, token=secret)
    row = await _connection_row(hub)
    tokens = await _mcp_tokens(hub)
    assert (await _mcp_call(hub, tokens["access_token"])).status_code == 200

    responses = [
        connect,
        await hub.get("/api/me/connections", headers=bearer("sk-ok")),
        await hub.get("/api/catalog", headers=bearer("sk-ok")),
        await hub.get("/remote-config", headers=bearer("sk-ok")),
        await hub.get("/metrics"),
        await hub.get("/ui/connections"),
        await hub.get("/ui/servers/tag"),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert secret not in response.text

    stored = (await _token_rows(hub, row["id"]))[0]["access_token_enc"]
    assert stored != secret
    assert secret not in stored
    assert hub.app.state.cipher.decrypt(stored) == secret

    cached = await hub.app.state.kv.get("conn:u1:tag")
    assert cached is not None, "кэш подключения не заполнен MCP-вызовом"
    assert cached["access_token_enc"] == stored
    assert secret not in dump_kv(hub.app)
    assert secret not in await dump_database(hub.app)


# --- AC-190 ----------------------------------------------------------------


@pytest.mark.ac("AC-190")
async def test_tokens_of_two_users_are_isolated(make_hub: HubFactory) -> None:
    """Токен пользователя A недоступен пользователю B и не меняется его действиями (R-U9)."""
    hub = await _hub(make_hub)
    await _user(hub, "sk-a", "ua")
    await _user(hub, "sk-b", "ub")
    await _connect(hub, token="tok-A", key="sk-a")
    await _connect(hub, token="tok-B", key="sk-b")
    conn_a = await _connection_row(hub, user_id="ua")
    tokens_b = await _mcp_tokens(hub, user_id="ub")

    call = await _mcp_call(hub, tokens_b["access_token"])
    assert call.status_code == 200, call.text
    assert hub.net is not None
    assert hub.net.upstreams["tag"].last().header("authorization") == "Bearer tok-B"

    listed = await hub.get("/api/me/connections", headers=bearer("sk-b"))
    assert listed.status_code == 200, listed.text
    assert [c["alias"] for c in listed.json()] == ["tag"]
    permissions = await hub.client.put(
        "/api/me/connections/tag/permissions",
        json={"preset": "readwrite"},
        headers=bearer("sk-b"),
    )
    assert permissions.status_code == 200, permissions.text
    dropped = await hub.client.delete("/api/me/connections/tag", headers=bearer("sk-b"))
    assert dropped.status_code == 200, dropped.text

    for response in (call, listed, permissions, dropped):
        assert "tok-A" not in response.text

    after_a = await _connection_row(hub, user_id="ua")
    assert after_a["status"] == "connected"
    assert after_a["revision"] == conn_a["revision"]
    rows = await _token_rows(hub, conn_a["id"])
    assert len(rows) == 1
    assert hub.app.state.cipher.decrypt(rows[0]["access_token_enc"]) == "tok-A"


# --- AC-191 ----------------------------------------------------------------


@pytest.mark.ac("AC-191")
async def test_tag_catalog_entry_from_spec_loads_and_is_published(make_hub: HubFactory) -> None:
    """Запись каталога ``tag`` из R-U10 загружается и публикуется корректно (R-U10)."""
    hub = await _hub(make_hub, servers=[tag_spec_server()])
    await _user(hub)
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    server = {s["alias"]: s for s in catalog["servers"]}["tag"]
    assert server["mode"] == "facade"
    assert server["mcp_url"] == "https://hub.test/mcp/tag"
    assert server["auth_kind"] == "user_token"
    methods = {m["id"]: m for m in server["auth_methods"]}
    assert methods["corp_oauth"]["type"] == "oauth2"
    assert methods["corp_oauth"]["available"] is False
    assert methods["corp_oauth"]["unavailable_reason"]
    assert methods["session_token"]["type"] == "user_token"
    assert methods["session_token"]["field"]["label"] == "Токен сессии ТЭГ"
    assert server["permission_model"]["kind"] == "tool_filter"


@pytest.mark.ac("AC-191")
async def test_tag_without_upstream_url_variable_is_unconfigured(make_hub: HubFactory) -> None:
    """Незаданная ``${TAG_MCP_URL}`` при ``status: beta`` скрывает сервер из каталога (R-C2)."""
    hub = await _hub(make_hub, servers=[tag_spec_server()], env={})
    await _user(hub)
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in catalog["servers"]] == []
    assert (await hub.get("/health")).json()["catalog_version"] == 1


# --- AC-192 ----------------------------------------------------------------


@pytest.mark.ac("AC-192")
async def test_tag_connected_by_token_works_through_hub_with_tool_filter(
    make_hub: HubFactory,
) -> None:
    """Коннектор ``tag`` работает через Hub с персональным срезом прав (R-U10, R-P8)."""
    hub = await _hub(make_hub, servers=[tag_spec_server()])
    await _user(hub)
    assert hub.net is not None
    upstream = hub.net.upstreams["tag"]
    upstream.tools = [dict(tool) for tool in TAG_TOOLS]

    await _connect(hub, token="tag-tok-1", preset="readonly")
    assert hub.net.verify.last().url == TAG_SPEC_VERIFY_URL
    tokens = await _mcp_tokens(hub)

    listed = await _mcp_call(hub, tokens["access_token"])
    assert listed.status_code == 200, listed.text
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert names == ["whoami", "search_posts", "get_thread"]
    assert upstream.last().header("authorization") == "Bearer tag-tok-1"

    calls_before = upstream.calls
    denied = await _mcp_call(
        hub, tokens["access_token"], method="tools/call", params={"name": "create_post"}
    )
    assert denied.status_code == 200, denied.text
    error = denied.json()["error"]
    assert error["code"] == CODE_TOOL_FORBIDDEN
    assert error["data"]["hint_url"] == "https://hub.test/ui/servers/tag"
    assert upstream.calls == calls_before, "запрещённый инструмент ушёл на upstream"


# --- AC-193 ----------------------------------------------------------------


@pytest.mark.ac("AC-193")
async def test_corp_oauth_is_declared_but_unavailable(make_hub: HubFactory) -> None:
    """Корпоративный OAuth объявлен, но подключиться им нельзя (R-U10, R-U4)."""
    hub = await _hub(make_hub, servers=[tag_spec_server()])
    await web_login(hub)
    await _user(hub)
    assert hub.net is not None

    response = await connect_with_token(hub, alias="tag", token="x", method="corp_oauth")
    body = _expect_error(response, 409, "auth_method_unavailable")
    assert body["message"] == "OAuth-приложение ТЭГ ещё не выдано администраторами"

    flow = await hub.get("/oauth/connect/tag")
    assert flow.status_code == 409, flow.text
    assert "OAuth-приложение ТЭГ ещё не выдано администраторами" in flow.text

    assert hub.net.verify.calls == 0
    assert await fetch_rows(hub.app, "SELECT id FROM connections") == []
    assert await fetch_rows(hub.app, "SELECT connection_id FROM upstream_tokens") == []

    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    methods = {m["id"]: m for m in catalog["servers"][0]["auth_methods"]}
    assert methods["corp_oauth"]["available"] is False
    page = await hub.get("/ui/servers/tag")
    assert page.status_code == 200, page.text
    assert "Корпоративная авторизация ТЭГ" in page.text
    assert "OAuth-приложение ТЭГ ещё не выдано администраторами" in page.text
# --- BUG-I4-005 (падает до фикса) ------------------------------------------


@pytest.mark.ac("AC-175")
@pytest.mark.parametrize(
    ("title", "token"),
    [
        ("кириллица", "zzzz-заведомо-неверный-zzzz"),
        ("управляющий символ", "tok\x01bad"),
        ("перевод строки", "tok\nX-Injected: 1"),
    ],
    ids=["non-ascii", "control", "newline"],
)
async def test_token_unusable_as_header_is_rejected_with_400(
    make_hub: HubFactory, title: str, token: str
) -> None:
    """BUG-I4-005: значение, непригодное для HTTP-заголовка, отвергается до обращения к системе.

    Токен подставляется в заголовок проверочного запроса (R-U3), а заголовки кодируются в ASCII:
    не-ASCII, управляющие символы и перевод строки обязаны отсеиваться проверкой тела запроса
    (400 ``invalid_request``/``token_rejected``, R-U4), а не превращаться в 500.
    """
    hub = await _hub(make_hub)
    await _user(hub)
    assert hub.net is not None

    try:
        response = await connect_with_token(hub, alias="tag", token=token)
    except Exception as exc:  # noqa: BLE001 — до фикса это UnicodeEncodeError, после фикса ответ 400
        pytest.fail(f"{title}: подключение упало исключением {type(exc).__name__}: {exc}")

    assert response.status_code == 400, f"{title}: {response.status_code} {response.text}"
    assert response.json()["error"] in ("invalid_request", "token_rejected"), response.text
    assert hub.net.verify.calls == 0, f"{title}: непригодное значение ушло в целевую систему"
    assert await fetch_rows(hub.app, "SELECT connection_id FROM upstream_tokens") == []
