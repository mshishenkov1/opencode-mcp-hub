"""Вход через LiteLLM CLI-SSO и постоянный ключ (R-L1..R-L10): AC-24..AC-47."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    LITELLM_URL,
    audit_rows,
    bearer,
    dump_all_tables,
    fetch_rows,
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
        httpx.Response(200, content=b"not json"),
    ],
    ids=["500", "503", "connect-error", "timeout", "no-login-id", "no-poll-secret", "not-json"],
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
        "not json at all",
        [1, 2, 3],
    ],
    ids=[
        "no-status",
        "unknown-status",
        "ready-no-key-no-teams",
        "requires-selection-no-lists",
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
    ],
    ids=["sub+email", "user_id-claim", "sub-with-at", "upstream-user_id-wins", "email-claim-wins"],
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
    ],
    ids=["500", "502", "network", "200-without-key"],
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
@pytest.mark.parametrize("status", [404, 401, 403, 410])
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
    # login_id LiteLLM допустим только внутри browser_url ответа /cli/start (R-L1)
    for body in bodies[1:]:
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
