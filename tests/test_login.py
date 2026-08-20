"""Вход через LiteLLM CLI-SSO и постоянный ключ (R-L1..R-L10): AC-24..AC-47."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import Hub, HubFactory
from tests.support import (
    LITELLM_URL,
    audit_rows,
    bearer,
    capture_json_logs,
    dump_all_tables,
    fetch_rows,
    insert_key,
    insert_user,
    kv_session,
    make_jwt,
    mock_key_generate,
    mock_poll,
    mock_start,
    ready_body,
    sha256_hex,
    start_body,
    teams_body,
)

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CLIENT = "opencode-fork/1.17.9"


def _jwt(
    hub: Hub,
    *,
    sub: str | None = "u1",
    email: str | None = "u1@corp.test",
    exp: int | None = 3600,
    **extra: Any,
) -> str:
    claims: dict[str, Any] = dict(extra)
    if sub is not None:
        claims["sub"] = sub
    if email is not None:
        claims["email"] = email
    if exp is not None:
        claims["exp"] = int(hub.clock.time()) + exp
    return make_jwt(claims)


async def _start(
    hub: Hub, client: str | None = CLIENT, *, ll_id: str = "ll-1", **start_kwargs: Any
) -> dict[str, Any]:
    mock_start(hub.litellm, start_body(login_id=ll_id, **start_kwargs))
    payload = {"client": client} if client is not None else None
    resp = await hub.post("/cli/start", json=payload) if payload else await hub.post("/cli/start")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _metric_active_sessions(hub: Hub) -> int:
    text = (await hub.get("/metrics")).text
    m = re.search(r"^hub_login_sessions_active (\d+(?:\.\d+)?)$", text, re.MULTILINE)
    assert m, text
    return int(float(m.group(1)))


def _hub_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("hub")]


# --- AC-24 -----------------------------------------------------------------


@pytest.mark.ac("AC-24")
async def test_cli_start_creates_hub_session(hub: Hub) -> None:
    start_route = mock_start(hub.litellm)
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"login_id", "poll_secret", "browser_url", "user_code", "expires_in"}
    assert body["login_id"] != "ll-1"
    assert UUID4_RE.match(body["login_id"]), body["login_id"]
    assert body["poll_secret"] != "ll-secret"
    assert len(body["poll_secret"]) >= 43  # ≥ 32 байт случайности в urlsafe-кодировке
    assert body["browser_url"] == f"{LITELLM_URL}/sso/key/generate?source=litellm-cli&key=ll-1"
    assert body["user_code"] == "ABCD-1234"
    assert body["expires_in"] == 600
    assert start_route.call_count == 1
    audits = await audit_rows(hub.app, "login_started")
    assert len(audits) == 1
    assert audits[0]["details"]["client"] == CLIENT


@pytest.mark.ac("AC-24")
async def test_cli_start_two_sessions_have_distinct_ids_and_secrets(hub: Hub) -> None:
    mock_start(hub.litellm)
    first = (await hub.post("/cli/start", json={"client": CLIENT})).json()
    second = (await hub.post("/cli/start", json={"client": CLIENT})).json()
    assert first["login_id"] != second["login_id"]
    assert first["poll_secret"] != second["poll_secret"]
    assert await _metric_active_sessions(hub) == 2


# --- AC-25 -----------------------------------------------------------------


@pytest.mark.ac("AC-25")
async def test_expires_in_is_min_of_hub_ttl_and_litellm(hub: Hub) -> None:
    mock_start(hub.litellm, start_body(expires_in=120))
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.json()["expires_in"] == 120


@pytest.mark.ac("AC-25")
async def test_expires_in_defaults_to_hub_ttl_when_litellm_omits_it(hub: Hub) -> None:
    mock_start(hub.litellm, start_body(expires_in=None))
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.json()["expires_in"] == 600


@pytest.mark.ac("AC-25")
async def test_expires_in_uses_hub_ttl_when_smaller(make_hub: HubFactory) -> None:
    hub = await make_hub(login_session_ttl=100)
    mock_start(hub.litellm, start_body(expires_in=600))
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.json()["expires_in"] == 100


@pytest.mark.ac("AC-25")
async def test_session_expires_after_min_ttl(hub: Hub) -> None:
    mock_start(hub.litellm, start_body(expires_in=120))
    start = (await hub.post("/cli/start", json={"client": CLIENT})).json()
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    hub.clock.advance(119)
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200
    hub.clock.advance(2)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 404
    assert resp.json()["error"] == "login_expired"
    assert poll_route.call_count == 1


# --- AC-26 -----------------------------------------------------------------


@pytest.mark.ac("AC-26")
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(500),
        httpx.Response(503, json={"error": "down"}),
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json={"poll_secret": "ll-secret"}),
        httpx.Response(200, json={"login_id": "ll-1"}),
        httpx.Response(200, json={"login_id": "", "poll_secret": "ll-secret"}),
        httpx.Response(200, json={"login_id": "ll-1", "poll_secret": ""}),
        httpx.Response(200, json={"login_id": 123, "poll_secret": "ll-secret"}),
        httpx.Response(200, json={"login_id": "ll-1", "poll_secret": 123}),
        httpx.Response(200, json=["ll-1", "ll-secret"]),
        httpx.Response(200, content=b"not json"),
        httpx.Response(302, headers={"Location": "https://sso.test"}),
        httpx.Response(404, json={"detail": "nope"}),
    ],
    ids=[
        "500",
        "503",
        "connect-error",
        "timeout",
        "no-login-id",
        "no-poll-secret",
        "empty-login-id",
        "empty-poll-secret",
        "login-id-not-string",
        "poll-secret-not-string",
        "array-body",
        "not-json",
        "302",
        "404",
    ],
)
async def test_cli_start_litellm_unavailable(hub: Hub, outcome: Any) -> None:
    hub.litellm.post("/sso/cli/start").mock(side_effect=[outcome])
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "litellm_unavailable"
    assert await _metric_active_sessions(hub) == 0
    assert await audit_rows(hub.app, "login_started") == []


# --- AC-27 -----------------------------------------------------------------


@pytest.mark.ac("AC-27")
@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"client": 123}),
        json.dumps({"client": "x" * 129}),
        json.dumps(["client"]),
        json.dumps("client"),
        "{not json",
        json.dumps({"client": CLIENT, "extra": 1}),
    ],
    ids=["client-int", "client-too-long", "array", "string", "not-json", "unknown-field"],
)
async def test_cli_start_invalid_body_rejected(hub: Hub, content: str) -> None:
    start_route = mock_start(hub.litellm)
    resp = await hub.post(
        "/cli/start", content=content, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_request"
    assert body["status"] == "error"
    assert not start_route.called
    assert await _metric_active_sessions(hub) == 0


@pytest.mark.ac("AC-27")
async def test_cli_start_empty_body_and_boundary_client_accepted(hub: Hub) -> None:
    start_route = mock_start(hub.litellm)
    resp = await hub.post("/cli/start")
    assert resp.status_code == 200
    resp = await hub.post("/cli/start", content=b"", headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    resp = await hub.post("/cli/start", json={})
    assert resp.status_code == 200
    resp = await hub.post("/cli/start", json={"client": "x" * 128})
    assert resp.status_code == 200
    assert start_route.call_count == 4


# --- AC-28 -----------------------------------------------------------------


@pytest.mark.ac("AC-28")
async def test_cli_start_rate_limit_sliding_window(hub: Hub) -> None:
    start_route = mock_start(hub.litellm)
    for i in range(30):
        resp = await hub.post("/cli/start", json={"client": CLIENT})
        assert resp.status_code == 200, f"запрос {i + 1}: {resp.text}"
        hub.clock.advance(0.5)  # 30 запросов за 15 с
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert int(resp.headers["Retry-After"]) >= 1
    assert start_route.call_count == 30

    # окно ещё не освободилось: первый запрос был 15 с назад
    hub.clock.advance(44)
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 429
    assert start_route.call_count == 30

    # прошло 61 с с момента первого запроса — окно скользит, запрос принимается
    hub.clock.advance(2)
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 200
    assert start_route.call_count == 31


@pytest.mark.ac("AC-28")
async def test_cli_start_rate_limit_retry_after_reflects_window(hub: Hub) -> None:
    mock_start(hub.litellm)
    for _ in range(30):
        assert (await hub.post("/cli/start")).status_code == 200
    hub.clock.advance(30)
    resp = await hub.post("/cli/start")
    assert resp.status_code == 429
    retry_after = int(resp.headers["Retry-After"])
    assert 1 <= retry_after <= 30
    hub.clock.advance(retry_after + 0.5)
    assert (await hub.post("/cli/start")).status_code == 200


@pytest.mark.ac("AC-28")
async def test_cli_start_rate_limit_is_per_ip(hub: Hub) -> None:
    mock_start(hub.litellm)
    for _ in range(30):
        assert (await hub.post("/cli/start")).status_code == 200
    assert (await hub.post("/cli/start")).status_code == 429
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.app, client=("10.1.2.3", 5555)),
        base_url="http://hub.test",
    ) as other:
        assert (await other.post("/cli/start")).status_code == 200


# --- AC-29 -----------------------------------------------------------------


@pytest.mark.ac("AC-29")
async def test_poll_unknown_login_id_is_404(hub: Hub) -> None:
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    resp = await hub.poll(str(uuid.uuid4()), "whatever")
    assert resp.status_code == 404
    assert resp.json() == {
        "status": "error",
        "error": "login_expired",
        "message": resp.json()["message"],
    }
    assert not poll_route.called


@pytest.mark.ac("AC-29")
async def test_poll_expired_session_is_404(hub: Hub) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    hub.clock.advance(601)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 404
    assert resp.json()["status"] == "error"
    assert resp.json()["error"] == "login_expired"
    assert not poll_route.called
    assert await _metric_active_sessions(hub) == 0


@pytest.mark.ac("AC-29")
async def test_poll_just_before_ttl_still_alive(hub: Hub) -> None:
    start = await _start(hub)
    mock_poll(hub.litellm, {"status": "pending"})
    hub.clock.advance(599)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


# --- AC-30 -----------------------------------------------------------------


@pytest.mark.ac("AC-30")
@pytest.mark.parametrize("secret", [None, "wrong", "ll-secret", ""])
async def test_poll_wrong_or_missing_secret_is_403(hub: Hub, secret: str | None) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    resp = await hub.poll(start["login_id"], secret)
    assert resp.status_code == 403
    assert resp.json() == {"status": "error", "error": "forbidden"}
    assert not poll_route.called


# --- AC-31 -----------------------------------------------------------------


@pytest.mark.ac("AC-31")
async def test_poll_pending_forwards_litellm_secret_header(hub: Hub) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}
    assert poll_route.call_count == 1
    request = poll_route.calls.last.request
    assert request.headers["x-litellm-cli-poll-secret"] == "ll-secret"
    assert request.url.path == "/sso/cli/poll/ll-1"
    assert "team_id" not in dict(request.url.params)


# --- AC-32 -----------------------------------------------------------------


@pytest.mark.ac("AC-32")
async def test_poll_throttled_to_one_upstream_call_per_2s(hub: Hub) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    for _ in range(3):
        resp = await hub.poll(start["login_id"], start["poll_secret"])
        assert resp.status_code == 200
        assert resp.json() == {"status": "pending"}
        hub.clock.advance(0.3)
    assert poll_route.call_count == 1
    hub.clock.advance(2)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json() == {"status": "pending"}
    assert poll_route.call_count == 2


@pytest.mark.ac("AC-32")
async def test_poll_throttle_boundary_exactly_2s(hub: Hub) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    await hub.poll(start["login_id"], start["poll_secret"])
    hub.clock.advance(1.99)
    await hub.poll(start["login_id"], start["poll_secret"])
    assert poll_route.call_count == 1
    hub.clock.advance(0.01)  # ровно 2.0 с
    await hub.poll(start["login_id"], start["poll_secret"])
    assert poll_route.call_count == 2


@pytest.mark.ac("AC-32")
async def test_poll_throttle_returns_cached_response_code_and_body(hub: Hub) -> None:
    """Кэшируется последний ответ клиенту, включая код ошибки 502."""
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    poll_route.mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"status": "pending"})]
    )
    first = await hub.poll(start["login_id"], start["poll_secret"])
    assert first.status_code == 502
    hub.clock.advance(1)
    second = await hub.poll(start["login_id"], start["poll_secret"])
    assert second.status_code == 502
    assert second.json() == first.json()
    assert poll_route.call_count == 1
    hub.clock.advance(1)
    third = await hub.poll(start["login_id"], start["poll_secret"])
    assert third.status_code == 200
    assert poll_route.call_count == 2


# --- AC-33 -----------------------------------------------------------------


@pytest.mark.ac("AC-33")
async def test_multiple_teams_require_selection_and_stop_polling(hub: Hub) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, teams_body(("t1", "A"), ("t2", "B")))
    expected = {
        "status": "team_selection_required",
        "teams": [{"team_id": "t1", "team_alias": "A"}, {"team_id": "t2", "team_alias": "B"}],
    }
    first = await hub.poll(start["login_id"], start["poll_secret"])
    assert first.status_code == 200
    assert first.json() == expected
    hub.clock.advance(3)
    second = await hub.poll(start["login_id"], start["poll_secret"])
    assert second.status_code == 200
    assert second.json() == expected
    assert poll_route.call_count == 1


@pytest.mark.ac("AC-33")
async def test_teams_without_details_use_id_as_alias(hub: Hub) -> None:
    start = await _start(hub)
    mock_poll(hub.litellm, teams_body(("t1", "A"), ("t2", "B"), with_details=False))
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json() == {
        "status": "team_selection_required",
        "teams": [{"team_id": "t1", "team_alias": "t1"}, {"team_id": "t2", "team_alias": "t2"}],
    }


# --- AC-34 -----------------------------------------------------------------


TWO_TEAMS = teams_body(("t1", "A"), ("t2", "B"))


async def _session_in_team_selection(hub: Hub) -> dict[str, Any]:
    """Сессия в состоянии team_selection_required (t1, t2). Маршрут poll с ``team_id`` (если нужен)
    тест регистрирует ДО вызова: respx сопоставляет маршруты в порядке регистрации."""
    start = await _start(hub)
    mock_poll(hub.litellm, TWO_TEAMS)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json()["status"] == "team_selection_required"
    return start


@pytest.mark.ac("AC-34")
async def test_choose_team_from_list_is_forwarded(hub: Hub) -> None:
    jwt = _jwt(hub)
    team_route = mock_poll(
        hub.litellm, ready_body(jwt, team_id="t2", teams=["t1", "t2"]), team_id="t2"
    )
    start = await _session_in_team_selection(hub)
    key_route = mock_key_generate(hub.litellm, "sk-test-1")

    chosen = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t2"})
    assert chosen.status_code == 200
    assert chosen.json() == {"status": "pending"}

    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["team_id"] == "t2"
    assert body["key"] == "sk-test-1"
    assert body["key_kind"] == "persistent"
    assert team_route.call_count == 1
    assert dict(team_route.calls.last.request.url.params)["team_id"] == "t2"
    assert key_route.call_count == 1
    assert json.loads(key_route.calls.last.request.content)["team_id"] == "t2"


# --- AC-35 -----------------------------------------------------------------


@pytest.mark.ac("AC-35")
async def test_choose_team_outside_list_or_invalid_body_rejected(hub: Hub) -> None:
    start = await _session_in_team_selection(hub)
    poll_route = mock_poll(hub.litellm, TWO_TEAMS)  # тот же маршрут (повторная регистрация)
    calls_before = poll_route.call_count
    assert calls_before == 1

    resp = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t9"})
    assert resp.status_code == 400
    assert resp.json() == {
        "status": "error",
        "error": "invalid_team",
        "message": resp.json()["message"],
    }

    resp = await hub.choose_team(start["login_id"], start["poll_secret"], {})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    assert resp.json()["status"] == "error"

    for bad in ({"team_id": 5}, {"team_id": ""}, "not json", []):
        resp = await hub.choose_team(start["login_id"], start["poll_secret"], bad)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_request"

    again = await hub.poll(start["login_id"], start["poll_secret"])
    assert again.json()["status"] == "team_selection_required"
    assert poll_route.call_count == calls_before


# --- AC-36 -----------------------------------------------------------------


@pytest.mark.ac("AC-36")
async def test_choose_team_access_and_state_errors(hub: Hub) -> None:
    start = await _start(hub)
    mock_poll(hub.litellm, {"status": "pending"})
    assert (await hub.poll(start["login_id"], start["poll_secret"])).json() == {"status": "pending"}

    resp = await hub.choose_team(start["login_id"], "wrong", {"team_id": "t1"})
    assert resp.status_code == 403
    assert resp.json() == {"status": "error", "error": "forbidden"}

    resp = await hub.choose_team(start["login_id"], None, {"team_id": "t1"})
    assert resp.status_code == 403

    resp = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t1"})
    assert resp.status_code == 409
    assert resp.json()["status"] == "error"
    assert resp.json()["error"] == "team_selection_not_required"

    resp = await hub.choose_team(str(uuid.uuid4()), start["poll_secret"], {"team_id": "t1"})
    assert resp.status_code == 404
    assert resp.json()["status"] == "error"
    assert resp.json()["error"] == "login_expired"


@pytest.mark.ac("AC-36")
async def test_choose_team_on_fresh_session_is_409(hub: Hub) -> None:
    start = await _start(hub)
    resp = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t1"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "team_selection_not_required"


# --- AC-37 -----------------------------------------------------------------


@pytest.mark.ac("AC-37")
async def test_single_team_selected_automatically(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    team_route = mock_poll(hub.litellm, ready_body(jwt, team_id="t1"), team_id="t1")
    plain_route = mock_poll(hub.litellm, teams_body(("t1", "A")))
    key_route = mock_key_generate(hub.litellm, "sk-test-1")

    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["team_id"] == "t1"
    assert body["key_kind"] == "persistent"
    assert plain_route.call_count == 1
    assert team_route.call_count == 1
    assert dict(team_route.calls.last.request.url.params)["team_id"] == "t1"
    assert json.loads(key_route.calls.last.request.content)["team_id"] == "t1"


@pytest.mark.ac("AC-37")
async def test_single_team_from_teams_list_without_details(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    team_route = mock_poll(hub.litellm, ready_body(jwt, team_id="t1"), team_id="t1")
    mock_poll(hub.litellm, teams_body(("t1", "A"), with_details=False))
    mock_key_generate(hub.litellm, "sk-test-1")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json()["status"] == "ready"
    assert team_route.call_count == 1


# --- AC-38 -----------------------------------------------------------------


@pytest.mark.ac("AC-38")
async def test_empty_team_list_is_invalid_response_but_session_survives(hub: Hub) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, teams_body())
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json() == {
        "status": "error",
        "error": "litellm_invalid_response",
        "message": resp.json()["message"],
    }
    hub.clock.advance(2)
    again = await hub.poll(start["login_id"], start["poll_secret"])
    assert again.status_code != 404
    assert again.status_code == 502
    assert poll_route.call_count == 2
    assert await _metric_active_sessions(hub) == 1


@pytest.mark.ac("AC-38")
@pytest.mark.parametrize(
    "upstream",
    [
        {"nostatus": True},
        {"status": "weird"},
        {"status": "ready"},
        {"status": "ready", "requires_team_selection": True},
        {"status": "ready", "key": 12345, "user_id": "u1"},
        {"status": "ready", "key": "opaque-token", "user_id": 12345},
        {"status": "ready", "key": "opaque-token", "user_id": ""},
        "not json at all",
        [1, 2, 3],
    ],
    ids=[
        "no-status",
        "unknown-status",
        "ready-no-key-no-teams",
        "requires-selection-no-lists",
        "ready-key-not-string",
        "ready-user-id-not-string",
        "ready-user-id-empty",
        "not-json",
        "array",
    ],
)
async def test_unexpected_poll_body_is_invalid_response(hub: Hub, upstream: Any) -> None:
    start = await _start(hub)
    if isinstance(upstream, str):
        hub.litellm.get("/sso/cli/poll/ll-1").respond(200, content=upstream.encode())
    else:
        mock_poll(hub.litellm, upstream)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json()["error"] == "litellm_invalid_response"
    hub.clock.advance(2)
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 502


@pytest.mark.ac("AC-38")
async def test_ready_without_user_id_anywhere_is_invalid_response(hub: Hub) -> None:
    start = await _start(hub)
    jwt = make_jwt({"email": "x@corp.test"})
    mock_poll(hub.litellm, ready_body(jwt, user_id=None, team_id=None))
    key_route = mock_key_generate(hub.litellm, "sk-x")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json()["error"] == "litellm_invalid_response"
    assert not key_route.called


# --- AC-39 -----------------------------------------------------------------


@pytest.mark.ac("AC-39")
async def test_user_without_teams_gets_key_without_team_id(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    mock_poll(hub.litellm, ready_body(jwt, team_id=None, teams=[]))
    key_route = mock_key_generate(hub.litellm, "sk-test-2")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["key"] == "sk-test-2"
    assert body["team_id"] is None
    assert "team_id" in body
    payload = json.loads(key_route.calls.last.request.content)
    assert "team_id" not in payload


# --- AC-40 -----------------------------------------------------------------


@pytest.mark.ac("AC-40")
async def test_ready_creates_persistent_key(hub: Hub) -> None:
    start = await _start(hub, CLIENT)
    jwt = _jwt(hub, sub="u1", email="u1@corp.test")
    poll_route = mock_poll(hub.litellm, ready_body(jwt, user_id="u1", team_id="t1"))
    key_route = mock_key_generate(hub.litellm, "sk-test-3")

    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "status": "ready",
        "key": "sk-test-3",
        "key_kind": "persistent",
        "user": {"user_id": "u1", "email": "u1@corp.test"},
        "team_id": "t1",
    }
    assert "expires_in" not in body
    assert poll_route.call_count == 1

    request = key_route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {jwt}"
    payload = json.loads(request.content)
    stamp = hub.clock.now().strftime("%Y%m%d-%H%M")
    assert payload["key_alias"] == f"opencode-u1-{stamp}"
    assert re.fullmatch(r"opencode-u1-\d{8}-\d{4}", payload["key_alias"])
    assert payload["metadata"] == {"source": "opencode-mcp-hub", "client": CLIENT}
    assert payload["team_id"] == "t1"
    assert set(payload) == {"key_alias", "metadata", "team_id"}


@pytest.mark.ac("AC-40")
async def test_key_alias_prefix_configurable_and_no_client(make_hub: HubFactory) -> None:
    hub = await make_hub(key_alias_prefix="corp")
    start = await _start(hub, client=None)
    jwt = _jwt(hub)
    mock_poll(hub.litellm, ready_body(jwt))
    key_route = mock_key_generate(hub.litellm, "sk-test-3")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    payload = json.loads(key_route.calls.last.request.content)
    assert payload["key_alias"].startswith("corp-u1-")
    assert payload["metadata"] == {"source": "opencode-mcp-hub"}


@pytest.mark.ac("AC-40")
@pytest.mark.parametrize(
    "claims, upstream_user_id, expected_user, expected_email",
    [
        ({"sub": "u1", "email": "u1@corp.test"}, "u1", "u1", "u1@corp.test"),
        ({"user_id": "u7"}, None, "u7", None),
        ({"sub": "u8@corp.test"}, None, "u8@corp.test", "u8@corp.test"),
        ({"sub": "ignored"}, "u9", "u9", None),
        ({"sub": "u10", "email": "e@corp.test"}, "u10@corp.test", "u10@corp.test", "e@corp.test"),
        ({"sub": "u11@corp.test", "email": 123}, None, "u11@corp.test", "u11@corp.test"),
        ({"sub": "u12", "email": ""}, None, "u12", None),
    ],
    ids=[
        "sub+email",
        "user_id-claim",
        "sub-with-at",
        "upstream-user_id-wins",
        "email-claim-wins",
        "email-claim-not-string",
        "email-claim-empty",
    ],
)
async def test_user_id_and_email_derivation(
    hub: Hub,
    claims: dict[str, Any],
    upstream_user_id: str | None,
    expected_user: str,
    expected_email: str | None,
) -> None:
    start = await _start(hub)
    jwt = make_jwt(claims)
    mock_poll(hub.litellm, ready_body(jwt, user_id=upstream_user_id, team_id=None))
    mock_key_generate(hub.litellm, "sk-x")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"] == {"user_id": expected_user, "email": expected_email}


# --- AC-41 -----------------------------------------------------------------


@pytest.mark.ac("AC-41")
@pytest.mark.parametrize("status", [401, 403, 400, 404, 422])
async def test_key_generate_4xx_falls_back_to_jwt(
    hub: Hub, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    start = await _start(hub)
    jwt = _jwt(hub, exp=3600)
    mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, None, status=status)
    caplog.clear()
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["key"] == jwt
    assert body["key_kind"] == "jwt"
    assert abs(body["expires_in"] - 3600) <= 5
    warnings = _hub_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    rows = await fetch_rows(hub.app, "SELECT key_kind, expires_at, key_sha256 FROM api_keys")
    assert len(rows) == 1
    assert rows[0]["key_kind"] == "jwt"
    assert rows[0]["expires_at"] is not None
    assert rows[0]["key_sha256"] == sha256_hex(jwt)


@pytest.mark.ac("AC-41")
async def test_jwt_fallback_without_exp_has_null_expires_in(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub, exp=None)
    mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, None, status=403)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    body = resp.json()
    assert body["key_kind"] == "jwt"
    assert "expires_in" in body
    assert body["expires_in"] is None
    rows = await fetch_rows(hub.app, "SELECT expires_at FROM api_keys")
    assert rows[0]["expires_at"] is None


@pytest.mark.ac("AC-41")
async def test_jwt_fallback_expired_token_gives_zero(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub, exp=-100)
    mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, None, status=401)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json()["expires_in"] == 0


@pytest.mark.ac("AC-41")
async def test_jwt_key_authenticates_api(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, None, status=403)
    await hub.poll(start["login_id"], start["poll_secret"])
    me = await hub.get("/api/me", headers=bearer(jwt))
    assert me.status_code == 200
    assert me.json()["key_kind"] == "jwt"


# --- AC-42 -----------------------------------------------------------------


@pytest.mark.ac("AC-42")
@pytest.mark.parametrize(
    "first_outcome",
    [
        httpx.Response(500),
        httpx.Response(502, json={}),
        httpx.ConnectError("boom"),
        httpx.Response(200, json={"nokey": 1}),
        httpx.Response(200, json={"key": ""}),
        httpx.Response(200, json={"key": 12345}),
        httpx.Response(201, content=b"not json"),
    ],
    ids=["500", "502", "network", "200-without-key", "200-empty-key", "200-key-not-string", "201-not-json"],
)
async def test_key_generate_5xx_returns_502_and_retries_only_key_generate(
    hub: Hub, first_outcome: Any
) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    poll_route = mock_poll(hub.litellm, ready_body(jwt))
    key_route = hub.litellm.post("/key/generate").mock(
        side_effect=[first_outcome, httpx.Response(200, json={"key": "sk-test-4"})]
    )
    first = await hub.poll(start["login_id"], start["poll_secret"])
    assert first.status_code == 502
    assert first.json()["status"] == "error"
    assert first.json()["error"] == "litellm_unavailable"
    assert await _metric_active_sessions(hub) == 1

    hub.clock.advance(2)
    second = await hub.poll(start["login_id"], start["poll_secret"])
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ready"
    assert second.json()["key"] == "sk-test-4"
    assert second.json()["key_kind"] == "persistent"
    assert poll_route.call_count == 1
    assert key_route.call_count == 2


@pytest.mark.ac("AC-42")
async def test_key_generate_retry_is_throttled(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    mock_poll(hub.litellm, ready_body(jwt))
    key_route = hub.litellm.post("/key/generate").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"key": "sk-test-4"})]
    )
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 502
    hub.clock.advance(1)
    cached = await hub.poll(start["login_id"], start["poll_secret"])
    assert cached.status_code == 502
    assert key_route.call_count == 1
    hub.clock.advance(1)
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200
    assert key_route.call_count == 2


# --- AC-43 -----------------------------------------------------------------


@pytest.mark.ac("AC-43")
async def test_login_session_is_single_use(hub: Hub) -> None:
    start = await _start(hub)
    assert await _metric_active_sessions(hub) == 1
    jwt = _jwt(hub)
    poll_route = mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, "sk-test-3")
    ready = await hub.poll(start["login_id"], start["poll_secret"])
    assert ready.json()["status"] == "ready"
    assert await _metric_active_sessions(hub) == 0

    hub.clock.advance(3)
    again = await hub.poll(start["login_id"], start["poll_secret"])
    assert again.status_code == 404
    assert again.json() == {
        "status": "error",
        "error": "login_expired",
        "message": again.json()["message"],
    }
    team = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t1"})
    assert team.status_code == 404
    assert team.json()["error"] == "login_expired"
    assert poll_route.call_count == 1


# --- AC-44 -----------------------------------------------------------------


@pytest.mark.ac("AC-44")
async def test_key_and_user_persisted_hashed_with_audit(hub: Hub) -> None:
    start = await _start(hub, "c1")
    jwt = _jwt(hub, sub="u1", email="u1@corp.test")
    mock_poll(hub.litellm, ready_body(jwt, user_id="u1", team_id="t1"))
    mock_key_generate(hub.litellm, "sk-test-3")
    ready = await hub.poll(start["login_id"], start["poll_secret"])
    assert ready.json()["key"] == "sk-test-3"

    users = await fetch_rows(hub.app, "SELECT user_id, email, groups FROM users")
    assert len(users) == 1
    assert users[0]["user_id"] == "u1"
    assert users[0]["email"] == "u1@corp.test"
    assert (
        json.loads(users[0]["groups"]) == ["all"]
        if isinstance(users[0]["groups"], str)
        else users[0]["groups"] == ["all"]
    )

    keys = await fetch_rows(
        hub.app,
        "SELECT key_sha256, user_id, key_kind, key_alias, client, created_at, expires_at FROM api_keys",
    )
    assert len(keys) == 1
    key = keys[0]
    assert key["key_sha256"] == sha256_hex("sk-test-3")
    assert key["key_sha256"] == key["key_sha256"].lower()
    assert len(key["key_sha256"]) == 64
    assert key["user_id"] == "u1"
    assert key["key_kind"] == "persistent"
    assert re.fullmatch(r"opencode-u1-\d{8}-\d{4}", key["key_alias"])
    assert key["client"] == "c1"
    assert key["created_at"] is not None
    assert key["expires_at"] is None

    dumped = await dump_all_tables(hub.app)
    assert "sk-test-3" not in dumped
    assert jwt not in dumped

    completed = await audit_rows(hub.app, "login_completed")
    assert len(completed) == 1
    assert completed[0]["details"]["key_kind"] == "persistent"
    assert completed[0]["details"]["team_id"] == "t1"
    assert completed[0]["details"]["client"] == "c1"
    assert completed[0]["user_id"] == "u1"


# --- AC-45 -----------------------------------------------------------------


async def _complete_login(
    hub: Hub,
    key: str,
    *,
    ll_id: str,
    client: str = "c1",
    user_id: str = "u1",
    email: str = "u1@corp.test",
) -> str:
    start = await _start(hub, client, ll_id=ll_id)
    jwt = _jwt(hub, sub=user_id, email=email)
    mock_poll(hub.litellm, ready_body(jwt, user_id=user_id, team_id="t1"), login_id=ll_id)
    mock_key_generate(hub.litellm, key)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == key
    return jwt


@pytest.mark.ac("AC-45")
async def test_repeated_login_adds_key_and_keeps_old_valid(hub: Hub) -> None:
    await _complete_login(hub, "sk-a", ll_id="ll-1")
    hub.clock.advance(120)
    await _complete_login(hub, "sk-b", ll_id="ll-2", email="new@corp.test")

    for key in ("sk-a", "sk-b"):
        me = await hub.get("/api/me", headers=bearer(key))
        assert me.status_code == 200, me.text
        assert me.json()["user_id"] == "u1"

    keys = await fetch_rows(hub.app, "SELECT user_id FROM api_keys WHERE user_id = 'u1'")
    assert len(keys) == 2
    users = await fetch_rows(hub.app, "SELECT user_id, email FROM users")
    assert len(users) == 1
    assert users[0]["email"] == "new@corp.test"


# --- AC-46 -----------------------------------------------------------------


@pytest.mark.ac("AC-46")
@pytest.mark.parametrize(
    "outcome",
    [httpx.Response(500), httpx.Response(503), httpx.ConnectError("x"), httpx.ReadTimeout("t")],
    ids=["500", "503", "network", "timeout"],
)
async def test_poll_5xx_or_network_is_502_and_session_survives(hub: Hub, outcome: Any) -> None:
    start = await _start(hub)
    poll_route = hub.litellm.get("/sso/cli/poll/ll-1").mock(
        side_effect=[outcome, httpx.Response(200, json={"status": "pending"})]
    )
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json() == {
        "status": "error",
        "error": "litellm_unavailable",
        "message": resp.json()["message"],
    }
    hub.clock.advance(2)
    again = await hub.poll(start["login_id"], start["poll_secret"])
    assert again.status_code == 200
    assert again.json() == {"status": "pending"}
    assert poll_route.call_count == 2


@pytest.mark.ac("AC-46")
@pytest.mark.parametrize("status", [404, 401, 403, 410, 400, 499])
async def test_poll_4xx_removes_session(hub: Hub, status: int) -> None:
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"detail": "expired"}, status=status)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 404
    assert resp.json() == {
        "status": "error",
        "error": "login_expired",
        "message": resp.json()["message"],
    }
    hub.clock.advance(2)
    again = await hub.poll(start["login_id"], start["poll_secret"])
    assert again.status_code == 404
    assert again.json()["error"] == "login_expired"
    assert poll_route.call_count == 1
    assert await _metric_active_sessions(hub) == 0


@pytest.mark.ac("AC-46")
async def test_poll_errors_two_sessions_side_by_side(hub: Hub) -> None:
    first = await _start(hub, ll_id="ll-1")
    second = await _start(hub, ll_id="ll-2")
    route1 = hub.litellm.get("/sso/cli/poll/ll-1").mock(return_value=httpx.Response(500))
    route2 = hub.litellm.get("/sso/cli/poll/ll-2").mock(return_value=httpx.Response(404))

    r1 = await hub.poll(first["login_id"], first["poll_secret"])
    r2 = await hub.poll(second["login_id"], second["poll_secret"])
    assert (r1.status_code, r1.json()["error"]) == (502, "litellm_unavailable")
    assert (r2.status_code, r2.json()["error"]) == (404, "login_expired")

    hub.clock.advance(2)
    assert (await hub.poll(first["login_id"], first["poll_secret"])).status_code == 502
    assert (await hub.poll(second["login_id"], second["poll_secret"])).status_code == 404
    assert route1.call_count == 2
    assert route2.call_count == 1
    assert await _metric_active_sessions(hub) == 1


# --- AC-47 -----------------------------------------------------------------


@pytest.mark.ac("AC-47")
async def test_cli_responses_do_not_leak_litellm_secret_or_login_id(hub: Hub) -> None:
    mock_start(hub.litellm)
    jwt = _jwt(hub)
    mock_poll(
        hub.litellm, ready_body(jwt, team_id="t2"), team_id="t2"
    )  # специфичный маршрут — первым
    bodies: list[str] = []
    start_resp = await hub.post("/cli/start", json={"client": CLIENT})
    bodies.append(start_resp.text)
    start = start_resp.json()
    assert start["login_id"] != "ll-1"

    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    bodies.append((await hub.poll(start["login_id"], start["poll_secret"])).text)
    hub.clock.advance(2)
    poll_route.mock(return_value=httpx.Response(200, json=teams_body(("t1", "A"), ("t2", "B"))))
    bodies.append((await hub.poll(start["login_id"], start["poll_secret"])).text)
    bodies.append(
        (await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t9"})).text
    )
    bodies.append(
        (await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t2"})).text
    )
    mock_key_generate(hub.litellm, "sk-test-9")
    bodies.append((await hub.poll(start["login_id"], start["poll_secret"])).text)
    bodies.append((await hub.poll(start["login_id"], start["poll_secret"])).text)  # 404 после ready
    bodies.append((await hub.poll("nope", "x")).text)
    bodies.append((await hub.poll(start["login_id"], "wrong")).text)

    assert bodies[-4] and '"ready"' in bodies[-4]
    for body in bodies:
        assert "ll-secret" not in body
    # ревизия 1.1 (R-L9): login_id LiteLLM допустим только как значение key= внутри browser_url
    # ответа /cli/start; вне browser_url (в т.ч. в /cli/poll/*, /cli/*/team) не появляется
    assert start["browser_url"] == f"{LITELLM_URL}/sso/key/generate?source=litellm-cli&key=ll-1"
    start_without_browser_url = start_resp.text.replace(start["browser_url"], "")
    assert "ll-1" not in start_without_browser_url
    for body in bodies[1:]:
        assert "ll-1" not in body


@pytest.mark.ac("AC-47")
async def test_cli_error_and_expired_responses_do_not_leak_litellm_ids(hub: Hub) -> None:
    """Ответы об ошибках (502 litellm_unavailable, 404 login_expired) также не раскрывают
    poll_secret и login_id LiteLLM (R-L9)."""
    mock_start(hub.litellm, start_body(login_id="ll-1", poll_secret="ll-secret"))
    start_resp = await hub.post("/cli/start", json={"client": CLIENT})
    start = start_resp.json()
    mock_poll(hub.litellm, {"error": "boom"}, status=500)
    unavailable = await hub.poll(start["login_id"], start["poll_secret"])
    assert unavailable.status_code == 502
    hub.clock.advance(601)
    expired = await hub.poll(start["login_id"], start["poll_secret"])
    assert expired.status_code == 404
    for body in (unavailable.text, expired.text):
        assert "ll-secret" not in body
        assert "ll-1" not in body


# --- дополнительные граничные случаи (R-L2..R-L4) ----------------------------


@pytest.mark.ac("AC-33")
async def test_teams_as_list_of_objects_without_details(hub: Hub) -> None:
    start = await _start(hub)
    mock_poll(
        hub.litellm,
        {
            "status": "ready",
            "requires_team_selection": True,
            "teams": [{"team_id": "t1", "team_alias": "A"}, {"team_id": "t2"}],
        },
    )
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json() == {
        "status": "team_selection_required",
        "teams": [{"team_id": "t1", "team_alias": "A"}, {"team_id": "t2", "team_alias": "t2"}],
    }


@pytest.mark.ac("AC-37")
async def test_single_team_loop_is_invalid_response(hub: Hub) -> None:
    """LiteLLM после автовыбора снова требует выбор — Hub не зацикливается, а отвечает 502."""
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, teams_body(("t1", "A")))
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json()["error"] == "litellm_invalid_response"
    assert poll_route.call_count == 2
    assert await _metric_active_sessions(hub) == 1


@pytest.mark.ac("AC-40")
async def test_non_jwt_key_uses_upstream_user_id_and_null_email(hub: Hub) -> None:
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body("opaque-token-without-dots", user_id="u42", team_id=None))
    mock_key_generate(hub.litellm, None, status=403)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"] == {"user_id": "u42", "email": None}
    assert body["key_kind"] == "jwt"
    assert body["expires_in"] is None


@pytest.mark.ac("AC-40")
async def test_jwt_with_non_object_payload_is_tolerated(hub: Hub) -> None:
    start = await _start(hub)
    bad_jwt = "aGVhZGVy.WzEsMl0.c2ln"  # payload = [1,2]
    mock_poll(hub.litellm, ready_body(bad_jwt, user_id="u5", team_id=None))
    mock_key_generate(hub.litellm, "sk-5")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"] == {"user_id": "u5", "email": None}


@pytest.mark.ac("AC-30")
async def test_poll_secret_check_precedes_upstream_and_team_state(hub: Hub) -> None:
    start = await _session_in_team_selection(hub)
    resp = await hub.poll(start["login_id"], "wrong")
    assert resp.status_code == 403
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json()["status"] == "team_selection_required"


@pytest.mark.ac("AC-29")
async def test_session_ttl_not_extended_by_client_activity(hub: Hub) -> None:
    """R-L7: сессия истекает по TTL независимо от активности клиента."""
    start = await _start(hub)
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    for _ in range(5):
        hub.clock.advance(119)  # 595 с активности
        assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200
    hub.clock.advance(6)  # 601 с с момента старта, последний poll был 6 с назад
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 404
    assert resp.json()["error"] == "login_expired"
    assert poll_route.call_count == 5


# --- усиление после review-1 / mutation: состояние сессии (spec §6), TTL, аудит, команды -----


def _naive(dt: Any) -> Any:
    return dt.replace(tzinfo=None)


@pytest.mark.ac("AC-24")
async def test_cli_start_session_record_matches_spec_layout(hub: Hub) -> None:
    """Запись ``login:<login_id>`` в KeyValueStore (spec §6): исходное состояние pending, пустые
    команды/ключ, TTL = expires_in; аудит ``login_started`` с меткой времени приложения."""
    mock_start(hub.litellm, start_body(expires_in=120))
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 200
    body = resp.json()
    session = await kv_session(hub.app, body["login_id"])
    assert session is not None
    expected_keys = {
        "poll_secret",
        "litellm_login_id",
        "litellm_poll_secret",
        "client",
        "state",
        "teams",
        "team_id",
        "jwt",
        "user_id",
        "email",
        "last_call_at",
        "last_response",
        "created_at",
        "expires_at",
    }
    assert expected_keys <= set(session), sorted(set(session) ^ expected_keys)
    assert session["poll_secret"] == body["poll_secret"]
    assert session["litellm_login_id"] == "ll-1"
    assert session["litellm_poll_secret"] == "ll-secret"
    assert session["client"] == CLIENT
    assert session["state"] == "pending"
    assert session["teams"] == []
    assert session["team_id"] is None
    assert session["jwt"] is None
    assert session["user_id"] is None
    assert session["email"] is None
    assert session["last_call_at"] is None
    assert session["last_response"] is None
    assert session["created_at"] == hub.clock.time()
    assert session["expires_at"] == hub.clock.time() + 120
    assert body["expires_in"] == 120

    audits = await audit_rows(hub.app, "login_started")
    assert len(audits) == 1
    assert audits[0]["ts"] == _naive(hub.clock.now()) or str(audits[0]["ts"]).startswith(
        _naive(hub.clock.now()).isoformat(sep=" ")
    )
    assert audits[0]["details"]["client"] == CLIENT
    assert audits[0]["user_id"] is None


@pytest.mark.ac("AC-25")
async def test_expires_in_one_second_session_usable_until_then(hub: Hub) -> None:
    """Граница: expires_in=1 от LiteLLM → TTL сессии ровно 1 с (не игнорируется и не округляется)."""
    mock_start(hub.litellm, start_body(expires_in=1))
    start = (await hub.post("/cli/start", json={"client": CLIENT})).json()
    assert start["expires_in"] == 1
    poll_route = mock_poll(hub.litellm, {"status": "pending"})
    hub.clock.advance(0.5)
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200
    hub.clock.advance(0.5)  # ровно 1 с — истекла
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 404
    assert resp.json()["error"] == "login_expired"
    assert poll_route.call_count == 1
    assert await kv_session(hub.app, start["login_id"]) is None


@pytest.mark.ac("AC-25")
@pytest.mark.parametrize("value", [0, -5, "600", True, None, 1.5])
async def test_expires_in_non_positive_or_non_numeric_uses_hub_ttl(hub: Hub, value: Any) -> None:
    """Непригодное expires_in LiteLLM (0, отрицательное, строка, bool, null) → TTL Hub (600);
    дробное положительное усекается до целого."""
    body = start_body(expires_in=None)
    body["expires_in"] = value
    mock_start(hub.litellm, body)
    resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 200, resp.text
    expected = 1 if value == 1.5 else 600
    assert resp.json()["expires_in"] == expected
    session = await kv_session(hub.app, resp.json()["login_id"])
    assert session is not None
    assert session["expires_at"] - session["created_at"] == expected


@pytest.mark.ac("AC-33")
async def test_requires_team_selection_with_key_present_still_requires_choice(hub: Hub) -> None:
    """Флаг requires_team_selection:true главнее наличия key: выбор команды обязателен,
    сессия переходит в состояние team_selection с сохранённым списком (spec §6)."""
    start = await _start(hub)
    body = teams_body(("t1", "A"), ("t2", "B"))
    body["key"] = _jwt(hub)
    poll_route = mock_poll(hub.litellm, body)
    key_route = mock_key_generate(hub.litellm, "sk-never")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "team_selection_required",
        "teams": [{"team_id": "t1", "team_alias": "A"}, {"team_id": "t2", "team_alias": "B"}],
    }
    assert not key_route.called
    session = await kv_session(hub.app, start["login_id"])
    assert session is not None
    assert session["state"] == "team_selection"
    assert session["teams"] == [{"team_id": "t1", "team_alias": "A"}, {"team_id": "t2", "team_alias": "B"}]
    assert session["team_id"] is None
    hub.clock.advance(5)
    assert (await hub.poll(start["login_id"], start["poll_secret"])).json()["status"] == (
        "team_selection_required"
    )
    assert poll_route.call_count == 1


@pytest.mark.ac("AC-33")
@pytest.mark.parametrize(
    "team_details",
    ["oops", 42, None, [], {"team_id": "t1"}],
    ids=["string", "int", "null", "empty-list", "object"],
)
async def test_missing_team_details_fall_back_to_teams_list(hub: Hub, team_details: Any) -> None:
    """R-L3: при отсутствии пригодного списка `team_details` (нет ключа, пустой список, не список)
    команды берутся из `teams`, где `team_alias = team_id`."""
    start = await _start(hub)
    body = teams_body(("t1", "A"), ("t2", "B"), with_details=False)
    body["team_details"] = team_details
    mock_poll(hub.litellm, body)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "team_selection_required",
        "teams": [{"team_id": "t1", "team_alias": "t1"}, {"team_id": "t2", "team_alias": "t2"}],
    }
    session = await kv_session(hub.app, start["login_id"])
    assert session is not None and session["state"] == "team_selection"


@pytest.mark.ac("AC-38")
@pytest.mark.parametrize(
    "team_details",
    [["t1", "t2"], [{"alias": "A"}, {"team_alias": "B"}], [{"team_id": ""}], [None]],
    ids=["strings", "objects-without-team_id", "empty-team_id", "nulls"],
)
async def test_team_details_list_without_usable_entries_is_invalid_response(
    hub: Hub, team_details: Any
) -> None:
    """`team_details` — непустой список, но ни одного элемента с `team_id`: пригодных команд ноль →
    502 litellm_invalid_response (R-L2, как для пустого списка), сессия жива."""
    start = await _start(hub)
    body = teams_body(("t1", "A"), ("t2", "B"), with_details=False)
    body["team_details"] = team_details
    mock_poll(hub.litellm, body)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"] == "litellm_invalid_response"
    assert await kv_session(hub.app, start["login_id"]) is not None


@pytest.mark.ac("AC-34")
async def test_choose_team_resets_poll_cache_and_stores_choice(hub: Hub) -> None:
    """После выбора команды: state=pending, team_id сохранён, кэш poll сброшен (spec §6, R-L3) —
    следующий poll сразу идёт в LiteLLM с ?team_id=, без ожидания дросселя."""
    jwt = _jwt(hub)
    team_route = mock_poll(hub.litellm, {"status": "pending"}, team_id="t2")
    start = await _session_in_team_selection(hub)
    before = await kv_session(hub.app, start["login_id"])
    assert before is not None and before["state"] == "team_selection"
    assert before["last_response"] is not None  # ответ team_selection_required закэширован

    chosen = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t2"})
    assert chosen.status_code == 200
    after = await kv_session(hub.app, start["login_id"])
    assert after is not None
    assert after["state"] == "pending"
    assert after["team_id"] == "t2"
    assert after["teams"] == before["teams"]
    assert after["last_call_at"] is None
    assert after["last_response"] is None
    assert after["expires_at"] == before["expires_at"]  # выбор команды не продлевает сессию

    # без сдвига часов — poll идёт в LiteLLM с team_id=t2 (кэш сброшен)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json() == {"status": "pending"}
    assert team_route.call_count == 1
    assert dict(team_route.calls.last.request.url.params)["team_id"] == "t2"
    # далее ready без team_id в теле → используется выбранная команда
    hub.clock.advance(2)
    team_route.mock(return_value=httpx.Response(200, json=ready_body(jwt, team_id=None, teams=["t1", "t2"])))
    key_route = mock_key_generate(hub.litellm, "sk-test-1")
    ready = await hub.poll(start["login_id"], start["poll_secret"])
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"
    assert ready.json()["team_id"] == "t2"
    assert json.loads(key_route.calls.last.request.content)["team_id"] == "t2"
    completed = await audit_rows(hub.app, "login_completed")
    assert completed[0]["details"]["team_id"] == "t2"


@pytest.mark.ac("AC-37")
async def test_auto_selected_single_team_is_kept_for_following_polls(hub: Hub) -> None:
    """Единственная команда выбрана автоматически; если LiteLLM с ?team_id= отвечает pending,
    выбор сохраняется в сессии и следующие poll идут уже с team_id (без повторного списка)."""
    start = await _start(hub)
    team_route = mock_poll(hub.litellm, {"status": "pending"}, team_id="t1")
    plain_route = mock_poll(hub.litellm, teams_body(("t1", "A")))
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json() == {"status": "pending"}
    assert plain_route.call_count == 1 and team_route.call_count == 1
    session = await kv_session(hub.app, start["login_id"])
    assert session is not None
    assert session["team_id"] == "t1"
    assert session["teams"] == [{"team_id": "t1", "team_alias": "A"}]
    assert session["state"] == "pending"
    hub.clock.advance(2)
    assert (await hub.poll(start["login_id"], start["poll_secret"])).json() == {"status": "pending"}
    assert plain_route.call_count == 1  # список команд больше не запрашивается
    assert team_route.call_count == 2


@pytest.mark.ac("AC-40")
async def test_key_generate_request_is_json_with_bearer_jwt(hub: Hub) -> None:
    start = await _start(hub)
    jwt = _jwt(hub)
    mock_poll(hub.litellm, ready_body(jwt))
    key_route = mock_key_generate(hub.litellm, "sk-test-3")
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200
    request = key_route.calls.last.request
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == f"Bearer {jwt}"
    assert json.loads(request.content)["metadata"]["source"] == "opencode-mcp-hub"


@pytest.mark.ac("AC-41")
async def test_jwt_fallback_stores_expires_at_from_exp_claim_in_utc(hub: Hub) -> None:
    start = await _start(hub)
    exp = int(hub.clock.time()) + 3600
    jwt = make_jwt({"sub": "u1", "email": "u1@corp.test", "exp": exp})
    mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, None, status=403)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.json()["expires_in"] == 3600
    rows = await fetch_rows(hub.app, "SELECT expires_at, created_at FROM api_keys")
    assert len(rows) == 1
    expected = datetime.fromtimestamp(exp, tz=UTC).replace(tzinfo=None)
    assert _as_datetime(rows[0]["expires_at"]) == expected
    assert _as_datetime(rows[0]["created_at"]) == _naive(hub.clock.now())


def _as_datetime(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


@pytest.mark.ac("AC-44")
async def test_persisted_timestamps_come_from_application_clock(hub: Hub) -> None:
    """created_at/updated_at пользователя и ключа и ts аудита — время приложения (часы Hub),
    а не системное; details аудита login_completed — ровно {key_kind, key_alias, team_id, client}."""
    start = await _start(hub, "c1")
    hub.clock.advance(90)
    jwt = _jwt(hub)
    mock_poll(hub.litellm, ready_body(jwt, user_id="u1", team_id="t1"))
    mock_key_generate(hub.litellm, "sk-test-3")
    assert (await hub.poll(start["login_id"], start["poll_secret"])).json()["key"] == "sk-test-3"
    now = _naive(hub.clock.now())

    users = await fetch_rows(hub.app, "SELECT created_at, updated_at FROM users")
    assert _as_datetime(users[0]["created_at"]) == now
    assert _as_datetime(users[0]["updated_at"]) == now
    keys = await fetch_rows(hub.app, "SELECT created_at, key_alias FROM api_keys")
    assert _as_datetime(keys[0]["created_at"]) == now
    assert keys[0]["key_alias"] == f"opencode-u1-{hub.clock.now():%Y%m%d-%H%M}"
    completed = await audit_rows(hub.app, "login_completed")
    assert len(completed) == 1
    assert _as_datetime(completed[0]["ts"]) == now
    assert completed[0]["details"] == {
        "key_kind": "persistent",
        "key_alias": keys[0]["key_alias"],
        "team_id": "t1",
        "client": "c1",
    }
    started = await audit_rows(hub.app, "login_started")
    assert _as_datetime(started[0]["ts"]) == now - timedelta(seconds=90)


@pytest.mark.ac("AC-45")
async def test_repeated_login_keeps_existing_groups_and_updates_timestamp(hub: Hub) -> None:
    """Повторный вход не сбрасывает назначенные группы пользователя; updated_at обновляется,
    created_at сохраняется; пустые группы приводятся к ['all']."""
    created = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
    await insert_user(hub.app, "u1", "old@corp.test", groups=["devs", "ops"], now=created)
    await insert_user(hub.app, "u2", "u2@corp.test", groups=[], now=created)
    await _complete_login(hub, "sk-u1", ll_id="ll-1", user_id="u1", email="new@corp.test")
    await _complete_login(hub, "sk-u2", ll_id="ll-2", user_id="u2", email="u2@corp.test")
    rows = await fetch_rows(
        hub.app, "SELECT user_id, email, groups, created_at, updated_at FROM users ORDER BY user_id"
    )
    by_id = {r["user_id"]: r for r in rows}
    assert json.loads(by_id["u1"]["groups"]) == ["devs", "ops"]
    assert by_id["u1"]["email"] == "new@corp.test"
    assert _as_datetime(by_id["u1"]["created_at"]) == _naive(created)
    assert _as_datetime(by_id["u1"]["updated_at"]) == _naive(hub.clock.now())
    assert json.loads(by_id["u2"]["groups"]) == ["all"]
    # группы действуют при аутентификации: u1 видит серверы audience devs
    assert (await hub.get("/api/me", headers=bearer("sk-u1"))).json()["user_id"] == "u1"


@pytest.mark.ac("AC-26")
async def test_litellm_unavailable_on_start_is_logged_as_json_warning_with_reason(hub: Hub) -> None:
    hub.litellm.post("/sso/cli/start").mock(side_effect=httpx.ConnectError("boom"))
    with capture_json_logs() as logs:
        resp = await hub.post("/cli/start", json={"client": CLIENT}, headers={"X-Request-ID": "req-w"})
    assert resp.status_code == 502
    warnings = [r for r in logs.records() if r["level"] == "WARNING"]
    assert len(warnings) == 1, logs.raw()
    warning = warnings[0]
    assert warning["message"] == "litellm_cli_start_failed"
    assert warning["request_id"] == "req-w"
    assert "ConnectError" in warning["reason"]
    # русский текст причины пишется как есть (UTF-8), а не \\u-последовательностями
    raw = "\n".join(logs.raw())
    assert re.search(r"[а-яА-Я]", raw), raw
    assert "\\u04" not in raw


# --- Усиление после mutation-прогона -----------------------------------------

LOG_BASE_FIELDS = {"ts", "level", "logger", "message", "request_id"}
_UNAVAILABLE_MSG = {"message": "LiteLLM недоступен, повторите попытку позже"}


def _single(records: list[dict[str, Any]], level: str, message: str) -> dict[str, Any]:
    """Единственная запись JSON-лога заданного уровня с заданным событием."""
    found = [r for r in records if r["level"] == level and r["message"] == message]
    assert len(found) == 1, (message, records)
    return found[0]


@pytest.mark.ac("AC-26")
@pytest.mark.parametrize("status", [404, 300], ids=["4xx", "3xx"])
async def test_cli_start_non_2xx_upstream_is_502_even_with_valid_body(
    hub: Hub, status: int
) -> None:
    """Любой ответ вне 2xx на /sso/cli/start — недоступность, даже если тело выглядит корректным."""
    mock_start(hub.litellm, start_body(), status=status)
    with capture_json_logs() as logs:
        resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 502
    assert resp.json() == {"status": "error", "error": "litellm_unavailable", **_UNAVAILABLE_MSG}
    warning = _single(logs.records(), "WARNING", "litellm_cli_start_failed")
    assert warning["reason"] == f"LiteLLM /sso/cli/start ответил {status}"
    assert await _metric_active_sessions(hub) == 0


@pytest.mark.ac("AC-26")
async def test_cli_start_2xx_without_required_fields_reports_invalid_answer(hub: Hub) -> None:
    """200 без login_id/poll_secret — это невалидный ответ LiteLLM, а не успешный старт."""
    mock_start(hub.litellm, {"user_code": "ABCD-1234"})
    with capture_json_logs() as logs:
        resp = await hub.post("/cli/start", json={"client": CLIENT})
    assert resp.status_code == 502
    warning = _single(logs.records(), "WARNING", "litellm_cli_start_failed")
    assert warning["reason"] == "LiteLLM /sso/cli/start вернул невалидный ответ"


@pytest.mark.ac("AC-24")
async def test_login_started_audit_details_carry_client_and_login_id(hub: Hub) -> None:
    """Аудит старта входа связывает запись с сессией: details == {client, login_id} (R-S3)."""
    start = await _start(hub)
    rows = await audit_rows(hub.app, "login_started")
    assert len(rows) == 1
    assert rows[0]["details"] == {"client": CLIENT, "login_id": start["login_id"]}


@pytest.mark.ac("AC-46")
async def test_poll_5xx_warning_record_names_session_and_reason(hub: Hub) -> None:
    """Недоступность LiteLLM при опросе диагностируется одной записью с login_id и причиной."""
    start = await _start(hub)
    mock_poll(hub.litellm, None, status=500)
    with capture_json_logs() as logs:
        resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    warning = _single(logs.records(), "WARNING", "litellm_poll_failed")
    assert set(warning) == LOG_BASE_FIELDS | {"login_id", "reason"}
    assert warning["login_id"] == start["login_id"]
    assert warning["reason"] == "LiteLLM /sso/cli/poll ответил 500"


@pytest.mark.ac("AC-46")
async def test_poll_4xx_is_logged_with_upstream_status(hub: Hub) -> None:
    """Отказ LiteLLM (4xx) закрывает сессию и фиксируется записью с кодом апстрима."""
    start = await _start(hub)
    mock_poll(hub.litellm, None, status=403)
    with capture_json_logs() as logs:
        resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 404
    record = _single(logs.records(), "INFO", "litellm_poll_rejected")
    assert set(record) == LOG_BASE_FIELDS | {"login_id", "upstream_status"}
    assert record["login_id"] == start["login_id"]
    assert record["upstream_status"] == 403


@pytest.mark.ac("AC-42")
async def test_key_generate_5xx_warning_record_names_session_and_reason(hub: Hub) -> None:
    """5xx от /key/generate: 502 клиенту и запись с login_id и причиной."""
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body(_jwt(hub)))
    mock_key_generate(hub.litellm, status=500)
    with capture_json_logs() as logs:
        resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    warning = _single(logs.records(), "WARNING", "litellm_key_generate_failed")
    assert set(warning) == LOG_BASE_FIELDS | {"login_id", "reason"}
    assert warning["login_id"] == start["login_id"]
    assert warning["reason"] == "LiteLLM /key/generate ответил 500"


@pytest.mark.ac("AC-42")
async def test_key_generate_2xx_without_key_is_502_and_session_survives(hub: Hub) -> None:
    """2xx без поля key — ключ не выдан: 502, сессия жива, повтор через 2 с получает ключ."""
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body(_jwt(hub)))
    empty = mock_key_generate(hub.litellm, key=None)
    with capture_json_logs() as logs:
        resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json()["error"] == "litellm_unavailable"
    warning = _single(logs.records(), "WARNING", "litellm_key_generate_no_key")
    assert set(warning) == LOG_BASE_FIELDS | {"login_id"}
    assert warning["login_id"] == start["login_id"]

    empty.mock(return_value=httpx.Response(200, json={"key": "sk-late"}))
    hub.clock.advance(2)
    retry = await hub.poll(start["login_id"], start["poll_secret"])
    assert retry.status_code == 200
    assert retry.json()["key"] == "sk-late"


@pytest.mark.ac("AC-42")
@pytest.mark.parametrize("status", [300, 302], ids=["boundary", "redirect"])
async def test_key_generate_3xx_is_unavailable(hub: Hub, status: int) -> None:
    """Перенаправление вместо ответа /key/generate — недоступность (2xx и 4xx исчерпывают контракт).

    Граница 300 проверяется отдельно: успешным считается только диапазон 2xx.
    """
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body(_jwt(hub)))
    mock_key_generate(hub.litellm, "sk-never", status=status)
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 502
    assert resp.json()["error"] == "litellm_unavailable"
    assert await fetch_rows(hub.app, "SELECT key_sha256 FROM api_keys") == []


@pytest.mark.ac("AC-41")
async def test_jwt_fallback_warning_record_fields(hub: Hub) -> None:
    """Переход на JWT сопровождается одной записью с login_id, кодом апстрима и пользователем."""
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body(_jwt(hub)))
    mock_key_generate(hub.litellm, status=403)
    with capture_json_logs() as logs:
        resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200
    assert resp.json()["key_kind"] == "jwt"
    warning = _single(logs.records(), "WARNING", "key_generate_fallback_jwt")
    assert set(warning) == LOG_BASE_FIELDS | {"login_id", "upstream_status", "user_id"}
    assert warning["login_id"] == start["login_id"]
    assert warning["upstream_status"] == 403
    assert warning["user_id"] == "u1"


@pytest.mark.ac("AC-44")
async def test_login_completed_log_record_fields(hub: Hub) -> None:
    """Завершение входа фиксируется записью {login_id, user_id, key_kind, team_id} без ключа."""
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body(_jwt(hub), team_id="t7"))
    mock_key_generate(hub.litellm, "sk-test-3")
    with capture_json_logs() as logs:
        resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200
    record = _single(logs.records(), "INFO", "login_completed")
    assert set(record) == LOG_BASE_FIELDS | {"login_id", "user_id", "key_kind", "team_id"}
    assert record["login_id"] == start["login_id"]
    assert record["user_id"] == "u1"
    assert record["key_kind"] == "persistent"
    assert record["team_id"] == "t7"
    assert "sk-test-3" not in "\n".join(logs.raw())


@pytest.mark.ac("AC-44")
async def test_new_user_row_gets_default_group_all(hub: Hub) -> None:
    """Созданный входом пользователь получает группу по умолчанию ['all'] (spec §6)."""
    start = await _start(hub)
    mock_poll(hub.litellm, ready_body(_jwt(hub)))
    mock_key_generate(hub.litellm, "sk-groups")
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200
    rows = await fetch_rows(hub.app, "SELECT user_id, groups FROM users")
    assert len(rows) == 1
    assert json.loads(rows[0]["groups"]) == ["all"]


@pytest.mark.ac("AC-44")
async def test_first_login_of_new_user_saves_user_key_and_audit_without_fk_violation(
    hub: Hub,
) -> None:
    """Первый вход ранее не существовавшего пользователя сохраняет users, api_keys и audit_log,
    не нарушая внешний ключ ``api_keys.user_id → users.user_id`` (регрессия BUG-I1-001).

    Сначала проверяется, что окружение действительно проверяет внешние ключи: иначе тест был бы
    зелёным независимо от порядка вставок. Email отсутствует — корпоративный SSO его не отдаёт.
    """
    # 1. Внешние ключи проверяются: ключ без пользователя вставить нельзя.
    with pytest.raises(IntegrityError):
        await insert_key(hub.app, "sk-orphan", "no-such-user")
    assert await fetch_rows(hub.app, "SELECT user_id FROM api_keys") == []

    # 2. Первый вход пользователя, которого в users ещё нет.
    assert await fetch_rows(hub.app, "SELECT user_id FROM users") == []
    team = "Департамент сопровождения ИТ"
    client = "opencode/1.17.9-magnit.1 darwin-arm64"
    start = await _start(hub, client)
    jwt = _jwt(hub, sub="shishenkov_ma", email=None)
    mock_poll(hub.litellm, ready_body(jwt, user_id="shishenkov_ma", team_id=team))
    mock_key_generate(hub.litellm, "sk-first-login")

    ready = await hub.poll(start["login_id"], start["poll_secret"])
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["status"] == "ready"
    assert body["key"] == "sk-first-login"
    assert body["key_kind"] == "persistent"
    assert body["user"] == {"user_id": "shishenkov_ma", "email": None}
    assert body["team_id"] == team

    # 3. Все три записи созданы, ссылка ключа на пользователя разрешима.
    users = await fetch_rows(hub.app, "SELECT user_id, email FROM users")
    assert users == [{"user_id": "shishenkov_ma", "email": None}]
    keys = await fetch_rows(
        hub.app, "SELECT key_sha256, user_id, key_kind, client, expires_at FROM api_keys"
    )
    assert len(keys) == 1
    assert keys[0]["key_sha256"] == sha256_hex("sk-first-login")
    assert keys[0]["user_id"] == "shishenkov_ma"
    assert keys[0]["key_kind"] == "persistent"
    assert keys[0]["client"] == client
    assert keys[0]["expires_at"] is None
    orphans = await fetch_rows(
        hub.app,
        "SELECT k.key_sha256 FROM api_keys k "
        "LEFT JOIN users u ON u.user_id = k.user_id WHERE u.user_id IS NULL",
    )
    assert orphans == []
    completed = await audit_rows(hub.app, "login_completed")
    assert len(completed) == 1
    assert completed[0]["user_id"] == "shishenkov_ma"
    assert completed[0]["details"]["key_kind"] == "persistent"
    assert completed[0]["details"]["team_id"] == team

    # 4. Выданный ключ работает: аутентификация находит пользователя.
    me = await hub.get("/api/me", headers=bearer("sk-first-login"))
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == "shishenkov_ma"


@pytest.mark.ac("AC-44")
async def test_failed_key_insert_rolls_back_new_user_and_audit(hub: Hub) -> None:
    """users, api_keys и audit_log сохраняются одной транзакцией (R-L8): если вставка ключа
    не проходит (LiteLLM вернул уже занятый ключ — api_keys.key_sha256 уникален), то и строка
    нового пользователя, и запись аудита откатываются.
    """
    await insert_user(hub.app, "u1", "u1@corp.test")
    await insert_key(hub.app, "sk-taken", "u1")

    start = await _start(hub, CLIENT)
    mock_poll(hub.litellm, ready_body(_jwt(hub, sub="u2", email=None), user_id="u2"))
    mock_key_generate(hub.litellm, "sk-taken")
    with pytest.raises(IntegrityError):
        await hub.poll(start["login_id"], start["poll_secret"])

    assert [r["user_id"] for r in await fetch_rows(hub.app, "SELECT user_id FROM users")] == ["u1"]
    keys = await fetch_rows(hub.app, "SELECT key_sha256, user_id FROM api_keys")
    assert keys == [{"key_sha256": sha256_hex("sk-taken"), "user_id": "u1"}]
    assert await audit_rows(hub.app, "login_completed") == []


@pytest.mark.ac("AC-33")
async def test_team_entries_without_team_id_are_skipped(hub: Hub) -> None:
    """В списке teams записи без team_id игнорируются, а не ломают разбор ответа."""
    start = await _start(hub)
    ready = mock_poll(hub.litellm, ready_body(_jwt(hub), team_id="t1"), team_id="t1")
    mock_poll(
        hub.litellm,
        {
            "status": "ready",
            "requires_team_selection": True,
            "teams": [{"team_alias": "без id"}, {"team_id": "t1", "team_alias": "A"}],
        },
    )
    mock_key_generate(hub.litellm, "sk-one-team")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    # осталась ровно одна пригодная команда → выбрана автоматически
    assert resp.json()["team_id"] == "t1"
    assert ready.called


@pytest.mark.ac("AC-33")
async def test_session_record_in_team_selection_keeps_declared_fields(hub: Hub) -> None:
    """Ожидание выбора команды меняет только state/teams/team_id — состав записи не расширяется."""
    start = await _start(hub)
    before = await kv_session(hub.app, start["login_id"])
    assert before is not None
    mock_poll(hub.litellm, teams_body(("t1", "A"), ("t2", "B")))
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200

    session = await kv_session(hub.app, start["login_id"])
    assert session is not None
    assert set(session) == set(before)
    assert session["state"] == "team_selection"
    assert session["team_id"] is None
    assert session["teams"] == [
        {"team_id": "t1", "team_alias": "A"},
        {"team_id": "t2", "team_alias": "B"},
    ]


@pytest.mark.ac("AC-24")
async def test_session_record_after_pending_poll_keeps_declared_fields(hub: Hub) -> None:
    """Ответ pending не добавляет в запись сессии новых полей и не меняет состояние."""
    start = await _start(hub)
    before = await kv_session(hub.app, start["login_id"])
    assert before is not None
    mock_poll(hub.litellm, {"status": "pending"})
    assert (await hub.poll(start["login_id"], start["poll_secret"])).json() == {"status": "pending"}

    session = await kv_session(hub.app, start["login_id"])
    assert session is not None
    assert set(session) == set(before)
    assert session["state"] == "pending"
    assert session["last_response"] == {"code": 200, "body": {"status": "pending"}}


@pytest.mark.ac("AC-35")
@pytest.mark.ac("AC-36")
async def test_choose_team_error_messages_are_exact(hub: Hub) -> None:
    """Ошибки выбора команды несут пояснение, а не только код (R-A7)."""
    start = await _start(hub)
    mock_poll(hub.litellm, teams_body(("t1", "A"), ("t2", "B")))
    assert (await hub.poll(start["login_id"], start["poll_secret"])).status_code == 200

    outside = await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t9"})
    assert outside.status_code == 400
    assert outside.json() == {
        "status": "error",
        "error": "invalid_team",
        "message": "Команда не входит в список доступных",
    }

    empty = await hub.choose_team(start["login_id"], start["poll_secret"], {})
    assert empty.status_code == 400
    assert empty.json() == {
        "status": "error",
        "error": "invalid_request",
        "message": "Ожидается JSON-объект {team_id: string}",
    }

    fresh = await _start(hub, ll_id="ll-2")
    conflict = await hub.choose_team(fresh["login_id"], fresh["poll_secret"], {"team_id": "t1"})
    assert conflict.status_code == 409
    assert conflict.json() == {
        "status": "error",
        "error": "team_selection_not_required",
        "message": "Сессия не ожидает выбора команды",
    }


@pytest.mark.ac("AC-27")
async def test_cli_start_body_json_errors_and_blank_body(hub: Hub) -> None:
    """Тело из пробелов равнозначно пустому; нечитаемый JSON — 400 с пояснением."""
    start_route = mock_start(hub.litellm)
    blank = await hub.post(
        "/cli/start", content="   \n", headers={"Content-Type": "application/json"}
    )
    assert blank.status_code == 200, blank.text
    assert start_route.call_count == 1

    broken = await hub.post(
        "/cli/start", content="{not json", headers={"Content-Type": "application/json"}
    )
    assert broken.status_code == 400
    assert broken.json() == {
        "status": "error",
        "error": "invalid_request",
        "message": "Тело запроса должно быть JSON",
    }
    assert start_route.call_count == 1


@pytest.mark.ac("AC-40")
async def test_jwt_claims_read_without_signature_and_without_padding(hub: Hub) -> None:
    """Claims читаются из токена без подписи и без выравнивания base64 (подпись Hub не проверяет)."""
    # длина payload-сегмента ≡ 3 (mod 4): без досыпания '=' base64 не декодируется
    for filler in range(64):
        token = make_jwt({"sub": "u9", "email": "u9@corp.test", "pad": "x" * filler})
        if len(token.split(".")[1]) % 4 == 3:
            break
    else:  # pragma: no cover - гарантированно находится на первых итерациях
        raise AssertionError("не удалось построить payload нужной длины")
    unsigned = ".".join(token.split(".")[:2])  # только header.payload

    start = await _start(hub)
    mock_poll(hub.litellm, {"status": "ready", "key": unsigned, "teams": []})
    mock_key_generate(hub.litellm, "sk-unsigned")
    resp = await hub.poll(start["login_id"], start["poll_secret"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"] == {"user_id": "u9", "email": "u9@corp.test"}
