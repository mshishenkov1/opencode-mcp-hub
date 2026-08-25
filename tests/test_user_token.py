"""Подключение коннектора пользовательским токеном (R-U1..R-U10): AC-169..AC-194.

Все проверки идут против локальных моков: целевая система (адрес проверки токена и upstream MCP)
поднята в ``MockNetwork``, БД — SQLite ``:memory:``, KV — in-memory. Обращений в сеть нет.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

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
    """Витрина отдаёт способы без ``verify``, секретов и OAuth-адресов (R-U8, R-C6)."""
    hub = await _hub(make_hub, servers=[user_token_facade(), native_server("plain")])
    await _user(hub)
    response = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert response.status_code == 200, response.text
    servers = {s["alias"]: s for s in response.json()["servers"]}

    methods = {m["id"]: m for m in servers["tag"]["auth_methods"]}
    assert set(methods) == {"corp_oauth", "session_token"}
    # R-U16 (ревизия 4): к публичному виду способа добавлен признак выпуска постоянного токена;
    # у способа без блока exchange он false, сам блок наружу не отдаётся.
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

    forbidden_keys = {
        "verify",
        "authorize_url",
        "token_url",
        "revoke_url",
        "scopes",
        "client_id",
        "client_secret",
    }
    assert not (_all_keys(response.json()) & forbidden_keys)
    assert VERIFY_URL not in response.text
    assert "/api/v4/users/me" not in response.text
    # У сервера без auth_methods ключа в публичном представлении нет вовсе.
    assert "auth_methods" not in servers["plain"]


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
    caplog.set_level(logging.DEBUG)
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
