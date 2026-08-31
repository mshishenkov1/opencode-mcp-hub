"""Каталог MCP-серверов: загрузка ``catalog.yaml``, ``$ref``, ``${VAR}``, ``env:VAR``, схема (R-C*)."""

from __future__ import annotations

import copy
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    StrictBool,
    ValidationError,
    model_validator,
)

from hub.errors import CatalogError

logger = logging.getLogger("hub.catalog")

# Spec 1.1, R-C1: alias — 1–32 символа, ^[a-z][a-z0-9-]{0,31}$ (односимвольные alias из AC-54/AC-55 валидны).
ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# CSS-цвет подложки значка-монограммы карточки: #RRGGBB, шесть шестнадцатеричных цифр.
ICON_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# R-U1: идентификатор способа подключения в auth_methods.
AUTH_METHOD_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_REF_RE = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
REF_RE = re.compile(r"^#/servers/([^/]+)/([^/]+)$")
# R-P2: шаблоны в значениях заголовков; допустим только {{access_token}}.
TEMPLATE_RE = re.compile(r"\{\{([^}]*)\}\}")

ServerStatus = Literal["beta", "ga", "deprecated"]
ServerMode = Literal["native", "facade"]
Preset = Literal["readonly", "readwrite", "none"]

# R-U1: способ подключения по умолчанию для сервера, объявившего прежнее поле auth.
DEFAULT_OAUTH_METHOD_ID = "oauth2"
DEFAULT_OAUTH_METHOD_TITLE = "Корпоративная авторизация"
# Потолок длины пользовательского токена (R-U2, решение 63).
TOKEN_MAX_LENGTH = 4096

# R-C7: сквозной узел карточки — Hub его не читает, не подставляет и не раскрывает.
PASSTHROUGH_GROUPS_FIELD = "permission_groups"
# R-C7.5 (решение 106): каталог словарей разрешений рядом с файлом каталога (HUB_CATALOG_PATH).
PERMISSIONS_DIR_NAME = "permissions"


class EnvRef:
    """Ссылка на секрет в переменной окружения. Значение читается лениво; наружу не сериализуется."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def get(self, environ: Mapping[str, str] | None = None) -> str | None:
        env = os.environ if environ is None else environ
        return env.get(self._name)

    def __repr__(self) -> str:  # pragma: no cover - тривиально
        return "EnvRef(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EnvRef) and other._name == self._name

    def __hash__(self) -> int:
        return hash(("EnvRef", self._name))


class Secret:
    """Литеральное секретное значение из каталога (например, ``client_secret``). Не сериализуется."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self, environ: Mapping[str, str] | None = None) -> str | None:
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - тривиально
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


def _parse_secret(value: Any) -> Secret | EnvRef:
    if isinstance(value, EnvRef | Secret):
        return value
    if not isinstance(value, str):
        raise ValueError("ожидается строка")  # noqa: TRY004 - pydantic ловит только ValueError
    m = ENV_REF_RE.match(value)
    if m:
        return EnvRef(m.group(1))
    return Secret(value)


def _parse_header_value(value: Any) -> str | EnvRef:
    if isinstance(value, EnvRef):
        return value
    if not isinstance(value, str):
        raise ValueError("ожидается строка")  # noqa: TRY004 - pydantic ловит только ValueError
    m = ENV_REF_RE.match(value)
    if m:
        return EnvRef(m.group(1))
    return value


SecretValue = Annotated[Secret | EnvRef, PlainValidator(_parse_secret)]
HeaderValue = Annotated[str | EnvRef, PlainValidator(_parse_header_value)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False, arbitrary_types_allowed=True)


class HeaderGroup(_Strict):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    preset: Preset
    # R-P8: необязательные маски инструментов группы (отсутствие поля = группа не расширяет allow).
    tools: list[str] | None = None


class ToolMasks(_Strict):
    """Маски фильтра инструментов сервера (R-P8): необязательные поля каталога."""

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class PermissionHeaderGroups(_Strict):
    kind: Literal["header_groups"]
    header: str = Field(min_length=1)
    always: list[str] = Field(default_factory=list)
    groups: list[HeaderGroup] = Field(min_length=1)
    # R-P8: необязательный общий фильтр инструментов сервера.
    tool_filter: ToolMasks | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> PermissionHeaderGroups:
        seen: set[str] = set()
        for g in self.groups:
            if g.id in seen:
                raise ValueError(f"groups: id '{g.id}' повторяется")
            seen.add(g.id)
        return self


class PermissionConsent(_Strict):
    kind: Literal["consent"]
    presets: dict[str, dict[str, Any]] = Field(min_length=1)


class ToolFilterPreset(_Strict):
    tools: list[str]


class PermissionToolFilter(_Strict):
    kind: Literal["tool_filter"]
    presets: dict[str, ToolFilterPreset] = Field(min_length=1)


PermissionModel = Annotated[
    PermissionHeaderGroups | PermissionConsent | PermissionToolFilter,
    Field(discriminator="kind"),
]
_PERMISSION_KINDS = {"header_groups", "consent", "tool_filter"}


class AuthScopes(_Strict):
    readonly: list[str]
    readwrite: list[str]


class _AuthMethodCommon(_Strict):
    """Общие поля способа подключения (R-U1).

    У прежнего блока ``auth`` их нет: он эквивалентен списку из одного способа, поэтому
    ``id``/``title`` необязательны на уровне схемы и проверяются для ``auth_methods`` отдельно.
    """

    id: str | None = None
    title: str | None = None
    available: bool = True
    unavailable_reason: str | None = None


class AuthOAuth2(_AuthMethodCommon):
    type: Literal["oauth2"]
    authorize_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    revoke_url: str | None = None
    client_id: str = Field(min_length=1)
    client_secret: SecretValue
    pkce: bool
    scopes: AuthScopes


class TokenField(_Strict):
    """Описание поля ввода пользовательского токена (R-U2)."""

    label: str = Field(min_length=1)
    hint: str | None = None
    docs_url: str | None = None
    secret: bool = True
    placeholder: str | None = None
    min_length: int = Field(default=1, ge=1, le=TOKEN_MAX_LENGTH)
    max_length: int = Field(default=TOKEN_MAX_LENGTH, ge=1, le=TOKEN_MAX_LENGTH)


class TokenVerify(_Strict):
    """Проверочный запрос к целевой системе перед сохранением токена (R-U3)."""

    url: str = Field(min_length=1)
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, HeaderValue] = Field(min_length=1)
    expect_status: int | None = None
    account_field: str | None = None
    # R-U3.1: требовать, чтобы целевая система назвала владельца токена полем account_field.
    require_account: StrictBool = False

    @model_validator(mode="after")
    def _require_account_needs_field(self) -> TokenVerify:
        """R-U3.1: требовать нечего, если не сказано, какое поле читать."""
        if self.require_account and not self.account_field:
            raise _PathError(
                ("require_account",),
                "требование назвать аккаунт невозможно без account_field",
            )
        return self


class TokenExchangeRevoke(_Strict):
    """Запрос отзыва выпущенного Hub'ом постоянного токена (R-U12).

    Обязателен при наличии ``exchange``: Hub не выпускает того, что не умеет отозвать.
    """

    url: str = Field(min_length=1)
    method: Literal["POST", "DELETE"] = "POST"
    headers: dict[str, HeaderValue] = Field(min_length=1)
    body: dict[str, str] | None = None
    expect_status: int = 200


class TokenExchangeList(_Strict):
    """Запрос списка ранее выпущенных токенов — для уборки «сирот» (R-U12, R-U15.3)."""

    url: str = Field(min_length=1)
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, HeaderValue] = Field(min_length=1)
    expect_status: int = 200
    items_field: str | None = None
    id_field: str = Field(min_length=1)
    description_field: str = Field(min_length=1)


class TokenExchange(_Strict):
    """Обмен присланного пользователем токена на постоянный личный токен системы (R-U12)."""

    url: str = Field(min_length=1)
    method: Literal["POST", "GET", "PUT"] = "POST"
    headers: dict[str, HeaderValue] = Field(min_length=1)
    body: dict[str, str] | None = None
    expect_status: int = 200
    token_field: str = Field(min_length=1)
    token_id_field: str = Field(min_length=1)
    # R-U15.1: постоянная часть маркера «свой токен»; длина ограничена, чтобы маркер был
    # сопоставим побайтово и после усечения по 255 символам.
    description: str = Field(min_length=1, max_length=100)
    revoke: TokenExchangeRevoke
    list: TokenExchangeList | None = None


class TokenExpiry(_Strict):
    """Срок годности **присланного** токена (R-U18): сиблинг ``verify`` и ``exchange``."""

    url: str = Field(min_length=1)
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, HeaderValue] = Field(min_length=1)
    expect_status: int = 200
    items_field: str | None = None
    expires_field: str = Field(min_length=1)
    expires_unit: Literal["ms", "s", "iso8601"] = "ms"


class AuthUserToken(_AuthMethodCommon):
    """Учётные данные вводит сам пользователь (R-U1, R-U2, R-U3, R-U12, R-U18)."""

    type: Literal["user_token"]
    field: TokenField
    verify: TokenVerify
    # R-U12: необязательный обмен присланного токена на постоянный; отсутствие блока —
    # прежнее поведение целиком.
    exchange: TokenExchange | None = None
    # R-U18: необязательное чтение срока годности присланного токена.
    expiry: TokenExpiry | None = None


AuthMethod = Annotated[AuthOAuth2 | AuthUserToken, Field(discriminator="type")]
_AUTH_METHOD_TYPES = {"oauth2", "user_token"}


class ServerModel(_Strict):
    alias: str = Field(pattern=ALIAS_RE.pattern)
    title: str = Field(min_length=1)
    # Короткая суть коннектора одной строкой — то, что страница показывает под названием.
    # Необязательное: карточка без него грузится и публикуется ровно как раньше (AC-22).
    summary: str | None = Field(default=None, min_length=1)
    description: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    contact: str | None = None
    docs_url: str | None = None
    icon: str | None = None
    # CSS-цвет подложки значка-монограммы: #RRGGBB. Необязательное: карточка без него
    # грузится и публикуется ровно как раньше (AC-22).
    icon_color: str | None = Field(default=None, pattern=ICON_COLOR_RE.pattern)
    status: ServerStatus
    audience: list[str] = Field(min_length=1)
    mode: ServerMode
    mcp_url: str | None = None
    upstream_url: str | None = None
    auth: AuthOAuth2 | None = None
    auth_methods: list[AuthMethod] | None = None
    credential_headers: dict[str, HeaderValue] | None = None
    static_headers: dict[str, HeaderValue] | None = None
    permission_model: PermissionModel
    # R-C7.1 (решения 104, 105): сквозные поля карточки. Hub — транспорт: внутрь
    # ``permission_groups`` схема не заглядывает (проверено только «это mapping»), у ``type`` нет
    # закрытого набора значений. Ни одно правило Hub эти поля не читает (R-C7.4).
    permission_groups: Any = None
    type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_passthrough_raw(cls, data: Any) -> Any:
        """R-C7.1: ``permission_groups`` — mapping, ``type`` — непустая строка; больше ничего.

        Проверка идёт по сырому узлу: явный ``null`` — это заданное значение неверного типа, а не
        отсутствие поля, и отличить одно от другого после разбора уже нельзя.
        """
        if not isinstance(data, dict):
            return data
        if "permission_groups" in data and not isinstance(data["permission_groups"], dict):
            raise _PathError(("permission_groups",), "ожидается объект")
        if "type" in data:
            value = data["type"]
            if not isinstance(value, str):
                raise _PathError(("type",), "ожидается строка")
            if not value:
                raise _PathError(("type",), "значение не может быть пустым")
        return data

    @model_validator(mode="before")
    @classmethod
    def _check_auth_methods_raw(cls, data: Any) -> Any:
        """R-U1: правила ``auth_methods``, которые проверяются до разбора самих способов."""
        if not isinstance(data, dict):
            return data
        methods = data.get("auth_methods")
        if methods is None:
            return data
        if data.get("auth") is not None:
            raise _PathError(("auth_methods",), "нельзя задавать вместе с auth (поля взаимоисключающи)")
        if not isinstance(methods, list):
            raise _PathError(("auth_methods",), "ожидается список")
        if not methods:
            raise _PathError(("auth_methods",), "список/объект не может быть пустым")
        seen: set[str] = set()
        for j, raw in enumerate(methods):
            if not isinstance(raw, dict):
                continue
            method_id = raw.get("id")
            if method_id is None:
                raise _PathError(("auth_methods", j, "id"), "обязательное поле отсутствует")
            if not isinstance(method_id, str) or not AUTH_METHOD_ID_RE.match(method_id):
                raise _PathError(("auth_methods", j, "id"), "значение не соответствует формату")
            if method_id in seen:
                raise _PathError(("auth_methods",), f"id '{method_id}' повторяется")
            seen.add(method_id)
            title = raw.get("title")
            if title is None:
                raise _PathError(("auth_methods", j, "title"), "обязательное поле отсутствует")
            if not isinstance(title, str) or not title:
                raise _PathError(("auth_methods", j, "title"), "значение не может быть пустым")
            if raw.get("type") == "user_token" and data.get("mode") != "facade":
                raise _PathError(
                    ("auth_methods", j, "type"),
                    "способ user_token допустим только при mode: facade",
                )
        return data

    @model_validator(mode="after")
    def _check_header_templates(self) -> ServerModel:
        """R-P2/R-C1/R-U3/R-U12/R-U18: какие шаблоны допустимы в заголовках и телах запросов.

        В заголовках — только ``{{access_token}}``; в теле запроса выпуска —
        только ``{{token_description}}``, в теле запроса отзыва — только ``{{token_id}}``.
        """
        for field_name in ("credential_headers", "static_headers"):
            _check_templates(getattr(self, field_name) or {}, (field_name,), "access_token")
        for j, method in enumerate(self.auth_methods or []):
            if not isinstance(method, AuthUserToken):
                continue
            base: tuple[Any, ...] = ("auth_methods", j)
            _check_templates(method.verify.headers, (*base, "verify", "headers"), "access_token")
            exchange = method.exchange
            if exchange is not None:
                _check_templates(exchange.headers, (*base, "exchange", "headers"), "access_token")
                _check_templates(
                    exchange.body or {}, (*base, "exchange", "body"), "token_description"
                )
                _check_templates(
                    exchange.revoke.headers,
                    (*base, "exchange", "revoke", "headers"),
                    "access_token",
                )
                _check_templates(
                    exchange.revoke.body or {}, (*base, "exchange", "revoke", "body"), "token_id"
                )
                if exchange.list is not None:
                    _check_templates(
                        exchange.list.headers,
                        (*base, "exchange", "list", "headers"),
                        "access_token",
                    )
            if method.expiry is not None:
                _check_templates(method.expiry.headers, (*base, "expiry", "headers"), "access_token")
        return self

    @model_validator(mode="after")
    def _mode_requirements(self) -> ServerModel:
        missing: list[str] = []
        if self.mode == "native":
            if not self.mcp_url:
                missing.append("mcp_url")
        else:
            if not self.upstream_url:
                missing.append("upstream_url")
            if self.auth is None and not self.auth_methods:
                missing.append("auth")
            if not self.credential_headers:
                missing.append("credential_headers")
        if missing:
            raise _ModeFieldsMissing(missing)
        # R-U1: сервер с прежним полем auth эквивалентен списку из одного способа.
        if self.auth is not None:
            if self.auth.id is None:
                self.auth.id = DEFAULT_OAUTH_METHOD_ID
            if self.auth.title is None:
                self.auth.title = DEFAULT_OAUTH_METHOD_TITLE
        return self

    @property
    def methods(self) -> list[AuthOAuth2 | AuthUserToken]:
        """Способы подключения сервера: объявленные списком либо один способ из ``auth`` (R-U1)."""
        if self.auth_methods is not None:
            return list(self.auth_methods)
        if self.auth is not None:
            return [self.auth]
        return []


class _ModeFieldsMissing(ValueError):
    def __init__(self, fields: list[str]) -> None:
        super().__init__(", ".join(fields))
        self.fields = fields


class _PathError(ValueError):
    """Ошибка схемы с явным путём к полю относительно сервера (R-C1)."""

    def __init__(self, parts: tuple[Any, ...], text: str) -> None:
        super().__init__(text)
        self.parts = parts
        self.text = text


# R-U8.1 п. 6: четыре адреса — единственное исключение из правила дословности. Пути даны
# относительно самого блока; ``exchange.list`` наружу не идёт никогда (R-U15.3), поэтому его
# значения на публикацию блока ``exchange`` не влияют.
_VERIFY_VERBATIM_SKIP = frozenset({("url",)})
_EXCHANGE_VERBATIM_SKIP = frozenset({("url",), ("revoke", "url"), ("list",)})
_UPSTREAM_VERBATIM_SKIP: frozenset[tuple[str, ...]] = frozenset()


def _is_verbatim(value: Any) -> bool:
    """R-U8.1 п. 6: строка записана в карточке **дословно** и её можно отдать наружу.

    Непубликуемы ровно два вида значений: ссылка ``env:VAR`` (R-C2/AC-14 — ни имя переменной, ни
    её значение наружу не идут никогда) и строка с подстановкой ``${VAR}`` (R-C2 разворачивает её
    при загрузке, и по строке результата уже не сказать, был там адрес или секрет). Решение
    принимается по **сырому** значению карточки — после ``$ref``, но до подстановки (R-C3):
    после неё ``"${GW_SECRET}"`` неотличима от литерала ``"Bearer {{access_token}}"``.
    Нестроковые листья (числа, флаги, ``null``) секрета не несут.
    """
    if not isinstance(value, str):
        return True
    if ENV_REF_RE.match(value):
        return False
    return "${" not in value


def _all_verbatim(node: Any, rel: tuple[str, ...], skip: frozenset[tuple[str, ...]]) -> bool:
    """Все строковые листья сырого блока публикуемы (R-U8.1 п. 6), кроме путей из ``skip``."""
    if rel in skip:
        return True
    if isinstance(node, dict):
        return all(_all_verbatim(v, (*rel, str(k)), skip) for k, v in node.items())
    if isinstance(node, list):
        return all(_all_verbatim(v, (*rel, str(i)), skip) for i, v in enumerate(node))
    return _is_verbatim(node)


@dataclass(frozen=True)
class Publication:
    """Какие блоки карточки прошли проверку дословности (R-U8.1 п. 6) и могут быть опубликованы.

    Решение принимается один раз при загрузке каталога по сырой карточке и от пользователя не
    зависит. Значения по умолчанию — «ничего не публикуется»: карточка, для которой проверка не
    выполнялась, прямой режим не отдаёт (отказ в безопасную сторону).
    """

    upstream: bool = False
    verify: frozenset[str] = frozenset()
    exchange: frozenset[str] = frozenset()


def _publication(raw: Any, model: ServerModel) -> Publication:
    """Проверка дословности (R-U8.1 п. 6) по сырой карточке — после ``$ref``, до ``${VAR}``."""
    raw_methods = raw.get("auth_methods") if isinstance(raw, dict) else None
    raw_list: list[Any] = raw_methods if isinstance(raw_methods, list) else []
    verify: set[str] = set()
    exchange: set[str] = set()
    for method, raw_method in zip(model.auth_methods or [], raw_list, strict=False):
        if not isinstance(method, AuthUserToken) or not isinstance(raw_method, dict):
            continue
        method_id = method.id or ""
        if _all_verbatim(raw_method.get("verify"), (), _VERIFY_VERBATIM_SKIP):
            verify.add(method_id)
        if _all_verbatim(raw_method.get("exchange"), (), _EXCHANGE_VERBATIM_SKIP):
            exchange.add(method_id)
    raw_server: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    upstream = all(
        _all_verbatim(raw_server.get(name), (), _UPSTREAM_VERBATIM_SKIP)
        for name in ("credential_headers", "static_headers")
    )
    return Publication(upstream=upstream, verify=frozenset(verify), exchange=frozenset(exchange))


def _public_headers(headers: Mapping[str, Any]) -> dict[str, str] | None:
    """Набор ``имя → строка`` наружу: заголовки и тела запросов (§3.2 «Граница публикации», AC-14).

    Публикуется только строковое значение — шаблон вида ``Bearer {{access_token}}``. Значение
    ``env:VAR`` наружу не идёт **ни ссылкой, ни развёрнутым значением** (AC-14): подставить его
    может лишь Hub, у которого есть переменные сборки каталога. Такой блок не публикуется целиком —
    возвращается ``None``, и способ прямым режимом не подключается (§3.2). Половинчатая публикация
    (заголовок выброшен, остальные оставлены) запрещена намеренно: клиент получил бы запрос без
    обязательного заголовка и узнал бы об этом только по отказу целевой системы.

    R-U8.1 п. 8: это единственный путь значений заголовков и тел в публичное представление.
    Вторая половина границы — дословность (R-U8.1 п. 6): она проверяется по сырой карточке при
    загрузке (``_publication``), потому что после подстановки ``${VAR}`` значение — обычная ``str``.
    """
    out: dict[str, str] = {}
    for name, value in headers.items():
        # Не ``str`` — это ``EnvRef`` (или ``Secret``): значения из них не читаются никогда.
        if not isinstance(value, str):
            return None
        out[name] = value
    return out


def _public_verify(verify: TokenVerify) -> dict[str, Any] | None:
    """Блок ``verify`` способа ``user_token`` наружу (§3.2): адрес, метод и шаблоны заголовков."""
    headers = _public_headers(verify.headers)
    if headers is None:
        return None
    return {
        "url": verify.url,
        "method": verify.method,
        "headers": headers,
        "expect_status": verify.expect_status,
        "account_field": verify.account_field,
        "require_account": verify.require_account,
    }


def _public_revoke(revoke: TokenExchangeRevoke) -> dict[str, Any] | None:
    """Блок отзыва выпущенного токена наружу (§3.2). Клиент ищет его как ``exchange.revoke``."""
    headers = _public_headers(revoke.headers)
    body = None if revoke.body is None else _public_headers(revoke.body)
    if headers is None or (revoke.body is not None and body is None):
        return None
    return {
        "url": revoke.url,
        "method": revoke.method,
        "headers": headers,
        "body": body,
        "expect_status": revoke.expect_status,
    }


def _public_exchange(exchange: TokenExchange) -> dict[str, Any] | None:
    """Блок ``exchange`` способа ``user_token`` наружу (§3.2) вместе с вложенным ``revoke``.

    Запрос списка выпущенных токенов (``exchange.list``, R-U15.3) не публикуется: уборку «сирот»
    делает Hub, в перечне §3.2 этого блока нет.

    R-U8.1 п. 7: непубликуемое значение в ``headers``, ``body``, ``revoke.headers`` или
    ``revoke.body`` снимает блок **целиком вместе с вложенным** ``revoke`` — публиковать обмен без
    отзыва нельзя: приложение выпустило бы токен, который нечем отозвать.
    """
    headers = _public_headers(exchange.headers)
    body = None if exchange.body is None else _public_headers(exchange.body)
    revoke = _public_revoke(exchange.revoke)
    if headers is None or revoke is None or (exchange.body is not None and body is None):
        return None
    return {
        "url": exchange.url,
        "method": exchange.method,
        "headers": headers,
        "body": body,
        "expect_status": exchange.expect_status,
        "token_field": exchange.token_field,
        "token_id_field": exchange.token_id_field,
        "description": exchange.description,
        "revoke": revoke,
    }


def _check_templates(values: Mapping[str, Any], parts: tuple[Any, ...], allowed: str) -> None:
    """В значениях ``имя → строка`` допустим ровно один шаблон ``{{<allowed>}}`` (R-C1)."""
    for name, value in values.items():
        if not isinstance(value, str):
            continue
        for found in TEMPLATE_RE.findall(value):
            if found.strip() != allowed:
                raise _PathError(
                    (*parts, name),
                    f"недопустимый шаблон {{{{{found}}}}} (поддерживается только {{{{{allowed}}}}})",
                )


@dataclass(frozen=True)
class ServerEntry:
    """Сервер каталога после загрузки."""

    model: ServerModel
    index: int
    unconfigured: bool = False
    missing_vars: tuple[str, ...] = ()
    # R-U8.1 п. 6: результат проверки дословности по сырой карточке — считается один раз при
    # загрузке (``_publication``), от пользователя не зависит.
    publication: Publication = Publication()

    @property
    def alias(self) -> str:
        return self.model.alias

    @property
    def status(self) -> str:
        return self.model.status

    @property
    def mode(self) -> str:
        return self.model.mode

    def is_visible_to(self, groups: list[str]) -> bool:
        if self.unconfigured:
            return False
        audience = self.model.audience
        return "all" in audience or bool(set(audience) & set(groups))

    @property
    def auth_methods(self) -> list[AuthOAuth2 | AuthUserToken]:
        return self.model.methods

    def auth_method(self, method_id: str | None) -> AuthOAuth2 | AuthUserToken | None:
        """Способ подключения по его ``id`` (R-U1).

        ``method_id is None`` — способ определяется однозначно: единственный объявленный способ
        либо (для подключений, созданных до появления колонки) единственный тип ``user_token``.
        """
        methods = self.auth_methods
        if method_id is not None:
            return next((m for m in methods if m.id == method_id), None)
        if len(methods) == 1:
            return methods[0]
        user_token = [m for m in methods if isinstance(m, AuthUserToken)]
        if user_token and len(user_token) == len(methods):
            return user_token[0]
        return None

    def uses_user_token(self, method_id: str | None) -> bool:
        """Подключение выполнено способом ``user_token`` (R-U5, R-U6, R-U7)."""
        return isinstance(self.auth_method(method_id), AuthUserToken)

    def user_token_methods(self) -> list[AuthUserToken]:
        return [m for m in self.auth_methods if isinstance(m, AuthUserToken)]

    def public_auth_kind(self) -> str:
        """R-C6: тип первого доступного способа, иначе первого объявленного, иначе ``oauth2``."""
        methods = self.auth_methods
        if not methods:
            return "oauth2"
        available = next((m for m in methods if m.available), None)
        return (available or methods[0]).type

    def public_auth_methods(self) -> list[dict[str, Any]]:
        """R-U8/R-U16 и §3.2 «Граница публикации» (ревизия 1.13): способы подключения наружу.

        Для ``type: "oauth2"`` граница прежняя **дословно**: ни ``client_id``/``client_secret``, ни
        ``scopes``, ни URL авторизации и обмена — рядом с ними лежат секреты и ссылки ``env:VAR``.

        Для ``type: "user_token"`` с ``available: true`` публикуются ``field``, ``verify`` и
        ``exchange`` (вместе с вложенным ``revoke``). Границу держит не «секретов тут нет по
        построению» — это неверно: ``env:VAR`` допустима в заголовках прямо по схеме, а ``${VAR}``
        разворачивается до валидации, — а правило R-U8.1 п. 6–8: наружу идёт только значение,
        записанное в карточке **дословно**, и любое другое снимает блок целиком (``_publication``
        и ``_public_headers``). ``expiry`` (R-U18) не публикуется никогда: срок годности присланного
        токена читает Hub, в перечне §3.2 блока нет.
        """
        result: list[dict[str, Any]] = []
        for method in self.auth_methods:
            view: dict[str, Any] = {
                "id": method.id,
                "title": method.title,
                "type": method.type,
                "available": method.available,
                "unavailable_reason": method.unavailable_reason,
                # R-U16: у способа объявлен обмен — Hub получит постоянный токен.
                "issues_permanent_token": (
                    isinstance(method, AuthUserToken) and method.exchange is not None
                ),
            }
            if isinstance(method, AuthUserToken):
                f = method.field
                view["field"] = {
                    "label": f.label,
                    "hint": f.hint,
                    "docs_url": f.docs_url,
                    "secret": f.secret,
                    "placeholder": f.placeholder,
                    "min_length": f.min_length,
                    "max_length": f.max_length,
                }
                # R-U1 (решение 73): у способа с available: false значения наружу не отдаются —
                # внутри него допустимы незаданные ${VAR}, и подключиться им нельзя (409, R-U4).
                if method.available:
                    # R-U8.1 п. 6 и п. 7: блок публикуется, только если **все** его строковые
                    # значения записаны в карточке дословно; иначе блока нет целиком.
                    if method.id in self.publication.verify:
                        verify = _public_verify(method.verify)
                        if verify is not None:
                            view["verify"] = verify
                    if method.exchange is not None and method.id in self.publication.exchange:
                        exchange = _public_exchange(method.exchange)
                        if exchange is not None:
                            view["exchange"] = exchange
            result.append(view)
        return result

    def public_upstream(self) -> dict[str, Any] | None:
        """Блок ``upstream`` наружу (§3.2, ревизия 1.13): ``{url, credential_headers, static_headers?}``.

        Публикуется **только** у карточек с **доступным** способом ``type: "user_token"``: адрес
        целевого MCP-сервера и шаблоны заголовков нужны приложению, чтобы подключиться без Hub.
        У карточки, где все способы — ``oauth2``, блока нет: граница для них прежняя дословно; у
        карточки, где единственный способ ``user_token`` объявлен недоступным, блока тоже нет —
        подключиться им нельзя (R-U1, R-U4), и отдавать наружу адрес незачем.
        Непубликуемое значение (R-U8.1 п. 6) в ``credential_headers`` или ``static_headers`` —
        ссылка ``env:VAR`` или подстановка ``${VAR}`` — снимает публикацию блока **целиком**:
        подставить её клиенту нечем, и прямым режимом такой коннектор не подключается (§3.2).
        Адрес ``upstream_url`` — одно из четырёх исключений п. 6: он публикуется подставленным
        (без него прямого режима нет), а ссылка ``env:VAR`` в адресе запрещена схемой (AC-15).
        """
        m = self.model
        if not any(method.available for method in self.user_token_methods()) or not m.upstream_url:
            return None
        if not self.publication.upstream:
            return None
        credential = _public_headers(m.credential_headers or {})
        if not credential:
            return None
        block: dict[str, Any] = {"url": m.upstream_url, "credential_headers": credential}
        if m.static_headers:
            static = _public_headers(m.static_headers)
            if static is None:
                return None
            block["static_headers"] = static
        return block

    def public_mcp_url(self, public_url: str) -> str:
        if self.model.mode == "native":
            return self.model.mcp_url or ""
        return f"{public_url}/mcp/{self.alias}"

    def public_permission_model(self) -> dict[str, Any]:
        pm = self.model.permission_model
        if isinstance(pm, PermissionHeaderGroups):
            return {
                "kind": pm.kind,
                "groups": [{"id": g.id, "title": g.title, "preset": g.preset} for g in pm.groups],
                "always": list(pm.always),
            }
        if isinstance(pm, PermissionConsent):
            return {"kind": pm.kind, "presets": copy.deepcopy(pm.presets)}
        return {
            "kind": pm.kind,
            "presets": {name: {"tools": list(p.tools)} for name, p in pm.presets.items()},
        }

    def public_view(self, public_url: str) -> dict[str, Any]:
        """Публичное представление сервера (R-C6): без секретов.

        Ревизия 1.13 (§3.2): у карточки со способом ``user_token`` появился блок ``upstream`` —
        адрес целевого MCP-сервера и шаблоны заголовков нужны приложению для подключения без Hub.
        У карточек, где все способы — ``oauth2``, состав представления не изменился.
        """
        m = self.model
        view: dict[str, Any] = {
            "alias": m.alias,
            "title": m.title,
            "description": m.description,
            "owner": m.owner,
            "contact": m.contact,
            "docs_url": m.docs_url,
            "status": m.status,
            "mode": m.mode,
            "mcp_url": self.public_mcp_url(public_url),
            "permission_model": self.public_permission_model(),
            "auth_kind": self.public_auth_kind(),
        }
        # Короткая суть — только у карточек, объявивших её: без поля состав представления прежний.
        if m.summary is not None:
            view["summary"] = m.summary
        # Цвет подложки значка — только у карточек, объявивших его: без поля состав прежний.
        if m.icon_color is not None:
            view["icon_color"] = m.icon_color
        # R-C6/R-U8 (решение 69): ключ auth_methods есть только у серверов, объявивших его в каталоге.
        if m.auth_methods is not None:
            view["auth_methods"] = self.public_auth_methods()
        # §3.2 (ревизия 1.13): блок upstream — только у карточек со способом user_token.
        upstream = self.public_upstream()
        if upstream is not None:
            view["upstream"] = upstream
        # R-C7.2, R-C7.3: сквозные поля отдаются дословно и только у объявивших их серверов —
        # ключи, порядок, вложенность и типы значений те же, что в каталоге.
        if m.permission_groups is not None:
            view["permission_groups"] = copy.deepcopy(m.permission_groups)
        if m.type is not None:
            view["type"] = m.type
        return view


@dataclass(frozen=True)
class Catalog:
    """Загруженный каталог."""

    version: int
    servers: tuple[ServerEntry, ...]
    defaults: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def configured(self) -> list[ServerEntry]:
        return [s for s in self.servers if not s.unconfigured]

    def unconfigured(self) -> list[ServerEntry]:
        return [s for s in self.servers if s.unconfigured]

    def visible_for(self, groups: list[str], *, include_deprecated: bool = True) -> list[ServerEntry]:
        result = []
        for s in self.servers:
            if not s.is_visible_to(groups):
                continue
            if not include_deprecated and s.status == "deprecated":
                continue
            result.append(s)
        return result

    def get(self, alias: str) -> ServerEntry | None:
        for s in self.servers:
            if s.alias == alias:
                return s
        return None


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------


def _path_str(base: str, parts: tuple[Any, ...]) -> str:
    out = base
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += f".{p}" if out else str(p)
    return out


def _resolve_refs(servers: list[Any]) -> list[Any]:
    """Заменить ``{ $ref: "#/servers/<alias>/<поле>" }`` копией поля целевого сервера."""
    by_alias: dict[str, dict[str, Any]] = {}
    for raw in servers:
        if isinstance(raw, dict) and isinstance(raw.get("alias"), str):
            by_alias.setdefault(raw["alias"], raw)

    def contains_ref(value: Any) -> bool:
        if isinstance(value, dict):
            if "$ref" in value:
                return True
            return any(contains_ref(v) for v in value.values())
        if isinstance(value, list):
            return any(contains_ref(v) for v in value)
        return False

    def resolve(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                if len(value) != 1 or not isinstance(ref, str):
                    raise CatalogError(f"{path}: некорректная ссылка $ref {ref!r}")
                m = REF_RE.match(ref)
                if not m:
                    raise CatalogError(
                        f"{path}: некорректный формат ссылки {ref!r} "
                        "(ожидается '#/servers/<alias>/<поле>')"
                    )
                alias, fld = m.group(1), m.group(2)
                if fld == PASSTHROUGH_GROUPS_FIELD:
                    # R-C7.7: сквозной узел не раскрывается и ссылкой извне (решение 108).
                    raise CatalogError(
                        f"{path}: ссылка {ref!r} на сквозное поле "
                        f"'{PASSTHROUGH_GROUPS_FIELD}' недопустима "
                        "(узел уходит клиенту дословно и не раскрывается)"
                    )
                target_server = by_alias.get(alias)
                if target_server is None:
                    raise CatalogError(f"{path}: ссылка {ref!r} указывает на неизвестный alias '{alias}'")
                if fld not in target_server:
                    raise CatalogError(
                        f"{path}: ссылка {ref!r} указывает на отсутствующее поле '{fld}' сервера '{alias}'"
                    )
                target = target_server[fld]
                if contains_ref(target):
                    raise CatalogError(
                        f"{path}: ссылка {ref!r} указывает на значение, которое само содержит $ref "
                        "(допустима только одна ступень)"
                    )
                return copy.deepcopy(target)
            return {k: resolve(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    result: list[Any] = []
    for i, raw in enumerate(servers):
        if not isinstance(raw, dict):
            result.append(raw)
            continue
        resolved: dict[Any, Any] = {}
        for k, v in raw.items():
            path = f"servers[{i}].{k}"
            if k == PASSTHROUGH_GROUPS_FIELD:
                # R-C7.7: $ref **на месте самого поля** — ошибка (автор имел в виду ссылку R-C3);
                # вложенный внутрь узла $ref — это данные и уходит клиенту дословно.
                if isinstance(v, dict) and "$ref" in v:
                    raise CatalogError(
                        f"{path}: ссылка $ref недопустима в этом поле "
                        "(узел уходит клиенту дословно и не раскрывается)"
                    )
                resolved[k] = v
                continue
            resolved[k] = resolve(v, path)
        result.append(resolved)
    return result


def _substitute_vars(value: Any, path: str, env: Mapping[str, str], missing: list[tuple[str, str]]) -> Any:
    """Подставить ``${VAR}``. Отсутствующие переменные собираются в ``missing`` (путь, имя)."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in env:
                return env[name]
            missing.append((path, name))
            return m.group(0)

        return VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_vars(v, f"{path}.{k}", env, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_vars(v, f"{path}[{i}]", env, missing) for i, v in enumerate(value)]
    return value


def _substitute_server_vars(
    raw: Any, path: str, env: Mapping[str, str], missing: list[tuple[str, str]]
) -> Any:
    """``${VAR}`` по карточке сервера, кроме сквозного узла ``permission_groups`` (R-C7.7).

    Решение 108: узел уходит наружу дословно, поэтому подстановка в нём не выполняется — иначе
    опечатка внутри непонятного Hub'у словаря молча пометила бы сервер ``unconfigured`` (R-C2).
    """
    if not isinstance(raw, dict):
        return _substitute_vars(raw, path, env, missing)
    out: dict[Any, Any] = {}
    for k, v in raw.items():
        if k == PASSTHROUGH_GROUPS_FIELD:
            out[k] = v
            continue
        out[k] = _substitute_vars(v, f"{path}.{k}", env, missing)
    return out


def _env_ref_allowed(rel: tuple[Any, ...]) -> bool:
    """R-C2/R-U3: где допустима ссылка ``env:VAR`` (путь относительно сервера)."""
    if rel == ("auth", "client_secret"):
        return True
    if len(rel) == 2 and rel[0] in ("credential_headers", "static_headers"):
        return True
    if len(rel) >= 3 and rel[0] == "auth_methods" and str(rel[1]).isdigit():
        tail = rel[2:]
        if tail == ("client_secret",):
            return True
        # R-U3/R-U12/R-U18: заголовки проверки, обмена, списка, отзыва и запроса срока годности.
        if len(tail) == 3 and tail[0] in ("verify", "exchange", "expiry") and tail[1] == "headers":
            return True
        if (
            len(tail) == 4
            and tail[0] == "exchange"
            and tail[1] in ("revoke", "list")
            and tail[2] == "headers"
        ):
            return True
    return False


def _unavailable_method_paths(raw: Any, index: int) -> tuple[str, ...]:
    """Пути способов подключения с ``available: false`` (R-U1, решение 73)."""
    if not isinstance(raw, dict):
        return ()
    methods = raw.get("auth_methods")
    if not isinstance(methods, list):
        return ()
    return tuple(
        f"servers[{index}].auth_methods[{j}]"
        for j, method in enumerate(methods)
        if isinstance(method, dict) and method.get("available") is False
    )


def _drop_unavailable_method_vars(
    missing: list[tuple[str, str]], raw: Any, index: int
) -> list[tuple[str, str]]:
    """Незаданные ``${VAR}`` внутри недоступного способа не делают сервер ``unconfigured``.

    Уточнение R-C2 (R-U1, решение 73): способом с ``available: false`` подключиться нельзя, его
    значения наружу не отдаются и не читаются, поэтому настроенным он быть не обязан.
    """
    prefixes = _unavailable_method_paths(raw, index)
    if not prefixes:
        return missing
    return [
        item
        for item in missing
        if not any(item[0] == p or item[0].startswith((f"{p}.", f"{p}[")) for p in prefixes)
    ]


def _check_env_refs(value: Any, rel: tuple[Any, ...], server_path: str) -> None:
    """``env:VAR`` допустим только в auth.client_secret, credential_headers.*, static_headers.*
    и в тех же полях способов подключения (``auth_methods[j].client_secret``,
    ``auth_methods[j].verify.headers.*``, R-U1/R-U3)."""
    if isinstance(value, str):
        if ENV_REF_RE.match(value) and not _env_ref_allowed(rel):
            raise CatalogError(
                f"{_path_str(server_path, rel)}: ссылка env:VAR недопустима в этом поле "
                "(разрешено только в auth.client_secret, credential_headers, static_headers)"
            )
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _check_env_refs(v, rel + (str(k),), server_path)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_env_refs(v, rel + (str(i),), server_path)


_TYPE_MESSAGES = {
    "missing": "обязательное поле отсутствует",
    "extra_forbidden": "неизвестное поле",
    "string_pattern_mismatch": "значение не соответствует формату",
    "string_too_short": "значение не может быть пустым",
    "string_too_long": "значение длиннее допустимого",
    "too_short": "список/объект не может быть пустым",
    "literal_error": "недопустимое значение",
    "enum": "недопустимое значение",
    "string_type": "ожидается строка",
    "bool_type": "ожидается логическое значение",
    "list_type": "ожидается список",
    "dict_type": "ожидается объект",
    "model_type": "ожидается объект",
    "union_tag_invalid": "недопустимое значение",
    "union_tag_not_found": "обязательное поле отсутствует",
}


def _pydantic_loc(loc: tuple[Any, ...]) -> tuple[Any, ...]:
    """Убрать теги discriminated union из пути (``permission_model.header_groups.header``,
    ``auth_methods[0].user_token.field``)."""
    out: list[Any] = []
    for i, part in enumerate(loc):
        if i > 0 and loc[i - 1] == "permission_model" and part in _PERMISSION_KINDS:
            continue
        if i > 1 and loc[i - 2] == "auth_methods" and part in _AUTH_METHOD_TYPES:
            continue
        out.append(part)
    return tuple(out)


def _format_schema_error(server_path: str, exc: ValidationError) -> str:
    messages: list[str] = []
    for err in exc.errors():
        loc = _pydantic_loc(tuple(err.get("loc") or ()))
        etype = str(err.get("type", ""))
        ctx_err = err.get("ctx", {}).get("error") if isinstance(err.get("ctx"), dict) else None
        if isinstance(ctx_err, _ModeFieldsMissing):
            for fld in ctx_err.fields:
                messages.append(
                    f"{_path_str(server_path, loc + (fld,))}: обязательное поле для режима отсутствует"
                )
            continue
        if isinstance(ctx_err, _PathError):
            messages.append(f"{_path_str(server_path, loc + ctx_err.parts)}: {ctx_err.text}")
            continue
        if etype in ("union_tag_not_found", "union_tag_invalid") and loc and loc[-1] == "permission_model":
            loc = loc + ("kind",)
        # Тег способа подключения — поле ``type`` (R-U1).
        if (
            etype in ("union_tag_not_found", "union_tag_invalid")
            and len(loc) >= 2
            and loc[-2] == "auth_methods"
            and isinstance(loc[-1], int)
        ):
            loc = loc + ("type",)
        msg = _TYPE_MESSAGES.get(etype)
        if msg is None:
            raw = str(err.get("msg", ""))
            msg = raw.removeprefix("Value error, ")
        messages.append(f"{_path_str(server_path, loc)}: {msg}")
    return "; ".join(messages)


def _validate_server(raw: Any, index: int) -> ServerModel:
    server_path = f"servers[{index}]"
    if not isinstance(raw, dict):
        raise CatalogError(f"{server_path}: ожидается объект (описание сервера)")
    _check_env_refs(raw, (), server_path)
    try:
        return ServerModel.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(_format_schema_error(server_path, exc)) from exc


def _dropped_public_blocks(entry: ServerEntry) -> list[tuple[str, str | None]]:
    """Блоки, снятые с публикации непубликуемым значением (R-U8.1 п. 7) — вход для следа п. 9.

    Перечисляются только блоки, которые **иначе были бы опубликованы**: способ ``oauth2``, способ с
    ``available: false``, ``expiry`` и ``exchange.list`` в этот перечень не попадают (они не
    публикуются по пп. 1, 3 и 5, а не из-за значения), как и карточки ``unconfigured`` — их вовсе
    нет в публичных списках.
    """
    if entry.unconfigured:
        return []
    dropped: list[tuple[str, str | None]] = []
    for method in entry.auth_methods:
        if not isinstance(method, AuthUserToken) or not method.available:
            continue
        if method.id not in entry.publication.verify:
            dropped.append(("verify", method.id))
        if method.exchange is not None and method.id not in entry.publication.exchange:
            dropped.append(("exchange", method.id))
    if (
        not entry.publication.upstream
        and entry.model.upstream_url
        and any(method.available for method in entry.user_token_methods())
    ):
        dropped.append(("upstream", None))
    return dropped


def _log_dropped_public_blocks(entries: list[ServerEntry]) -> None:
    """R-U8.1 п. 9: одна запись WARNING на снятый блок при **каждой** загрузке каталога.

    В записи только ``alias``, ``id`` способа и имя блока: ни значений заголовков и тел, ни имён
    переменных с префиксом ``env:`` (R-C2, R-U9). Потеря прямого режима не должна быть молчаливой.
    """
    for entry in entries:
        for block, method_id in _dropped_public_blocks(entry):
            if method_id is None:
                logger.warning(
                    "каталог: сервер '%s' — блок upstream не публикуется: среди значений "
                    "credential_headers/static_headers есть непубликуемое (R-U8.1 п. 6, п. 7); "
                    "прямой режим этой карточкой недоступен",
                    entry.alias,
                )
            else:
                logger.warning(
                    "каталог: сервер '%s', способ '%s' — блок %s не публикуется: среди его "
                    "значений есть непубликуемое (R-U8.1 п. 6, п. 7); прямой режим этим способом "
                    "недоступен",
                    entry.alias,
                    method_id,
                    block,
                )


def parse_catalog(document: Any, env: Mapping[str, str] | None = None, *, source: str | None = None) -> Catalog:
    """Разобрать уже загруженный YAML-документ по правилам R-C1..R-C3."""
    environ: Mapping[str, str] = os.environ if env is None else env
    if not isinstance(document, dict):
        raise CatalogError("каталог: ожидается объект верхнего уровня с полями version и servers")

    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CatalogError("version: ожидается целое число ≥ 1")
    if "servers" not in document:
        raise CatalogError("servers: обязательное поле отсутствует")
    servers_raw = document.get("servers")
    if servers_raw is None:
        servers_raw = []
    if not isinstance(servers_raw, list):
        raise CatalogError("servers: ожидается список")
    defaults_raw = document.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise CatalogError("defaults: ожидается объект")

    # defaults: ${VAR} обязательны.
    missing_defaults: list[tuple[str, str]] = []
    defaults = _substitute_vars(defaults_raw, "defaults", environ, missing_defaults)
    if missing_defaults:
        path, name = missing_defaults[0]
        raise CatalogError(f"{path}: не задана переменная окружения {name}")

    # 1. $ref → 2. ${VAR} → 3. схема
    servers_resolved = _resolve_refs(servers_raw)
    entries: list[ServerEntry] = []
    seen_alias: dict[str, int] = {}
    for i, raw in enumerate(servers_resolved):
        missing: list[tuple[str, str]] = []
        substituted = _substitute_server_vars(raw, f"servers[{i}]", environ, missing)
        missing = _drop_unavailable_method_vars(missing, raw, i)
        status = raw.get("status") if isinstance(raw, dict) else None
        if missing and status != "beta":
            path, name = missing[0]
            raise CatalogError(f"{path}: не задана переменная окружения {name}")
        model = _validate_server(substituted, i)
        if model.alias in seen_alias:
            raise CatalogError(
                f"servers[{i}].alias: alias '{model.alias}' повторяется "
                f"(уже задан в servers[{seen_alias[model.alias]}])"
            )
        seen_alias[model.alias] = i
        entries.append(
            ServerEntry(
                model=model,
                index=i,
                unconfigured=bool(missing),
                missing_vars=tuple(dict.fromkeys(name for _, name in missing)),
                # R-U8.1 п. 6: решение о дословности принимается по сырой карточке — до подстановки.
                publication=_publication(raw, model),
            )
        )
    _log_dropped_public_blocks(entries)
    return Catalog(version=version, servers=tuple(entries), defaults=defaults, source=source)


def _load_permission_groups_file(directory: Path, alias: str) -> Any | None:
    """Словарь разрешений сервера из ``permissions/<alias>.yaml``; ``None`` — файла нет (R-C7.5).

    Имя ищется точно: ``<alias>.yml`` и любые другие имена этим файлом не считаются. Файл, который
    существует, но непригоден, — ошибка загрузки каталога, а не молчаливый пропуск (R-C7.6):
    он явно адресован этому серверу, и тихий пропуск отдал бы клиентам карточку без словаря.
    """
    file = directory / f"{alias}.yaml"
    rel = f"{PERMISSIONS_DIR_NAME}/{alias}.yaml"
    if not file.is_file():
        return None
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"{rel}: не удалось прочитать файл: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{rel}: ошибка разбора YAML: {exc}") from exc
    if document is None:
        raise CatalogError(f"{rel}: файл пуст")
    if not isinstance(document, dict):
        raise CatalogError(f"{rel}: ожидается объект (словарь групп разрешений)")
    # R-C7.7, R-K3: узел уходит наружу целиком, поэтому ссылкам на секреты в нём места нет.
    _check_env_refs(document, (), rel)
    return document


def _apply_permission_files(document: Any, directory: Path) -> Any:
    """Подставить ``permissions/<alias>.yaml`` в карточки серверов (R-C7.5).

    Файл главнее карточки и берётся целиком (решение 107): поэлементного слияния нет — сливать
    словари, устройства которых Hub не понимает, запрещено. Файл, имя которого не совпадает ни с
    одним alias каталога, не читается вовсе и ошибкой не является.
    """
    if not isinstance(document, dict) or not directory.is_dir():
        return document
    servers = document.get("servers")
    if not isinstance(servers, list):
        return document
    patched: list[Any] = []
    replaced = False
    for raw in servers:
        alias = raw.get("alias") if isinstance(raw, dict) else None
        # Alias ещё не проверен схемой: читаем файл только по имени допустимого формата.
        groups = (
            _load_permission_groups_file(directory, alias)
            if isinstance(alias, str) and ALIAS_RE.match(alias)
            else None
        )
        if groups is None:
            patched.append(raw)
            continue
        patched.append({**raw, PASSTHROUGH_GROUPS_FIELD: groups})
        replaced = True
    return {**document, "servers": patched} if replaced else document


def load_catalog(path: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> Catalog:
    """Прочитать и разобрать файл каталога. Любая ошибка → ``CatalogError``.

    Здесь же (и только здесь — R-C7.5) подставляются словари разрешений из соседнего каталога
    ``permissions``: когда каталог разбирается не из файла, соседнего каталога не существует по
    определению, поэтому ``parse_catalog`` о файлах ничего не знает.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogError(f"файл каталога не найден: {p}") from exc
    except OSError as exc:
        raise CatalogError(f"не удалось прочитать файл каталога {p}: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"ошибка разбора YAML в {p}: {exc}") from exc
    if document is None:
        raise CatalogError(f"файл каталога пуст: {p}")
    document = _apply_permission_files(document, p.parent / PERMISSIONS_DIR_NAME)
    return parse_catalog(document, env, source=str(p))


__all__ = [
    "TOKEN_MAX_LENGTH",
    "AuthMethod",
    "AuthOAuth2",
    "AuthUserToken",
    "Catalog",
    "CatalogError",
    "EnvRef",
    "PermissionConsent",
    "PermissionHeaderGroups",
    "PermissionToolFilter",
    "Publication",
    "Secret",
    "ServerEntry",
    "ServerModel",
    "TokenExchange",
    "TokenExchangeList",
    "TokenExchangeRevoke",
    "TokenExpiry",
    "TokenField",
    "TokenVerify",
    "ToolMasks",
    "load_catalog",
    "parse_catalog",
]
