"""Срок годности присланного токена (R-U18): AC-230, AC-231.

Целевая система не говорит, какая из сессий соответствует присланному токену, поэтому показанный
срок — верхняя граница («не позднее»). Проверяется наблюдаемое: значение в ответах API, значение
в БД, текст страницы и отсутствие выдуманных дат при любом неуспехе.

Все проверки идут против локальных моков; обращений в сеть нет.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    EXPIRY_URL,
    JIRA_AS,
    TAG_ENV,
    add_key,
    bearer,
    capture_all_levels,
    capture_json_logs,
    catalog_doc,
    connect_with_token,
    exchange_block,
    expiry_block,
    fetch_rows,
    oauth_method,
    parse_db_datetime,
    record_text,
    user_token_facade,
    user_token_method,
    web_login,
)

SUBMITTED_TEXT = "прервётся при выходе из мессенджера"
UNTIL_TEXT = "не позднее"
FORBIDDEN = httpx.Response(403, json={"id": "api.context.permissions.app_error"})


# --- вспомогательное -------------------------------------------------------


async def _hub(make_hub: HubFactory) -> Hub:
    method = user_token_method("session_token")
    method["exchange"] = exchange_block()
    method["expiry"] = expiry_block()
    hub = await make_hub(
        catalog=catalog_doc([user_token_facade("tag", methods=[method])]),
        env=TAG_ENV,
        base_url="https://hub.test",
    )
    await add_key(hub, "sk-ok", "u1")
    return hub


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


async def _stored_expiry(hub: Hub) -> Any:
    rows = await fetch_rows(
        hub.app,
        "SELECT t.submitted_expires_at FROM upstream_tokens t "
        "JOIN connections c ON c.id = t.connection_id WHERE c.alias = 'tag'",
    )
    assert rows, "строки upstream_tokens нет"
    return rows[0]["submitted_expires_at"]


# --- AC-230 ----------------------------------------------------------------


@pytest.mark.ac("AC-230")
async def test_submitted_token_expiry_is_the_upper_bound(make_hub: HubFactory) -> None:
    """Берётся наибольший пригодный срок и показывается как «не позднее» (R-U18.2, R-U18.3)."""
    hub = await _hub(make_hub)
    await web_login(hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(FORBIDDEN)

    now = hub.clock.now()
    farthest = now + timedelta(days=179)
    api.sessions = [
        {"id": "s1", "expires_at": _ms(farthest)},
        {"id": "s2", "expires_at": _ms(now + timedelta(days=3))},
        {"id": "s3", "expires_at": 0},
        {"id": "s4", "expires_at": _ms(now - timedelta(days=1))},
    ]

    response = await connect_with_token(hub, alias="tag", token="SESSION-10")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_origin"] == "submitted"
    assert body["session_expires_at"] == farthest.isoformat()

    listed = await hub.get("/api/me/connections", headers=bearer("sk-ok"))
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["session_expires_at"] == farthest.isoformat()

    assert parse_db_datetime(await _stored_expiry(hub)) == farthest

    assert len(api.sessions_requests) == 1
    request = api.sessions_requests[0]
    assert str(request.url).split("?")[0] == EXPIRY_URL
    # Срок читается присланным токеном: постоянного у подключения нет (R-U18.1).
    assert request.header("authorization") == "Bearer SESSION-10"

    page = await hub.get("/ui/servers/tag")
    assert page.status_code == 200, page.text
    assert SUBMITTED_TEXT in page.text
    assert UNTIL_TEXT in page.text
    assert farthest.strftime("%d.%m.%Y") in page.text
    # Ни ближайшая сессия, ни «без срока» верхней границей не становятся (R-U18.3).
    assert (now + timedelta(days=3)).strftime("%d.%m.%Y") not in page.text


@pytest.mark.ac("AC-230")
async def test_issued_token_never_asks_for_session_expiry(make_hub: HubFactory) -> None:
    """При выпущенном постоянном токене запрос сессий не отправляется вовсе (R-U18.1)."""
    hub = await _hub(make_hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(httpx.Response(200, json={"id": "tokid-10", "token": "PERMANENT-10"}))
    api.sessions = [{"id": "s1", "expires_at": _ms(hub.clock.now() + timedelta(days=179))}]

    response = await connect_with_token(hub, alias="tag", token="SESSION-10")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_origin"] == "issued"
    assert body["session_expires_at"] is None

    assert api.sessions_requests == []
    assert await _stored_expiry(hub) is None


_EXPIRY_UNITS: list[tuple[str, str, Any]] = [
    ("s", "s", lambda moment: int(moment.timestamp())),
    ("iso8601", "iso8601", lambda moment: moment.isoformat()),
]


@pytest.mark.ac("AC-230")
@pytest.mark.parametrize(
    ("title", "unit", "encode"), _EXPIRY_UNITS, ids=[c[0] for c in _EXPIRY_UNITS]
)
async def test_expires_unit_is_honoured(
    make_hub: HubFactory, title: str, unit: str, encode: Any
) -> None:
    """``expires_unit`` истолковывает значение поля; результат — тот же момент (R-U18)."""
    method = user_token_method("session_token")
    method["exchange"] = exchange_block()
    method["expiry"] = expiry_block(expires_unit=unit, items_field="sessions")
    hub = await make_hub(
        catalog=catalog_doc([user_token_facade("tag", methods=[method])]),
        env=TAG_ENV,
        base_url="https://hub.test",
    )
    await add_key(hub, "sk-ok", "u1")
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(FORBIDDEN)
    farthest = hub.clock.now() + timedelta(days=179)
    api.push_sessions(
        httpx.Response(200, json={"sessions": [{"expires_at": encode(farthest)}]})
    )

    response = await connect_with_token(hub, alias="tag", token="SESSION-unit")
    assert response.status_code == 200, f"{title}: {response.text}"
    assert response.json()["session_expires_at"] == farthest.isoformat(), title


def _expiry_schema_cases() -> list[tuple[str, dict[str, Any], str]]:
    extra = expiry_block()
    extra["retry"] = 2
    empty_headers = expiry_block()
    empty_headers["headers"] = {}
    unknown_unit = expiry_block(expires_unit="minutes")
    return [
        ("лишнее подполе", extra, "servers[0].auth_methods[0].expiry.retry"),
        ("пустой headers", empty_headers, "servers[0].auth_methods[0].expiry.headers"),
        ("неизвестный expires_unit", unknown_unit, "servers[0].auth_methods[0].expiry.expires_unit"),
    ]


@pytest.mark.ac("AC-230")
@pytest.mark.parametrize(
    ("title", "block", "fragment"),
    _expiry_schema_cases(),
    ids=[c[0] for c in _expiry_schema_cases()],
)
async def test_invalid_expiry_block_is_schema_error(
    make_hub: HubFactory, title: str, block: dict[str, Any], fragment: str
) -> None:
    """Непригодный блок ``expiry`` — ошибка схемы с путём к подполю (R-U18, R-C1)."""
    method = user_token_method("session_token")
    method["expiry"] = block
    with pytest.raises(Exception) as excinfo:
        await make_hub(
            catalog=catalog_doc([user_token_facade("tag", methods=[method])]),
            env=TAG_ENV,
            base_url="https://hub.test",
        )
    assert fragment in str(excinfo.value), f"{title}: {excinfo.value}"


@pytest.mark.ac("AC-230")
async def test_expiry_on_oauth_method_is_schema_error(make_hub: HubFactory) -> None:
    """``expiry`` у способа ``type: oauth2`` — ошибка схемы (R-U18)."""
    method = oauth_method("corp_oauth", as_base=JIRA_AS)
    method["expiry"] = expiry_block()
    with pytest.raises(Exception) as excinfo:
        await make_hub(
            catalog=catalog_doc(
                [user_token_facade("tag", methods=[method, user_token_method()])]
            ),
            env=TAG_ENV,
            base_url="https://hub.test",
        )
    assert "servers[0].auth_methods[0].expiry" in str(excinfo.value), excinfo.value


# --- AC-231 ----------------------------------------------------------------


BODY_MARKER = "SESSIONS-BODY-MARKER"


def _expiry_failures(now: datetime) -> list[tuple[str, Any]]:
    return [
        ("403", httpx.Response(403, json={"id": BODY_MARKER})),
        ("500", httpx.Response(500, json={"error": BODY_MARKER})),
        ("сетевая ошибка", httpx.ConnectError("connection refused")),
        ("таймаут", httpx.ReadTimeout("timed out")),
        (
            "не JSON",
            httpx.Response(
                200, content=BODY_MARKER.encode(), headers={"Content-Type": "text/plain"}
            ),
        ),
        ("пустой список", httpx.Response(200, json=[])),
        (
            "все expires_at равны 0",
            httpx.Response(200, json=[{"id": BODY_MARKER, "expires_at": 0} for _ in range(3)]),
        ),
        (
            "все сроки в прошлом",
            httpx.Response(
                200,
                json=[
                    {"id": BODY_MARKER, "expires_at": _ms(now - timedelta(days=days))}
                    for days in (1, 30)
                ],
            ),
        ),
    ]


@pytest.mark.ac("AC-231")
@pytest.mark.parametrize(
    ("title", "outcome"),
    _expiry_failures(datetime.fromisoformat("2026-08-19T12:30:00+00:00")),
    ids=[c[0] for c in _expiry_failures(datetime.fromisoformat("2026-08-19T12:30:00+00:00"))],
)
async def test_unreadable_expiry_invents_no_date(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture, title: str, outcome: Any
) -> None:
    """Любой неуспех чтения срока оставляет ``null`` и не ломает подключение (R-U18.4)."""
    hub = await _hub(make_hub)
    await web_login(hub)
    assert hub.net is not None
    api = hub.net.tokens
    api.push_issue(FORBIDDEN)
    api.push_sessions(outcome)
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await connect_with_token(hub, alias="tag", token="SESSION-11")
    assert response.status_code == 200, f"{title}: {response.text}"
    body = response.json()
    assert body["status"] == "connected", title
    assert body["token_origin"] == "submitted", title
    assert body["token_origin_reason"] == "policy_denied", title
    assert body["session_expires_at"] is None, title

    assert await _stored_expiry(hub) is None, title
    listed = await hub.get("/api/me/connections", headers=bearer("sk-ok"))
    assert listed.json()[0]["session_expires_at"] is None, title

    page = await hub.get("/ui/servers/tag")
    assert page.status_code == 200, page.text
    assert SUBMITTED_TEXT in page.text, title
    assert UNTIL_TEXT not in page.text, title

    logged = "\n".join([record_text(r) for r in caplog.records] + json_logs.raw())
    assert logged, "журнал пуст — проверка вырождена"
    assert BODY_MARKER not in logged, title
