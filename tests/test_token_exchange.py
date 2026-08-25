"""Обмен присланного токена на постоянный (R-U12…R-U17): AC-214…AC-224, AC-226, AC-228, AC-229.

Проверяется наблюдаемое поведение подключения: что уходит в целевую систему, что остаётся в
хранилище и что видно снаружи. Судьба присланного токена — отдельная тема AC-223: при удавшемся
обмене он не должен остаться нигде, включая журнал и аудит.

Все проверки идут против локальных моков (``MockNetwork``: адрес проверки токена, личные токены
целевой системы, upstream MCP), БД — SQLite ``:memory:``, KV — in-memory. Обращений в сеть нет.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    EXCHANGE_REVOKE_URL,
    EXCHANGE_URL,
    JIRA_AS,
    TAG_ENV,
    TAG_SPEC_EXCHANGE_URL,
    add_key,
    all_log,
    audit_rows,
    bearer,
    capture_all_levels,
    capture_json_logs,
    catalog_doc,
    connect_with_token,
    dump_database,
    dump_kv,
    exchange_block,
    fetch_rows,
    hub_log,
    issue_hub_tokens,
    jsonrpc_body,
    mcp_headers,
    native_server,
    oauth_method,
    tag_spec_server_rev4,
    user_token_facade,
    user_token_method,
    web_login,
)

CODE_CONNECTION = -32002

MARKER = "OpenCode Hub (hub.test)"
STAGE_PUBLIC_URL = "https://hub-stage.test:8443"
STAGE_MARKER = "OpenCode Hub (hub-stage.test:8443)"


# --- вспомогательное -------------------------------------------------------


def _method(
    method_id: str = "session_token",
    *,
    exchange: dict[str, Any] | None = "default",  # type: ignore[assignment]
    expiry: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Способ ``user_token`` с блоком ``exchange`` (по умолчанию — полным, с ``list``)."""
    method = user_token_method(method_id, **overrides)
    if exchange == "default":
        exchange = exchange_block()
    if exchange is not None:
        method["exchange"] = exchange
    if expiry is not None:
        method["expiry"] = expiry
    return method


def _servers(*methods: dict[str, Any], alias: str = "tag") -> list[dict[str, Any]]:
    return [user_token_facade(alias, methods=list(methods))]


async def _hub(
    make_hub: HubFactory,
    *,
    servers: list[dict[str, Any]] | None = None,
    key: str = "sk-ok",
    user_id: str = "u1",
    **overrides: Any,
) -> Hub:
    hub = await make_hub(
        catalog=catalog_doc(servers if servers is not None else _servers(_method())),
        env=TAG_ENV,
        base_url=overrides.pop("base_url", "https://hub.test"),
        **overrides,
    )
    await add_key(hub, key, user_id)
    return hub


async def _upstream_row(hub: Hub, *, user_id: str = "u1", alias: str = "tag") -> dict[str, Any]:
    rows = await fetch_rows(
        hub.app,
        "SELECT t.access_token_enc, t.issued_token_id, t.token_origin, t.token_origin_reason, "
        "t.submitted_expires_at FROM upstream_tokens t "
        "JOIN connections c ON c.id = t.connection_id "
        "WHERE c.user_id = :u AND c.alias = :a",
        u=user_id,
        a=alias,
    )
    assert rows, f"строки upstream_tokens для {user_id}/{alias} нет"
    return rows[0]


async def _connection_id(hub: Hub, *, user_id: str = "u1", alias: str = "tag") -> int:
    rows = await fetch_rows(
        hub.app,
        "SELECT id FROM connections WHERE user_id = :u AND alias = :a",
        u=user_id,
        a=alias,
    )
    assert rows, f"подключения {user_id}/{alias} нет"
    return int(rows[0]["id"])


def _stored(hub: Hub, row: dict[str, Any]) -> str:
    return str(hub.app.state.cipher.decrypt(row["access_token_enc"]))


def _authorizations(requests: list[Any]) -> list[str | None]:
    return [r.header("authorization") for r in requests]


def _fragments(value: str, length: int = 8) -> list[str]:
    return [value[i : i + length] for i in range(len(value) - length + 1)]


async def _connect(hub: Hub, token: str, *, alias: str = "tag", key: str = "sk-ok") -> Any:
    response = await connect_with_token(hub, alias=alias, token=token, key=key)
    assert response.status_code == 200, response.text
    return response


async def _mcp_call(hub: Hub, *, alias: str = "tag", user_id: str = "u1") -> httpx.Response:
    tokens = await issue_hub_tokens(
        hub,
        user_id=user_id,
        alias=alias,
        connection_id=await _connection_id(hub, user_id=user_id, alias=alias),
        scope=f"{alias}:readonly",
    )
    return await hub.post(
        f"/mcp/{alias}",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )


# --- AC-214 ----------------------------------------------------------------


@pytest.mark.ac("AC-214")
async def test_exchange_stores_issued_token_not_the_submitted_one(make_hub: HubFactory) -> None:
    """Обмен прошёл: хранится выпущенный целевой системой токен, а не присланный (R-U13)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(
        httpx.Response(
            200,
            json={
                "id": "tokid-1",
                "token": "PERMANENT-1",
                "user_id": "u1",
                "description": MARKER,
                "is_active": True,
            },
        )
    )

    response = await _connect(hub, "SESSION-1")
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "issued"
    assert body["token_origin_reason"] is None
    assert body["account"] == "m.ivanov"

    assert len(api.issue_requests) == 1
    issued = api.issue_requests[0]
    assert issued.method == "POST"
    assert str(issued.url).split("?")[0] == EXCHANGE_URL
    assert issued.header("authorization") == "Bearer SESSION-1"
    assert issued.json_body == {"description": MARKER}

    row = await _upstream_row(hub)
    assert _stored(hub, row) == "PERMANENT-1"
    assert row["issued_token_id"] == "tokid-1"
    assert row["token_origin"] == "issued"
    assert row["token_origin_reason"] is None

    # R-U13.4: выпущенный токен проверяется тем же блоком verify.
    assert hub.net.verify.tokens_seen() == ["SESSION-1", "PERMANENT-1"]


_VERIFY_REFUSALS: list[tuple[str, Any, int, str]] = [
    ("401", httpx.Response(401, json={"error": "unauthorized"}), 400, "token_rejected"),
    ("403", httpx.Response(403, json={"error": "forbidden"}), 400, "token_rejected"),
    ("500", httpx.Response(500, json={"error": "boom"}), 502, "upstream_unavailable"),
    ("таймаут", httpx.ReadTimeout("timed out"), 502, "upstream_unavailable"),
]


@pytest.mark.ac("AC-214")
@pytest.mark.parametrize(
    ("title", "answer", "status", "error"),
    _VERIFY_REFUSALS,
    ids=[c[0] for c in _VERIFY_REFUSALS],
)
async def test_rejected_submitted_token_never_reaches_the_issue_request(
    make_hub: HubFactory, title: str, answer: Any, status: int, error: str
) -> None:
    """Шаг 2 R-U13: не принятый проверкой токен в запрос выпуска не уходит.

    Порядок шагов — не деталь реализации: отправка присланного значения на выпуск после того,
    как целевая система его уже отвергла, была бы лишней передачей отвергнутого секрета.
    """
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    hub.net.verify.push(answer)

    response = await connect_with_token(hub, alias="tag", token="SESSION-refused")
    assert response.status_code == status, f"{title}: {response.text}"
    assert response.json()["error"] == error, title

    # Ни выпуска, ни списка, ни отзыва — обмен не выполнялся вовсе.
    assert api.issue_requests == [], title
    assert api.list_requests == [], title
    assert api.revoke_requests == [], title
    assert api.sessions_requests == [], title
    # Подключение не создано: строки токена нет, статус не connected.
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == [], title
    statuses = [
        row["status"] for row in await fetch_rows(hub.app, "SELECT status FROM connections")
    ]
    assert "connected" not in statuses, title


# --- AC-215 ----------------------------------------------------------------


@pytest.mark.ac("AC-215")
async def test_proxy_sends_issued_token_and_never_the_submitted_one(
    make_hub: HubFactory,
) -> None:
    """В целевую систему при проксировании уходит постоянный токен (R-U13, R-P2)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": "tokid-1", "token": "PERMANENT-1"}))
    await _connect(hub, "SESSION-1")

    # Всё, что уходило наружу до проксирования, уже проверено AC-214; смотрим только новые запросы.
    seen = {
        "verify": len(hub.net.verify.requests),
        "issue": len(api.issue_requests),
        "list": len(api.list_requests),
        "revoke": len(api.revoke_requests),
        "upstream": len(hub.net.upstreams["tag"].requests),
    }

    proxied = await _mcp_call(hub)
    assert proxied.status_code == 200, proxied.text
    assert "result" in proxied.json()

    upstream = hub.net.upstreams["tag"]
    assert upstream.calls > seen["upstream"]
    assert upstream.last().header("authorization") == "Bearer PERMANENT-1"

    outgoing = [
        *hub.net.verify.requests[seen["verify"] :],
        *api.issue_requests[seen["issue"] :],
        *api.list_requests[seen["list"] :],
        *api.revoke_requests[seen["revoke"] :],
        *upstream.requests[seen["upstream"] :],
    ]
    dumped = json.dumps(
        [{"url": r.url, "headers": r.headers, "body": r.content.decode("utf-8", "replace")}
         for r in outgoing],
        ensure_ascii=False,
    )
    assert "SESSION-1" not in dumped, dumped


# --- AC-216 ----------------------------------------------------------------


def _schema_cases() -> list[tuple[str, list[dict[str, Any]], str | None]]:
    no_revoke = exchange_block()
    del no_revoke["revoke"]

    on_oauth = oauth_method("corp_oauth", as_base=JIRA_AS)
    on_oauth["exchange"] = exchange_block()

    body_wrong_template = exchange_block()
    body_wrong_template["body"] = {"description": "{{access_token}}"}

    revoke_wrong_template = exchange_block()
    revoke_wrong_template["revoke"]["body"] = {"token_id": "{{token_description}}"}

    extra_field = exchange_block()
    extra_field["retry"] = {"attempts": 2}

    empty_headers = exchange_block()
    empty_headers["headers"] = {}

    long_description = exchange_block(description="Д" * 101)

    list_no_description = exchange_block()
    del list_no_description["list"]["description_field"]

    return [
        ("(а) корректный exchange", _servers(_method()), None),
        (
            "(б) без revoke",
            _servers(_method(exchange=no_revoke)),
            "servers[0].auth_methods[0].exchange.revoke",
        ),
        (
            "(в) exchange у oauth2",
            _servers(on_oauth, _method()),
            "servers[0].auth_methods[0].exchange",
        ),
        (
            "(г) body с {{access_token}}",
            _servers(_method(exchange=body_wrong_template)),
            "servers[0].auth_methods[0].exchange.body.description",
        ),
        (
            "(д) revoke.body с {{token_description}}",
            _servers(_method(exchange=revoke_wrong_template)),
            "servers[0].auth_methods[0].exchange.revoke.body.token_id",
        ),
        (
            "(е) лишнее подполе exchange.retry",
            _servers(_method(exchange=extra_field)),
            "servers[0].auth_methods[0].exchange.retry",
        ),
        (
            "(ж) пустой headers",
            _servers(_method(exchange=empty_headers)),
            "servers[0].auth_methods[0].exchange.headers",
        ),
        (
            "(з) description из 101 символа",
            _servers(_method(exchange=long_description)),
            "servers[0].auth_methods[0].exchange.description",
        ),
        (
            "(и) list без description_field",
            _servers(_method(exchange=list_no_description)),
            "servers[0].auth_methods[0].exchange.list.description_field",
        ),
        ("(к) способ без exchange", _servers(_method(exchange=None)), None),
    ]


@pytest.mark.ac("AC-216")
@pytest.mark.parametrize(
    ("title", "servers", "fragment"), _schema_cases(), ids=[c[0] for c in _schema_cases()]
)
async def test_exchange_schema_is_checked_with_paths(
    make_hub: HubFactory, title: str, servers: list[dict[str, Any]], fragment: str | None
) -> None:
    """Блок ``exchange`` принимается только в допустимом виде; иначе путь к полю (R-U12, R-C1)."""
    if fragment is not None:
        with pytest.raises(Exception) as excinfo:
            await _hub(make_hub, servers=servers)
        assert fragment in str(excinfo.value), f"{title}: {excinfo.value}"
        return

    hub = await _hub(make_hub, servers=servers)
    catalog = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert catalog.status_code == 200, catalog.text
    assert [s["alias"] for s in catalog.json()["servers"]] == ["tag"]


@pytest.mark.ac("AC-216")
async def test_method_without_exchange_keeps_previous_behaviour(make_hub: HubFactory) -> None:
    """(к) без блока обмена подключение прежнее: обмен не выполняется вовсе (R-U12, R-U13)."""
    hub = await _hub(make_hub, servers=_servers(_method(exchange=None)))
    assert hub.net is not None
    api = hub.net.tokens

    body = (await _connect(hub, "SESSION-0")).json()
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] is None
    assert api.issue_requests == []
    assert api.list_requests == []
    assert api.revoke_requests == []

    row = await _upstream_row(hub)
    assert _stored(hub, row) == "SESSION-0"
    assert row["issued_token_id"] is None


# --- AC-217 ----------------------------------------------------------------


_POLICY_DENIED = [
    ("403", httpx.Response(403, json={"id": "api.context.permissions.app_error"})),
    ("501", httpx.Response(501, json={"error": "not implemented"})),
    ("400", httpx.Response(400, json={"error": "bad request"})),
    ("404", httpx.Response(404, json={"error": "not found"})),
]


@pytest.mark.ac("AC-217")
@pytest.mark.parametrize(
    ("title", "outcome"), _POLICY_DENIED, ids=[c[0] for c in _POLICY_DENIED]
)
async def test_policy_denied_connects_with_submitted_token(
    make_hub: HubFactory, title: str, outcome: httpx.Response
) -> None:
    """Целевая система запретила выпуск — подключение всё равно создаётся (R-U14)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(outcome)

    response = await connect_with_token(hub, alias="tag", token="SESSION-2")
    assert response.status_code == 200, f"{title}: {response.text}"
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] == "policy_denied"

    row = await _upstream_row(hub)
    assert _stored(hub, row) == "SESSION-2"
    assert row["issued_token_id"] is None
    assert api.revoke_requests == [], title
    assert api.list_requests == [], title

    connected = await audit_rows(hub.app, "connection_connected")
    assert len(connected) == 1
    assert connected[0]["details"]["token_origin"] == "submitted"
    assert connected[0]["details"]["token_origin_reason"] == "policy_denied"


# --- AC-218 ----------------------------------------------------------------


_UNAVAILABLE: list[tuple[str, Any]] = [
    ("500", httpx.Response(500, json={"error": "boom"})),
    ("503", httpx.Response(503, json={"error": "unavailable"})),
    ("429", httpx.Response(429, json={"error": "too many"})),
    ("сетевая ошибка", httpx.ConnectError("connection refused")),
    ("таймаут", httpx.ReadTimeout("timed out")),
    (
        "не JSON",
        httpx.Response(200, content=b"not a json", headers={"Content-Type": "text/plain"}),
    ),
    ("не объект", httpx.Response(200, json=[1, 2])),
    ("пустой token", httpx.Response(200, json={"id": "tokid", "token": ""})),
    ("token из пробелов", httpx.Response(200, json={"id": "tokid", "token": "   "})),
    ("token не строка", httpx.Response(200, json={"id": "tokid", "token": 42})),
    ("без id", httpx.Response(200, json={"token": "PERM"})),
    ("пустой id", httpx.Response(200, json={"id": "", "token": "PERM"})),
    ("201 при expect_status 200", httpx.Response(201, json={"id": "tokid", "token": "PERM"})),
]


@pytest.mark.ac("AC-218")
@pytest.mark.parametrize(("title", "outcome"), _UNAVAILABLE, ids=[c[0] for c in _UNAVAILABLE])
async def test_unusable_issue_response_connects_with_submitted_token(
    make_hub: HubFactory, title: str, outcome: Any
) -> None:
    """Недоступность и непригодный ответ на выпуск подключения не отменяют (R-U14)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(outcome)

    response = await connect_with_token(hub, alias="tag", token="SESSION-3")
    assert response.status_code == 200, f"{title}: {response.text}"
    body = response.json()
    assert body["status"] == "connected", title
    assert body["token_origin"] == "submitted", title
    assert body["token_origin_reason"] == "upstream_unavailable", title

    row = await _upstream_row(hub)
    assert _stored(hub, row) == "SESSION-3", title
    assert row["issued_token_id"] is None, title
    assert api.revoke_requests == [], title


# --- AC-219 ----------------------------------------------------------------


_UNUSABLE_ISSUED: list[tuple[str, Any]] = [
    ("401 на выпущенный", httpx.Response(401, json={"error": "unauthorized"})),
    ("500 на выпущенный", httpx.Response(500, json={"error": "boom"})),
    ("другой аккаунт", httpx.Response(200, json={"username": "a.petrov"})),
]


@pytest.mark.ac("AC-219")
@pytest.mark.parametrize(
    ("title", "verify_answer"), _UNUSABLE_ISSUED, ids=[c[0] for c in _UNUSABLE_ISSUED]
)
async def test_unusable_issued_token_is_revoked_and_connection_stays_submitted(
    make_hub: HubFactory, title: str, verify_answer: Any
) -> None:
    """Выпущенный токен не прошёл проверку — он отзывается присланным (R-U13.4, R-U14)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": "tokid-9", "token": "PERMANENT-9"}))
    hub.net.verify.by_token["PERMANENT-9"] = verify_answer

    response = await connect_with_token(hub, alias="tag", token="SESSION-4")
    assert response.status_code == 200, f"{title}: {response.text}"
    body = response.json()
    assert body["status"] == "connected", title
    assert body["token_origin"] == "submitted", title
    assert body["token_origin_reason"] == "token_unusable", title

    row = await _upstream_row(hub)
    assert _stored(hub, row) == "SESSION-4", title
    assert row["issued_token_id"] is None, title

    assert api.revoked_ids == ["tokid-9"], title
    assert str(api.revoke_requests[0].url).split("?")[0] == EXCHANGE_REVOKE_URL
    # Отзыв идёт присланным токеном: выпущенный непригоден (R-U13.4, R-U15.2).
    assert api.revoke_requests[0].header("authorization") == "Bearer SESSION-4", title

    revoked = await audit_rows(hub.app, "upstream_token_revoked")
    assert len(revoked) == 1, title
    assert revoked[0]["details"]["reason"] == "unusable", title
    assert "tokid-9" not in json.dumps(revoked, ensure_ascii=False), title


# --- AC-220 ----------------------------------------------------------------


@pytest.mark.ac("AC-220")
async def test_reconnect_does_not_multiply_personal_tokens(make_hub: HubFactory) -> None:
    """Три подключения подряд оставляют ровно один свой токен; чужой не тронут (R-U15)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.add_item("foreign-1", "Личный токен пользователя")

    for token in ("SESSION-a", "SESSION-b", "SESSION-c"):
        body = (await _connect(hub, token)).json()
        assert body["token_origin"] == "issued", token
        assert body["token_origin_reason"] is None, token

    assert api.item_ids == ["foreign-1", "tokid-3"]
    assert api.descriptions() == [MARKER, MARKER, MARKER]

    row = await _upstream_row(hub)
    assert row["issued_token_id"] == "tokid-3"
    assert _stored(hub, row) == "PERMANENT-3"

    assert api.revoked_ids == ["tokid-1", "tokid-2"]
    assert "foreign-1" not in api.revoked_ids
    # Список и отзыв выполняются новым постоянным токеном (R-U15.3).
    assert _authorizations(api.revoke_requests) == ["Bearer PERMANENT-2", "Bearer PERMANENT-3"]
    assert _authorizations(api.list_requests) == [
        "Bearer PERMANENT-1",
        "Bearer PERMANENT-2",
        "Bearer PERMANENT-3",
    ]


# --- AC-221 ----------------------------------------------------------------


def _cleanup_items() -> list[dict[str, Any]]:
    return [
        {"id": "own-old", "description": MARKER},
        {"id": "case", "description": "opencode hub (hub.test)"},
        {"id": "suffix", "description": f"{MARKER} — копия"},
        {"id": "prefix", "description": "OpenCode Hub"},
        {"id": "other-install", "description": "OpenCode Hub (other.test)"},
        {"id": "nonstr", "description": 42},
        {"id": "nodesc"},
        {"id": "tokid-new", "description": MARKER},
    ]


async def _hub_with_cleanup_list(make_hub: HubFactory) -> Hub:
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.items = _cleanup_items()
    api.push_issue(httpx.Response(200, json={"id": "tokid-new", "token": "PERMANENT-new"}))
    return hub


@pytest.mark.ac("AC-221")
async def test_cleanup_revokes_only_exact_marker_matches(make_hub: HubFactory) -> None:
    """Отзыв запускает только побайтовое равенство маркеру (R-U15.2)."""
    hub = await _hub_with_cleanup_list(make_hub)
    assert hub.net is not None
    api = hub.net.tokens

    body = (await _connect(hub, "SESSION-5")).json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "issued"

    assert api.revoked_ids == ["own-old"]
    assert api.item_ids == [
        "case",
        "suffix",
        "prefix",
        "other-install",
        "nonstr",
        "nodesc",
        "tokid-new",
    ]
    assert _stored(hub, await _upstream_row(hub)) == "PERMANENT-new"


@pytest.mark.ac("AC-221")
@pytest.mark.parametrize("stage", ["list", "revoke"])
async def test_cleanup_failure_does_not_break_the_connection(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    """Сбой уборки не влияет на подключение; тела ответов в журнал не попадают (R-U15.3)."""
    hub = await _hub_with_cleanup_list(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    failure = httpx.Response(500, json={"error": "CLEANUP-BODY-MARKER"})
    if stage == "list":
        api.push_list(failure)
    else:
        api.push_revoke(failure)
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token="SESSION-5")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "issued"
    assert _stored(hub, await _upstream_row(hub)) == "PERMANENT-new"

    failed = await audit_rows(hub.app, "upstream_token_cleanup_failed")
    assert [row["details"]["stage"] for row in failed] == [stage]

    logged = all_log(caplog, json_logs)
    assert logged, "журнал пуст — проверка вырождена"
    assert "CLEANUP-BODY-MARKER" not in logged
    assert "CLEANUP-BODY-MARKER" not in json.dumps(
        await audit_rows(hub.app), default=str, ensure_ascii=False
    )


@pytest.mark.ac("AC-221")
async def test_cleanup_revokes_at_most_twenty_items_per_pass(make_hub: HubFactory) -> None:
    """За один заход отзывается не более 20 элементов — страховка от массового отзыва (R-U15.2)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.items = [{"id": f"own-{i}", "description": MARKER} for i in range(25)]
    api.push_issue(httpx.Response(200, json={"id": "tokid-new", "token": "PERMANENT-new"}))

    body = (await _connect(hub, "SESSION-many")).json()
    assert body["token_origin"] == "issued"

    assert len(api.revoked_ids) == 20
    assert set(api.revoked_ids) <= {f"own-{i}" for i in range(25)}
    # Остаток остаётся до следующего подключения, а не отзывается вслепую.
    assert len(api.item_ids) == 5


@pytest.mark.ac("AC-218")
async def test_numeric_token_id_is_accepted(make_hub: HubFactory) -> None:
    """Числовой идентификатор выпущенного токена годится и приводится к строке (R-U13.3)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": 4210, "token": "PERMANENT-N"}))

    body = (await _connect(hub, "SESSION-num")).json()
    assert body["token_origin"] == "issued"
    row = await _upstream_row(hub)
    assert row["issued_token_id"] == "4210"
    assert _stored(hub, row) == "PERMANENT-N"


# --- AC-222 ----------------------------------------------------------------


_DISCONNECT_CASES = [
    ("issued", "issued", None),
    ("submitted", "submitted", None),
    ("issued, отзыв 500", "issued", httpx.Response(500, json={"error": "boom"})),
]


@pytest.mark.ac("AC-222")
@pytest.mark.parametrize(
    ("title", "origin", "revoke_answer"), _DISCONNECT_CASES, ids=[c[0] for c in _DISCONNECT_CASES]
)
async def test_disconnect_revokes_only_the_token_hub_issued(
    make_hub: HubFactory, title: str, origin: str, revoke_answer: httpx.Response | None
) -> None:
    """Отключение отзывает выпущенный Hub'ом токен и никогда — присланный (R-U15.4, R-U5)."""
    hub = await _hub(
        make_hub,
        servers=_servers(oauth_method("corp_oauth", as_base=JIRA_AS), _method()),
    )
    assert hub.net is not None
    api = hub.net.tokens
    if origin == "issued":
        api.push_issue(httpx.Response(200, json={"id": "tokid-7", "token": "PERMANENT-7"}))
    else:
        api.push_issue(httpx.Response(403, json={"error": "forbidden"}))

    body = (await _connect(hub, "SESSION-7")).json()
    assert body["token_origin"] == origin, title
    connection_id = await _connection_id(hub)
    hub_tokens = await issue_hub_tokens(
        hub, user_id="u1", alias="tag", connection_id=connection_id, scope="tag:readonly"
    )
    revoked_before = len(api.revoke_requests)
    if revoke_answer is not None:
        api.push_revoke(revoke_answer)

    response = await hub.client.delete("/api/me/connections/tag", headers=bearer("sk-ok"))
    assert response.status_code == 200, f"{title}: {response.text}"
    assert response.json() == {"alias": "tag", "status": "not_connected"}

    new_revokes = api.revoke_requests[revoked_before:]
    if origin == "issued":
        assert [(r.json_body or {}).get("token_id") for r in new_revokes] == ["tokid-7"], title
        assert _authorizations(new_revokes) == ["Bearer PERMANENT-7"], title
    else:
        assert new_revokes == [], title

    # OAuth-адрес отзыва не вызывается для user_token никогда (R-U5, решение 66).
    assert hub.net.providers["jira"].revoke_requests == [], title

    assert await fetch_rows(
        hub.app, "SELECT id FROM upstream_tokens WHERE connection_id = :c", c=connection_id
    ) == [], title
    rows = await fetch_rows(
        hub.app, "SELECT status FROM connections WHERE id = :c", c=connection_id
    )
    assert rows[0]["status"] == "not_connected", title
    assert len(await audit_rows(hub.app, "connection_disconnected")) == 1, title

    call = await hub.post(
        "/mcp/tag",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(hub_tokens["access_token"]),
    )
    if call.status_code == 200:
        error = call.json()["error"]
        assert error["code"] == CODE_CONNECTION, title
        assert error["data"]["reason"] == "not_connected", title
    else:
        assert call.status_code == 401, f"{title}: {call.text}"


# --- AC-223 ----------------------------------------------------------------


@pytest.mark.ac("AC-223")
async def test_submitted_token_is_not_stored_anywhere_after_successful_exchange(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """При удавшемся обмене присланный токен не остаётся нигде (R-U13, R-U17)."""
    secret = "SESSION-SECRET-6"
    hub = await _hub(make_hub)
    await web_login(hub)
    assert hub.net is not None
    hub.net.tokens.push_issue(
        httpx.Response(200, json={"id": "tokid-6", "token": "PERMANENT-6"})
    )
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        connected = await connect_with_token(hub, alias="tag", token=secret)
        assert connected.status_code == 200, connected.text
        responses = [
            connected,
            await hub.get("/api/me/connections", headers=bearer("sk-ok")),
            await hub.get("/api/catalog", headers=bearer("sk-ok")),
            await hub.get("/ui/servers/tag"),
        ]
        proxied = await _mcp_call(hub)
        assert proxied.status_code == 200, proxied.text

    fragments = _fragments(secret)
    for response in responses:
        assert response.status_code == 200, response.text
        leaked = [f for f in fragments if f in response.text]
        assert leaked == [], f"{response.url}: {leaked}"

    database = await dump_database(hub.app)
    assert [f for f in fragments if f in database] == []
    kv = dump_kv(hub.app)
    assert "conn:u1:tag" in kv, "кэш подключения не заполнен MCP-вызовом"
    assert [f for f in fragments if f in kv] == []
    audit = json.dumps(await audit_rows(hub.app), default=str, ensure_ascii=False)
    assert [f for f in fragments if f in audit] == []

    logged = all_log(caplog, json_logs)
    assert logged, "журнал пуст — проверка вырождена"
    assert [f for f in fragments if f in logged] == []

    assert _stored(hub, await _upstream_row(hub)) == "PERMANENT-6"


# --- AC-224 ----------------------------------------------------------------


@pytest.mark.ac("AC-224")
async def test_issued_token_id_and_response_body_never_leak(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Постоянный токен, тело выпуска и его идентификатор наружу не отдаются (R-U17)."""
    hub = await _hub(make_hub)
    await web_login(hub)
    assert hub.net is not None
    hub.net.tokens.push_issue(
        httpx.Response(
            200,
            json={
                "id": "tokid-SECRET-ID",
                "token": "PERMANENT-SECRET-8",
                "user_id": "u1",
                "extra": "BODY-MARKER-8",
            },
        )
    )
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        connected = await connect_with_token(hub, alias="tag", token="SESSION-8")
        assert connected.status_code == 200, connected.text
        responses = [
            connected,
            await hub.get("/api/me/connections", headers=bearer("sk-ok")),
            await hub.get("/api/catalog", headers=bearer("sk-ok")),
            await hub.get("/remote-config", headers=bearer("sk-ok")),
            await hub.get("/metrics"),
            await hub.get("/ui/servers/tag"),
        ]

    secrets = ("PERMANENT-SECRET-8", "BODY-MARKER-8", "tokid-SECRET-ID")
    for response in responses:
        assert response.status_code == 200, response.text
        for secret in secrets:
            assert secret not in response.text, f"{response.url}: {secret}"

    # Значение постоянного токена и тело ответа на выпуск не должны появиться ни у одного
    # логгера: фильтр по ``hub`` замаскировал бы утечку через стороннюю библиотеку.
    everything = all_log(caplog, json_logs)
    assert everything, "журнал пуст — проверка вырождена"
    for secret in ("PERMANENT-SECRET-8", "BODY-MARKER-8"):
        assert secret not in everything, secret
    # Идентификатор выпущенного токена хранится открытым (R-U17.3) и законно виден в DEBUG-эхе
    # SQL драйвера БД; правило запрещает его в журнале Hub — там и проверяем.
    hub_logged = hub_log(caplog, json_logs)
    assert hub_logged, "журнал Hub пуст — проверка вырождена"
    assert "tokid-SECRET-ID" not in hub_logged

    rows = await audit_rows(hub.app)
    dumped = json.dumps(rows, default=str, ensure_ascii=False)
    for secret in secrets:
        assert secret not in dumped, secret
    issued = [r for r in rows if r["action"] == "upstream_token_issued"]
    assert len(issued) == 1
    assert issued[0]["details"] == {"alias": "tag", "auth_method": "session_token"}
    connected_rows = [r for r in rows if r["action"] == "connection_connected"]
    assert connected_rows[0]["details"]["token_origin"] == "issued"

    # R-U17.1: в журнале есть alias, идентификатор способа и HTTP-код запроса выпуска.
    records = json_logs.records()
    exchange_records = [
        r
        for r in records
        if r.get("alias") == "tag" and r.get("auth_method") == "session_token"
        and r.get("status") == 200 and "exchange" in str(r.get("message"))
    ]
    assert exchange_records, json.dumps(records, ensure_ascii=False)


# --- AC-226 ----------------------------------------------------------------


@pytest.mark.ac("AC-226")
async def test_marker_isolates_hub_installations(make_hub: HubFactory, tmp_path: Any) -> None:
    """Маркер выпущенного токена содержит установку Hub и изолирует их (R-U15.1)."""
    production = await _hub(make_hub)
    stage = await _hub(
        make_hub,
        path=tmp_path / "catalog-stage.yaml",
        base_url=STAGE_PUBLIC_URL,
        public_url=STAGE_PUBLIC_URL,
    )
    assert production.net is not None and stage.net is production.net
    api = production.net.tokens
    api.add_item("stage-1", STAGE_MARKER)

    body = (await _connect(production, "SESSION-prod")).json()
    assert body["token_origin"] == "issued"
    assert api.descriptions() == [MARKER]
    assert "stage-1" not in api.revoked_ids
    assert "stage-1" in api.item_ids

    body = (await _connect(stage, "SESSION-stage")).json()
    assert body["token_origin"] == "issued"
    assert api.descriptions() == [MARKER, STAGE_MARKER]
    assert api.revoked_ids == ["stage-1"]
    # Токен, выпущенный установкой hub.test, не тронут.
    assert "tokid-1" in api.item_ids
    assert _stored(production, await _upstream_row(production)) == "PERMANENT-1"


# --- AC-228 ----------------------------------------------------------------


def _tag_spec_catalog() -> dict[str, Any]:
    return catalog_doc([tag_spec_server_rev4()])


@pytest.mark.ac("AC-228")
async def test_tag_catalog_entry_of_revision_4_loads_and_works(make_hub: HubFactory) -> None:
    """Запись каталога ``tag`` с блоками exchange и expiry принимается и работает (R-U10.1)."""
    hub = await _hub(make_hub, servers=_tag_spec_catalog()["servers"])
    assert hub.net is not None
    api = hub.net.spec_tokens
    api.push_issue(httpx.Response(200, json={"id": "mm-1", "token": "MM-PERMANENT"}))

    catalog = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert catalog.status_code == 200, catalog.text
    methods = {m["id"]: m for m in catalog.json()["servers"][0]["auth_methods"]}
    assert methods["session_token"]["issues_permanent_token"] is True
    assert methods["corp_oauth"]["issues_permanent_token"] is False

    body = (await _connect(hub, "MMAUTHTOKEN-1")).json()
    assert body["token_origin"] == "issued"
    assert body["token_origin_reason"] is None

    assert len(api.issue_requests) == 1
    issued = api.issue_requests[0]
    assert issued.method == "POST"
    assert str(issued.url).split("?")[0] == TAG_SPEC_EXCHANGE_URL
    assert issued.header("authorization") == "Bearer MMAUTHTOKEN-1"
    assert issued.json_body == {"description": MARKER}
    assert _stored(hub, await _upstream_row(hub)) == "MM-PERMANENT"


@pytest.mark.ac("AC-228")
async def test_tag_catalog_entry_of_revision_4_survives_forbidden_exchange(
    make_hub: HubFactory,
) -> None:
    """Боевой контур ТЭГ отвечает 403 и на выпуск, и на список — подключение есть (R-U10.1)."""
    hub = await _hub(make_hub, servers=_tag_spec_catalog()["servers"])
    assert hub.net is not None
    api = hub.net.spec_tokens
    forbidden = httpx.Response(403, json={"id": "api.context.permissions.app_error"})
    api.push_issue(forbidden)
    api.push_list(forbidden)

    body = (await _connect(hub, "MMAUTHTOKEN-2")).json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] == "policy_denied"
    assert _stored(hub, await _upstream_row(hub)) == "MMAUTHTOKEN-2"


# --- AC-229 ----------------------------------------------------------------


@pytest.mark.ac("AC-229")
async def test_failed_exchange_is_not_remembered(make_hub: HubFactory) -> None:
    """Отрицательного кэша нет: следующее подключение повышает коннектор (R-U14.4)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(403, json={"error": "forbidden"}))

    first = (await _connect(hub, "SESSION-early")).json()
    assert first["token_origin"] == "submitted"
    assert first["token_origin_reason"] == "policy_denied"
    assert len(api.issue_requests) == 1

    api.push_issue(httpx.Response(200, json={"id": "tokid-late", "token": "PERMANENT-late"}))
    second = (await _connect(hub, "SESSION-late")).json()
    assert second["token_origin"] == "issued"
    assert second["token_origin_reason"] is None
    assert len(api.issue_requests) == 2, "попытка выпуска пропущена"
    row = await _upstream_row(hub)
    assert _stored(hub, row) == "PERMANENT-late"
    assert row["issued_token_id"] == "tokid-late"

    api.push_issue(httpx.Response(403, json={"error": "forbidden"}))
    third = (await _connect(hub, "SESSION-again")).json()
    assert third["token_origin"] == "submitted"
    assert third["token_origin_reason"] == "policy_denied"
    assert len(api.issue_requests) == 3
    row = await _upstream_row(hub)
    assert _stored(hub, row) == "SESSION-again"
    assert row["issued_token_id"] is None


@pytest.mark.ac("AC-227")
async def test_server_without_auth_methods_is_untouched(make_hub: HubFactory) -> None:
    """Сервер без способов подключения ревизией 4 не затронут (AC-22 не изменился)."""
    hub = await _hub(
        make_hub, servers=[*_servers(_method()), native_server("plain")]
    )
    catalog = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert catalog.status_code == 200, catalog.text
    servers = {s["alias"]: s for s in catalog.json()["servers"]}
    assert "auth_methods" not in servers["plain"]
    assert "issues_permanent_token" not in servers["plain"]
