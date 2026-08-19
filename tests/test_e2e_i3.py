"""Сквозной сценарий MCP-клиента и восстановление подключения: AC-74, AC-147, AC-149."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    LOOPBACK_REDIRECT,
    OIDC_ISSUER,
    PUBLIC_URL,
    UPSTREAM_ACCESS,
    UPSTREAM_REFRESH,
    audit_rows,
    authorize_params,
    authorize_to_code,
    capture_json_logs,
    connected_client,
    exchange_code,
    fetch_rows,
    i3_catalog,
    jsonrpc_body,
    litellm_web_login,
    mcp_headers,
    pkce_pair,
    provider_callback,
    query_of,
    refresh_grant,
    register_client,
    seed_connection,
    submit_consent,
    web_login,
)

KEYCLOAK_SECRET = "KC-S3CRET"


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


# --- AC-147 ----------------------------------------------------------------


@pytest.mark.ac("AC-147")
async def test_standard_mcp_client_end_to_end(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)

    # 1. Клиент стучится в ресурс без токена и узнаёт адрес метаданных.
    unauthorized = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("initialize"), headers={"Content-Type": "application/json"}
    )
    assert unauthorized.status_code == 401
    header = unauthorized.headers["WWW-Authenticate"]
    prm_url = header.split('resource_metadata="', 1)[1].split('"', 1)[0]
    assert prm_url == f"{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp/gitlab"

    # 2. Метаданные ресурса и authorization server.
    prm = await hub.get(urlsplit(prm_url).path)
    assert prm.status_code == 200
    assert prm.json()["authorization_servers"] == [PUBLIC_URL]
    as_meta = await hub.get("/.well-known/oauth-authorization-server")
    assert as_meta.status_code == 200
    assert as_meta.json()["registration_endpoint"] == f"{PUBLIC_URL}/oauth/register"

    # 3. Динамическая регистрация клиента.
    registration = await hub.post(
        "/oauth/register",
        json={
            "redirect_uris": [LOOPBACK_REDIRECT],
            "client_name": "OpenCode",
            "token_endpoint_auth_method": "none",
        },
    )
    assert registration.status_code == 201
    client_id = registration.json()["client_id"]

    # 4. Authorize без веб-сессии → вход → повтор.
    verifier, challenge = pkce_pair()
    params = authorize_params(client_id, challenge=challenge, state="e2e-state")
    to_login = await hub.get("/oauth/authorize", params=params)
    assert to_login.status_code == 302
    assert to_login.headers["location"].startswith("/auth/login?next=")
    assert quote("/oauth/authorize", safe="") in to_login.headers["location"]
    logged_in = await litellm_web_login(hub)
    assert logged_in.status_code in (302, 303)

    started = await hub.get("/oauth/authorize", params=params)
    assert started.status_code == 302

    # 5. OAuth целевой системы и экран прав.
    consent_page = await provider_callback(hub, started.headers["location"])
    assert consent_page.status_code == 200
    assert "Разрешить доступ" in consent_page.text
    granted = await submit_consent(hub, consent_page.text, groups=["code_review"])
    assert granted.status_code == 302
    redirect = query_of(granted.headers["location"])
    assert redirect["state"] == "e2e-state"
    code = redirect["code"]

    # 6. Обмен кода на пару токенов.
    issued = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert issued.status_code == 200, issued.text
    tokens = issued.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == "gitlab:readonly"

    # 7. Работа через proxy.
    headers = mcp_headers(tokens["access_token"])
    init = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("initialize"), headers=headers
    )
    assert init.status_code == 200, init.text
    session_id = init.headers["Mcp-Session-Id"]
    session_headers = mcp_headers(tokens["access_token"], session_id=session_id)
    listed = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=2), headers=session_headers
    )
    assert listed.status_code == 200
    assert [t["name"] for t in listed.json()["result"]["tools"]] == ["list_mrs"]
    called = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=3),
        headers=session_headers,
    )
    assert called.status_code == 200
    assert "result" in called.json()
    sent = hub.upstream.last()
    assert sent.header("authorization") == f"Bearer {UPSTREAM_ACCESS}"
    assert sent.header("enabled-groups") == "core,code_review"
    assert sent.header("x-static") == "st-1"

    # 8. Обновление пары и отзыв.
    rotated = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=client_id
    )
    assert rotated.status_code == 200, rotated.text
    new_tokens = rotated.json()
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    revoked = await hub.post("/oauth/revoke", data={"token": new_tokens["refresh_token"]})
    assert revoked.status_code == 200
    after = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=4),
        headers=mcp_headers(new_tokens["access_token"]),
    )
    assert after.status_code == 401
    assert after.json()["error"] == "unauthorized"


# --- AC-149 ----------------------------------------------------------------


@pytest.mark.ac("AC-149")
async def test_reauth_restores_connection_without_new_registration(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    conn, tokens = await connected_needs_reauth(hub, client_id)
    clients_before = await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients")

    _verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    assert code

    clients_after = await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients")
    assert clients_before == clients_after

    rows = await fetch_rows(hub.app, "SELECT id, status FROM connections")
    assert rows == [{"id": conn.id, "status": "connected"}]

    proxied = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert proxied.status_code == 200, proxied.text

    rotated = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=tokens["client_id"]
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]


async def connected_needs_reauth(hub: Hub, client_id: str) -> tuple[Any, dict[str, Any]]:
    conn, tokens = await connected_client(hub, client_id=client_id)
    await hub.app.state.broker.mark_needs_reauth(conn, "нужна повторная авторизация")
    tokens["client_id"] = client_id
    return conn, tokens


# --- AC-74 -----------------------------------------------------------------


@pytest.mark.ac("AC-74")
async def test_revision2_secrets_do_not_leak(make_hub: HubFactory) -> None:
    hub = await _hub(
        make_hub,
        web_auth="keycloak",
        keycloak_issuer=OIDC_ISSUER,
        keycloak_client_secret=KEYCLOAK_SECRET,
    )
    with capture_json_logs() as logs:
        login = await hub.get("/auth/login", params={"next": "/ui/connections"})
        oidc_params = query_of(login.headers["location"])
        hub.oidc.next_id_token = hub.oidc.make_id_token(
            nonce=oidc_params["nonce"], now=hub.clock.time()
        )
        callback = await hub.get(
            "/auth/callback", params={"code": "kc-code", "state": oidc_params["state"]}
        )
        assert callback.status_code in (302, 303), callback.text
        session_cookie = hub.client.cookies.get("hub_session")
        csrf_token = hub.client.cookies.get("hub_csrf")

        client_id = await register_client(hub)
        verifier, challenge = pkce_pair()
        started = await hub.get(
            "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
        )
        consent_page = await provider_callback(hub, started.headers["location"])
        granted = await submit_consent(hub, consent_page.text)
        code = query_of(granted.headers["location"])["code"]
        tokens = (
            await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
        ).json()
        proxied = await hub.post(
            "/mcp/gitlab",
            content=jsonrpc_body("tools/call", {"name": "list_mrs"}),
            headers=mcp_headers(tokens["access_token"]),
        )
        assert proxied.status_code == 200, proxied.text

    metrics = await hub.get("/metrics")
    connections_page = await hub.get("/ui/connections")
    server_page = await hub.get("/ui/servers/gitlab")
    assert metrics.status_code == connections_page.status_code == server_page.status_code == 200

    secrets = [
        KEYCLOAK_SECRET,
        UPSTREAM_ACCESS,
        UPSTREAM_REFRESH,
        tokens["refresh_token"],
        code,
        verifier,
        session_cookie,
        csrf_token,
    ]
    haystacks = {
        "логи": "\n".join(str(record) for record in logs.records()),
        "аудит": "\n".join(str(row["details"]) for row in await audit_rows(hub.app)),
        "метрики": metrics.text,
        "/ui/connections": connections_page.text,
        "/ui/servers/gitlab": server_page.text,
    }
    for secret in secrets:
        assert secret, "секрет для проверки не должен быть пустым"
        for where, text in haystacks.items():
            assert secret not in text, f"{where} содержит секрет {secret[:6]}…"


@pytest.mark.ac("AC-74")
async def test_needs_reauth_page_has_no_tokens(make_hub: HubFactory) -> None:
    """Граница: страница сервера в состоянии needs_reauth тоже не показывает токены."""
    hub = await _hub(make_hub)
    await web_login(hub)
    conn = await seed_connection(hub, status="needs_reauth")
    await hub.app.state.broker.mark_needs_reauth(conn, "провайдер отклонил обновление")
    page = await hub.get("/ui/servers/gitlab")
    assert page.status_code == 200
    assert "Нужна повторная авторизация" in page.text
    for secret in (UPSTREAM_ACCESS, UPSTREAM_REFRESH, "gl-secret", "mcp-gitlab.internal.test"):
        assert secret not in page.text
