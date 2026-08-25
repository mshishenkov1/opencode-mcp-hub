"""Брокер токенов целевых систем (R-B1..R-B9): AC-102..AC-113."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    GL_SECRET,
    UPSTREAM_ACCESS,
    UPSTREAM_REFRESH,
    add_key,
    audit_rows,
    authorize_params,
    authorize_to_code,
    bearer,
    capture_json_logs,
    catalog_doc,
    connected_client,
    fetch_rows,
    gitlab_facade,
    html_error_code,
    i3_catalog,
    jira_facade,
    jsonrpc_body,
    mcp_headers,
    native_server,
    parse_db_datetime,
    pkce_pair,
    provider_callback,
    query_of,
    refresh_grant,
    register_client,
    seed_connection,
    submit_consent,
    web_login,
    web_logout,
)

CODE_CONNECTION = -32002


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


async def _upstream_token_row(hub: Hub, connection_id: int) -> dict[str, Any]:
    rows = await fetch_rows(
        hub.app,
        "SELECT access_token_enc, refresh_token_enc, expires_at, scopes "
        "FROM upstream_tokens WHERE connection_id = :cid",
        cid=connection_id,
    )
    assert rows, "нет строки upstream_tokens"
    return rows[0]


# --- AC-102 ----------------------------------------------------------------


@pytest.mark.ac("AC-102")
async def test_provider_scopes_follow_preset_and_pkce(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    responses: list[str] = []
    with capture_json_logs() as logs:
        await web_login(hub, "u1")
        client_id = await register_client(hub)
        _verifier, challenge = pkce_pair()
        readonly = await hub.get(
            "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
        )
        location = readonly.headers["location"]
        params = query_of(location)
        assert params["scope"] == "read_api read_user read_repository"
        assert params["code_challenge_method"] == "S256"
        consent = await provider_callback(hub, location)
        responses.append(consent.text)
        responses.append((await submit_consent(hub, consent.text)).text)

        web_logout(hub)
        await web_login(hub, "u2")
        readwrite = await hub.get(
            "/oauth/authorize",
            params=authorize_params(client_id, challenge=challenge, scope="gitlab:readwrite"),
        )
        rw_params = query_of(readwrite.headers["location"])
        assert rw_params["scope"] == "api read_user"
        responses.append((await provider_callback(hub, readwrite.headers["location"])).text)

    assert all(r.form["client_secret"] == GL_SECRET for r in hub.provider.token_requests)
    assert len(hub.provider.token_requests) == 2
    for text in responses:
        assert GL_SECRET not in text
    for record in logs.records():
        assert GL_SECRET not in str(record)


# --- AC-103 ----------------------------------------------------------------


async def _start_provider_flow(hub: Hub) -> tuple[str, str]:
    """Начать подключение и вернуть ``(location AS, state)``."""
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    response = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    return location, query_of(location)["state"]


@pytest.mark.ac("AC-103")
async def test_callback_without_state_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    await _start_provider_flow(hub)
    response = await hub.get("/oauth/callback/gitlab", params={"code": "c"})
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_state"
    assert hub.provider.token_requests == []


@pytest.mark.ac("AC-103")
async def test_callback_with_foreign_state_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    await _start_provider_flow(hub)
    response = await hub.get("/oauth/callback/gitlab", params={"code": "c", "state": "other"})
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_state"
    assert hub.provider.token_requests == []


@pytest.mark.ac("AC-103")
async def test_callback_state_is_single_use(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    _location, state = await _start_provider_flow(hub)
    first = await hub.get("/oauth/callback/gitlab", params={"code": "c", "state": state})
    assert first.status_code == 200
    second = await hub.get("/oauth/callback/gitlab", params={"code": "c", "state": state})
    assert second.status_code == 400
    assert html_error_code(second.text) == "invalid_state"


@pytest.mark.ac("AC-103")
async def test_callback_from_other_web_session_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub, "u1")
    _location, state = await _start_provider_flow(hub)
    web_logout(hub)
    await web_login(hub, "u2")
    response = await hub.get("/oauth/callback/gitlab", params={"code": "c", "state": state})
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_state"
    assert hub.provider.token_requests == []


@pytest.mark.ac("AC-103")
async def test_callback_with_provider_error_returns_access_denied(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    _location, state = await _start_provider_flow(hub)
    response = await hub.get(
        "/oauth/callback/gitlab", params={"error": "access_denied", "state": state}
    )
    assert response.status_code == 302
    assert query_of(response.headers["location"])["error"] == "access_denied"
    assert hub.provider.token_requests == []
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == []


# --- AC-104 ----------------------------------------------------------------


@pytest.mark.ac("AC-104")
async def test_provider_tokens_stored_encrypted(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    await authorize_to_code(hub, client_id, challenge=challenge)

    connections = await fetch_rows(hub.app, "SELECT id, status FROM connections")
    assert connections[0]["status"] == "connected"
    row = await _upstream_token_row(hub, connections[0]["id"])
    assert UPSTREAM_ACCESS not in row["access_token_enc"]
    assert UPSTREAM_REFRESH not in str(row["refresh_token_enc"])
    cipher = hub.app.state.cipher
    assert cipher.decrypt(row["access_token_enc"]) == UPSTREAM_ACCESS
    assert cipher.decrypt(row["refresh_token_enc"]) == UPSTREAM_REFRESH

    expires_at = parse_db_datetime(row["expires_at"])
    expected = hub.clock.now().timestamp() + 7200
    assert abs(expires_at.timestamp() - expected) < 5

    audit = await audit_rows(hub.app, "connection_connected")
    assert audit
    serialized = str(audit[0]["details"])
    assert UPSTREAM_ACCESS not in serialized
    assert UPSTREAM_REFRESH not in serialized


# --- AC-105 ----------------------------------------------------------------


@pytest.mark.ac("AC-105")
async def test_background_refresh_runs_ahead_and_locks(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, token_refresh_lead=300)
    conn = await seed_connection(hub, expires_in=200)
    hub.provider.push(
        httpx.Response(
            200,
            json={
                "access_token": "ups-access-2",
                "refresh_token": "ups-refresh-2",
                "expires_in": 7200,
                "token_type": "Bearer",
            },
        )
    )
    broker = hub.app.state.broker
    results = await asyncio.gather(
        broker.refresh_connection(conn), broker.refresh_connection(conn)
    )
    assert len(hub.provider.token_requests) == 1
    assert hub.provider.token_requests[0].form["grant_type"] == "refresh_token"
    assert results[0] == results[1] == "ups-access-2"

    row = await _upstream_token_row(hub, conn.id)
    assert hub.app.state.cipher.decrypt(row["access_token_enc"]) == "ups-access-2"
    assert hub.app.state.cipher.decrypt(row["refresh_token_enc"]) == "ups-refresh-2"
    refreshed_at = (
        await fetch_rows(hub.app, "SELECT last_refresh_at FROM connections WHERE id = :cid", cid=conn.id)
    )[0]["last_refresh_at"]
    assert refreshed_at is not None


@pytest.mark.ac("AC-105")
async def test_background_refresh_skips_fresh_token(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, token_refresh_lead=300)
    await seed_connection(hub, expires_in=3600)
    assert await hub.app.state.token_refresher.run_once() == 0
    assert hub.provider.token_requests == []


@pytest.mark.ac("AC-105")
async def test_background_refresh_picks_due_connection(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, token_refresh_lead=300)
    await seed_connection(hub, expires_in=200)
    assert await hub.app.state.token_refresher.run_once() == 1
    assert len(hub.provider.token_requests) == 1


# --- AC-106 ----------------------------------------------------------------


@pytest.mark.ac("AC-106")
async def test_upstream_401_triggers_refresh_and_single_retry(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    hub.upstream.push(httpx.Response(401, json={"error": "unauthorized"}))
    hub.provider.push(
        httpx.Response(
            200,
            json={"access_token": "ups-access-2", "expires_in": 7200, "token_type": "Bearer"},
        )
    )
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert "result" in response.json()
    assert hub.upstream.calls == 2
    assert hub.upstream.requests[0].header("authorization") == f"Bearer {UPSTREAM_ACCESS}"
    assert hub.upstream.requests[1].header("authorization") == "Bearer ups-access-2"


@pytest.mark.ac("AC-106")
async def test_second_upstream_401_gives_connection_error(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    hub.upstream.push_many(httpx.Response(401, json={"error": "unauthorized"}), 2)
    hub.provider.push(
        httpx.Response(
            200,
            json={"access_token": "ups-access-2", "expires_in": 7200, "token_type": "Bearer"},
        )
    )
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=7),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"]["code"] == CODE_CONNECTION
    assert body["id"] == 7
    assert hub.upstream.calls == 2


# --- AC-107 ----------------------------------------------------------------


@pytest.mark.ac("AC-107")
async def test_refresh_failure_marks_needs_reauth(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    conn, tokens = await connected_client(hub, expires_in=60)
    await add_key(hub, "sk-owner", "u1")
    hub.provider.push(httpx.Response(400, json={"error": "invalid_grant"}))
    hub.clock.advance(120)

    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=3),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["code"] == CODE_CONNECTION
    assert error["message"]
    assert error["data"]["reason"] == "needs_reauth"
    assert error["data"]["hint_url"] == "https://hub.test/ui/servers/gitlab"

    rows = await fetch_rows(
        hub.app, "SELECT status, needs_reauth_reason FROM connections WHERE id = :cid", cid=conn.id
    )
    assert rows[0]["status"] == "needs_reauth"
    assert rows[0]["needs_reauth_reason"]

    connections = await hub.get("/api/me/connections", headers=bearer("sk-owner"))
    assert connections.status_code == 200
    assert connections.json()[0]["status"] == "needs_reauth"

    refreshed = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=tokens["client_id"]
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]

    assert await audit_rows(hub.app, "connection_needs_reauth")


# --- AC-108 ----------------------------------------------------------------


@pytest.mark.ac("AC-108")
async def test_expired_token_without_refresh_needs_reauth(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    conn, tokens = await connected_client(hub, refresh_token=None, expires_in=60)
    hub.clock.advance(120)
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=5),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["code"] == CODE_CONNECTION
    assert error["data"]["reason"] == "needs_reauth"
    assert hub.upstream.calls == 0
    assert hub.provider.token_requests == []
    rows = await fetch_rows(
        hub.app, "SELECT status FROM connections WHERE id = :cid", cid=conn.id
    )
    assert rows[0]["status"] == "needs_reauth"


# --- AC-109 ----------------------------------------------------------------


@pytest.mark.ac("AC-109")
async def test_provider_refresh_rotation_is_optional(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    rotating, rotating_tokens = await connected_client(hub, user_id="u1", expires_in=60)
    keeping, keeping_tokens = await connected_client(hub, user_id="u2", expires_in=60)

    hub.provider.push(
        httpx.Response(
            200,
            json={
                "access_token": "ups-access-2",
                "refresh_token": "ups-refresh-2",
                "expires_in": 7200,
                "token_type": "Bearer",
            },
        )
    )
    hub.provider.push(
        httpx.Response(
            200, json={"access_token": "ups-access-3", "expires_in": 7200, "token_type": "Bearer"}
        )
    )
    hub.clock.advance(120)

    call = jsonrpc_body("tools/call", {"name": "list_mrs"})
    first = await hub.post(
        "/mcp/gitlab", content=call, headers=mcp_headers(rotating_tokens["access_token"])
    )
    assert first.status_code == 200, first.text
    second = await hub.post(
        "/mcp/gitlab", content=call, headers=mcp_headers(keeping_tokens["access_token"])
    )
    assert second.status_code == 200, second.text

    cipher = hub.app.state.cipher
    rotated = await _upstream_token_row(hub, rotating.id)
    kept = await _upstream_token_row(hub, keeping.id)
    assert cipher.decrypt(rotated["refresh_token_enc"]) == "ups-refresh-2"
    assert cipher.decrypt(kept["refresh_token_enc"]) == UPSTREAM_REFRESH

    statuses = await fetch_rows(hub.app, "SELECT status FROM connections ORDER BY id")
    assert [row["status"] for row in statuses] == ["connected", "connected"]
    assert hub.upstream.requests[0].header("authorization") == "Bearer ups-access-2"
    assert hub.upstream.requests[1].header("authorization") == "Bearer ups-access-3"


# --- AC-110 ----------------------------------------------------------------


@pytest.mark.ac("AC-110")
async def test_permission_change_applies_to_next_call(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    csrf = await web_login(hub)
    _conn, tokens = await connected_client(hub, groups=("code_review",))
    headers = mcp_headers(tokens["access_token"])
    first = await hub.post("/mcp/gitlab", content=jsonrpc_body("tools/list"), headers=headers)
    assert first.status_code == 200
    assert hub.upstream.calls == 1

    updated = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": ["code_review", "devops"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["alias"] == "gitlab"
    assert body["preset"] == "readonly"
    assert body["groups"] == ["code_review", "devops"]
    assert body["status"] == "connected"

    second = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=2), headers=headers
    )
    assert second.status_code == 200, second.text
    assert hub.upstream.calls == 2
    assert hub.upstream.last().header("Enabled-Groups") == "core,code_review,devops"
    assert hub.provider.token_requests == []
    assert await audit_rows(hub.app, "connection_permissions_changed")


# --- AC-111 ----------------------------------------------------------------


@pytest.mark.ac("AC-111")
async def test_upgrade_to_readwrite_requires_reauth(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    csrf = await web_login(hub)
    await connected_client(hub)
    response = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readwrite", "groups": ["repo_write"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_reauth"
    assert body["message"]
    assert any("Ѐ" <= ch <= "ӿ" for ch in body["message"])


@pytest.mark.ac("AC-111")
@pytest.mark.parametrize("groups", [["nope"], ["admin"]])
async def test_unknown_and_denied_groups_rejected(
    make_hub: HubFactory, groups: list[str]
) -> None:
    hub = await _hub(make_hub)
    csrf = await web_login(hub)
    await connected_client(hub)
    response = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": groups},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_request"


@pytest.mark.ac("AC-111")
async def test_readonly_preset_drops_readwrite_group(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    csrf = await web_login(hub)
    _conn, tokens = await connected_client(hub)
    response = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": ["repo_write"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["groups"] == []

    call = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert call.status_code == 200, call.text
    header = hub.upstream.last().header("Enabled-Groups")
    assert header == "core"
    assert "repo_write" not in (header or "")


# --- AC-112 ----------------------------------------------------------------


@pytest.mark.ac("AC-112")
async def test_disconnect_revokes_provider_and_client_tokens(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    conn, tokens = await connected_client(hub)
    await seed_connection(hub, alias="jira", groups=("issues",))
    await add_key(hub, "sk-owner", "u1")

    response = await hub.client.delete(
        "/api/me/connections/gitlab", headers=bearer("sk-owner")
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"alias": "gitlab", "status": "not_connected"}
    assert hub.provider.revoke_requests
    assert hub.provider.revoke_requests[0].form["token"] == UPSTREAM_ACCESS
    assert await fetch_rows(
        hub.app, "SELECT id FROM upstream_tokens WHERE connection_id = :cid", cid=conn.id
    ) == []

    proxied = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert proxied.status_code == 401

    refreshed = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=tokens["client_id"]
    )
    assert refreshed.status_code == 400
    assert refreshed.json()["error"] == "invalid_grant"

    remote = await hub.get("/remote-config", headers=bearer("sk-owner"))
    assert "gitlab" not in remote.json()["config"]["mcp"]
    assert "jira" in remote.json()["config"]["mcp"]

    catalog = await hub.get("/api/catalog", headers=bearer("sk-owner"))
    by_alias = {s["alias"]: s for s in catalog.json()["servers"]}
    assert by_alias["gitlab"]["connection"]["status"] == "not_connected"

    assert await audit_rows(hub.app, "connection_disconnected")


# --- AC-113 ----------------------------------------------------------------


@pytest.mark.ac("AC-113")
async def test_provider_tokens_never_leak_outside(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    conn, _tokens = await connected_client(hub)
    await add_key(hub, "sk-owner", "u1")
    row = await _upstream_token_row(hub, conn.id)

    responses = [
        await hub.get("/api/catalog", headers=bearer("sk-owner")),
        await hub.get("/api/me/connections", headers=bearer("sk-owner")),
        await hub.get("/ui/connections"),
        await hub.get("/ui/servers/gitlab"),
        await hub.get("/metrics"),
        await hub.get("/remote-config", headers=bearer("sk-owner")),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        for secret in (UPSTREAM_ACCESS, UPSTREAM_REFRESH, row["access_token_enc"]):
            assert secret not in response.text

    connections = (await hub.get("/api/me/connections", headers=bearer("sk-owner"))).json()
    assert set(connections[0]) == {
        "alias",
        "status",
        "preset",
        "groups",
        "created_at",
        "updated_at",
        # §28 ревизии 4: три новых ключа; значений токенов среди них нет (R-U16, R-U17.3).
        "token_origin",
        "token_origin_reason",
        "session_expires_at",
    }


# --- дополнительные ветки брокера -----------------------------------------


@pytest.mark.ac("AC-105")
async def test_background_refresh_survives_provider_failure(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, token_refresh_lead=300)
    conn = await seed_connection(hub, expires_in=200)
    hub.provider.push(httpx.Response(400, json={"error": "invalid_grant"}))
    assert await hub.app.state.token_refresher.run_once() == 0
    rows = await fetch_rows(
        hub.app, "SELECT status FROM connections WHERE id = :cid", cid=conn.id
    )
    assert rows[0]["status"] == "needs_reauth"


@pytest.mark.ac("AC-105")
async def test_refresher_start_and_stop_are_idempotent(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    refresher = hub.app.state.token_refresher
    await refresher.start()
    await refresher.start()
    await refresher.stop()
    await refresher.stop()


@pytest.mark.ac("AC-112")
async def test_disconnect_without_revoke_url_still_works(make_hub: HubFactory) -> None:
    jira = jira_facade()
    jira["auth"] = {k: v for k, v in jira["auth"].items() if k != "revoke_url"}
    hub = await make_hub(
        catalog=catalog_doc([gitlab_facade(), jira, native_server("tag")]),
        env=CATALOG_ENV,
        base_url="https://hub.test",
    )
    await seed_connection(hub, alias="jira", groups=("issues",))
    await add_key(hub, "sk-owner", "u1")
    response = await hub.client.delete("/api/me/connections/jira", headers=bearer("sk-owner"))
    assert response.status_code == 200, response.text
    assert hub.net is not None
    assert hub.net.providers["jira"].revoke_requests == []
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == []


@pytest.mark.ac("AC-112")
async def test_disconnect_of_missing_connection_is_404(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await add_key(hub, "sk-owner", "u1")
    response = await hub.client.delete("/api/me/connections/gitlab", headers=bearer("sk-owner"))
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.ac("AC-109")
async def test_token_without_expires_in_stays_valid(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, expires_in=None)
    hub.clock.advance(3000)
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert hub.provider.token_requests == []


@pytest.mark.ac("AC-104")
async def test_provider_response_without_access_token_fails(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    _location, state = await _start_provider_flow(hub)
    hub.provider.push(httpx.Response(200, json={"token_type": "Bearer"}))
    response = await hub.get("/oauth/callback/gitlab", params={"code": "c", "state": state})
    assert response.status_code == 502
    assert html_error_code(response.text) == "upstream_auth_failed"
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == []


@pytest.mark.ac("AC-104")
async def test_provider_network_error_fails_exchange(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    _location, state = await _start_provider_flow(hub)
    hub.provider.push(httpx.ConnectError("сеть недоступна"))
    response = await hub.get("/oauth/callback/gitlab", params={"code": "c", "state": state})
    assert response.status_code == 502
    assert html_error_code(response.text) == "upstream_auth_failed"
