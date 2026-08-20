"""Настройки Hub: переменные окружения ``HUB_*`` (R-K1..R-K3)."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from hub.errors import ConfigError

ENV_PREFIX = "HUB_"
PUBLIC_URL_PLACEHOLDER = "<HUB_PUBLIC_URL>"
DEFAULT_AUTH_COMMAND = ["opencode", "corp", "login", "--hub", PUBLIC_URL_PLACEHOLDER]

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

# R-T1: переменные Keycloak читаются без префикса HUB_ (решение 59).
KEYCLOAK_PREFIX = "keycloak_"
WEB_AUTH_VALUES = ("keycloak", "litellm")
CONSENT_VALUES = ("always", "remember")
DEFAULT_ALLOWED_REDIRECTS = ["http://127.0.0.1:*", "http://localhost:*"]


def env_name(field: str) -> str:
    """Имя переменной окружения для поля настроек: ``public_url`` → ``HUB_PUBLIC_URL``.

    Поля ``keycloak_*`` читаются без префикса ``HUB_`` (R-T1): ``keycloak_issuer`` → ``KEYCLOAK_ISSUER``.
    """
    if field.startswith(KEYCLOAK_PREFIX):
        return field.upper()
    return ENV_PREFIX + field.upper()


def _keycloak(field: str) -> AliasChoices:
    return AliasChoices(field.upper())


def _strip_slash(url: str) -> str:
    return url.strip().rstrip("/")


def _check_choice(value: str, field: str, allowed: tuple[str, ...]) -> str:
    """Значение из перечня; иначе — ошибка с именем переменной и перечнем допустимых (R-T2)."""
    candidate = value.strip().lower()
    if candidate not in allowed:
        raise ValueError(f"{env_name(field)}: допустимые значения — {', '.join(allowed)}")
    return candidate


def _validate_fernet_key(value: str) -> str:
    err = (
        f"{env_name('encryption_key')}: ожидается ключ Fernet — 44 символа urlsafe-base64, "
        "декодирующиеся в 32 байта"
    )
    if len(value) != 44:
        raise ValueError(err)
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(err) from exc
    if len(raw) != 32:
        raise ValueError(err)
    return value


class Settings(BaseSettings):
    """Настройки приложения. Читаются из окружения с префиксом ``HUB_``.

    При программном создании (``Settings(public_url=..., ...)``) окружение по-прежнему
    учитывается для незаданных полей, но обязательные значения можно передать явно.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        case_sensitive=False,
        env_file=None,
        validate_default=True,
        populate_by_name=True,
    )

    # --- обязательные ---
    public_url: str
    litellm_base_url: str
    secret_key: SecretStr
    encryption_key: SecretStr

    # --- LiteLLM / провайдер OpenCode ---
    litellm_model: str = "MagnitCopilot"
    litellm_provider_id: str = "magnit_prod"
    litellm_provider_name: str = "LiteLLM Copilot prod"
    litellm_context_limit: int = Field(default=250000, gt=0)
    litellm_output_limit: int = Field(default=8192, gt=0)
    litellm_timeout: float = Field(default=10.0, gt=0)

    # --- каталог, хранилища ---
    catalog_path: str = "./catalog.yaml"
    database_url: str = "sqlite+aiosqlite:///./hub.db"
    redis_url: str = ""
    admin_token: SecretStr | None = None

    # --- well-known ---
    wellknown_auth_command: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_AUTH_COMMAND)
    )
    wellknown_env_name: str = "MAGNIT_COPILOT_KEY"

    # --- вход ---
    login_session_ttl: int = Field(default=600, gt=0)
    key_alias_prefix: str = "opencode"
    log_level: str = "INFO"

    # --- I-3: веб-интерфейс и экран прав (R-T1) ---
    web_auth: str = "litellm"
    consent: str = "always"
    web_session_ttl: int = Field(default=28800, gt=0)

    # --- I-3: Hub как authorization server ---
    oauth_allowed_redirects: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_REDIRECTS)
    )
    access_token_ttl: int = Field(default=3600, gt=0)
    refresh_token_ttl: int = Field(default=2592000, gt=0)
    auth_code_ttl: int = Field(default=60, gt=0)
    oauth_tx_ttl: int = Field(default=600, gt=0)
    rate_limit_register: int = Field(default=10, gt=0)
    rate_limit_token: int = Field(default=60, gt=0)
    # Доверять ли X-Forwarded-For при вычислении IP для лимитов (по умолчанию — нет, R-O3).
    trust_proxy: bool = False

    # --- I-3: MCP-proxy ---
    rate_limit_mcp: int = Field(default=120, gt=0)
    max_sse_per_user: int = Field(default=4, gt=0)
    max_body_bytes: int = Field(default=1048576, gt=0)
    upstream_timeout: float = Field(default=30.0, gt=0)
    upstream_sse_idle_timeout: float = Field(default=300.0, gt=0)
    upstream_idle_ttl: int = Field(default=600, gt=0)
    client_session_ttl: int = Field(default=86400, gt=0)
    tools_cache_ttl: int = Field(default=300, gt=0)
    cb_failures: int = Field(default=5, gt=0)
    cb_reset: float = Field(default=30.0, gt=0)
    # Запас к TTL права на пробу поверх самого долгого возможного запроса (H5-1).
    cb_probe_grace: float = Field(default=5.0, gt=0)
    connection_cache_ttl: float = Field(default=60.0, gt=0)

    # --- I-3: брокер токенов целевых систем ---
    token_refresh_lead: int = Field(default=300, gt=0)
    token_refresh_interval: float = Field(default=60.0, gt=0)
    token_refresh_enabled: bool = True

    # --- I-3: миграции ---
    db_auto_migrate: bool = True

    # --- I-3: OIDC (Keycloak); имена переменных — без префикса HUB_ ---
    keycloak_issuer: str = Field(default="", validation_alias=_keycloak("keycloak_issuer"))
    keycloak_client_id: str = Field(
        default="opencode-mcp-hub", validation_alias=_keycloak("keycloak_client_id")
    )
    keycloak_client_secret: SecretStr | None = Field(
        default=None, validation_alias=_keycloak("keycloak_client_secret")
    )
    keycloak_scopes: str = Field(
        default="openid profile email", validation_alias=_keycloak("keycloak_scopes")
    )
    keycloak_jwks_ttl: float = Field(
        default=3600.0, gt=0, validation_alias=_keycloak("keycloak_jwks_ttl")
    )

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(exc)) from exc

    # --- валидаторы полей ---

    @field_validator("public_url", "litellm_base_url", mode="after")
    @classmethod
    def _normalize_urls(cls, value: str) -> str:
        value = _strip_slash(value)
        if not value:
            raise ValueError("значение не может быть пустым")
        return value

    @field_validator("secret_key", mode="after")
    @classmethod
    def _secret_key_not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError(f"{env_name('secret_key')}: значение не может быть пустым")
        return value

    @field_validator("encryption_key", mode="after")
    @classmethod
    def _check_encryption_key(cls, value: SecretStr) -> SecretStr:
        _validate_fernet_key(value.get_secret_value())
        return value

    @field_validator("admin_token", mode="after")
    @classmethod
    def _empty_admin_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            return None
        return value

    @field_validator("wellknown_auth_command", mode="before")
    @classmethod
    def _parse_auth_command(cls, value: Any) -> Any:
        name = env_name("wellknown_auth_command")
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{name}: ожидается JSON-массив строк") from exc
            value = parsed
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"{name}: ожидается непустой JSON-массив строк")
        return value

    @field_validator("log_level", mode="after")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(
                f"{env_name('log_level')}: допустимые значения — {', '.join(sorted(_LOG_LEVELS))}"
            )
        return level

    @field_validator("web_auth", mode="after")
    @classmethod
    def _check_web_auth(cls, value: str) -> str:
        return _check_choice(value, "web_auth", WEB_AUTH_VALUES)

    @field_validator("consent", mode="after")
    @classmethod
    def _check_consent(cls, value: str) -> str:
        return _check_choice(value, "consent", CONSENT_VALUES)

    @field_validator("oauth_allowed_redirects", mode="before")
    @classmethod
    def _parse_allowed_redirects(cls, value: Any) -> Any:
        name = env_name("oauth_allowed_redirects")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{name}: ожидается JSON-массив строк") from exc
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise ValueError(f"{name}: ожидается непустой JSON-массив непустых строк")
        return value

    @field_validator("keycloak_client_secret", mode="after")
    @classmethod
    def _empty_keycloak_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            return None
        return value

    @model_validator(mode="after")
    def _check_keycloak_required(self) -> Settings:
        """R-T2: при ``HUB_WEB_AUTH=keycloak`` обязательны issuer и секрет клиента."""
        if self.web_auth != "keycloak":
            return self
        if not self.keycloak_issuer.strip():
            raise ValueError(
                f"{env_name('keycloak_issuer')}: обязательная переменная при "
                f"{env_name('web_auth')}=keycloak"
            )
        if self.keycloak_client_secret is None:
            raise ValueError(
                f"{env_name('keycloak_client_secret')}: обязательная переменная при "
                f"{env_name('web_auth')}=keycloak"
            )
        return self

    @model_validator(mode="after")
    def _substitute_public_url(self) -> Settings:
        self.wellknown_auth_command = [
            item.replace(PUBLIC_URL_PLACEHOLDER, self.public_url)
            for item in self.wellknown_auth_command
        ]
        return self

    # --- удобные свойства ---

    @property
    def admin_enabled(self) -> bool:
        return self.admin_token is not None

    @property
    def log_level_int(self) -> int:
        return logging.getLevelName(self.log_level)  # type: ignore[no-any-return]

    def is_admin_token(self, candidate: str | None) -> bool:
        import hmac

        if self.admin_token is None or candidate is None:
            return False
        return hmac.compare_digest(self.admin_token.get_secret_value(), candidate)


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        msg = str(err.get("msg", ""))
        # Пользовательские ValueError уже начинаются с имени переменной.
        msg = msg.removeprefix("Value error, ")
        if not loc:
            # Ошибка model_validator (например, обязательные KEYCLOAK_*) — текст уже содержит имя.
            parts.append(msg)
            continue
        field = str(loc[0])
        name = env_name(field)
        if err.get("type") == "missing":
            msg = "обязательная переменная не задана"
        if name in msg:
            parts.append(msg)
        else:
            parts.append(f"{name}: {msg}")
    return "Ошибка конфигурации Hub: " + "; ".join(parts)


def load_settings(**overrides: Any) -> Settings:
    """Прочитать настройки из окружения (с необязательными переопределениями)."""
    return Settings(**overrides)


__all__ = [
    "CONSENT_VALUES",
    "DEFAULT_ALLOWED_REDIRECTS",
    "DEFAULT_AUTH_COMMAND",
    "WEB_AUTH_VALUES",
    "Settings",
    "env_name",
    "load_settings",
]
