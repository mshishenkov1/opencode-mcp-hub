"""Конфигурация (R-K1..R-K4): AC-01..AC-06."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hub.app import create_app
from hub.errors import ConfigError
from hub.settings import Settings
from tests.conftest import Hub, HubFactory
from tests.support import (
    FERNET_KEY,
    LITELLM_URL,
    PUBLIC_URL,
    litellm_http_client,
    make_jwt,
    make_litellm_router,
    mock_key_generate,
    mock_poll,
    mock_start,
    ready_body,
    record_text,
)

REQUIRED = ["HUB_PUBLIC_URL", "HUB_LITELLM_BASE_URL", "HUB_SECRET_KEY", "HUB_ENCRYPTION_KEY"]


def _set_required_env(monkeypatch: pytest.MonkeyPatch, catalog_path: Path, **extra: str) -> None:
    monkeypatch.setenv("HUB_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("HUB_LITELLM_BASE_URL", LITELLM_URL)
    monkeypatch.setenv("HUB_SECRET_KEY", "S3CR3T-HUB")
    monkeypatch.setenv("HUB_ENCRYPTION_KEY", FERNET_KEY)
    monkeypatch.setenv("HUB_CATALOG_PATH", str(catalog_path))
    monkeypatch.setenv("HUB_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


@pytest.mark.ac("AC-01")
@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_env_var_fails_start_with_name(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, missing: str
) -> None:
    _set_required_env(monkeypatch, catalog_path)
    monkeypatch.delenv(missing)
    with pytest.raises(Exception) as excinfo:
        create_app()
    assert missing in str(excinfo.value)


@pytest.mark.ac("AC-01")
def test_all_required_env_vars_present_app_starts(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _set_required_env(monkeypatch, catalog_path)
    app = create_app()
    assert app is not None


@pytest.mark.ac("AC-02")
@pytest.mark.parametrize("bad_key", ["not-a-key", "", "x" * 44, "QUJD" * 11])
def test_invalid_fernet_key_rejected(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, bad_key: str
) -> None:
    _set_required_env(monkeypatch, catalog_path, HUB_ENCRYPTION_KEY=bad_key)
    with pytest.raises(Exception) as excinfo:
        create_app()
    assert "HUB_ENCRYPTION_KEY" in str(excinfo.value)


@pytest.mark.ac("AC-02")
def test_valid_fernet_key_accepted(monkeypatch: pytest.MonkeyPatch, catalog_path: Path) -> None:
    from cryptography.fernet import Fernet

    _set_required_env(monkeypatch, catalog_path, HUB_ENCRYPTION_KEY=Fernet.generate_key().decode())
    assert create_app() is not None


@pytest.mark.ac("AC-03")
async def test_defaults_visible_in_wellknown(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _set_required_env(monkeypatch, catalog_path)
    app = create_app(litellm_client=litellm_http_client(make_litellm_router()))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
        ) as client,
    ):
        resp = await client.get("/.well-known/opencode")
    assert resp.status_code == 200
    body = resp.json()
    config = body["config"]
    assert config["enabled_providers"] == ["magnit_prod"]
    provider = config["provider"]["magnit_prod"]
    assert provider["name"] == "LiteLLM Copilot prod"
    assert provider["models"]["MagnitCopilot"]["limit"] == {"context": 250000, "output": 8192}
    assert body["auth"]["env"] == "MAGNIT_COPILOT_KEY"


@pytest.mark.ac("AC-03")
def test_default_setting_values(monkeypatch: pytest.MonkeyPatch, catalog_path: Path) -> None:
    _set_required_env(monkeypatch, catalog_path)
    monkeypatch.delenv("HUB_DATABASE_URL")
    monkeypatch.delenv("HUB_CATALOG_PATH")
    settings = Settings()
    assert settings.litellm_model == "MagnitCopilot"
    assert settings.catalog_path == "./catalog.yaml"
    assert settings.database_url == "sqlite+aiosqlite:///./hub.db"
    assert settings.redis_url == ""
    assert settings.wellknown_env_name == "MAGNIT_COPILOT_KEY"
    assert settings.login_session_ttl == 600
    assert settings.key_alias_prefix == "opencode"
    assert settings.log_level == "INFO"


@pytest.mark.ac("AC-04")
async def test_auth_command_default_with_public_url_substitution(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _set_required_env(monkeypatch, catalog_path, HUB_PUBLIC_URL="https://hub.test/")
    app = create_app(litellm_client=litellm_http_client(make_litellm_router()))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
        ) as client,
    ):
        resp = await client.get("/.well-known/opencode")
    assert resp.json()["auth"]["command"] == [
        "opencode",
        "corp",
        "login",
        "--hub",
        "https://hub.test",
    ]


@pytest.mark.ac("AC-04")
def test_auth_command_custom_json_array_with_placeholder(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _set_required_env(
        monkeypatch,
        catalog_path,
        HUB_WELLKNOWN_AUTH_COMMAND='["mycli", "login", "<HUB_PUBLIC_URL>/x"]',
    )
    settings = Settings()
    assert settings.wellknown_auth_command == ["mycli", "login", f"{PUBLIC_URL}/x"]


@pytest.mark.ac("AC-04")
@pytest.mark.parametrize("value", ["not json", "[]", '"str"', "[1, 2]", "{}"])
def test_auth_command_invalid_json_fails_start(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, value: str
) -> None:
    _set_required_env(monkeypatch, catalog_path, HUB_WELLKNOWN_AUTH_COMMAND=value)
    with pytest.raises(Exception) as excinfo:
        create_app()
    assert "HUB_WELLKNOWN_AUTH_COMMAND" in str(excinfo.value)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("HUB_LOGIN_SESSION_TTL", "0"),
        ("HUB_LOGIN_SESSION_TTL", "-5"),
        ("HUB_LITELLM_CONTEXT_LIMIT", "0"),
        ("HUB_LITELLM_OUTPUT_LIMIT", "abc"),
        ("HUB_LOG_LEVEL", "VERBOSE"),
    ],
)
@pytest.mark.ac("AC-01")
def test_invalid_numeric_or_level_settings_fail_with_var_name(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, var: str, value: str
) -> None:
    _set_required_env(monkeypatch, catalog_path, **{var: value})
    with pytest.raises(Exception) as excinfo:
        create_app()
    assert var in str(excinfo.value)


@pytest.mark.ac("AC-05")
async def test_secrets_never_logged(make_hub: HubFactory, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hub: Hub = await make_hub(secret_key="S3CR3T-HUB", admin_token="ADM-SECRET-TOKEN")
    jwt = make_jwt({"sub": "u1", "email": "u1@corp.test", "exp": int(hub.clock.time()) + 3600})
    mock_start(hub.litellm)
    mock_poll(hub.litellm, ready_body(jwt))
    mock_key_generate(hub.litellm, "sk-test-abc")

    start = (await hub.post("/cli/start", json={"client": "opencode-fork/1.17.9"})).json()
    ready = await hub.poll(start["login_id"], start["poll_secret"])
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "ADM-SECRET-TOKEN"})

    secrets = [
        FERNET_KEY,
        "GL_SECRET",
        "S3CR3T-HUB",
        "sk-test-abc",
        jwt,
        start["poll_secret"],
        "ll-secret",
        "ADM-SECRET-TOKEN",
    ]
    hub_records = [r for r in caplog.records if r.name == "hub" or r.name.startswith("hub.")]
    assert hub_records, "ожидались записи логов приложения"
    for record in caplog.records:
        text = record_text(record)
        for secret in secrets:
            assert secret not in text, f"секрет {secret!r} попал в лог: {text}"


@pytest.mark.ac("AC-06")
async def test_create_app_with_settings_object_without_env(catalog_path: Path) -> None:
    settings = Settings(
        public_url=PUBLIC_URL,
        litellm_base_url=LITELLM_URL,
        secret_key="s",
        encryption_key=FERNET_KEY,
        catalog_path=str(catalog_path),
        database_url="sqlite+aiosqlite:///:memory:",
    )
    app = create_app(settings=settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
        ) as client,
    ):
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.ac("AC-06")
def test_settings_object_missing_required_field_fails() -> None:
    with pytest.raises(Exception) as excinfo:
        Settings(public_url=PUBLIC_URL, litellm_base_url=LITELLM_URL, secret_key="s")
    assert "HUB_ENCRYPTION_KEY" in str(excinfo.value)


@pytest.mark.ac("AC-01")
@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("HUB_PUBLIC_URL", "/"),
        ("HUB_PUBLIC_URL", "   "),
        ("HUB_LITELLM_BASE_URL", ""),
        ("HUB_SECRET_KEY", ""),
    ],
)
def test_empty_required_values_fail_with_var_name(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, var: str, value: str
) -> None:
    _set_required_env(monkeypatch, catalog_path, **{var: value})
    with pytest.raises(Exception) as excinfo:
        create_app()
    assert var in str(excinfo.value)


@pytest.mark.ac("AC-02")
def test_fernet_key_with_invalid_base64_chars_rejected(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _set_required_env(monkeypatch, catalog_path, HUB_ENCRYPTION_KEY="!" * 44)
    with pytest.raises(Exception) as excinfo:
        create_app()
    assert "HUB_ENCRYPTION_KEY" in str(excinfo.value)


@pytest.mark.ac("AC-04")
def test_urls_normalized_without_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    _set_required_env(
        monkeypatch,
        catalog_path,
        HUB_PUBLIC_URL="https://hub.test///",
        HUB_LITELLM_BASE_URL="https://litellm.test/",
    )
    settings = Settings()
    assert settings.public_url == "https://hub.test"
    assert settings.litellm_base_url == "https://litellm.test"
    assert settings.wellknown_auth_command[-1] == "https://hub.test"


@pytest.mark.ac("AC-06")
async def test_litellm_client_injectable_via_app_state(catalog_path: Path) -> None:
    """R-K4: HTTP-клиент к LiteLLM подменяется через атрибут состояния приложения."""
    settings = Settings(
        public_url=PUBLIC_URL,
        litellm_base_url=LITELLM_URL,
        secret_key="s",
        encryption_key=FERNET_KEY,
        catalog_path=str(catalog_path),
        database_url="sqlite+aiosqlite:///:memory:",
    )
    app = create_app(settings=settings)
    router = make_litellm_router()
    start_route = mock_start(router)
    app.state.litellm_client = litellm_http_client(router)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
        ) as client,
    ):
        resp = await client.post("/cli/start", json={"client": "c"})
    assert resp.status_code == 200
    assert start_route.call_count == 1


# --- Усиление после mutation-прогона -----------------------------------------

FERNET_ERROR = (
    "HUB_ENCRYPTION_KEY: ожидается ключ Fernet — 44 символа urlsafe-base64, "
    "декодирующиеся в 32 байта"
)


@pytest.mark.ac("AC-01")
def test_missing_required_var_message_is_exact(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    """Сообщение об отсутствующей переменной называет её и причину дословно (R-K1)."""
    _set_required_env(monkeypatch, catalog_path)
    monkeypatch.delenv("HUB_PUBLIC_URL")
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    assert str(excinfo.value) == (
        "Ошибка конфигурации Hub: HUB_PUBLIC_URL: обязательная переменная не задана"
    )


@pytest.mark.ac("AC-01")
def test_two_missing_required_vars_are_listed_through_semicolon(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path
) -> None:
    """Несколько ошибок конфигурации перечисляются через `; ` в одном сообщении."""
    _set_required_env(monkeypatch, catalog_path)
    monkeypatch.delenv("HUB_PUBLIC_URL")
    monkeypatch.delenv("HUB_SECRET_KEY")
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    assert str(excinfo.value) == (
        "Ошибка конфигурации Hub: "
        "HUB_PUBLIC_URL: обязательная переменная не задана; "
        "HUB_SECRET_KEY: обязательная переменная не задана"
    )


@pytest.mark.ac("AC-02")
@pytest.mark.parametrize(
    "bad_key",
    ["not-a-key", "", "x" * 44, "QUJD" * 11, "!" * 44],
    ids=["short", "empty", "not-base64", "wrong-length-decoded", "non-ascii-alphabet"],
)
def test_fernet_key_error_message_is_exact(
    monkeypatch: pytest.MonkeyPatch, catalog_path: Path, bad_key: str
) -> None:
    """Ошибка ключа шифрования формулируется одинаково для всех причин (R-K2).

    Сообщение пользовательского валидатора уже содержит имя переменной, поэтому имя
    не дублируется префиксом.
    """
    _set_required_env(monkeypatch, catalog_path, HUB_ENCRYPTION_KEY=bad_key)
    with pytest.raises(ConfigError) as excinfo:
        create_app()
    assert str(excinfo.value) == f"Ошибка конфигурации Hub: {FERNET_ERROR}"
