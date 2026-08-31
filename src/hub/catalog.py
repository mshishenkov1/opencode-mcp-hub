"""Каталог MCP-серверов: загрузка ``catalog.yaml``, ``$ref``, ``${VAR}``, ``env:VAR``, схема (R-C*)."""

from __future__ import annotations

import copy
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
    ValidationError,
    model_validator,
)

from hub.errors import CatalogError

# Spec 1.1, R-C1: alias — 1–32 символа, ^[a-z][a-z0-9-]{0,31}$ (односимвольные alias из AC-54/AC-55 валидны).
ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ENV_REF_RE = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
REF_RE = re.compile(r"^#/servers/([^/]+)/([^/]+)$")

# Поля, в которых допустима ссылка env:VAR (относительно сервера).
_ENV_REF_ALLOWED_PREFIXES = (("auth", "client_secret"), ("credential_headers",), ("static_headers",))

ServerStatus = Literal["beta", "ga", "deprecated"]
ServerMode = Literal["native", "facade"]
Preset = Literal["readonly", "readwrite", "none"]


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


class PermissionHeaderGroups(_Strict):
    kind: Literal["header_groups"]
    header: str = Field(min_length=1)
    always: list[str] = Field(default_factory=list)
    groups: list[HeaderGroup] = Field(min_length=1)

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


class AuthOAuth2(_Strict):
    type: Literal["oauth2"]
    authorize_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    revoke_url: str | None = None
    client_id: str = Field(min_length=1)
    client_secret: SecretValue
    pkce: bool
    scopes: AuthScopes


class ServerModel(_Strict):
    alias: str = Field(pattern=ALIAS_RE.pattern)
    title: str = Field(min_length=1)
    # Короткая суть коннектора одной строкой — то, что витрина показывает под названием.
    # Необязательное: карточка без него грузится и публикуется ровно как раньше (AC-22).
    summary: str | None = Field(default=None, min_length=1)
    description: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    contact: str | None = None
    docs_url: str | None = None
    icon: str | None = None
    status: ServerStatus
    audience: list[str] = Field(min_length=1)
    mode: ServerMode
    mcp_url: str | None = None
    upstream_url: str | None = None
    auth: AuthOAuth2 | None = None
    credential_headers: dict[str, HeaderValue] | None = None
    static_headers: dict[str, HeaderValue] | None = None
    permission_model: PermissionModel
    # R-C7.1: сквозной тип коннектора карточки — Hub его не читает и не подставляет, отдаёт
    # дословно клиенту (тот же принцип, что и у прочих сквозных полей карточки).
    type: str | None = None

    @model_validator(mode="after")
    def _mode_requirements(self) -> ServerModel:
        missing: list[str] = []
        if self.mode == "native":
            if not self.mcp_url:
                missing.append("mcp_url")
        else:
            if not self.upstream_url:
                missing.append("upstream_url")
            if self.auth is None:
                missing.append("auth")
            if not self.credential_headers:
                missing.append("credential_headers")
        if missing:
            raise _ModeFieldsMissing(missing)
        return self


class _ModeFieldsMissing(ValueError):
    def __init__(self, fields: list[str]) -> None:
        super().__init__(", ".join(fields))
        self.fields = fields


@dataclass(frozen=True)
class ServerEntry:
    """Сервер каталога после загрузки."""

    model: ServerModel
    index: int
    unconfigured: bool = False
    missing_vars: tuple[str, ...] = ()

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
        """Публичное представление сервера (R-C6): без секретов и внутренних URL."""
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
            "auth_kind": "oauth2",
        }
        # Короткая суть — только у карточек, объявивших её: без поля состав представления прежний.
        if m.summary is not None:
            view["summary"] = m.summary
        # R-C7.1: сквозной тип коннектора — отдаётся дословно только у карточек, объявивших его.
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
        if isinstance(raw, dict):
            result.append({k: resolve(v, f"servers[{i}].{k}") for k, v in raw.items()})
        else:
            result.append(raw)
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


def _check_env_refs(value: Any, rel: tuple[str, ...], server_path: str) -> None:
    """``env:VAR`` допустим только в auth.client_secret, credential_headers.*, static_headers.*."""
    if isinstance(value, str):
        if ENV_REF_RE.match(value):
            allowed = any(
                rel[: len(prefix)] == prefix and len(rel) == len(prefix) + (0 if prefix[0] == "auth" else 1)
                for prefix in _ENV_REF_ALLOWED_PREFIXES
            )
            if not allowed:
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
    """Убрать теги discriminated union из пути (``permission_model.header_groups.header``)."""
    out: list[Any] = []
    for i, part in enumerate(loc):
        if i > 0 and loc[i - 1] == "permission_model" and part in _PERMISSION_KINDS:
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
        if etype == "union_tag_not_found" and loc and loc[-1] == "permission_model":
            loc = loc + ("kind",)
        if etype == "union_tag_invalid" and loc and loc[-1] == "permission_model":
            loc = loc + ("kind",)
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
        substituted = _substitute_vars(raw, f"servers[{i}]", environ, missing)
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
            )
        )
    return Catalog(version=version, servers=tuple(entries), defaults=defaults, source=source)


def load_catalog(path: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> Catalog:
    """Прочитать и разобрать файл каталога. Любая ошибка → ``CatalogError``."""
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
    return parse_catalog(document, env, source=str(p))


__all__ = [
    "Catalog",
    "CatalogError",
    "EnvRef",
    "Secret",
    "ServerEntry",
    "ServerModel",
    "load_catalog",
    "parse_catalog",
]
