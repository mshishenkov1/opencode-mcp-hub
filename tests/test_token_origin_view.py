"""Что видит пользователь о происхождении токена (R-U16, R-U8, R-C6): AC-225, AC-227.

Пользователь должен знать, отвалится ли коннектор при выходе из мессенджера, — и при этом не
должен видеть ни значения токенов, ни устройства обмена. Проверяется наблюдаемое: ответы API и
HTML страниц.

Все проверки идут против локальных моков; обращений в сеть нет.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    TAG_ENV,
    add_key,
    bearer,
    catalog_doc,
    connect_with_token,
    exchange_block,
    expiry_block,
    native_server,
    user_token_facade,
    user_token_method,
    web_login,
)

ISSUED_TEXT = "выход из мессенджера подключение не разорвёт"
SUBMITTED_TEXT = "прервётся при выходе из мессенджера"
POLICY_TEXT = "целевая система не разрешает выпуск личных токенов"


# --- вспомогательное -------------------------------------------------------


def _server(alias: str, *, exchange: dict[str, Any] | None, title: str) -> dict[str, Any]:
    method = user_token_method("session_token")
    if exchange is not None:
        method["exchange"] = exchange
    return user_token_facade(alias, methods=[method], title=title)


async def _hub(make_hub: HubFactory, servers: list[dict[str, Any]]) -> Hub:
    hub = await make_hub(
        catalog=catalog_doc(servers), env=TAG_ENV, base_url="https://hub.test"
    )
    await add_key(hub, "sk-ok", "u1")
    return hub


def _card(html: str, alias: str) -> str:
    """Карточка одного коннектора на странице «Мои подключения»."""
    marker = f'id="conn-{alias}"'
    assert marker in html, f"на странице нет карточки {alias}"
    return html.split(marker, 1)[1].split('class="card"', 1)[0]


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


# --- AC-225 ----------------------------------------------------------------


@pytest.mark.ac("AC-225")
async def test_permanent_and_session_connections_look_different(make_hub: HubFactory) -> None:
    """Постоянное, временное и «неизвестно какое» подключения различаются видом (R-U16)."""
    hub = await _hub(
        make_hub,
        [
            _server("a", exchange=exchange_block(), title="Коннектор A"),
            _server("b", exchange=exchange_block(), title="Коннектор B"),
            _server("c", exchange=None, title="Коннектор C"),
        ],
    )
    await web_login(hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": "tokid-a", "token": "PERMANENT-A"}))
    api.push_issue(httpx.Response(403, json={"error": "forbidden"}))

    for alias, token in (("a", "SESSION-A"), ("b", "SESSION-B"), ("c", "SESSION-C")):
        response = await connect_with_token(hub, alias=alias, token=token)
        assert response.status_code == 200, f"{alias}: {response.text}"

    listed = await hub.get("/api/me/connections", headers=bearer("sk-ok"))
    assert listed.status_code == 200, listed.text
    items = {c["alias"]: c for c in listed.json()}
    assert (items["a"]["token_origin"], items["a"]["token_origin_reason"]) == ("issued", None)
    assert (items["b"]["token_origin"], items["b"]["token_origin_reason"]) == (
        "submitted",
        "policy_denied",
    )
    assert (items["c"]["token_origin"], items["c"]["token_origin_reason"]) == ("submitted", None)

    connections = await hub.get("/ui/connections")
    assert connections.status_code == 200, connections.text
    card_a, card_b, card_c = (_card(connections.text, alias) for alias in ("a", "b", "c"))
    assert ISSUED_TEXT in card_a
    assert SUBMITTED_TEXT not in card_a
    assert SUBMITTED_TEXT in card_b
    assert POLICY_TEXT in card_b
    # У способа без блока exchange вид прежний: Hub не знает, какой токен прислал пользователь.
    assert SUBMITTED_TEXT not in card_c
    assert ISSUED_TEXT not in card_c

    pages = {}
    for alias in ("a", "b", "c"):
        page = await hub.get(f"/ui/servers/{alias}")
        assert page.status_code == 200, page.text
        pages[alias] = page.text
    assert ISSUED_TEXT in pages["a"]
    assert SUBMITTED_TEXT not in pages["a"]
    assert SUBMITTED_TEXT in pages["b"]
    assert POLICY_TEXT in pages["b"]
    assert SUBMITTED_TEXT not in pages["c"]
    assert ISSUED_TEXT not in pages["c"]

    for html in (connections.text, *pages.values()):
        for secret in ("SESSION-A", "SESSION-B", "SESSION-C", "PERMANENT-A", "tokid-a"):
            assert secret not in html, secret


@pytest.mark.ac("AC-225")
async def test_connect_form_of_exchanging_method_explains_permanent_token(
    make_hub: HubFactory,
) -> None:
    """До подключения форма способа с exchange объясняет, что присланный токен не сохранится."""
    hub = await _hub(
        make_hub,
        [
            _server("a", exchange=exchange_block(), title="Коннектор A"),
            _server("c", exchange=None, title="Коннектор C"),
        ],
    )
    await web_login(hub)

    with_exchange = await hub.get("/ui/servers/a")
    without = await hub.get("/ui/servers/c")
    assert with_exchange.status_code == 200 and without.status_code == 200
    assert "постоянный токен" in with_exchange.text
    assert "не сохранит" in with_exchange.text
    assert "не сохранит" not in without.text


# --- AC-227 ----------------------------------------------------------------


EXCHANGE_URL_MARKER = "https://tag.test/api/v4/marker/tokens"
REVOKE_URL_MARKER = "https://tag.test/api/v4/marker/revoke"
EXPIRY_URL_MARKER = "https://tag.test/api/v4/marker/sessions"
DESCRIPTION_MARKER = "Маркер выпуска ревизии 4"
TOKEN_FIELD_MARKER = "permanent_value_field"
TOKEN_ID_FIELD_MARKER = "permanent_ident_field"
HEADER_NAME_MARKER = "X-Issue-Header-Marker"
HEADER_VALUE_MARKER = "issue-header-value-marker"


def _marked_exchange() -> dict[str, Any]:
    block = exchange_block(
        url=EXCHANGE_URL_MARKER,
        list_url=EXCHANGE_URL_MARKER,
        revoke_url=REVOKE_URL_MARKER,
        description=DESCRIPTION_MARKER,
        token_field=TOKEN_FIELD_MARKER,
        token_id_field=TOKEN_ID_FIELD_MARKER,
    )
    block["headers"] = {
        "Authorization": "Bearer {{access_token}}",
        HEADER_NAME_MARKER: HEADER_VALUE_MARKER,
    }
    return block


@pytest.mark.ac("AC-227")
async def test_only_the_flag_is_published_not_the_exchange_block(make_hub: HubFactory) -> None:
    """Признак ``issues_permanent_token`` не зависит от публикации exchange: в /api/catalog у
    доступного способа с обменом есть exchange (с вложенным revoke, без list), у способа без
    него — нет; expiry не публикуется никогда; на страницах — ни exchange, ни expiry (R-U16,
    R-U8.1)."""
    with_exchange = user_token_method("issuing")
    with_exchange["exchange"] = _marked_exchange()
    with_exchange["expiry"] = expiry_block(url=EXPIRY_URL_MARKER)
    plain = user_token_method("plain")
    hub = await _hub(
        make_hub,
        [
            user_token_facade("tag", methods=[with_exchange, plain]),
            native_server("plain-server"),
        ],
    )
    await web_login(hub)

    catalog = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert catalog.status_code == 200, catalog.text
    servers = {s["alias"]: s for s in catalog.json()["servers"]}
    methods = {m["id"]: m for m in servers["tag"]["auth_methods"]}
    assert methods["issuing"]["issues_permanent_token"] is True
    assert methods["plain"]["issues_permanent_token"] is False

    # У сервера без auth_methods новых ключей не появилось: ни auth_methods, ни
    # issues_permanent_token, ни upstream (AC-22 не изменился).
    assert "auth_methods" not in servers["plain-server"]
    assert "issues_permanent_token" not in servers["plain-server"]
    assert "upstream" not in servers["plain-server"]

    page = await hub.get("/ui/servers/tag")
    assert page.status_code == 200, page.text
    connections = await hub.get("/ui/connections")
    assert connections.status_code == 200, connections.text

    # R-U8.1 п. 2: у доступного способа с объявленным обменом exchange публикуется в /api/catalog
    # дословно, вместе с вложенным revoke; у способа без обмена ключа exchange нет.
    exchange = methods["issuing"]["exchange"]
    assert exchange["url"] == EXCHANGE_URL_MARKER
    assert exchange["description"] == DESCRIPTION_MARKER
    assert exchange["token_field"] == TOKEN_FIELD_MARKER
    assert exchange["token_id_field"] == TOKEN_ID_FIELD_MARKER
    assert exchange["headers"][HEADER_NAME_MARKER] == HEADER_VALUE_MARKER
    assert exchange["revoke"]["url"] == REVOKE_URL_MARKER
    # R-U15.3: запрос списка выпущенных токенов наружу не идёт никогда, даже когда exchange
    # публикуется целиком.
    assert "list" not in exchange
    assert "exchange" not in methods["plain"]

    # expiry (R-U18) не публикуется никогда, ни при каких условиях.
    assert not (_all_keys(catalog.json()) & {"expiry"})
    for response in (catalog, page, connections):
        text = response.text
        assert '"expiry"' not in text
        assert EXPIRY_URL_MARKER not in text
    # exchange нужен только приложению для прямого режима, а не странице (R-U8.1 п. 11).
    for response in (page, connections):
        text = response.text
        assert '"exchange"' not in text
        for marker in (
            EXCHANGE_URL_MARKER,
            REVOKE_URL_MARKER,
            DESCRIPTION_MARKER,
            TOKEN_FIELD_MARKER,
            TOKEN_ID_FIELD_MARKER,
            HEADER_VALUE_MARKER,
        ):
            assert marker not in text, f"{response.url}: {marker}"

    # Признак и exchange — единственное, что добавилось к публичному виду доступного способа с
    # обменом сверх способа без него (R-U8, R-C6, R-U8.1).
    assert set(methods["issuing"]) - set(methods["plain"]) == {"exchange"}
