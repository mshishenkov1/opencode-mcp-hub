"""Сирота выпущенного токена при неудавшемся обмене (R-U19): AC-232…AC-237.

Ревизия 4.1 закрывает пробел ревизии 4: раньше неудавшийся обмен на переподключении затирал
``issued_token_id``, и выпущенный Hub'ом долгоживущий токен оставался в учётной записи
пользователя, а Hub переставал знать его идентификатор. Теперь перед потерей идентификатора
выполняется попытка отзыва (не более двух запросов), а неудача оставляет **пометку на уборку** —
пару ``token_origin = "submitted"`` при непустом ``issued_token_id``.

Проверяется наблюдаемое: что ушло в целевую систему (адрес, тело, учётные данные запроса), что
лежит в строке ``upstream_tokens`` после подключения, что записано в аудит и журнал.

Про два способа проверки журнала. Значения токенов ищутся по записям **всех** логгеров
(``all_log``): утечка через стороннюю библиотеку тоже утечка. Идентификатор выпущенного токена
ищется по журналу Hub (``hub_log``): по R-U17.3 он не учётные данные, хранится открытым и потому
законно виден в DEBUG-эхе SQL драйвера БД, а правило запрещает его именно в журнале Hub — тот же
разбор, что принят ревью для AC-224.

Все проверки идут против локальных моков; обращений в сеть нет.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    EXCHANGE_REVOKE_URL,
    JIRA_AS,
    TAG_ENV,
    add_key,
    all_log,
    audit_rows,
    bearer,
    capture_all_levels,
    capture_json_logs,
    catalog_doc,
    connect_with_token,
    exchange_block,
    fetch_rows,
    hub_log,
    oauth_method,
    user_token_facade,
    user_token_method,
    web_login,
    write_catalog,
)

FORBIDDEN = httpx.Response(403, json={"id": "api.context.permissions.app_error"})
SERVER_ERROR = httpx.Response(500, json={"error": "REVOKE-BODY-MARKER"})

OLD_ID = "tokid-1"
OLD_TOKEN = "PERMANENT-1"
SUBMITTED = "SESSION-2"


# --- вспомогательное -------------------------------------------------------


def _method(method_id: str = "session_token", *, exchange: Any = "default", **overrides: Any) -> dict[str, Any]:
    method = user_token_method(method_id, **overrides)
    if exchange == "default":
        exchange = exchange_block()
    if exchange is not None:
        method["exchange"] = exchange
    return method


def _catalog(*methods: dict[str, Any]) -> dict[str, Any]:
    return catalog_doc([user_token_facade("tag", methods=list(methods or (_method(),)))])


async def _hub(make_hub: HubFactory, *, catalog: Any = None, **overrides: Any) -> Hub:
    hub = await make_hub(
        catalog=catalog if catalog is not None else _catalog(),
        env=TAG_ENV,
        base_url="https://hub.test",
        **overrides,
    )
    await add_key(hub, "sk-ok", "u1")
    return hub


async def _row(hub: Hub, *, user_id: str = "u1", alias: str = "tag") -> dict[str, Any]:
    rows = await fetch_rows(
        hub.app,
        "SELECT t.access_token_enc, t.issued_token_id, t.token_origin, t.token_origin_reason "
        "FROM upstream_tokens t JOIN connections c ON c.id = t.connection_id "
        "WHERE c.user_id = :u AND c.alias = :a",
        u=user_id,
        a=alias,
    )
    assert rows, f"строки upstream_tokens для {user_id}/{alias} нет"
    return rows[0]


def _stored(hub: Hub, row: dict[str, Any]) -> str:
    return str(hub.app.state.cipher.decrypt(row["access_token_enc"]))


async def _seed_issued(
    hub: Hub, *, token_id: str = OLD_ID, value: str = OLD_TOKEN, submitted: str = "SESSION-1"
) -> None:
    """Прежнее подключение, прошедшее обменом: token_origin issued с сохранённым идентификатором."""
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": token_id, "token": value}))
    response = await connect_with_token(hub, alias="tag", token=submitted)
    assert response.status_code == 200, response.text
    assert response.json()["token_origin"] == "issued", response.text
    row = await _row(hub)
    assert row["issued_token_id"] == token_id
    api.reset_requests()


async def _seed_orphan_mark(hub: Hub) -> None:
    """Пометка на уборку: обмен не удался и отзыв прежнего токена провалился (R-U19.4)."""
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    api.push_issue(FORBIDDEN)
    api.revoke_responder = lambda recorded: SERVER_ERROR
    response = await connect_with_token(hub, alias="tag", token="SESSION-mark")
    assert response.status_code == 200, response.text
    assert response.json()["token_origin"] == "submitted", response.text
    row = await _row(hub)
    assert row["issued_token_id"] == OLD_ID, "пометка на уборку не сохранена"
    api.revoke_responder = None
    api.reset_requests()


async def _audit(hub: Hub, action: str) -> list[dict[str, Any]]:
    return await audit_rows(hub.app, action)


def _dump(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, default=str, ensure_ascii=False)


# --- AC-232 ----------------------------------------------------------------


@pytest.mark.ac("AC-232")
async def test_orphan_is_revoked_before_hub_forgets_its_id(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Неудавшийся обмен: прежний выпущенный токен отзывается до потери идентификатора (R-U19)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    api.push_issue(FORBIDDEN)
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token=SUBMITTED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] == "policy_denied"

    # Ровно один запрос отзыва — прежним постоянным токеном подключения (R-U19.3(а)).
    assert api.revoked_ids == [OLD_ID]
    assert api.revoke_credentials == [f"Bearer {OLD_TOKEN}"]
    assert str(api.revoke_requests[0].url).split("?")[0] == EXCHANGE_REVOKE_URL

    row = await _row(hub)
    assert _stored(hub, row) == SUBMITTED
    assert row["token_origin"] == "submitted"
    # R-U14.3 в редакции 4.1: NULL — только по подтверждённому отзыву.
    assert row["issued_token_id"] is None

    revoked = await _audit(hub, "upstream_token_revoked")
    assert len(revoked) == 1
    assert revoked[0]["details"] == {
        "alias": "tag",
        "reason": "exchange_failed",
        "outcome": "ok",
    }
    assert await _audit(hub, "upstream_token_cleanup_failed") == []

    everything = all_log(caplog, json_logs)
    assert everything, "журнал пуст — проверка вырождена"
    for secret in (OLD_TOKEN, SUBMITTED):
        assert secret not in everything, secret
    assert OLD_ID not in hub_log(caplog, json_logs)
    assert OLD_ID not in _dump(await audit_rows(hub.app))


@pytest.mark.ac("AC-232")
async def test_revoke_block_is_taken_from_the_issuing_method(make_hub: HubFactory) -> None:
    """Блок ``exchange.revoke`` берётся у способа, которым токен был выпущен (R-U19.3).

    Пользователь переподключается **другим** способом, у которого обмена нет вовсе. Если бы Hub
    смотрел на текущий способ, отзывать было бы нечем и сирота осталась бы; правило требует брать
    блок у способа из ``connections.auth_method`` до подключения — тогда отзыв выполним.
    """
    hub = await _hub(make_hub, catalog=_catalog(_method("issuing"), _method("plain", exchange=None)))
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": OLD_ID, "token": OLD_TOKEN}))
    first = await connect_with_token(
        hub, alias="tag", body={"token": "SESSION-1", "method": "issuing"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["token_origin"] == "issued"
    api.reset_requests()

    second = await connect_with_token(
        hub, alias="tag", body={"token": SUBMITTED, "method": "plain"}
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] is None
    assert body["auth_method"] == "plain"

    assert api.revoked_ids == [OLD_ID], "отзыв не выполнен блоком способа, выпустившего токен"
    assert api.revoke_credentials == [f"Bearer {OLD_TOKEN}"]
    assert str(api.revoke_requests[0].url).split("?")[0] == EXCHANGE_REVOKE_URL
    row = await _row(hub)
    assert row["issued_token_id"] is None
    assert _stored(hub, row) == SUBMITTED


# --- AC-233 ----------------------------------------------------------------


@pytest.mark.ac("AC-233")
async def test_fallback_attempt_with_submitted_token_succeeds(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """(а) прежний токен отвергнут — запасная попытка присланным снимает сироту (R-U19.3(б))."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    api.push_issue(FORBIDDEN)
    api.revoke_responder = lambda recorded: (
        httpx.Response(401, json={"error": "unauthorized"})
        if recorded.header("authorization") == f"Bearer {OLD_TOKEN}"
        else httpx.Response(200, json={"status": "OK"})
    )
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token=SUBMITTED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] == "policy_denied"

    # Ровно два запроса и не больше: прежним постоянным, затем присланным (R-U19.3).
    assert api.revoked_ids == [OLD_ID, OLD_ID]
    assert api.revoke_credentials == [f"Bearer {OLD_TOKEN}", f"Bearer {SUBMITTED}"]

    row = await _row(hub)
    assert _stored(hub, row) == SUBMITTED
    assert row["issued_token_id"] is None

    revoked = await _audit(hub, "upstream_token_revoked")
    assert [r["details"]["outcome"] for r in revoked] == ["ok"], "событие пишется один раз на заход"
    assert await _audit(hub, "upstream_token_cleanup_failed") == []
    assert OLD_ID not in hub_log(caplog, json_logs)


@pytest.mark.ac("AC-233")
async def test_both_revoke_attempts_fail_and_leave_a_cleanup_mark(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """(б) обе попытки провалились — подключение цело, идентификатор остаётся пометкой (R-U19.4)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    api.push_issue(FORBIDDEN)
    api.revoke_responder = lambda recorded: SERVER_ERROR
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token=SUBMITTED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] == "policy_denied"

    assert api.revoked_ids == [OLD_ID, OLD_ID], "запросов отзыва должно быть ровно два"
    assert api.revoke_credentials == [f"Bearer {OLD_TOKEN}", f"Bearer {SUBMITTED}"]

    row = await _row(hub)
    assert _stored(hub, row) == SUBMITTED
    # Пометка на уборку: submitted при непустом идентификаторе — законное состояние (решение 100).
    assert row["token_origin"] == "submitted"
    assert row["issued_token_id"] == OLD_ID

    revoked = await _audit(hub, "upstream_token_revoked")
    assert [r["details"] for r in revoked] == [
        {"alias": "tag", "reason": "exchange_failed", "outcome": "failed"}
    ]
    failed = await _audit(hub, "upstream_token_cleanup_failed")
    assert [r["details"] for r in failed] == [{"alias": "tag", "stage": "revoke"}]

    everything = all_log(caplog, json_logs)
    assert everything, "журнал пуст — проверка вырождена"
    assert "REVOKE-BODY-MARKER" not in everything, "тело ответа целевой системы попало в журнал"
    for secret in (OLD_TOKEN, SUBMITTED):
        assert secret not in everything, secret
    assert OLD_ID not in hub_log(caplog, json_logs)
    assert OLD_ID not in _dump(await audit_rows(hub.app))

    listed = await hub.get("/api/me/connections", headers=bearer("sk-ok"))
    assert listed.status_code == 200, listed.text
    assert OLD_ID not in listed.text
    assert response.text.find(OLD_ID) == -1


# --- AC-234 ----------------------------------------------------------------


@pytest.mark.ac("AC-234")
async def test_successful_reconnect_picks_up_the_mark(make_hub: HubFactory) -> None:
    """(а) удачное подключение убирает помеченный токен новым постоянным (R-U19.6, R-U15.3)."""
    hub = await _hub(
        make_hub, catalog=_catalog(oauth_method("corp_oauth", as_base=JIRA_AS), _method())
    )
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_orphan_mark(hub)
    api.push_issue(httpx.Response(200, json={"id": "tokid-2", "token": "PERMANENT-2"}))

    response = await connect_with_token(hub, alias="tag", token="SESSION-3")
    assert response.status_code == 200, response.text
    assert response.json()["token_origin"] == "issued"

    assert api.revoked_ids == [OLD_ID]
    assert api.revoke_credentials == ["Bearer PERMANENT-2"]
    row = await _row(hub)
    assert row["issued_token_id"] == "tokid-2"
    assert _stored(hub, row) == "PERMANENT-2"
    assert hub.net.providers["jira"].revoke_requests == []


@pytest.mark.ac("AC-234")
async def test_repeated_failed_connect_retries_the_mark(make_hub: HubFactory) -> None:
    """(б) повторно неудачное подключение не пропускает уборку (R-U19.6)."""
    hub = await _hub(
        make_hub, catalog=_catalog(oauth_method("corp_oauth", as_base=JIRA_AS), _method())
    )
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_orphan_mark(hub)
    api.push_issue(FORBIDDEN)

    response = await connect_with_token(hub, alias="tag", token="SESSION-4")
    assert response.status_code == 200, response.text
    assert response.json()["token_origin"] == "submitted"

    assert api.revoked_ids == [OLD_ID], "попытка отзыва пропущена"
    row = await _row(hub)
    assert row["issued_token_id"] is None
    assert _stored(hub, row) == "SESSION-4"
    assert hub.net.providers["jira"].revoke_requests == []


@pytest.mark.ac("AC-234")
async def test_disconnect_picks_up_the_mark(make_hub: HubFactory) -> None:
    """(в) отключение убирает помеченный токен и не трогает присланный (уточнение R-U15.4)."""
    hub = await _hub(
        make_hub, catalog=_catalog(oauth_method("corp_oauth", as_base=JIRA_AS), _method())
    )
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_orphan_mark(hub)

    response = await hub.client.delete("/api/me/connections/tag", headers=bearer("sk-ok"))
    assert response.status_code == 200, response.text
    assert response.json() == {"alias": "tag", "status": "not_connected"}

    assert api.revoked_ids == [OLD_ID]
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == []
    rows = await fetch_rows(hub.app, "SELECT status FROM connections WHERE alias = 'tag'")
    assert rows[0]["status"] == "not_connected"
    assert len(await _audit(hub, "connection_disconnected")) == 1
    # Присланный токен не отзывается никогда, OAuth revoke_url для user_token не вызывается.
    assert all(value not in api.revoked_ids for value in ("SESSION-mark", "SESSION-1"))
    assert hub.net.providers["jira"].revoke_requests == []


# --- AC-235 ----------------------------------------------------------------


async def _reload_without_exchange(hub: Hub) -> None:
    """Каталог перечитан: у того же способа блока ``exchange`` больше нет (R-U19.5)."""
    write_catalog(hub.catalog_path, _catalog(_method(exchange=None)))
    reloaded = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert reloaded.status_code == 200, reloaded.text


@pytest.mark.ac("AC-235")
async def test_nothing_to_revoke_with_keeps_the_mark_and_records_the_fact(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Механизма отзыва нет: ни одного запроса, пометка цела, факт зафиксирован (R-U19.5)."""
    hub = await _hub(make_hub, admin_token="adm")
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    await _reload_without_exchange(hub)
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token="SESSION-9")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    # Обмен не предусмотрен — предупреждать не о чем (R-U14.1).
    assert body["token_origin_reason"] is None

    row = await _row(hub)
    assert _stored(hub, row) == "SESSION-9"
    assert row["issued_token_id"] == OLD_ID
    assert api.revoke_requests == [], "отзывать нечем, а запрос ушёл"
    assert api.list_requests == []
    assert api.issue_requests == []

    failed = await _audit(hub, "upstream_token_cleanup_failed")
    assert [r["details"] for r in failed] == [{"alias": "tag", "stage": "orphan"}]
    assert await _audit(hub, "upstream_token_revoked") == []

    records = [r for r in json_logs.records() if r.get("message") == "upstream_token_cleanup_failed"]
    assert records, json.dumps(json_logs.records(), ensure_ascii=False)
    assert records[0]["level"] == "WARNING"
    assert records[0]["alias"] == "tag"
    assert records[0]["stage"] == "orphan"

    everything = all_log(caplog, json_logs)
    for secret in (OLD_TOKEN, "SESSION-9"):
        assert secret not in everything, secret
    assert OLD_ID not in hub_log(caplog, json_logs)
    assert OLD_ID not in _dump(await audit_rows(hub.app))


@pytest.mark.ac("AC-235")
async def test_disconnect_with_unenforceable_mark_still_succeeds(make_hub: HubFactory) -> None:
    """Отключение проходит и тогда, когда пометку исполнить нечем (R-U19.8)."""
    hub = await _hub(make_hub, admin_token="adm")
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    await _reload_without_exchange(hub)
    connected = await connect_with_token(hub, alias="tag", token="SESSION-9")
    assert connected.status_code == 200, connected.text
    api.reset_requests()

    response = await hub.client.delete("/api/me/connections/tag", headers=bearer("sk-ok"))
    assert response.status_code == 200, response.text
    assert response.json() == {"alias": "tag", "status": "not_connected"}

    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == []
    assert len(await _audit(hub, "connection_disconnected")) == 1
    orphan = [
        r for r in await _audit(hub, "upstream_token_cleanup_failed")
        if r["details"]["stage"] == "orphan"
    ]
    assert len(orphan) == 2, "факт потери следа не записан при отключении"
    assert api.revoke_requests == [], "запросов отзыва по-прежнему ноль"


# --- AC-236 ----------------------------------------------------------------


EX_URL = "https://tag.test/api/v4/marker/tokens"
EX_REVOKE = "https://tag.test/api/v4/marker/revoke"
EX_DESCRIPTION = "Маркер выпуска ревизии 4.1"
EX_TOKEN_FIELD = "permanent_value_field"
EX_TOKEN_ID_FIELD = "permanent_ident_field"
UNAVAILABLE_REASON = "Способ отключён администраторами до миграции"


@pytest.mark.ac("AC-236")
async def test_flag_is_published_for_unavailable_method_but_block_is_not(
    make_hub: HubFactory,
) -> None:
    """``issues_permanent_token`` отдаётся и при ``available: false`` (R-U16, решение 103)."""
    marked = exchange_block(
        url=EX_URL,
        list_url=None,
        revoke_url=EX_REVOKE,
        description=EX_DESCRIPTION,
        token_field=EX_TOKEN_FIELD,
        token_id_field=EX_TOKEN_ID_FIELD,
    )
    hidden = _method(
        "legacy_token",
        exchange=marked,
        available=False,
        unavailable_reason=UNAVAILABLE_REASON,
    )
    plain = _method("session_token", exchange=None)
    hub = await _hub(make_hub, catalog=_catalog(hidden, plain))
    await web_login(hub)
    assert hub.net is not None

    catalog = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert catalog.status_code == 200, catalog.text
    methods = {m["id"]: m for m in catalog.json()["servers"][0]["auth_methods"]}
    assert methods["legacy_token"]["issues_permanent_token"] is True
    assert methods["legacy_token"]["available"] is False
    assert methods["legacy_token"]["unavailable_reason"] == UNAVAILABLE_REASON
    assert methods["session_token"]["issues_permanent_token"] is False

    page = await hub.get("/ui/servers/tag")
    assert page.status_code == 200, page.text
    assert UNAVAILABLE_REASON in page.text

    for response in (catalog, page):
        text = response.text
        assert "exchange" not in text
        for secret in (EX_URL, EX_REVOKE, EX_DESCRIPTION, EX_TOKEN_FIELD, EX_TOKEN_ID_FIELD):
            assert secret not in text, f"{response.url}: {secret}"

    # AC-179 и AC-193 не изменились: подключение недоступным способом отвергается до сети.
    refused = await connect_with_token(
        hub, alias="tag", token="SESSION-x", body={"token": "SESSION-x", "method": "legacy_token"}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == "auth_method_unavailable"
    assert hub.net.tokens.issue_requests == []
    assert hub.net.verify.requests == []


# --- AC-237 ----------------------------------------------------------------


_ID_CASES: list[tuple[str, Any, Any, bool, Any]] = [
    ("(1) обмен и отзыв удались", "issue-ok", None, False, "tokid-new"),
    ("(2) обмен удался, отзыв 500", "issue-ok", SERVER_ERROR, False, "tokid-new"),
    ("(3) обмен 403, отзыв удался", "issue-403", None, False, None),
    ("(4) обмен 403, отзыв 500", "issue-403", SERVER_ERROR, False, OLD_ID),
    ("(5) выпущенный непригоден, отзыв 500", "issue-ok", SERVER_ERROR, True, OLD_ID),
]


@pytest.mark.ac("AC-237")
@pytest.mark.parametrize(
    ("title", "issue", "revoke", "unusable", "expected"),
    _ID_CASES,
    ids=[case[0] for case in _ID_CASES],
)
async def test_issued_token_id_disappears_only_on_confirmed_revoke(
    make_hub: HubFactory, title: str, issue: str, revoke: Any, unusable: bool, expected: Any
) -> None:
    """Непустой идентификатор исчезает только по отзыву или по замене новым (R-U19.8, R-U14.3)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)

    if issue == "issue-ok":
        api.push_issue(httpx.Response(200, json={"id": "tokid-new", "token": "PERMANENT-new"}))
    else:
        api.push_issue(FORBIDDEN)
    if unusable:
        hub.net.verify.by_token["PERMANENT-new"] = httpx.Response(401, json={"error": "no"})
    if revoke is not None:
        api.revoke_responder = lambda recorded: revoke

    response = await connect_with_token(hub, alias="tag", token=SUBMITTED)
    assert response.status_code == 200, f"{title}: {response.text}"
    body = response.json()
    assert body["status"] == "connected", title

    row = await _row(hub)
    assert row["issued_token_id"] == expected, title

    if issue == "issue-ok" and not unusable:
        assert body["token_origin"] == "issued", title
        assert _stored(hub, row) == "PERMANENT-new", title
    else:
        assert body["token_origin"] == "submitted", title
        assert _stored(hub, row) == SUBMITTED, title

    if title.startswith("(2)"):
        failed = await _audit(hub, "upstream_token_cleanup_failed")
        assert [r["details"]["stage"] for r in failed] == ["revoke"], title


@pytest.mark.ac("AC-237")
async def test_method_without_exchange_keeps_the_mark(make_hub: HubFactory) -> None:
    """(6) способ без блока exchange пометку не теряет и запросов не шлёт (R-U19.5, R-U19.8)."""
    hub = await _hub(make_hub, admin_token="adm")
    assert hub.net is not None
    api = hub.net.tokens
    await _seed_issued(hub)
    await _reload_without_exchange(hub)

    response = await connect_with_token(hub, alias="tag", token="SESSION-6")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["token_origin"] == "submitted"
    assert body["token_origin_reason"] is None

    row = await _row(hub)
    assert row["issued_token_id"] == OLD_ID
    assert _stored(hub, row) == "SESSION-6"
    assert api.revoke_requests == []
