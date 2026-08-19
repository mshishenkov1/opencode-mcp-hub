"""Наблюдаемость и организация тестов (R-N1, R-N2, R-N4): AC-143, AC-144, AC-146."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from hub.broker import NeedsReauth
from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    UPSTREAM_ACCESS,
    UPSTREAM_REFRESH,
    add_key,
    audit_rows,
    authorize_to_code,
    bearer,
    connected_client,
    exchange_code,
    i3_catalog,
    jsonrpc_body,
    litellm_web_login,
    mcp_headers,
    pkce_pair,
    refresh_grant,
    register_client,
    seed_connection,
    web_login,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LOAD_TEST_FILE = REPO_ROOT / "tests" / "test_load_sse.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


# --- AC-143 ----------------------------------------------------------------


@pytest.mark.ac("AC-143")
async def test_revision2_metrics_are_exposed(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    tokens = (await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)).json()
    refreshed = (
        await refresh_grant(hub, refresh_token=tokens["refresh_token"], client_id=client_id)
    ).json()
    headers = mcp_headers(refreshed["access_token"])

    assert (
        await hub.post("/mcp/gitlab", content=jsonrpc_body("initialize"), headers=headers)
    ).status_code == 200
    hub.upstream.push(httpx.Response(500, json={"error": "boom"}))
    failed = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=2),
        headers=headers,
    )
    assert failed.status_code == 502

    ok_connection = await seed_connection(hub, user_id="u2", expires_in=60)
    hub.provider.push(
        httpx.Response(
            200, json={"access_token": "ups-access-9", "expires_in": 7200, "token_type": "Bearer"}
        )
    )
    await hub.app.state.broker.refresh_connection(ok_connection)

    bad_connection = await seed_connection(hub, user_id="u3", expires_in=60)
    hub.provider.push(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(NeedsReauth):
        await hub.app.state.broker.refresh_connection(bad_connection)

    metrics = await hub.get("/metrics")
    assert metrics.status_code == 200
    text = metrics.text
    assert re.search(r'hub_mcp_requests_total\{[^}]*alias="gitlab"', text), text
    assert re.search(r'hub_mcp_request_duration_seconds_bucket\{[^}]*alias="gitlab"', text)
    assert re.search(r'hub_upstream_sessions_active\{alias="gitlab"\} \d', text)
    assert re.search(r'hub_upstream_errors_total\{[^}]*kind="http_5xx"', text)
    assert 'hub_oauth_tokens_issued_total{grant="authorization_code"}' in text
    assert 'hub_oauth_tokens_issued_total{grant="refresh_token"}' in text
    assert re.search(r'hub_token_refresh_total\{[^}]*result="ok"', text)
    assert re.search(r'hub_token_refresh_total\{[^}]*result="failed"', text)

    for secret in (
        tokens["access_token"],
        tokens["refresh_token"],
        refreshed["access_token"],
        UPSTREAM_ACCESS,
        UPSTREAM_REFRESH,
    ):
        assert secret not in text
    assert "user_id" not in text
    assert '"u1"' not in text
    assert "u1" not in re.sub(r"^#.*$", "", text, flags=re.MULTILINE)


# --- AC-144 ----------------------------------------------------------------


@pytest.mark.ac("AC-144")
async def test_audit_records_revision2_actions_without_secrets(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    login = await litellm_web_login(hub)
    assert login.status_code in (302, 303)
    csrf = hub.client.cookies.get("hub_csrf")
    session_cookie = hub.client.cookies.get("hub_session")

    client_id = await register_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    tokens = (await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)).json()
    refreshed = (
        await refresh_grant(hub, refresh_token=tokens["refresh_token"], client_id=client_id)
    ).json()

    changed = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": ["code_review", "devops"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200, changed.text
    assert (
        await hub.post("/oauth/revoke", data={"token": refreshed["refresh_token"]})
    ).status_code == 200
    await add_key(hub, "sk-owner", "u1")
    disconnected = await hub.client.delete(
        "/api/me/connections/gitlab", headers=bearer("sk-owner")
    )
    assert disconnected.status_code == 200, disconnected.text

    rows = await audit_rows(hub.app)
    actions = {row["action"] for row in rows}
    assert {
        "oauth_client_registered",
        "oauth_code_issued",
        "oauth_token_issued",
        "oauth_token_revoked",
        "connection_connected",
        "connection_permissions_changed",
        "connection_disconnected",
        "web_login",
    } <= actions

    serialized = "\n".join(str(row["details"]) for row in rows)
    for secret in (
        tokens["access_token"],
        tokens["refresh_token"],
        refreshed["access_token"],
        refreshed["refresh_token"],
        UPSTREAM_ACCESS,
        UPSTREAM_REFRESH,
        code,
        verifier,
        "gl-secret",
        session_cookie,
        csrf,
    ):
        assert secret and secret not in serialized


# --- AC-146 ----------------------------------------------------------------


@pytest.mark.ac("AC-146")
async def test_outgoing_traffic_is_intercepted(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    for client in (
        hub.app.state.litellm_client,
        hub.app.state.upstream_client,
        hub.app.state.oauth_client,
        hub.app.state.oidc_client,
    ):
        assert isinstance(client._transport, httpx.MockTransport)

    with pytest.raises(AssertionError, match="неизвестному адресу"):
        await hub.app.state.upstream_client.get("https://example.test/anything")
    assert hub.net is not None
    assert hub.net.unmatched == ["GET https://example.test/anything"]


@pytest.mark.ac("AC-146")
def test_load_marker_is_registered_and_excluded(request: pytest.FixtureRequest) -> None:
    assert 'load: ' in PYPROJECT.read_text(encoding="utf-8")
    source = LOAD_TEST_FILE.read_text(encoding="utf-8")
    assert "@pytest.mark.load" in source
    assert "100" in source
    collected = [item.nodeid for item in request.session.items]
    assert collected, "нет собранных тестов"
    assert all(item.get_closest_marker("load") is None for item in request.session.items)
    assert not any("test_load_sse.py" in node for node in collected)


@pytest.mark.ac("AC-146")
async def test_hot_path_uses_only_mocks(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert hub.net is not None
    assert hub.net.unmatched == []
    assert hub.upstream.calls == 1
