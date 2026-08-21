"""Требование назвать владельца токена (R-U3.1, R-U11, R-U4): AC-195..AC-202.

Класс дефекта BUG-I4-009: целевая система отвечает **кодом успеха и анонимным телом**, поэтому
проверка «по коду ответа» принимает заведомо неверный токен. Прежние моки на неверный токен всегда
отвечали 401/403 — новый класс моков здесь: ``200`` + тело без ``account_field``.

Все проверки идут против локальных моков (``MockNetwork``, SQLite ``:memory:``, in-memory KV).
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
    TAG_ENV,
    TAG_UPSTREAM,
    VERIFY_URL,
    add_key,
    authorize_params,
    bearer,
    capture_json_logs,
    catalog_doc,
    connect_with_token,
    dump_kv,
    fetch_rows,
    i3_catalog,
    issue_hub_tokens,
    jsonrpc_body,
    mcp_headers,
    pkce_pair,
    provider_callback,
    query_of,
    record_text,
    register_client,
    user_token_facade,
    user_token_method,
    web_login,
)

TOKEN = "usr-tok-1"
SECOND_TOKEN = "usr-tok-2"
NONSENSE = "nonsense-token-value"

# Тело, которым живая система (Confluence /rest/api/user/current) отвечает на любой неверный
# токен: код успеха, но владелец не назван — поля ``username`` в теле нет.
ANONYMOUS_BODY = {"type": "anonymous", "displayName": "Anonymous"}


# --- вспомогательное -------------------------------------------------------


def _verify(
    *, account_field: str | None = "username", require_account: Any = True
) -> dict[str, Any]:
    verify: dict[str, Any] = {
        "url": VERIFY_URL,
        "method": "GET",
        "headers": {"Authorization": "Bearer {{access_token}}"},
    }
    if account_field is not None:
        verify["account_field"] = account_field
    if require_account is not None:
        verify["require_account"] = require_account
    return verify


def _catalog(**verify_kwargs: Any) -> dict[str, Any]:
    """Каталог из одного facade-сервера ``tag`` со способом ``user_token`` (способ — индекс 0)."""
    method = user_token_method("session_token", verify=_verify(**verify_kwargs))
    return catalog_doc([user_token_facade("tag", methods=[method])])


async def _hub(make_hub: HubFactory, **verify_kwargs: Any) -> Hub:
    hub = await make_hub(
        catalog=_catalog(**verify_kwargs), env=TAG_ENV, base_url="https://hub.test"
    )
    await add_key(hub, "sk-ok", "u1")
    return hub


def _anonymous(**extra: Any) -> httpx.Response:
    """Ответ нового класса: код успеха, тело без имени владельца токена."""
    return httpx.Response(200, json={**ANONYMOUS_BODY, **extra})


async def _connection_rows(hub: Hub) -> list[dict[str, Any]]:
    return await fetch_rows(
        hub.app, "SELECT id, status, provider_account, revision FROM connections"
    )


async def _token_rows(hub: Hub) -> list[dict[str, Any]]:
    return await fetch_rows(hub.app, "SELECT connection_id, access_token_enc FROM upstream_tokens")


def _expect_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"] == code, response.text


async def _mcp_call(hub: Hub, token: str) -> httpx.Response:
    return await hub.post(
        "/mcp/tag", content=jsonrpc_body("tools/list"), headers=mcp_headers(token)
    )


# --- AC-195 ----------------------------------------------------------------


@pytest.mark.ac("AC-195")
async def test_anonymous_success_response_rejects_token(make_hub: HubFactory) -> None:
    """200 с анонимным телом при ``require_account: true`` → отказ, подключения нет (R-U3.1)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    hub.net.verify.push(_anonymous())

    response = await connect_with_token(hub, alias="tag", token=NONSENSE)
    _expect_error(response, 400, "token_rejected")
    assert response.json()["message"] == "Целевая система не приняла токен"

    assert hub.net.verify.calls == 1
    assert await _token_rows(hub) == []
    assert [row["status"] for row in await _connection_rows(hub)] != ["connected"]

    kv = dump_kv(hub.app)
    assert "conn:u1:tag" not in kv, kv
    assert NONSENSE not in kv

    listed = await hub.get("/api/me/connections", headers=bearer("sk-ok"))
    assert listed.status_code == 200, listed.text
    assert [c for c in listed.json() if c["alias"] == "tag" and c["status"] == "connected"] == []


# --- AC-196 ----------------------------------------------------------------


_UNNAMED_BODIES: list[tuple[str, dict[str, Any]]] = [
    ("нет поля", {}),
    ("null", {"username": None}),
    ("пустая строка", {"username": ""}),
    ("пробелы", {"username": "   "}),
    ("число", {"username": 12345}),
    ("bool", {"username": True}),
    ("объект", {"username": {"name": "x"}}),
    ("список", {"username": ["x"]}),
]


@pytest.mark.ac("AC-196")
@pytest.mark.parametrize(
    ("title", "body"), _UNNAMED_BODIES, ids=[case[0] for case in _UNNAMED_BODIES]
)
async def test_any_unnamed_account_value_is_rejected(
    make_hub: HubFactory, title: str, body: dict[str, Any]
) -> None:
    """Любое неназванное значение ``account_field`` — отказ, а не пустой account (R-U3.1)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    hub.net.verify.push(httpx.Response(200, json=body))

    response = await connect_with_token(hub, alias="tag", token="usr-tok-x")
    _expect_error(response, 400, "token_rejected")
    assert await _token_rows(hub) == [], title
    assert [row["status"] for row in await _connection_rows(hub)] != ["connected"], title


# --- AC-197 ----------------------------------------------------------------


@pytest.mark.ac("AC-197")
@pytest.mark.parametrize("require_account", [None, False], ids=["поля нет", "false"])
async def test_without_requirement_anonymous_response_connects(
    make_hub: HubFactory, require_account: bool | None
) -> None:
    """Без требования поведение AC-173 не изменилось: подключение с пустым account (R-U3.1)."""
    hub = await _hub(make_hub, require_account=require_account)
    assert hub.net is not None
    hub.net.verify.push(_anonymous())

    response = await connect_with_token(hub, alias="tag", token=NONSENSE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["account"] is None

    rows = await _connection_rows(hub)
    assert [row["status"] for row in rows] == ["connected"]
    assert rows[0]["provider_account"] is None
    assert len(await _token_rows(hub)) == 1


# --- AC-198 ----------------------------------------------------------------


@pytest.mark.ac("AC-198")
async def test_named_account_is_accepted_with_requirement(make_hub: HubFactory) -> None:
    """Верный токен с названным аккаунтом принимается и при включённом требовании (R-U3.1)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    hub.net.verify.push(
        httpx.Response(200, json={"type": "known", "username": "m.ivanov", "displayName": "Иванов"})
    )

    response = await connect_with_token(hub, alias="tag", token=TOKEN)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"
    assert response.json()["account"] == "m.ivanov"

    assert hub.net.verify.last().header("authorization") == f"Bearer {TOKEN}"
    rows = await _connection_rows(hub)
    assert rows[0]["provider_account"] == "m.ivanov"
    tokens = await _token_rows(hub)
    assert len(tokens) == 1
    assert hub.app.state.cipher.decrypt(tokens[0]["access_token_enc"]) == TOKEN


# --- AC-199 ----------------------------------------------------------------


_OUTCOMES: list[tuple[str, Any, int, str]] = [
    ("анонимное тело", _anonymous(), 400, "token_rejected"),
    (
        "не JSON",
        httpx.Response(200, content=b"not a json", headers={"Content-Type": "text/plain"}),
        502,
        "upstream_unavailable",
    ),
    ("не объект", httpx.Response(200, json=[1, 2]), 502, "upstream_unavailable"),
    ("500", httpx.Response(500, json={"error": "boom"}), 502, "upstream_unavailable"),
    ("сетевая ошибка", httpx.ConnectError("connection refused"), 502, "upstream_unavailable"),
    ("таймаут", httpx.ReadTimeout("timed out"), 502, "upstream_unavailable"),
]


@pytest.mark.ac("AC-199")
@pytest.mark.parametrize(
    ("title", "outcome", "status", "error"), _OUTCOMES, ids=[case[0] for case in _OUTCOMES]
)
async def test_rejection_and_unavailability_differ_and_keep_connection(
    make_hub: HubFactory, title: str, outcome: Any, status: int, error: str
) -> None:
    """Отказ и недоступность различаются; прежнее подключение не разрушается (R-U3.1, R-U4)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    hub.net.verify.push(httpx.Response(200, json={"username": "m.ivanov"}))
    first = await connect_with_token(hub, alias="tag", token=TOKEN)
    assert first.status_code == 200, first.text
    before = (await _connection_rows(hub))[0]

    hub.net.verify.push(outcome)
    response = await connect_with_token(hub, alias="tag", token=SECOND_TOKEN)
    _expect_error(response, status, error)

    after = (await _connection_rows(hub))[0]
    assert after["status"] == "connected", title
    assert after["revision"] == before["revision"], title
    tokens = await _token_rows(hub)
    assert len(tokens) == 1
    assert hub.app.state.cipher.decrypt(tokens[0]["access_token_enc"]) == TOKEN, title

    hub_tokens = await issue_hub_tokens(
        hub, user_id="u1", alias="tag", connection_id=after["id"], scope="tag:readonly"
    )
    proxied = await _mcp_call(hub, hub_tokens["access_token"])
    assert proxied.status_code == 200, proxied.text
    assert hub.net.upstreams["tag"].url == TAG_UPSTREAM
    assert hub.net.upstreams["tag"].last().header("authorization") == f"Bearer {TOKEN}"


# --- AC-200 ----------------------------------------------------------------


@pytest.mark.ac("AC-200")
@pytest.mark.parametrize(
    ("title", "verify_kwargs"),
    [
        ("без account_field", {"account_field": None, "require_account": True}),
        ("не bool", {"require_account": "yes"}),
    ],
    ids=["без account_field", "не bool"],
)
async def test_invalid_require_account_is_schema_error(
    make_hub: HubFactory, title: str, verify_kwargs: dict[str, Any]
) -> None:
    """``require_account`` без ``account_field`` и не-bool — ошибка схемы с путём (R-U3.1, R-C1)."""
    with pytest.raises(Exception) as excinfo:
        await make_hub(catalog=_catalog(**verify_kwargs), env=TAG_ENV, base_url="https://hub.test")
    message = str(excinfo.value)
    assert "servers[0].auth_methods[0].verify.require_account" in message, f"{title}: {message}"


@pytest.mark.ac("AC-200")
async def test_require_account_with_field_loads(make_hub: HubFactory) -> None:
    """``require_account: true`` вместе с ``account_field`` — валидный каталог (R-U3.1)."""
    hub = await _hub(make_hub)
    catalog = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert catalog.status_code == 200, catalog.text
    assert [s["alias"] for s in catalog.json()["servers"]] == ["tag"]


@pytest.mark.ac("AC-200")
async def test_catalog_without_require_account_has_requirement_off(make_hub: HubFactory) -> None:
    """Без обоих полей каталог валиден, требование выключено по умолчанию (R-U3.1)."""
    hub = await _hub(make_hub, account_field=None, require_account=None)
    assert hub.net is not None
    hub.net.verify.push(_anonymous())
    response = await connect_with_token(hub, alias="tag", token=NONSENSE)
    assert response.status_code == 200, response.text
    assert response.json()["account"] is None


# --- AC-201 ----------------------------------------------------------------


async def _start_oauth(hub: Hub) -> str:
    """Дойти до редиректа на AS целевой системы; вернуть его ``location``."""
    await web_login(hub)
    client_id = await register_client(hub)
    started = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=pkce_pair()[1])
    )
    assert started.status_code == 302, started.text
    location = started.headers["location"]
    assert location.startswith("https://gitlab.test/oauth/authorize"), location
    assert query_of(location)["state"]
    return location


@pytest.mark.ac("AC-201")
@pytest.mark.parametrize(
    ("title", "response"),
    [
        ("анонимное тело", httpx.Response(200, json=ANONYMOUS_BODY)),
        (
            "не JSON",
            httpx.Response(200, content=b"not a json", headers={"Content-Type": "text/plain"}),
        ),
    ],
    ids=["анонимное тело", "не JSON"],
)
async def test_oauth_exchange_without_access_token_creates_nothing(
    make_hub: HubFactory, title: str, response: httpx.Response
) -> None:
    """Успешный код обмена без ``access_token`` подключения не создаёт (R-B2, тот же класс)."""
    hub = await make_hub(catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test")
    location = await _start_oauth(hub)
    hub.provider.push(response)

    finished = await provider_callback(hub, location)
    assert finished.status_code == 502, finished.text
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == [], title
    statuses = [row["status"] for row in await fetch_rows(hub.app, "SELECT status FROM connections")]
    assert "connected" not in statuses, title


@pytest.mark.ac("AC-201")
async def test_oauth_exchange_with_access_token_but_no_account_connects(
    make_hub: HubFactory,
) -> None:
    """Тело с ``access_token``, но без имени владельца — подключение есть, account пуст (R-U11)."""
    hub = await make_hub(catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test")
    location = await _start_oauth(hub)
    hub.provider.push(
        httpx.Response(200, json={"access_token": "ups-access-1", "token_type": "Bearer"})
    )

    finished = await provider_callback(hub, location)
    assert finished.status_code == 200, finished.text  # экран прав
    rows = await fetch_rows(hub.app, "SELECT status, provider_account FROM connections")
    assert rows[0]["status"] == "connected"
    assert rows[0]["provider_account"] is None
    assert len(await fetch_rows(hub.app, "SELECT id FROM upstream_tokens")) == 1


# --- AC-202 ----------------------------------------------------------------


def _fragments(value: str, length: int = 8) -> list[str]:
    return [value[i : i + length] for i in range(len(value) - length + 1)]


@pytest.mark.ac("AC-202")
async def test_rejection_does_not_log_response_body(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Отказ по неназванному аккаунту не выносит тело ответа в журнал (R-U3.1, R-U9)."""
    secret = "usr-tok-SECRET-3"
    hub = await _hub(make_hub)
    assert hub.net is not None
    hub.net.verify.push(_anonymous(userKey="SECRET-BODY-MARKER"))
    caplog.set_level(logging.DEBUG)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token=secret)
    _expect_error(response, 400, "token_rejected")

    logged = "\n".join([record_text(record) for record in caplog.records] + json_logs.raw())
    assert logged, "журнал пуст — проверка вырождена"
    assert "SECRET-BODY-MARKER" not in logged
    assert [fragment for fragment in _fragments(secret) if fragment in logged] == []

    records = json_logs.records()
    named = [
        record
        for record in records
        if record.get("alias") == "tag"
        and record.get("auth_method") == "session_token"
        and record.get("status") == 200
        and "account" in str(record.get("message"))
    ]
    assert named, f"нет записи о том, что аккаунт не назван: {json.dumps(records, ensure_ascii=False)}"
