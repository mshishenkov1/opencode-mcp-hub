"""Веб-интерфейс Hub (R-W1..R-W6): AC-131..AC-137, AC-154."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    OIDC_ISSUER,
    PUBLIC_URL,
    UPSTREAM_ACCESS,
    WEB_LOGIN_POLL_RE,
    add_key,
    audit_rows,
    authorize_params,
    bearer,
    catalog_doc,
    connected_client,
    cookie_attributes,
    execute,
    facade_server,
    fetch_rows,
    gitlab_facade,
    hidden_inputs,
    html_error_code,
    i3_catalog,
    jira_facade,
    litellm_web_login,
    mock_poll,
    mock_start,
    native_server,
    pkce_pair,
    provider_callback,
    query_of,
    register_client,
    restricted_facade,
    seed_connection,
    set_cookie_header,
    start_body,
    submit_consent,
    teams_body,
    unconfigured_facade,
    web_login,
    web_logout,
)

KEYCLOAK_SECRET = "KC-S3CRET"


def _confluence_facade() -> dict[str, Any]:
    return facade_server(
        "confluence",
        title="Confluence",
        description="Пространства и страницы Confluence.",
        upstream_url="https://mcp-confluence.internal.test/mcp",
        auth={
            "type": "oauth2",
            "authorize_url": "https://confluence.test/oauth/authorize",
            "token_url": "https://confluence.test/oauth/token",
            "client_id": "conf-id",
            "client_secret": "env:CONF_SECRET",
            "pkce": True,
            "scopes": {"readonly": ["READ"], "readwrite": ["WRITE"]},
        },
        permission_model={
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": [],
            "groups": [{"id": "pages", "title": "Страницы", "preset": "readonly"}],
        },
    )


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


async def _oidc_hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await _hub(
        make_hub,
        web_auth="keycloak",
        keycloak_issuer=OIDC_ISSUER,
        keycloak_client_secret=KEYCLOAK_SECRET,
        **overrides,
    )


async def _start_oidc_login(hub: Hub, next_url: str = "/ui/connections") -> dict[str, str]:
    response = await hub.get("/auth/login", params={"next": next_url})
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(hub.oidc.authorize_url)
    return query_of(location)


# --- AC-131 ----------------------------------------------------------------


@pytest.mark.ac("AC-131")
@pytest.mark.ac("AC-154")
async def test_oidc_login_creates_web_session(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    params = await _start_oidc_login(hub)
    assert params["client_id"] == "opencode-mcp-hub"
    assert params["redirect_uri"] == f"{PUBLIC_URL}/auth/callback"
    assert params["response_type"] == "code"
    assert params["scope"] == "openid profile email"
    assert params["state"] and params["nonce"] and params["code_challenge"]

    hub.oidc.next_id_token = hub.oidc.make_id_token(
        nonce=params["nonce"], now=hub.clock.time()
    )
    callback = await hub.get(
        "/auth/callback", params={"code": "kc-code", "state": params["state"]}
    )
    assert callback.status_code in (302, 303), callback.text
    assert callback.headers["location"] == "/ui/connections"
    # Атрибуты проверяются у каждой cookie отдельно: в ответе их две (hub_session и hub_csrf),
    # и в склеенном заголовке атрибут соседней cookie удовлетворял бы проверку.
    session_cookie = set_cookie_header(callback, "hub_session")
    session_attrs = cookie_attributes(session_cookie)
    assert session_cookie.split("=", 1)[1].split(";")[0]
    assert "httponly" in session_attrs
    assert session_attrs["samesite"].lower() == "lax"
    assert "secure" in session_attrs  # HUB_PUBLIC_URL — https
    assert session_attrs["path"] == "/"

    # R-W6: hub_csrf — double-submit, читается скриптом страницы, поэтому без HttpOnly.
    csrf_attrs = cookie_attributes(set_cookie_header(callback, "hub_csrf"))
    assert "httponly" not in csrf_attrs
    assert csrf_attrs["samesite"].lower() == "lax"
    assert "secure" in csrf_attrs

    users = await fetch_rows(hub.app, "SELECT user_id, email FROM users")
    assert users == [{"user_id": "u1", "email": "u1@corp"}]
    sessions = await fetch_rows(hub.app, "SELECT user_id, auth_method FROM sessions")
    assert sessions == [{"user_id": "u1", "auth_method": "keycloak"}]


@pytest.mark.ac("AC-131")
async def test_oidc_callback_with_foreign_state_fails(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    await _start_oidc_login(hub)
    response = await hub.get("/auth/callback", params={"code": "kc", "state": "someone-else"})
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_state"
    assert "set-cookie" not in response.headers
    assert await fetch_rows(hub.app, "SELECT id FROM sessions") == []


@pytest.mark.ac("AC-131")
async def test_external_next_is_replaced(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    params = await _start_oidc_login(hub, next_url="https://evil.test/")
    hub.oidc.next_id_token = hub.oidc.make_id_token(nonce=params["nonce"], now=hub.clock.time())
    callback = await hub.get("/auth/callback", params={"code": "c", "state": params["state"]})
    assert callback.headers["location"] == "/ui/connections"


# --- AC-154 ----------------------------------------------------------------


@pytest.mark.ac("AC-154")
@pytest.mark.parametrize("alg", ["HS256", "none"])
@pytest.mark.parametrize("jwks_available", [True, False], ids=["jwks_ok", "jwks_down"])
async def test_id_token_with_forbidden_alg_is_rejected(
    make_hub: HubFactory, alg: str, jwks_available: bool
) -> None:
    """R-W1: ``none`` и HS* отклоняются до обращения к JWKS (защита от algorithm confusion)."""
    hub = await _oidc_hub(make_hub)
    params = await _start_oidc_login(hub)
    if not jwks_available:
        hub.oidc.jwks_status = 503
    hub.oidc.next_id_token = hub.oidc.make_id_token(
        nonce=params["nonce"], now=hub.clock.time(), alg=alg
    )

    response = await hub.get("/auth/callback", params={"code": "c", "state": params["state"]})
    assert response.status_code == 400, response.text
    assert html_error_code(response.text) == "invalid_id_token"
    assert any("Ѐ" <= ch <= "ӿ" for ch in response.text)
    assert "set-cookie" not in response.headers
    assert hub.client.cookies.get("hub_session") is None
    assert await fetch_rows(hub.app, "SELECT id FROM sessions") == []
    # Решение принято по alg: к JWKS не обращались ни разу (результат не зависит от его доступности).
    assert hub.oidc.jwks_requests == []


# --- AC-132 ----------------------------------------------------------------


@pytest.mark.ac("AC-132")
@pytest.mark.parametrize(
    "variant", ["wrong_key", "wrong_nonce", "wrong_issuer", "expired"]
)
async def test_invalid_id_token_creates_no_session(make_hub: HubFactory, variant: str) -> None:
    hub = await _oidc_hub(make_hub)
    params = await _start_oidc_login(hub)
    now = hub.clock.time()
    kwargs: dict[str, Any] = {"nonce": params["nonce"], "now": now}
    if variant == "wrong_key":
        kwargs["wrong_key"] = True
    elif variant == "wrong_nonce":
        kwargs["nonce"] = "not-the-nonce"
    elif variant == "wrong_issuer":
        kwargs["issuer"] = "https://other.test/realms/x"
    else:
        kwargs["expires_at"] = now - 10
    hub.oidc.next_id_token = hub.oidc.make_id_token(**kwargs)

    response = await hub.get("/auth/callback", params={"code": "c", "state": params["state"]})
    assert response.status_code == 400, response.text
    assert html_error_code(response.text) == "invalid_id_token"
    assert any("Ѐ" <= ch <= "ӿ" for ch in response.text)
    assert "hub_session=" not in response.headers.get("set-cookie", "")
    assert await fetch_rows(hub.app, "SELECT id FROM sessions") == []


# --- AC-133 ----------------------------------------------------------------


@pytest.mark.ac("AC-133")
async def test_litellm_web_login_creates_session(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    assert hub.settings.web_auth == "litellm"
    mock_start(hub.litellm, start_body())
    page = await hub.get("/auth/login", params={"next": "/ui/connections"})
    assert page.status_code == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert "ABCD-1234" in page.text
    assert "/sso/key/generate" in page.text
    assert "Вход в Hub" in page.text

    web_logout(hub)
    result = await litellm_web_login(hub, next_url="/ui/connections", key="sk-web-1")
    assert result.status_code in (302, 303), result.text
    assert result.headers["location"] == "/ui/connections"
    assert "hub_session=" in result.headers["set-cookie"]

    sessions = await fetch_rows(hub.app, "SELECT user_id, auth_method FROM sessions")
    assert sessions and sessions[-1] == {"user_id": "u1", "auth_method": "litellm"}
    keys = await fetch_rows(hub.app, "SELECT key_sha256, user_id FROM api_keys")
    assert keys and keys[0]["user_id"] == "u1"
    assert "sk-web-1" not in str(keys)
    assert await audit_rows(hub.app, "web_login")


# --- AC-134 ----------------------------------------------------------------


@pytest.mark.ac("AC-134")
async def test_consent_screen_shows_catalog_groups_and_saves_choice(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
    )
    page = await provider_callback(hub, started.headers["location"])
    assert page.status_code == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    html = page.text
    assert "GitLab" in html
    assert "OpenCode" in html
    for title in ("Code review", "DevOps", "Пользователи", "Запись в репозиторий"):
        assert title in html
    assert "Администрирование" not in html
    assert "core" in html
    assert "включена всегда" in html
    assert 'value="readonly" checked' in html.replace(" >", ">")

    response = await submit_consent(hub, html, preset="readonly", groups=["code_review"])
    assert response.status_code == 302
    assert response.headers["location"].startswith("http://127.0.0.1:19876")
    assert "code" in query_of(response.headers["location"])

    rows = await fetch_rows(hub.app, "SELECT preset, groups FROM connections")
    assert rows[0]["preset"] == "readonly"
    assert rows[0]["groups"] in ('["code_review"]', ["code_review"])


# --- AC-135 ----------------------------------------------------------------


@pytest.mark.ac("AC-135")
async def test_connections_page_shows_only_own_connections(make_hub: HubFactory) -> None:
    catalog = catalog_doc(
        [gitlab_facade(), jira_facade(), _confluence_facade(), native_server("tag")]
    )
    hub = await make_hub(
        catalog=catalog,
        env={**CATALOG_ENV, "CONF_SECRET": "conf-secret"},
        base_url="https://hub.test",
    )
    await web_login(hub, "u1")
    await seed_connection(hub, user_id="u1", alias="gitlab", status="connected")
    await seed_connection(
        hub, user_id="u1", alias="jira", status="needs_reauth", groups=("issues",)
    )
    await seed_connection(hub, user_id="u2", alias="confluence", status="connected")

    page = await hub.get("/ui/connections")
    assert page.status_code == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert page.headers["Cache-Control"] == "private, no-store"
    html = page.text
    assert "Мои подключения" in html
    gitlab_card = html.split('id="conn-gitlab"')[1].split("</div>")[0]
    assert "Подключён" in gitlab_card
    jira_card = html.split('id="conn-jira"')[1]
    assert "Нужна повторная авторизация" in jira_card
    assert "Переподключить" in jira_card
    assert "Отключить" in html
    confluence_card = html.split('id="conn-confluence"')[1]
    assert "Не подключён" in confluence_card.split("</div>")[0]

    web_logout(hub)
    anonymous = await hub.get("/ui/connections")
    assert anonymous.status_code == 302
    assert anonymous.headers["location"].startswith("/auth/login")


# --- AC-136 ----------------------------------------------------------------


@pytest.mark.ac("AC-136")
async def test_server_card_hides_internal_data(make_hub: HubFactory) -> None:
    catalog = catalog_doc(
        [gitlab_facade(), jira_facade(), native_server("tag"), unconfigured_facade("u"), restricted_facade("b")]
    )
    hub = await make_hub(catalog=catalog, env=CATALOG_ENV, base_url="https://hub.test")
    await web_login(hub)
    conn, _tokens = await connected_client(hub)
    row = (
        await fetch_rows(
            hub.app,
            "SELECT access_token_enc FROM upstream_tokens WHERE connection_id = :cid",
            cid=conn.id,
        )
    )[0]

    page = await hub.get("/ui/servers/gitlab")
    assert page.status_code == 200
    html = page.text
    assert "GitLab" in html
    assert "Репозитории, merge requests, issues GitLab." in html
    assert "AI Lab" in html
    assert "Подключён" in html
    assert f"{PUBLIC_URL}/mcp/gitlab" in html
    assert "Code review" in html
    for secret in (
        "mcp-gitlab.internal.test",
        "upstream_url",
        "client_secret",
        "credential_headers",
        UPSTREAM_ACCESS,
        row["access_token_enc"],
    ):
        assert secret not in html

    for alias in ("u", "b", "nope"):
        missing = await hub.get(f"/ui/servers/{alias}")
        assert missing.status_code == 404, alias
        assert missing.headers["content-type"] == "text/html; charset=utf-8"
        assert html_error_code(missing.text) == "not_found"


# --- AC-137 ----------------------------------------------------------------


@pytest.mark.ac("AC-137")
async def test_consent_without_csrf_is_forbidden(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    started = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
    )
    page = await provider_callback(hub, started.headers["location"])
    fields = hidden_inputs(page.text)
    response = await hub.post(
        "/oauth/consent", data={"tx": fields["tx"], "action": "allow", "preset": "readonly"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "forbidden"


@pytest.mark.ac("AC-137")
async def test_permissions_put_requires_csrf_with_cookie(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    csrf = await web_login(hub)
    await connected_client(hub)
    payload = {"preset": "readonly", "groups": ["code_review"]}

    without = await hub.client.put("/api/me/connections/gitlab/permissions", json=payload)
    assert without.status_code == 403
    assert without.json()["error"] == "forbidden"

    with_token = await hub.client.put(
        "/api/me/connections/gitlab/permissions", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert with_token.status_code == 200, with_token.text


@pytest.mark.ac("AC-137")
async def test_bearer_key_needs_no_csrf(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    await connected_client(hub)
    await add_key(hub, "sk-owner", "u1")
    response = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": ["code_review"]},
        headers=bearer("sk-owner"),
    )
    assert response.status_code == 200, response.text


@pytest.mark.ac("AC-137")
async def test_logout_clears_session(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    csrf = await web_login(hub)
    assert await fetch_rows(hub.app, "SELECT id FROM sessions")

    response = await hub.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/auth/login"
    assert 'hub_session=""' in response.headers["set-cookie"] or "hub_session=;" in response.headers["set-cookie"]
    assert await fetch_rows(hub.app, "SELECT id FROM sessions") == []


@pytest.mark.ac("AC-137")
async def test_html_responses_have_charset_and_cache_control(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    for path in ("/ui/connections", "/ui/servers/gitlab", "/ui/servers/nope"):
        response = await hub.get(path)
        assert response.headers["content-type"] == "text/html; charset=utf-8", path
        assert response.headers["Cache-Control"] == "private, no-store", path


# --- дополнительные ветки входа и страниц ---------------------------------


@pytest.mark.ac("AC-133")
async def test_login_with_active_session_redirects_to_next(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await web_login(hub)
    response = await hub.get("/auth/login", params={"next": "/ui/servers/gitlab"})
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/servers/gitlab"


@pytest.mark.ac("AC-133")
async def test_login_start_failure_shows_error_page(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    hub.litellm.post("/sso/cli/start").respond(500, json={"error": "boom"})
    response = await hub.get("/auth/login")
    assert response.status_code >= 400
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert html_error_code(response.text)


@pytest.mark.ac("AC-133")
async def test_login_poll_states(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    mock_start(hub.litellm, start_body())
    page = await hub.get("/auth/login")
    login_id = WEB_LOGIN_POLL_RE.search(page.text).group(1)

    hub.litellm.get("/sso/cli/poll/ll-1").respond(200, json={"status": "pending"})
    pending = await hub.get(f"/auth/login/poll/{login_id}")
    assert pending.status_code == 200
    assert "Ожидаем подтверждения входа" in pending.text

    hub.litellm.reset()
    expired = await hub.get("/auth/login/poll/no-such-login")
    assert expired.status_code == 200
    assert "истекла" in expired.text


@pytest.mark.ac("AC-133")
async def test_login_team_selection_fragment(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    mock_start(hub.litellm, start_body())
    page = await hub.get("/auth/login")
    login_id = WEB_LOGIN_POLL_RE.search(page.text).group(1)

    mock_poll(hub.litellm, teams_body(("t1", "Команда 1"), ("t2", "Команда 2")))
    fragment = await hub.get(f"/auth/login/poll/{login_id}")
    assert fragment.status_code == 200
    assert "Команда 1" in fragment.text
    assert "Выберите команду" in fragment.text

    mock_poll(hub.litellm, {"status": "pending"}, team_id="t1")
    chosen = await hub.post(f"/auth/login/team/{login_id}", data={"team_id": "t1"})
    assert chosen.status_code == 200
    assert "Ожидаем подтверждения входа" in chosen.text

    missing = await hub.post("/auth/login/team/no-such-login", data={"team_id": "t1"})
    assert missing.status_code == 200
    assert "истекла" in missing.text


@pytest.mark.ac("AC-131")
async def test_oidc_provider_unavailable_shows_error(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    hub.oidc.metadata_status = 503
    response = await hub.get("/auth/login")
    assert response.status_code == 502
    assert html_error_code(response.text) == "oidc_unavailable"


@pytest.mark.ac("AC-131")
async def test_oidc_callback_error_and_missing_code(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    denied = await hub.get("/auth/callback", params={"error": "access_denied"})
    assert denied.status_code == 400
    assert html_error_code(denied.text) == "access_denied"

    params = await _start_oidc_login(hub)
    without_code = await hub.get("/auth/callback", params={"state": params["state"]})
    assert without_code.status_code == 400
    assert html_error_code(without_code.text) == "invalid_request"


@pytest.mark.ac("AC-131")
async def test_oidc_token_without_id_token_fails(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    params = await _start_oidc_login(hub)
    hub.oidc.next_id_token = None
    response = await hub.get("/auth/callback", params={"code": "c", "state": params["state"]})
    assert response.status_code == 400
    assert html_error_code(response.text) == "invalid_id_token"
    assert await fetch_rows(hub.app, "SELECT id FROM sessions") == []


@pytest.mark.ac("AC-131")
async def test_oidc_login_updates_existing_user(make_hub: HubFactory) -> None:
    hub = await _oidc_hub(make_hub)
    await add_key(hub, "sk-old", "u1")
    params = await _start_oidc_login(hub)
    hub.oidc.next_id_token = hub.oidc.make_id_token(
        nonce=params["nonce"], email="new@corp", now=hub.clock.time()
    )
    response = await hub.get("/auth/callback", params={"code": "c", "state": params["state"]})
    assert response.status_code in (302, 303)
    users = await fetch_rows(hub.app, "SELECT user_id, email FROM users")
    assert users == [{"user_id": "u1", "email": "new@corp"}]


@pytest.mark.ac("AC-137")
async def test_logout_without_session_redirects_to_login(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    response = await hub.post("/auth/logout")
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login"


@pytest.mark.ac("AC-136")
async def test_server_card_requires_session(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    response = await hub.get("/ui/servers/gitlab")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/auth/login?next=")


@pytest.mark.ac("AC-136")
async def test_server_card_visible_to_audience_group(make_hub: HubFactory) -> None:
    catalog = catalog_doc([gitlab_facade(), restricted_facade("b"), native_server("tag")])
    hub = await make_hub(catalog=catalog, env=CATALOG_ENV, base_url="https://hub.test")
    await web_login(hub, "u1")
    await execute(hub.app, "UPDATE users SET groups = '[\"devs\"]' WHERE user_id = 'u1'")
    response = await hub.get("/ui/servers/b")
    assert response.status_code == 200
    assert "Внутренний сервер" in response.text
