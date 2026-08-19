"""Hub как authorization server (R-O1..R-O13): AC-75..AC-101, AC-148, AC-153, AC-155."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import pytest

from hub.crypto import jwt_decode, jwt_encode, sha256_hex
from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    LOOPBACK_REDIRECT,
    PUBLIC_URL,
    audit_rows,
    authorize_params,
    authorize_to_code,
    bearer,
    connected_client,
    exchange_code,
    fetch_rows,
    html_error_code,
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
    signature_bytes,
    submit_consent,
    tamper_signature,
    web_login,
    web_logout,
)

# Клиент B отличается от A путём: расхождение только по порту loopback разрешено R-O4.1
# и проверяется в AC-83 (test_loopback_redirect_with_other_port_is_accepted).
CLIENT_B_REDIRECT = "http://127.0.0.1:20000/other-callback"
PRM_URL = f"{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp/gitlab"


async def _hub(make_hub: HubFactory, *, extra: bool = False, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(extra=extra),
        env=CATALOG_ENV,
        base_url="https://hub.test",
        **overrides,
    )


async def _ready_client(hub: Hub, **kwargs: Any) -> str:
    await web_login(hub)
    return await register_client(hub, **kwargs)


# --- AC-75 -----------------------------------------------------------------


@pytest.mark.ac("AC-75")
async def test_as_metadata_matches_rfc8414(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    response = await hub.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=300"
    body = response.json()
    assert body["issuer"] == PUBLIC_URL
    assert body["authorization_endpoint"] == f"{PUBLIC_URL}/oauth/authorize"
    assert body["token_endpoint"] == f"{PUBLIC_URL}/oauth/token"
    assert body["registration_endpoint"] == f"{PUBLIC_URL}/oauth/register"
    assert body["revocation_endpoint"] == f"{PUBLIC_URL}/oauth/revoke"
    assert body["response_types_supported"] == ["code"]
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]
    assert body["scopes_supported"] == [
        "gitlab:readonly",
        "gitlab:readwrite",
        "jira:readonly",
        "jira:readwrite",
    ]


@pytest.mark.ac("AC-75")
async def test_as_metadata_needs_no_authentication(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    anonymous = await hub.get("/.well-known/oauth-authorization-server")
    with_key = await hub.get(
        "/.well-known/oauth-authorization-server", headers=bearer("no-such-key")
    )
    assert anonymous.status_code == with_key.status_code == 200
    assert anonymous.json() == with_key.json()


# --- AC-76 -----------------------------------------------------------------


@pytest.mark.ac("AC-76")
async def test_as_metadata_with_resource_suffix(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    plain = await hub.get("/.well-known/oauth-authorization-server")
    suffixed = await hub.get("/.well-known/oauth-authorization-server/mcp/gitlab")
    assert suffixed.status_code == 200
    assert suffixed.json() == plain.json()


@pytest.mark.ac("AC-76")
@pytest.mark.parametrize("alias", ["tag", "nope"])
async def test_as_metadata_suffix_404_for_non_facade(make_hub: HubFactory, alias: str) -> None:
    hub = await _hub(make_hub)
    response = await hub.get(f"/.well-known/oauth-authorization-server/mcp/{alias}")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --- AC-77 -----------------------------------------------------------------


@pytest.mark.ac("AC-77")
async def test_protected_resource_metadata(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    response = await hub.get("/.well-known/oauth-protected-resource/mcp/gitlab")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == f"{PUBLIC_URL}/mcp/gitlab"
    assert body["authorization_servers"] == [PUBLIC_URL]
    assert body["scopes_supported"] == ["gitlab:readonly", "gitlab:readwrite"]
    assert body["bearer_methods_supported"] == ["header"]
    assert body["resource_name"] == "GitLab"
    assert body["resource_documentation"] == "https://portal.test/docs/gitlab"
    raw = response.text
    for secret in ("upstream_url", "mcp-gitlab.internal.test", "client_secret", "credential_headers"):
        assert secret not in raw


# --- AC-78 -----------------------------------------------------------------


@pytest.mark.ac("AC-78")
async def test_mcp_without_bearer_returns_401_with_resource_metadata(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    post = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("initialize"),
        headers={"Content-Type": "application/json"},
    )
    get = await hub.get("/mcp/gitlab")
    for response in (post, get):
        assert response.status_code == 401, response.text
        body = response.json()
        assert body["error"] == "unauthorized"
        assert body["message"] and body["hint"]
        header = response.headers["WWW-Authenticate"]
        assert header.startswith("Bearer")
        assert f'resource_metadata="{PRM_URL}"' in header
    assert hub.upstream.calls == 0


@pytest.mark.ac("AC-78")
async def test_mcp_with_malformed_authorization_returns_401(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    response = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("initialize"), headers={"Authorization": "Basic zzz"}
    )
    assert response.status_code == 401
    assert hub.upstream.calls == 0


# --- AC-79 -----------------------------------------------------------------


@pytest.mark.ac("AC-79")
@pytest.mark.parametrize("alias", ["tag", "u", "nope"])
async def test_prm_404_for_native_unconfigured_unknown(make_hub: HubFactory, alias: str) -> None:
    hub = await _hub(make_hub, extra=True)
    response = await hub.get(f"/.well-known/oauth-protected-resource/mcp/{alias}")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.ac("AC-79")
@pytest.mark.parametrize("alias", ["tag", "u", "nope"])
async def test_proxy_404_for_native_unconfigured_unknown(make_hub: HubFactory, alias: str) -> None:
    hub = await _hub(make_hub, extra=True)
    _conn, tokens = await connected_client(hub)
    response = await hub.post(
        f"/mcp/{alias}",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert hub.upstream.calls == 0


# --- AC-80 -----------------------------------------------------------------


@pytest.mark.ac("AC-80")
async def test_dynamic_registration_returns_public_client(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    payload = {
        "redirect_uris": [LOOPBACK_REDIRECT],
        "client_name": "OpenCode",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    response = await hub.post("/oauth/register", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["client_id"]
    assert isinstance(body["client_id_issued_at"], int)
    assert body["client_id_issued_at"] == int(hub.clock.time())
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [LOOPBACK_REDIRECT]
    assert "client_secret" not in body

    rows = await audit_rows(hub.app, "oauth_client_registered")
    assert len(rows) == 1
    assert rows[0]["details"]["client_id"] == body["client_id"]
    assert "secret" not in str(rows[0]["details"]).lower()

    second = await hub.post("/oauth/register", json=payload)
    assert second.status_code == 201
    assert second.json()["client_id"] != body["client_id"]


# --- AC-81 -----------------------------------------------------------------


@pytest.mark.ac("AC-81")
@pytest.mark.parametrize(
    "redirect_uris",
    [["http://evil.test/cb"], ["myapp://cb"], ["https://hub.test/cb#frag"]],
)
async def test_register_rejects_bad_redirect_uri(
    make_hub: HubFactory, redirect_uris: list[str]
) -> None:
    hub = await _hub(make_hub)
    response = await hub.post("/oauth/register", json={"redirect_uris": redirect_uris})
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "invalid_redirect_uri"
    assert body["error_description"]
    assert await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients") == []


@pytest.mark.ac("AC-81")
@pytest.mark.parametrize(
    "payload",
    [
        {"redirect_uris": []},
        {"redirect_uris": [LOOPBACK_REDIRECT], "token_endpoint_auth_method": "client_secret_post"},
        {"redirect_uris": [LOOPBACK_REDIRECT], "response_types": ["token"]},
    ],
)
async def test_register_rejects_bad_metadata(make_hub: HubFactory, payload: dict[str, Any]) -> None:
    hub = await _hub(make_hub)
    response = await hub.post("/oauth/register", json=payload)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"]
    assert await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients") == []


# --- AC-82 -----------------------------------------------------------------


@pytest.mark.ac("AC-82")
async def test_register_rate_limited_per_ip(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, rate_limit_register=2)
    payload = {"redirect_uris": [LOOPBACK_REDIRECT]}
    first = await hub.post("/oauth/register", json=payload)
    second = await hub.post("/oauth/register", json=payload)
    assert (first.status_code, second.status_code) == (201, 201)

    third = await hub.post("/oauth/register", json=payload)
    assert third.status_code == 429
    assert third.json()["error"] == "rate_limited"
    assert int(third.headers["Retry-After"]) >= 1
    assert len(await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients")) == 2

    hub.clock.advance(61)
    fourth = await hub.post("/oauth/register", json=payload)
    assert fourth.status_code == 201
    assert len(await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients")) == 3


# --- AC-153 ----------------------------------------------------------------

XFF_TWO_HOPS = "10.0.0.1, 10.0.0.9"
XFF_OTHER = "10.0.0.2"


def _register_payload() -> dict[str, Any]:
    return {"redirect_uris": [LOOPBACK_REDIRECT]}


@pytest.mark.ac("AC-153")
async def test_forwarded_for_is_ignored_without_trust_proxy(make_hub: HubFactory) -> None:
    """Дефолт HUB_TRUST_PROXY=false: заголовок не влияет, ключ лимита один на все запросы."""
    hub = await _hub(make_hub, rate_limit_register=2)
    assert hub.settings.trust_proxy is False

    first = await hub.post(
        "/oauth/register", json=_register_payload(), headers={"X-Forwarded-For": XFF_TWO_HOPS}
    )
    second = await hub.post(
        "/oauth/register", json=_register_payload(), headers={"X-Forwarded-For": XFF_OTHER}
    )
    assert (first.status_code, second.status_code) == (201, 201), second.text

    third = await hub.post(
        "/oauth/register", json=_register_payload(), headers={"X-Forwarded-For": "10.0.0.3"}
    )
    assert third.status_code == 429, third.text
    assert third.json()["error"] == "rate_limited"
    assert int(third.headers["Retry-After"]) >= 1
    assert len(await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients")) == 2


@pytest.mark.ac("AC-153")
async def test_trust_proxy_counts_limit_per_left_forwarded_address(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, rate_limit_register=2, trust_proxy=True)

    for header in (XFF_TWO_HOPS, XFF_OTHER):
        for _ in range(2):
            response = await hub.post(
                "/oauth/register", json=_register_payload(), headers={"X-Forwarded-For": header}
            )
            assert response.status_code == 201, response.text
    assert len(await fetch_rows(hub.app, "SELECT client_id FROM oauth_clients")) == 4

    # Левый адрес '10.0.0.1' исчерпал своё окно, '10.0.0.9' в правой части ключом не является.
    fifth = await hub.post(
        "/oauth/register", json=_register_payload(), headers={"X-Forwarded-For": XFF_TWO_HOPS}
    )
    assert fifth.status_code == 429, fifth.text
    assert fifth.json()["error"] == "rate_limited"


@pytest.mark.ac("AC-153")
@pytest.mark.parametrize("trust_proxy", [False, True])
async def test_forwarded_for_does_not_affect_auth_or_alias(
    make_hub: HubFactory, trust_proxy: bool
) -> None:
    """Заголовок влияет только на ключ лимита: аутентификация и выбор alias от него не зависят."""
    hub = await _hub(make_hub, trust_proxy=trust_proxy)
    _conn, tokens = await connected_client(hub)
    headers = {**mcp_headers(tokens["access_token"]), "X-Forwarded-For": XFF_TWO_HOPS}

    response = await hub.post("/mcp/gitlab", content=jsonrpc_body("tools/list"), headers=headers)
    assert response.status_code == 200, response.text
    assert hub.upstream.calls == 1

    assert hub.net is not None
    assert hub.net.upstreams["jira"].calls == 0
    anonymous = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers={"Content-Type": "application/json", "X-Forwarded-For": XFF_TWO_HOPS},
    )
    assert anonymous.status_code == 401, anonymous.text


# --- AC-83, AC-148 ---------------------------------------------------------


@pytest.mark.ac("AC-83")
async def test_authorize_unknown_client_shows_page(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    response = await hub.get(
        "/oauth/authorize", params=authorize_params("no-such-client", challenge=pkce_pair()[1])
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "location" not in response.headers
    assert html_error_code(response.text) == "invalid_client"
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


@pytest.mark.ac("AC-83")
@pytest.mark.ac("AC-148")
async def test_authorize_rejects_other_clients_redirect(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_a = await _ready_client(hub)
    await register_client(hub, redirect_uris=[CLIENT_B_REDIRECT], client_name="Other")
    response = await hub.get(
        "/oauth/authorize",
        params=authorize_params(client_a, redirect_uri=CLIENT_B_REDIRECT, challenge=pkce_pair()[1]),
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert html_error_code(response.text) == "invalid_redirect_uri"
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


@pytest.mark.ac("AC-83")
async def test_authorize_without_redirect_uri_shows_page(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    params = authorize_params(client_id, challenge=pkce_pair()[1])
    params.pop("redirect_uri")
    response = await hub.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert html_error_code(response.text) == "invalid_redirect_uri"
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


@pytest.mark.ac("AC-148")
async def test_authorize_rejects_path_traversal_redirect(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    response = await hub.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id, redirect_uri=f"{LOOPBACK_REDIRECT}/../evil", challenge=pkce_pair()[1]
        ),
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert html_error_code(response.text) == "invalid_redirect_uri"
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


@pytest.mark.ac("AC-83")
async def test_loopback_redirect_with_other_port_is_accepted(make_hub: HubFactory) -> None:
    """R-O4.1 (RFC 8252): расхождение только по порту loopback ошибкой не является.

    Флоу продолжается до выдачи кода, а код привязан к предъявленному redirect_uri: обмен с
    ним проходит, обмен с зарегистрированным — invalid_grant.
    """
    presented = "http://127.0.0.1:20000/cb"
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub, redirect_uris=["http://127.0.0.1:19876/cb"])
    verifier, challenge = pkce_pair()
    started = await hub.get(
        "/oauth/authorize",
        params=authorize_params(client_id, redirect_uri=presented, challenge=challenge),
    )
    assert started.status_code == 302
    assert started.headers["location"].startswith("https://gitlab.test/oauth/authorize")

    consent_page = await provider_callback(hub, started.headers["location"])
    granted = await submit_consent(hub, consent_page.text)
    assert granted.status_code == 302, granted.text
    location = granted.headers["location"]
    assert location.startswith(f"{presented}?")
    code = query_of(location)["code"]

    wrong = await exchange_code(
        hub,
        code=code,
        client_id=client_id,
        verifier=verifier,
        redirect_uri="http://127.0.0.1:19876/cb",
    )
    assert wrong.status_code == 400, wrong.text
    assert wrong.json()["error"] == "invalid_grant"

    exchanged = await exchange_code(
        hub, code=code, client_id=client_id, verifier=verifier, redirect_uri=presented
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["access_token"]


# --- AC-84 -----------------------------------------------------------------


@pytest.mark.ac("AC-84")
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"challenge": None}, "invalid_request"),
        ({"method": "plain"}, "invalid_request"),
        ({"resource": "https://hub.test/mcp/nope"}, "invalid_target"),
        ({"scope": "gitlab:admin"}, "invalid_scope"),
    ],
)
async def test_authorize_errors_redirect_with_state(
    make_hub: HubFactory, overrides: dict[str, Any], expected: str
) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    state = "st/+ ate=1&x"
    params = authorize_params(client_id, challenge=pkce_pair()[1], state=state)
    for key, value in overrides.items():
        if value is None:
            params.pop({"challenge": "code_challenge"}.get(key, key), None)
        else:
            params[{"challenge": "code_challenge", "method": "code_challenge_method"}.get(key, key)] = value
    response = await hub.get("/oauth/authorize", params=params)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(LOOPBACK_REDIRECT)
    query = query_of(location)
    assert query["error"] == expected
    assert query["state"] == state
    assert "code" not in query
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


# --- AC-85 -----------------------------------------------------------------


@pytest.mark.ac("AC-85")
async def test_authorize_without_session_redirects_to_login_and_back(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    params = authorize_params(client_id, challenge=challenge)
    first = await hub.get("/oauth/authorize", params=params)
    assert first.status_code == 302
    location = first.headers["location"]
    assert location.startswith("/auth/login?next=")
    original = str(first.request.url)
    expected_next = quote(f"{urlsplit(original).path}?{urlsplit(original).query}", safe="")
    assert location == f"/auth/login?next={expected_next}"

    logged_in = await litellm_web_login(hub, next_url="/ui/connections")
    assert logged_in.status_code in (302, 303)
    assert "hub_session" in hub.client.cookies

    second = await hub.get("/oauth/authorize", params=params)
    assert second.status_code == 302
    assert second.headers["location"].startswith("https://gitlab.test/oauth/authorize")


# --- AC-86 -----------------------------------------------------------------


@pytest.mark.ac("AC-86")
async def test_alias_from_resource_defaults_to_readonly(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    rows = await fetch_rows(hub.app, "SELECT alias, scope FROM oauth_codes")
    assert code
    assert rows == [{"alias": "gitlab", "scope": "gitlab:readonly"}]


@pytest.mark.ac("AC-86")
async def test_alias_from_scope_without_resource(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    await authorize_to_code(
        hub,
        client_id,
        challenge=challenge,
        alias="jira",
        resource=None,
        scope="jira:readwrite",
        preset="readwrite",
        groups=[],
    )
    rows = await fetch_rows(hub.app, "SELECT alias, scope FROM oauth_codes")
    assert rows == [{"alias": "jira", "scope": "jira:readwrite"}]


@pytest.mark.ac("AC-86")
@pytest.mark.parametrize(
    ("resource", "scope"),
    [("https://hub.test/mcp/gitlab", "jira:readonly"), (None, None)],
)
async def test_alias_conflict_or_absence_is_invalid_request(
    make_hub: HubFactory, resource: str | None, scope: str | None
) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    response = await hub.get(
        "/oauth/authorize",
        params=authorize_params(client_id, challenge=pkce_pair()[1], resource=resource, scope=scope),
    )
    assert response.status_code == 302
    assert query_of(response.headers["location"])["error"] == "invalid_request"


# --- AC-87 -----------------------------------------------------------------


@pytest.mark.ac("AC-87")
async def test_authorize_runs_provider_oauth_then_consent(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    first = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    assert first.status_code == 302
    location = first.headers["location"]
    assert location.startswith("https://gitlab.test/oauth/authorize")
    params = query_of(location)
    assert params["client_id"] == "hub-client-id"
    assert params["redirect_uri"] == f"{PUBLIC_URL}/oauth/callback/gitlab"
    assert params["scope"] == "read_api read_user read_repository"
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"]
    assert params["state"]

    consent = await provider_callback(hub, location)
    assert consent.status_code == 200, consent.text
    assert "Разрешить доступ" in consent.text

    token_request = hub.provider.token_requests[-1].form
    assert token_request["grant_type"] == "authorization_code"
    assert token_request["client_secret"] == "gl-secret"
    assert token_request["code_verifier"]
    assert token_request["redirect_uri"] == f"{PUBLIC_URL}/oauth/callback/gitlab"

    rows = await fetch_rows(hub.app, "SELECT status FROM connections WHERE alias = 'gitlab'")
    assert rows == [{"status": "connected"}]


# --- AC-88 -----------------------------------------------------------------


async def _grant_once(hub: Hub, client_id: str, challenge: str) -> None:
    first = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    consent = await provider_callback(hub, first.headers["location"])
    assert (await submit_consent(hub, consent.text)).status_code == 302


@pytest.mark.ac("AC-88")
async def test_consent_remember_skips_screen(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, consent="remember")
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    await _grant_once(hub, client_id, challenge)

    again = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    assert again.status_code == 302
    assert "code" in query_of(again.headers["location"])


@pytest.mark.ac("AC-88")
async def test_consent_always_shows_screen_again(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, consent="always")
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    await _grant_once(hub, client_id, challenge)

    again = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    assert again.status_code == 200
    assert "Разрешить доступ" in again.text


@pytest.mark.ac("AC-88")
async def test_consent_remember_asks_again_for_other_scope(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, consent="remember")
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    await _grant_once(hub, client_id, challenge)

    again = await hub.get(
        "/oauth/authorize",
        params=authorize_params(client_id, challenge=challenge, scope="gitlab:readwrite"),
    )
    assert again.status_code == 200
    assert "Разрешить доступ" in again.text


# --- AC-89 -----------------------------------------------------------------


@pytest.mark.ac("AC-89")
async def test_consent_deny_returns_access_denied(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    first = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=challenge, state="st-9")
    )
    consent = await provider_callback(hub, first.headers["location"])
    response = await submit_consent(hub, consent.text, action="deny")
    assert response.status_code == 302
    query = query_of(response.headers["location"])
    assert response.headers["location"].startswith(LOOPBACK_REDIRECT)
    assert query["error"] == "access_denied"
    assert query["state"] == "st-9"
    assert "code" not in query
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


# --- AC-90 -----------------------------------------------------------------


@pytest.mark.ac("AC-90")
async def test_code_is_single_use_and_revokes_chain(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, auth_code_ttl=60)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    first = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert first.status_code == 200
    tokens = first.json()

    replay = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert replay.json()["error_description"]

    proxied = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert proxied.status_code == 401

    refreshed = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=client_id
    )
    assert refreshed.status_code == 400
    assert refreshed.json()["error"] == "invalid_grant"


@pytest.mark.ac("AC-90")
async def test_expired_code_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, auth_code_ttl=60)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    hub.clock.advance(61)
    response = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


# --- AC-91 -----------------------------------------------------------------


@pytest.mark.ac("AC-91")
async def test_code_exchange_issues_token_pair(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    response = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "gitlab:readonly"
    assert body["access_token"] and body["refresh_token"]

    rows = await audit_rows(hub.app, "oauth_token_issued")
    assert rows and rows[0]["details"]["grant"] == "authorization_code"
    serialized = str(rows[0]["details"])
    assert body["access_token"] not in serialized
    assert body["refresh_token"] not in serialized


# --- AC-92 -----------------------------------------------------------------


@pytest.mark.ac("AC-92")
async def test_wrong_or_missing_verifier_rejected_and_code_survives(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)

    wrong = await exchange_code(
        hub, code=code, client_id=client_id, verifier="wrong-verifier"
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    assert wrong.json()["error_description"]

    missing = await exchange_code(hub, code=code, client_id=client_id, verifier=None)
    assert missing.status_code == 400
    assert missing.json()["error"] == "invalid_grant"
    assert await fetch_rows(hub.app, "SELECT id FROM refresh_tokens") == []

    good = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert good.status_code == 200, good.text


# --- AC-155 ----------------------------------------------------------------


@pytest.mark.ac("AC-155")
async def test_token_requires_redirect_uri_used_for_code(make_hub: HubFactory) -> None:
    """RFC 6749 §4.1.3: код выдан с redirect_uri → обмен без него отклоняется (код не сгорает)."""
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)

    missing = await exchange_code(
        hub, code=code, client_id=client_id, verifier=verifier, redirect_uri=None
    )
    assert missing.status_code == 400, missing.text
    body = missing.json()
    assert body["error"] == "invalid_grant"
    assert any("Ѐ" <= ch <= "ӿ" for ch in body["error_description"])
    assert await fetch_rows(hub.app, "SELECT id FROM refresh_tokens") == []

    good = await exchange_code(
        hub, code=code, client_id=client_id, verifier=verifier, redirect_uri=LOOPBACK_REDIRECT
    )
    assert good.status_code == 200, good.text
    tokens = good.json()
    assert tokens["access_token"] and tokens["refresh_token"]


# --- AC-93 -----------------------------------------------------------------


@pytest.mark.ac("AC-93")
async def test_code_exchange_checks_client_and_redirect(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_a = await _ready_client(hub)
    client_b = await register_client(hub, redirect_uris=[CLIENT_B_REDIRECT], client_name="B")
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_a, challenge=challenge)

    other_client = await exchange_code(hub, code=code, client_id=client_b, verifier=verifier)
    assert other_client.status_code == 400
    assert other_client.json()["error"] == "invalid_grant"

    other_redirect = await exchange_code(
        hub, code=code, client_id=client_a, verifier=verifier, redirect_uri=CLIENT_B_REDIRECT
    )
    assert other_redirect.status_code == 400
    assert other_redirect.json()["error"] == "invalid_grant"

    unknown = await exchange_code(hub, code=code, client_id="nope", verifier=verifier)
    assert unknown.status_code == 401
    assert unknown.json()["error"] == "invalid_client"

    assert await fetch_rows(hub.app, "SELECT id FROM refresh_tokens") == []


# --- AC-94 -----------------------------------------------------------------


@pytest.mark.ac("AC-94")
async def test_access_token_claims(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, secret_key="hub-secret")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    body = (await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)).json()

    header_b64 = body["access_token"].split(".")[0]
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
    assert header["alg"] == "HS256"

    claims = jwt_decode(body["access_token"], "hub-secret")
    connection = (await fetch_rows(hub.app, "SELECT id FROM connections"))[0]["id"]
    assert claims["iss"] == PUBLIC_URL
    assert claims["sub"] == "u1"
    assert claims["aud"] == f"{PUBLIC_URL}/mcp/gitlab"
    assert claims["scope"] == "gitlab:readonly"
    assert claims["cid"] == connection
    assert claims["client_id"] == client_id
    assert claims["jti"]
    assert claims["exp"] - claims["iat"] == 3600

    second = await refresh_grant(hub, refresh_token=body["refresh_token"], client_id=client_id)
    other_claims = jwt_decode(second.json()["access_token"], "hub-secret")
    assert other_claims["jti"] != claims["jti"]

    stored = await fetch_rows(hub.app, "SELECT token_sha256 FROM refresh_tokens")
    digests = {row["token_sha256"] for row in stored}
    assert body["refresh_token"] not in digests
    assert sha256_hex(body["refresh_token"]) in digests
    assert len(body["refresh_token"].split(".")) != 3


# --- AC-95 -----------------------------------------------------------------


@pytest.mark.ac("AC-95")
async def test_refresh_rotates_pair(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    first = (await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)).json()

    response = await refresh_grant(hub, refresh_token=first["refresh_token"], client_id=client_id)
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["scope"] == first["scope"]

    proxied = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(second["access_token"]),
    )
    assert proxied.status_code == 200, proxied.text

    rows = await fetch_rows(
        hub.app, "SELECT token_sha256, status FROM refresh_tokens ORDER BY id"
    )
    by_digest = {row["token_sha256"]: row["status"] for row in rows}
    assert by_digest[sha256_hex(first["refresh_token"])] == "rotated"
    assert by_digest[sha256_hex(second["refresh_token"])] == "active"


# --- AC-96 -----------------------------------------------------------------


@pytest.mark.ac("AC-96")
async def test_refresh_reuse_revokes_whole_chain(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    r1 = (await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)).json()
    r2 = (await refresh_grant(hub, refresh_token=r1["refresh_token"], client_id=client_id)).json()
    r3 = (await refresh_grant(hub, refresh_token=r2["refresh_token"], client_id=client_id)).json()

    reuse = await refresh_grant(hub, refresh_token=r1["refresh_token"], client_id=client_id)
    assert reuse.status_code == 400
    assert reuse.json()["error"] == "invalid_grant"

    latest = await refresh_grant(hub, refresh_token=r3["refresh_token"], client_id=client_id)
    assert latest.status_code == 400
    assert latest.json()["error"] == "invalid_grant"

    proxied = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(r3["access_token"]),
    )
    assert proxied.status_code == 401
    assert proxied.json()["error"] == "unauthorized"

    assert await audit_rows(hub.app, "oauth_refresh_reuse_detected")


# --- AC-97 -----------------------------------------------------------------


@pytest.mark.ac("AC-97")
async def test_revoke_always_returns_200(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    tokens = (await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)).json()

    revoked = await hub.post("/oauth/revoke", data={"token": tokens["refresh_token"]})
    assert revoked.status_code == 200
    assert revoked.json() == {}

    proxied = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert proxied.status_code == 401

    refreshed = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=client_id
    )
    assert refreshed.status_code == 400
    assert refreshed.json()["error"] == "invalid_grant"

    unknown = await hub.post("/oauth/revoke", data={"token": "nope"})
    assert unknown.status_code == 200
    assert unknown.json() == {}

    empty = await hub.post("/oauth/revoke", data={})
    assert empty.status_code == 400
    assert empty.json()["error"] == "invalid_request"

    assert await audit_rows(hub.app, "oauth_token_revoked")


# --- AC-98 -----------------------------------------------------------------


@pytest.mark.ac("AC-98")
@pytest.mark.parametrize("position", [0, 20, -1], ids=["first", "middle", "last"])
async def test_token_signature_expiry_and_audience(make_hub: HubFactory, position: int) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    # Изменяется любой значащий символ подписи — декодированные байты гарантированно отличаются.
    tampered = tamper_signature(token, position)
    assert signature_bytes(tampered) != signature_bytes(token)

    claims = jwt_decode(token, hub.settings.secret_key.get_secret_value())
    expired = jwt_encode(
        {**claims, "exp": int(hub.clock.time()) - 1},
        hub.settings.secret_key.get_secret_value(),
    )
    foreign = jwt_encode(claims, "another-secret")

    for candidate in (tampered, expired, foreign):
        response = await hub.post(
            "/mcp/gitlab", content=jsonrpc_body("tools/list"), headers=mcp_headers(candidate)
        )
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "unauthorized"
        header = response.headers["WWW-Authenticate"]
        assert f'resource_metadata="{PRM_URL}"' in header
        assert 'error="invalid_token"' in header

    await seed_connection(hub, alias="jira", groups=("issues",))
    wrong_audience = await hub.post(
        "/mcp/jira", content=jsonrpc_body("tools/list"), headers=mcp_headers(token)
    )
    assert wrong_audience.status_code == 403
    assert wrong_audience.json()["error"] == "forbidden"

    assert hub.upstream.calls == 0
    assert hub.net is not None
    assert hub.net.upstreams["jira"].calls == 0


# --- AC-99 -----------------------------------------------------------------


@pytest.mark.ac("AC-99")
async def test_hot_path_uses_cache_and_denylist(
    make_hub: HubFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = await _hub(make_hub, connection_cache_ttl=60)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    first = await hub.post("/mcp/gitlab", content=jsonrpc_body("initialize"), headers=headers)
    assert first.status_code == 200, first.text

    def _blocked() -> Any:
        raise AssertionError("горячий путь не должен обращаться к БД")

    monkeypatch.setattr(hub.app.state.db, "session", _blocked)
    second = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("initialize", request_id=2), headers=headers
    )
    assert second.status_code == 200, second.text
    monkeypatch.undo()

    await hub.post("/oauth/revoke", data={"token": tokens["access_token"]})
    revoked = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("initialize", request_id=3), headers=headers
    )
    assert revoked.status_code == 401


@pytest.mark.ac("AC-99")
async def test_permission_change_invalidates_connection_cache(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, connection_cache_ttl=60)
    csrf = await web_login(hub)
    conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    assert conn.id
    first = await hub.post("/mcp/gitlab", content=jsonrpc_body("tools/call", {"name": "list_mrs"}), headers=headers)
    assert first.status_code == 200, first.text
    assert hub.upstream.last().header("Enabled-Groups") == "core,code_review"

    updated = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": ["code_review", "devops"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text

    second = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=2),
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert hub.upstream.last().header("Enabled-Groups") == "core,code_review,devops"


# --- AC-100 ----------------------------------------------------------------


@pytest.mark.ac("AC-100")
async def test_oauth_errors_follow_rfc6749(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)

    password = await hub.post(
        "/oauth/token", data={"grant_type": "password", "client_id": client_id}
    )
    assert password.status_code == 400
    assert password.json()["error"] == "unsupported_grant_type"
    assert password.headers["Cache-Control"] == "no-store"

    empty = await hub.post("/oauth/token", data={})
    assert empty.status_code == 400
    assert empty.json()["error"] == "invalid_request"

    not_json = await hub.post(
        "/oauth/register", content=b"<xml/>", headers={"Content-Type": "application/json"}
    )
    assert not_json.status_code == 400
    assert not_json.json()["error"] == "invalid_client_metadata"

    for response in (password, empty, not_json):
        body = response.json()
        assert set(body) == {"error", "error_description"}
        assert body["error_description"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Request-ID"]


# --- AC-101 ----------------------------------------------------------------


@pytest.mark.ac("AC-101")
async def test_token_rate_limit_per_client_and_ip(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, rate_limit_token=3)
    client_a = await _ready_client(hub)
    client_b = await register_client(hub, redirect_uris=[CLIENT_B_REDIRECT], client_name="B")

    for _ in range(3):
        response = await hub.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": client_a},
        )
        assert response.status_code == 400

    limited = await hub.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": client_a},
    )
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1

    other = await hub.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": client_b},
    )
    assert other.status_code == 400
    assert other.json()["error"] == "invalid_grant"


# --- дополнительные ветки authorize / consent / callback -------------------


@pytest.mark.ac("AC-89")
async def test_consent_with_expired_transaction(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, oauth_tx_ttl=600)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    page = await provider_callback(hub, started.headers["location"])
    hub.clock.advance(601)
    response = await submit_consent(hub, page.text)
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_transaction"


@pytest.mark.ac("AC-89")
async def test_consent_of_other_user_is_forbidden(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    page = await provider_callback(hub, started.headers["location"])
    web_logout(hub)
    csrf = await web_login(hub, "u2")
    response = await submit_consent(hub, page.text, csrf=csrf)
    assert response.status_code == 403
    assert html_error_code(response.text) == "forbidden"


@pytest.mark.ac("AC-89")
async def test_consent_without_session_is_forbidden(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    page = await provider_callback(hub, started.headers["location"])
    web_logout(hub)
    response = await submit_consent(hub, page.text, csrf="anything")
    assert response.status_code == 403
    assert html_error_code(response.text) == "forbidden"


@pytest.mark.ac("AC-111")
async def test_consent_readwrite_repeats_provider_oauth(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    started = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
    )
    page = await provider_callback(hub, started.headers["location"])
    assert query_of(started.headers["location"])["scope"] == "read_api read_user read_repository"

    upgraded = await submit_consent(hub, page.text, preset="readwrite", groups=["repo_write"])
    assert upgraded.status_code == 302
    location = upgraded.headers["location"]
    assert location.startswith("https://gitlab.test/oauth/authorize")
    assert query_of(location)["scope"] == "api read_user"

    finished = await provider_callback(hub, location, code="prov-code-2")
    rows = await fetch_rows(hub.app, "SELECT preset, groups FROM connections")
    assert rows[0]["preset"] == "readwrite"
    assert "repo_write" in str(rows[0]["groups"])

    # HUB_CONSENT=always: после расширения прав в системе экран прав показывается ещё раз,
    # подтверждение уже не требует нового OAuth целевой системы (R-O6.3, R-B7).
    assert finished.status_code == 200, finished.text
    confirmed = await submit_consent(hub, finished.text, preset="readwrite", groups=["repo_write"])
    assert confirmed.status_code == 302, confirmed.text
    code = query_of(confirmed.headers["location"])["code"]
    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.status_code == 200, tokens.text
    assert len(hub.provider.token_requests) == 2


@pytest.mark.ac("AC-88")
async def test_remembered_consent_row_is_updated(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, consent="remember")
    client_id = await _ready_client(hub)
    _verifier, challenge = pkce_pair()
    for groups in (["code_review"], ["devops"]):
        started = await hub.get(
            "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
        )
        if started.status_code == 302 and started.headers["location"].startswith("https://gitlab"):
            page = await provider_callback(hub, started.headers["location"])
        else:
            page = started
        if page.status_code == 200:
            assert (await submit_consent(hub, page.text, groups=groups)).status_code == 302
    rows = await fetch_rows(hub.app, "SELECT user_id, client_id, alias, scope FROM consents")
    assert len(rows) == 1
    assert rows[0]["scope"] == "gitlab:readonly"


@pytest.mark.ac("AC-103")
async def test_callback_without_code_is_invalid_request(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    state = query_of(started.headers["location"])["state"]
    response = await hub.get("/oauth/callback/gitlab", params={"state": state})
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_request"


@pytest.mark.ac("AC-103")
async def test_callback_with_provider_failure_shows_page(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    hub.provider.push(httpx.Response(500, json={"error": "server_error"}))
    response = await provider_callback(hub, started.headers["location"])
    assert response.status_code == 502
    assert html_error_code(response.text) == "upstream_auth_failed"
    assert await fetch_rows(hub.app, "SELECT id FROM upstream_tokens") == []


@pytest.mark.ac("AC-102")
async def test_missing_provider_secret_fails_exchange(make_hub: HubFactory) -> None:
    env = {k: v for k, v in CATALOG_ENV.items() if k != "GL_SECRET"}
    hub = await make_hub(catalog=i3_catalog(), env=env, base_url="https://hub.test")
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    response = await provider_callback(hub, started.headers["location"])
    assert response.status_code == 502
    assert html_error_code(response.text) == "upstream_auth_failed"
    assert hub.provider.token_requests == []


@pytest.mark.ac("AC-149")
async def test_connect_endpoint_reconnects_from_hub_page(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    await seed_connection(hub, status="needs_reauth")
    started = await hub.get("/oauth/connect/gitlab")
    assert started.status_code == 302
    location = started.headers["location"]
    assert location.startswith("https://gitlab.test/oauth/authorize")
    finished = await provider_callback(hub, location)
    assert finished.status_code == 302
    assert finished.headers["location"] == "/ui/servers/gitlab"
    rows = await fetch_rows(hub.app, "SELECT status FROM connections")
    assert rows[0]["status"] == "connected"


@pytest.mark.ac("AC-149")
async def test_connect_requires_session_and_known_alias(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    anonymous = await hub.get("/oauth/connect/gitlab")
    assert anonymous.status_code == 302
    assert anonymous.headers["location"].startswith("/auth/login?next=")

    await web_login(hub)
    unknown = await hub.get("/oauth/connect/nope")
    assert unknown.status_code == 404
    assert html_error_code(unknown.text) == "not_found"
