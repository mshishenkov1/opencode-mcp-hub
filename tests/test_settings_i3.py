"""Настройки ревизии 2 (R-T1..R-T4) и ``deploy/.env.example``: AC-70..AC-73, AC-145, AC-156."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.app import create_app
from hub.crypto import jwt_decode
from hub.errors import ConfigError
from hub.kv import InMemoryKeyValueStore, RedisKeyValueStore
from hub.settings import Settings
from tests.conftest import Hub, HubFactory, base_settings_kwargs
from tests.support import (
    CATALOG_ENV,
    FERNET_KEY,
    LITELLM_URL,
    PUBLIC_URL,
    MockNetwork,
    authorize_params,
    authorize_to_code,
    connected_client,
    exchange_code,
    i3_catalog,
    jsonrpc_body,
    litellm_http_client,
    make_litellm_router,
    mcp_headers,
    mock_start,
    pkce_pair,
    provider_callback,
    record_text,
    register_client,
    submit_consent,
    web_login,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CATALOG = REPO_ROOT / "catalog.yaml"
ENV_EXAMPLE = REPO_ROOT / "deploy" / ".env.example"
REQUIRED_I1_ENV = {
    "HUB_PUBLIC_URL": PUBLIC_URL,
    "HUB_LITELLM_BASE_URL": LITELLM_URL,
    "HUB_SECRET_KEY": "hub-secret",
    "HUB_ENCRYPTION_KEY": FERNET_KEY,
}
REPO_CATALOG_VARS = {
    "TAG_MCP_URL": "https://tag-mcp.test/mcp",
    "TAG_VERIFY_URL": "https://tag.test/api/v4/users/me",
    "TAG_API_BASE": "https://tag.test/api/v4",
    "GITLAB_OAUTH_CLIENT_ID": "gl-client",
    "GITLAB_PLATFORM_OAUTH_CLIENT_ID": "glp-client",
    "JIRA_OAUTH_CLIENT_ID": "jira-client",
    "CONFLUENCE_OAUTH_CLIENT_ID": "conf-client",
}

# Таблица настроек spec.md §10 (R-T1) — все переменные ревизии 2.
I3_SETTINGS_VARS = (
    "HUB_WEB_AUTH",
    "HUB_CONSENT",
    "HUB_OAUTH_ALLOWED_REDIRECTS",
    "HUB_ACCESS_TOKEN_TTL",
    "HUB_REFRESH_TOKEN_TTL",
    "HUB_AUTH_CODE_TTL",
    "HUB_OAUTH_TX_TTL",
    "HUB_WEB_SESSION_TTL",
    "HUB_RATE_LIMIT_REGISTER",
    "HUB_RATE_LIMIT_TOKEN",
    "HUB_RATE_LIMIT_MCP",
    "HUB_TRUST_PROXY",
    "HUB_MAX_SSE_PER_USER",
    "HUB_MAX_BODY_BYTES",
    "HUB_UPSTREAM_TIMEOUT",
    "HUB_UPSTREAM_SSE_IDLE_TIMEOUT",
    "HUB_UPSTREAM_IDLE_TTL",
    "HUB_CLIENT_SESSION_TTL",
    "HUB_TOOLS_CACHE_TTL",
    "HUB_TOKEN_REFRESH_LEAD",
    "HUB_TOKEN_REFRESH_INTERVAL",
    "HUB_TOKEN_REFRESH_ENABLED",
    "HUB_CB_FAILURES",
    "HUB_CB_RESET",
    "HUB_CONNECTION_CACHE_TTL",
    "HUB_DB_AUTO_MIGRATE",
    "KEYCLOAK_ISSUER",
    "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_CLIENT_SECRET",
    "KEYCLOAK_SCOPES",
    "KEYCLOAK_JWKS_TTL",
)


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


def _prepare_env(monkeypatch: pytest.MonkeyPatch, catalog_path: Path, **extra: str) -> None:
    for name, value in REQUIRED_I1_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HUB_CATALOG_PATH", str(catalog_path))
    monkeypatch.setenv("HUB_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    for name in ("KEYCLOAK_ISSUER", "KEYCLOAK_CLIENT_SECRET", "KEYCLOAK_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


# --- AC-70 -----------------------------------------------------------------


@pytest.mark.ac("AC-70")
async def test_default_access_token_ttl_is_one_hour(make_hub: HubFactory) -> None:
    """HUB_ACCESS_TOKEN_TTL по умолчанию 3600: и в ответе, и в claims токена."""
    hub = await _hub(make_hub)
    await web_login(hub)
    client_id = await register_client(hub)
    verifier, challenge = pkce_pair()
    code = await authorize_to_code(hub, client_id, challenge=challenge)
    response = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == 3600
    claims = jwt_decode(body["access_token"], hub.settings.secret_key.get_secret_value())
    assert claims["exp"] - claims["iat"] == 3600


@pytest.mark.ac("AC-70")
async def test_default_consent_is_always(make_hub: HubFactory) -> None:
    """HUB_CONSENT по умолчанию always — экран прав показывается при каждом authorize."""
    hub = await _hub(make_hub)
    assert hub.settings.consent == "always"
    await web_login(hub)
    client_id = await register_client(hub)
    _verifier, challenge = pkce_pair()
    first = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    consent_page = await provider_callback(hub, first.headers["location"])
    assert consent_page.status_code == 200
    assert (await submit_consent(hub, consent_page.text)).status_code == 302

    second = await hub.get("/oauth/authorize", params=authorize_params(client_id, challenge=challenge))
    assert second.status_code == 200, second.text
    assert "Разрешить доступ" in second.text


@pytest.mark.ac("AC-70")
async def test_default_web_auth_is_litellm(make_hub: HubFactory) -> None:
    """HUB_WEB_AUTH по умолчанию litellm: страница входа рендерится через CLI-SSO."""
    hub = await _hub(make_hub)
    assert hub.settings.web_auth == "litellm"
    mock_start(hub.litellm)
    hub.litellm.get("/sso/cli/poll/ll-1").respond(200, json={"status": "pending"})
    response = await hub.get("/auth/login?next=/ui/connections")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ABCD-1234" in response.text


@pytest.mark.ac("AC-70")
async def test_default_tools_cache_ttl_serves_second_call_from_cache(make_hub: HubFactory) -> None:
    """HUB_TOOLS_CACHE_TTL по умолчанию 300 с: второй tools/list не уходит на upstream."""
    hub = await _hub(make_hub)
    assert hub.settings.tools_cache_ttl == 300
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    body = jsonrpc_body("tools/list", request_id=1)
    first = await hub.post("/mcp/gitlab", content=body, headers=headers)
    assert first.status_code == 200, first.text
    hub.clock.advance(299)
    second = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=2), headers=headers
    )
    assert second.status_code == 200
    assert second.json()["id"] == 2
    assert hub.upstream.calls == 1


# --- AC-71 -----------------------------------------------------------------


@pytest.mark.ac("AC-71")
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("HUB_ACCESS_TOKEN_TTL", "0"),
        ("HUB_WEB_AUTH", "bad"),
        ("HUB_CONSENT", "x"),
        ("HUB_OAUTH_ALLOWED_REDIRECTS", "not json"),
        ("HUB_MAX_SSE_PER_USER", "-1"),
    ],
)
async def test_invalid_new_settings_break_start(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, variable: str, value: str
) -> None:
    _prepare_env(monkeypatch, catalog_path, **{variable: value})
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    assert variable in str(excinfo.value)


@pytest.mark.ac("AC-71")
@pytest.mark.parametrize(
    ("variable", "value", "allowed"),
    [("HUB_WEB_AUTH", "bad", ("keycloak", "litellm")), ("HUB_CONSENT", "x", ("always", "remember"))],
)
async def test_choice_settings_list_allowed_values(
    monkeypatch: pytest.MonkeyPatch,
    catalog_path: Path,
    variable: str,
    value: str,
    allowed: tuple[str, ...],
) -> None:
    _prepare_env(monkeypatch, catalog_path, **{variable: value})
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    message = str(excinfo.value)
    for candidate in allowed:
        assert candidate in message, message


@pytest.mark.ac("AC-71")
async def test_valid_new_settings_start(monkeypatch: pytest.MonkeyPatch, catalog_path: Path) -> None:
    """Граница: те же переменные с допустимыми значениями старт не ломают."""
    _prepare_env(
        monkeypatch,
        catalog_path,
        HUB_ACCESS_TOKEN_TTL="1",
        HUB_WEB_AUTH="litellm",
        HUB_CONSENT="remember",
        HUB_OAUTH_ALLOWED_REDIRECTS='["http://127.0.0.1:*"]',
        HUB_MAX_SSE_PER_USER="1",
    )
    app = create_app()
    assert app.state.settings.access_token_ttl == 1
    assert app.state.settings.oauth_allowed_redirects == ["http://127.0.0.1:*"]


# --- AC-72 -----------------------------------------------------------------


@pytest.mark.ac("AC-72")
async def test_keycloak_mode_requires_issuer(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _prepare_env(
        monkeypatch, catalog_path, HUB_WEB_AUTH="keycloak", KEYCLOAK_CLIENT_SECRET="kc-secret"
    )
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    assert "KEYCLOAK_ISSUER" in str(excinfo.value)


@pytest.mark.ac("AC-72")
async def test_keycloak_mode_requires_client_secret(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _prepare_env(
        monkeypatch, catalog_path, HUB_WEB_AUTH="keycloak", KEYCLOAK_ISSUER="https://kc.test/realms/c"
    )
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    assert "KEYCLOAK_CLIENT_SECRET" in str(excinfo.value)


@pytest.mark.ac("AC-72")
async def test_keycloak_mode_with_both_starts(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _prepare_env(
        monkeypatch,
        catalog_path,
        HUB_WEB_AUTH="keycloak",
        KEYCLOAK_ISSUER="https://kc.test/realms/c",
        KEYCLOAK_CLIENT_SECRET="kc-secret",
    )
    app = create_app()
    assert app.state.settings.web_auth == "keycloak"


@pytest.mark.ac("AC-72")
async def test_litellm_mode_needs_no_keycloak_vars(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _prepare_env(monkeypatch, catalog_path, HUB_WEB_AUTH="litellm")
    app = create_app()
    assert app.state.settings.web_auth == "litellm"
    assert app.state.settings.keycloak_issuer == ""


# --- AC-73 -----------------------------------------------------------------


@pytest.mark.ac("AC-73")
async def test_i1_environment_serves_i3_endpoints(
    make_hub: HubFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ни одной новой переменной: каталог репозитория, четыре обязательных переменных I-1.

    Переменные каталога (``${GITLAB_OAUTH_CLIENT_ID}`` и т.п.) — не настройки Hub; без них
    beta-серверы каталога попадают в состояние unconfigured (R-C3), и PRM для них 404 (AC-79).
    """
    for name, value in REPO_CATALOG_VARS.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "repo-catalog.yaml"
    shutil.copy(REPO_CATALOG, path)
    hub: Hub = await make_hub(catalog=None, path=path, base_url="https://hub.test")

    assert (await hub.get("/health")).status_code == 200
    as_meta = await hub.get("/.well-known/oauth-authorization-server")
    assert as_meta.status_code == 200
    assert as_meta.json()["issuer"] == PUBLIC_URL
    prm = await hub.get("/.well-known/oauth-protected-resource/mcp/gitlab")
    assert prm.status_code == 200
    assert prm.json()["resource"] == f"{PUBLIC_URL}/mcp/gitlab"

    wellknown = await hub.get("/.well-known/opencode")
    assert wellknown.status_code == 200
    assert wellknown.headers["Cache-Control"] == "public, max-age=300"
    assert wellknown.headers.get("ETag")
    body = wellknown.json()
    assert body["auth"]["env"] == "MAGNIT_COPILOT_KEY"
    assert body["config"]["$schema"] == "https://opencode.ai/config.json"
    assert body["config"]["enabled_providers"] == ["magnit_prod"]
    provider = body["config"]["provider"]["magnit_prod"]
    assert provider["options"]["baseURL"] == f"{LITELLM_URL}/v1"
    assert provider["options"]["apiKey"] == "{env:MAGNIT_COPILOT_KEY}"
    assert body["remote_config"] == {
        "url": f"{PUBLIC_URL}/remote-config",
        "headers": {"Authorization": "Bearer {env:MAGNIT_COPILOT_KEY}"},
    }
    assert body["config"]["mcp"]["gitlab"] == {
        "type": "remote",
        "url": f"{PUBLIC_URL}/mcp/gitlab",
        "enabled": False,
        "oauth": {},
    }
    assert "env:" not in str(body["config"]["mcp"])


@pytest.mark.ac("AC-73")
async def test_catalog_vars_are_not_hub_settings(
    make_hub: HubFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Переменные каталога не заданы: Hub поднимается, серверы — unconfigured, PRM 404 (R-C3/AC-79).

    Так фиксируется граница из ``given`` AC-73: ``GITLAB_OAUTH_CLIENT_ID`` и подобные — настройки
    серверов каталога, а не Hub; их отсутствие не делает окружение неполным.
    """
    for name in (*REPO_CATALOG_VARS, "KEYCLOAK_ISSUER", "KEYCLOAK_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert not [name for name in os.environ if name.startswith(("HUB_", "KEYCLOAK_"))]

    path = tmp_path / "repo-catalog.yaml"
    shutil.copy(REPO_CATALOG, path)
    hub: Hub = await make_hub(catalog=None, path=path, env={}, base_url="https://hub.test")

    assert (await hub.get("/health")).status_code == 200
    assert (await hub.get("/.well-known/opencode")).status_code == 200
    assert (await hub.get("/.well-known/oauth-authorization-server")).status_code == 200
    prm = await hub.get("/.well-known/oauth-protected-resource/mcp/gitlab")
    assert prm.status_code == 404, prm.text


@pytest.mark.ac("AC-73")
async def test_i1_defaults_of_new_settings(make_hub: HubFactory) -> None:
    """Ни одна переменная ревизии 2 не задана — применяются дефолты таблицы R-T1."""
    hub = await _hub(make_hub)
    s = hub.settings
    assert (s.web_auth, s.consent, s.web_session_ttl) == ("litellm", "always", 28800)
    assert (s.access_token_ttl, s.refresh_token_ttl, s.auth_code_ttl) == (3600, 2592000, 60)
    assert (s.rate_limit_register, s.rate_limit_token, s.rate_limit_mcp) == (10, 60, 120)
    assert (s.max_sse_per_user, s.max_body_bytes) == (4, 1048576)
    assert (s.upstream_timeout, s.upstream_sse_idle_timeout) == (30.0, 300.0)
    assert (s.upstream_idle_ttl, s.client_session_ttl, s.tools_cache_ttl) == (600, 86400, 300)
    assert (s.cb_failures, s.cb_reset, s.connection_cache_ttl) == (5, 30.0, 60.0)
    assert (s.token_refresh_lead, s.token_refresh_interval, s.token_refresh_enabled) == (
        300,
        60.0,
        True,
    )
    assert s.db_auto_migrate is True
    assert s.oauth_allowed_redirects == ["http://127.0.0.1:*", "http://localhost:*"]


# --- AC-145 ----------------------------------------------------------------

ENV_LINE_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=(.*)$")


def _env_example_values() -> dict[str, str]:
    """Переменные из ``deploy/.env.example`` (включая закомментированные) → значение."""
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE_RE.match(raw)
        if match:
            values[match.group(1)] = match.group(2).split("#", 1)[0].strip()
    return values


@pytest.mark.ac("AC-145")
def test_env_example_lists_all_new_settings() -> None:
    names = set(_env_example_values())
    missing = [name for name in I3_SETTINGS_VARS if name not in names]
    assert not missing, f"в deploy/.env.example нет переменных: {missing}"


CATALOG_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _vars_affecting_visibility(node: Any, hidden: bool = False) -> set[str]:
    """``${VAR}`` каталога, без которых сервер выпадает из витрины (R-C3).

    Переменные внутри способа с ``available: false`` пропускаются: их отсутствие сервер не
    прячет (R-U1, уточнение R-C2, поведение закреплено AC-194).
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "auth_methods" and isinstance(value, list):
                for method in value:
                    unavailable = isinstance(method, dict) and method.get("available") is False
                    found |= _vars_affecting_visibility(method, hidden or unavailable)
                continue
            found |= _vars_affecting_visibility(value, hidden)
    elif isinstance(node, list):
        for item in node:
            found |= _vars_affecting_visibility(item, hidden)
    elif isinstance(node, str) and not hidden:
        found |= set(CATALOG_VAR_RE.findall(node))
    return found


@pytest.mark.ac("AC-145")
def test_env_example_lists_every_catalog_variable_that_hides_a_server() -> None:
    """BUG-I3-005: переменная каталога без строки в ``deploy/.env.example`` уносит коннектор.

    ``.env.example`` — образец окружения развёртывания: по нему заполняют ``deploy/.env``.
    Переменная, которой там нет, на новом стенде останется незаданной, сервер каталога станет
    unconfigured и **молча исчезнет** из витрины и из ``/api/catalog`` (R-C3). Именно так
    ``TAG_API_BASE`` (добавлена в ``catalog.yaml`` вместе с блоком ``exchange``) уносит
    коннектор ТЭГ — тот самый, ради которого делались ревизии 4 и 4.1.
    """
    document = yaml.safe_load(REPO_CATALOG.read_text(encoding="utf-8"))
    required = _vars_affecting_visibility(document)
    assert required, "в каталоге репозитория нет ни одной переменной — проверка вырождена"
    documented = set(_env_example_values())
    missing = sorted(required - documented)
    assert not missing, (
        f"в deploy/.env.example нет переменных каталога {missing}: "
        "на новом стенде их серверы исчезнут из витрины"
    )


@pytest.mark.ac("AC-145")
def test_env_example_keeps_secrets_empty() -> None:
    for name, value in _env_example_values().items():
        if "SECRET" in name or "PASSWORD" in name:
            assert value in ("", "change-me") or value.startswith("change-me"), (
                f"{name} в .env.example должен быть пустым или change-me, а не {value!r}"
            )


# --- AC-156 ----------------------------------------------------------------

REDIS_WARNING = "kv_in_memory"


def _kv_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record_text(record)
        for record in caplog.records
        if record.levelname == "WARNING" and record.getMessage() == REDIS_WARNING
    ]


@pytest.mark.ac("AC-156")
async def test_start_without_redis_url_warns_about_unshared_state(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """R-N5: пустой HUB_REDIS_URL — WARNING о том, что реплики не делят состояние."""
    caplog.clear()
    hub = await _hub(make_hub)
    assert hub.settings.redis_url == ""
    assert isinstance(hub.app.state.kv, InMemoryKeyValueStore)

    warnings = _kv_warnings(caplog)
    assert len(warnings) == 1, warnings
    text = warnings[0]
    assert "HUB_REDIS_URL" in text
    for topic in ("denylist", "MCP-сесси", "rate-limit", "circuit-breaker"):
        assert topic in text, f"в предупреждении нет упоминания {topic}: {text}"

    # Приложение при этом создаётся и обслуживает запросы.
    assert (await hub.get("/health")).status_code == 200
    assert (await hub.get("/api/catalog", headers={"Authorization": "Bearer nope"})).status_code == 401


@pytest.mark.ac("AC-156")
async def test_start_with_redis_url_does_not_warn(
    catalog_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        **base_settings_kwargs(catalog_path, redis_url="redis://redis.test:6379/0")
    )
    litellm = litellm_http_client(make_litellm_router())
    outbound = MockNetwork().client()
    caplog.clear()
    try:
        app = create_app(settings, litellm_client=litellm, http_client=outbound)
        assert isinstance(app.state.kv, RedisKeyValueStore)
        assert _kv_warnings(caplog) == []
    finally:
        await litellm.aclose()
        await outbound.aclose()
