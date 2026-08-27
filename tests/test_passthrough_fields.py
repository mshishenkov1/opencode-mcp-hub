"""Сквозные поля карточки каталога ``permission_groups`` и ``type`` (R-C7): AC-238…AC-243.

Hub в этой истории — **транспорт**: словарь разрешений разбирает и применяет клиент (S-V20 форка),
а Hub обязан лишь довезти содержимое ``catalog.yaml`` до клиента дословно и ничего с ним не делать.
Поэтому проверяется ровно две вещи: что доезжает (ключи, порядок, типы, вложенность) и что от этого
ничего не меняется (ни одно правило Hub эти поля не читает).

Все проверки идут против локальных моков; обращений в сеть нет.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from hub.app import create_app
from hub.cli import main
from hub.settings import Settings
from tests.conftest import Hub, HubFactory, base_settings_kwargs
from tests.support import (
    TAG_ENV,
    add_key,
    bearer,
    catalog_doc,
    connect_with_token,
    jsonrpc_body,
    mcp_headers,
    native_server,
    seed_user_with_key,
    user_token_facade,
    web_login,
    write_catalog,
)

# Состав публичного представления сервера до ревизии 4.2 (AC-22): ключи ревизии сюда не входят.
PUBLIC_FIELDS_BEFORE = {
    "alias", "title", "description", "owner", "contact", "docs_url", "status", "mode",
    "mcp_url", "permission_model", "auth_kind", "connection",
}  # fmt: skip

# Словарь с вложенностью и смешанными типами: Hub не знает ни одного из этих ключей.
NESTED_GROUPS: dict[str, Any] = {
    "version": 2,
    "groups": [
        {"id": "b", "title": "B", "default": False, "tools": ["t2", "t1"]},
        {"id": "a", "title": "A", "default": True, "tools": []},
    ],
    "rest": {"limit": 10, "enabled": True, "note": None},
    "unknown_to_hub": {"x": [1, 2, 3]},
}

SIMPLE_GROUPS: dict[str, Any] = {
    "version": 1,
    "groups": [{"id": "read", "title": "Чтение", "description": "d", "default": True,
                "tools": ["a"]}],
    "rest": {"mode": "deny"},
}  # fmt: skip


# --- вспомогательное -------------------------------------------------------


def _server(alias: str = "alpha", **overrides: Any) -> dict[str, Any]:
    return native_server(alias, **overrides)


def _write(path: Path, document: Any) -> Path:
    return write_catalog(path, document)


def _permissions_dir(catalog_path: Path) -> Path:
    directory = catalog_path.parent / "permissions"
    directory.mkdir(exist_ok=True)
    return directory


def _create_app(path: Path, document: Any, env: dict[str, str] | None = None) -> Any:
    """Приложение с каталогом из файла: подстановка словарей идёт только для файла (R-C7.5)."""
    _write(path, document)
    return create_app(Settings(**base_settings_kwargs(path)), catalog_env=env)


def _expect_load_error(
    path: Path, document: Any, *fragments: str, env: dict[str, str] | None = None
) -> str:
    with pytest.raises(Exception) as excinfo:
        _create_app(path, document, env)
    message = str(excinfo.value)
    for fragment in fragments:
        assert fragment in message, f"ожидалось {fragment!r} в сообщении: {message}"
    return message


async def _catalog_of(hub: Hub, key: str = "sk-ok") -> dict[str, Any]:
    response = await hub.get("/api/catalog", headers=bearer(key))
    assert response.status_code == 200, response.text
    return {s["alias"]: s for s in response.json()["servers"]}


async def _hub_with(make_hub: HubFactory, document: Any, path: Path, **overrides: Any) -> Hub:
    hub = await make_hub(catalog=document, path=path, base_url="https://hub.test", **overrides)
    await seed_user_with_key(hub.app, "sk-ok")
    return hub


# --- AC-238: схема ---------------------------------------------------------


def _schema_cases() -> list[tuple[str, dict[str, Any], str | None]]:
    valid = _server(permission_groups=copy.deepcopy(SIMPLE_GROUPS), type="messenger")
    unknown_field = _server()
    unknown_field["colour"] = "red"
    return [
        ("(а) mapping и type", valid, None),
        ("(б) строка", _server(permission_groups="строка"), "servers[0].permission_groups"),
        ("(в) список", _server(permission_groups=[1, 2]), "servers[0].permission_groups"),
        ("(г) число", _server(permission_groups=42), "servers[0].permission_groups"),
        # Явный null — заданное значение неверного типа, а не отсутствие поля.
        ("(д) null", _server(permission_groups=None), "servers[0].permission_groups"),
        ("(е) пустой type", _server(type=""), "servers[0].type"),
        ("(ж) type числом", _server(type=7), "servers[0].type"),
        ("(з) неизвестное поле", unknown_field, "servers[0].colour"),
        ("(и) ни одного из полей", _server(), None),
    ]


@pytest.mark.ac("AC-238")
@pytest.mark.parametrize(
    ("title", "server", "fragment"), _schema_cases(), ids=[c[0] for c in _schema_cases()]
)
def test_schema_accepts_two_optional_fields_and_stays_strict(
    tmp_path: Path, title: str, server: dict[str, Any], fragment: str | None
) -> None:
    """Схема проверяет ровно два свойства и в остальном строга дословно (R-C7.1, доп. R-C1)."""
    path = tmp_path / "catalog.yaml"
    if fragment is None:
        app = _create_app(path, catalog_doc([server]))
        assert [s.alias for s in app.state.catalog.servers] == ["alpha"], title
        return
    _expect_load_error(path, catalog_doc([server]), fragment)


@pytest.mark.ac("AC-238")
async def test_server_without_the_fields_looks_exactly_as_before(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """(и) без обоих полей публичное представление совпадает с прежним (AC-22 не изменился)."""
    hub = await _hub_with(make_hub, catalog_doc([_server()]), tmp_path / "catalog.yaml")
    server = (await _catalog_of(hub))["alpha"]
    assert set(server) == PUBLIC_FIELDS_BEFORE
    assert "permission_groups" not in server
    assert "type" not in server


@pytest.mark.ac("AC-238")
def test_valid_node_is_not_inspected_inside(tmp_path: Path) -> None:
    """Внутрь узла схема не заглядывает: ни version, ни groups Hub не требует (решение 105)."""
    for node in ({}, {"что угодно": [1, {"вложенное": None}]}, {"groups": "не список"}):
        app = _create_app(tmp_path / "catalog.yaml", catalog_doc([_server(permission_groups=node)]))
        assert app.state.catalog.servers[0].model.permission_groups == node


# --- AC-239: дословность ---------------------------------------------------


@pytest.mark.ac("AC-239")
async def test_node_reaches_the_client_verbatim(make_hub: HubFactory, tmp_path: Path) -> None:
    """Узел доезжает без изменений: ключи, порядок, вложенность и типы (R-C7.2, R-C7.3)."""
    source = copy.deepcopy(NESTED_GROUPS)
    document = catalog_doc(
        [_server("alpha", permission_groups=copy.deepcopy(source), type="vcs"), _server("beta")]
    )
    hub = await _hub_with(make_hub, document, tmp_path / "catalog.yaml")
    servers = await _catalog_of(hub)

    got = servers["alpha"]["permission_groups"]
    # Значение равно исходному узлу целиком — вместе с ключом, которого Hub не знает.
    assert got == source
    assert list(got) == list(source), "порядок ключей узла изменён"
    assert [g["id"] for g in got["groups"]] == ["b", "a"], "порядок групп изменён"
    assert got["groups"][0]["tools"] == ["t2", "t1"], "порядок инструментов изменён"
    assert got["unknown_to_hub"] == {"x": [1, 2, 3]}

    # Типы значений сохранены: число осталось числом, true — булевым, null — null.
    assert isinstance(got["version"], int) and not isinstance(got["version"], bool)
    assert got["rest"]["limit"] == 10 and isinstance(got["rest"]["limit"], int)
    assert got["rest"]["enabled"] is True
    assert got["rest"]["note"] is None
    assert got["groups"][0]["default"] is False and got["groups"][1]["default"] is True
    assert servers["alpha"]["type"] == "vcs"

    # У сервера, не объявившего поля, ключей нет вовсе (доп. R-C6, решение 69).
    assert "permission_groups" not in servers["beta"]
    assert "type" not in servers["beta"]


@pytest.mark.ac("AC-239")
async def test_key_order_survives_json_serialisation(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Порядок ключей проверяется по сырому телу ответа, а не только по разобранному словарю."""
    source = copy.deepcopy(NESTED_GROUPS)
    hub = await _hub_with(
        make_hub,
        catalog_doc([_server("alpha", permission_groups=copy.deepcopy(source))]),
        tmp_path / "catalog.yaml",
    )
    response = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    body = json.loads(response.text)
    node = body["servers"][0]["permission_groups"]
    assert list(node) == ["version", "groups", "rest", "unknown_to_hub"]
    assert list(node["groups"][0]) == ["id", "title", "default", "tools"]
    assert list(node["rest"]) == ["limit", "enabled", "note"]


@pytest.mark.ac("AC-239")
async def test_wellknown_and_remote_config_are_untouched(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Границы (R-C7.8): ни well-known, ни /remote-config новых полей не содержат."""
    plain = catalog_doc([_server("alpha")])
    enriched = catalog_doc(
        [_server("alpha", permission_groups=copy.deepcopy(NESTED_GROUPS), type="vcs")]
    )

    hub_plain = await _hub_with(make_hub, plain, tmp_path / "plain.yaml")
    before = {
        "wellknown": (await hub_plain.get("/.well-known/opencode")).text,
        "remote": (await hub_plain.get("/remote-config", headers=bearer("sk-ok"))).text,
    }

    hub_rich = await _hub_with(make_hub, enriched, tmp_path / "rich.yaml")
    after = {
        "wellknown": (await hub_rich.get("/.well-known/opencode")).text,
        "remote": (await hub_rich.get("/remote-config", headers=bearer("sk-ok"))).text,
    }

    # Главная проверка — побайтовое равенство ответов с каталогом без сквозных полей и с ними.
    assert after == before, "well-known или /remote-config изменились от сквозных полей"
    for text in after.values():
        # Ключ "type" в well-known свой (тип MCP-сервера в конфиге OpenCode) и к R-C7 отношения
        # не имеет, поэтому ищутся только значения и имена самой ревизии.
        for marker in ("permission_groups", "unknown_to_hub", "vcs"):
            assert marker not in text, f"{marker} просочился в {text[:200]}"


# --- AC-240: файл permissions/<alias>.yaml ---------------------------------


FROM_FILE: dict[str, Any] = {"version": 9, "groups": [{"id": "from-file"}]}
# ``only_in_card`` есть только в карточке: при поэлементном слиянии ключ уцелел бы, при замене
# целиком — нет. Без этого различия слияние неотличимо от замены и проверка была бы вырождена.
FROM_CARD: dict[str, Any] = {
    "version": 1,
    "groups": [{"id": "from-card"}],
    "only_in_card": {"остаток": True},
}


def _three_servers() -> dict[str, Any]:
    return catalog_doc(
        [
            _server("alpha", permission_groups=copy.deepcopy(FROM_CARD)),
            _server("beta"),
            _server("gamma"),
        ]
    )


@pytest.mark.ac("AC-240")
async def test_file_replaces_the_card_value_entirely(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Файл главнее карточки и берётся целиком: слияния нет (R-C7.5, решения 106, 107)."""
    path = tmp_path / "catalog.yaml"
    directory = _permissions_dir(path)
    _write(directory / "alpha.yaml", copy.deepcopy(FROM_FILE))
    # Файл для несуществующего alias игнорируется, .yml файлом словаря не считается.
    _write(directory / "ghost.yaml", {"version": 100})
    _write(directory / "beta.yml", {"version": 100})

    hub = await _hub_with(make_hub, _three_servers(), path)
    servers = await _catalog_of(hub)

    node = servers["alpha"]["permission_groups"]
    assert node == FROM_FILE, "значение файла не взято целиком"
    assert node["version"] == 9
    assert [g["id"] for g in node["groups"]] == ["from-file"]
    # Значение карточки не использовано и ни с чем не слито: ключа, который есть только в
    # карточке, в ответе быть не должно (поэлементное слияние запрещено, решение 107).
    assert "only_in_card" not in node, "файл слит с карточкой вместо замены"
    assert "from-card" not in json.dumps(servers["alpha"], ensure_ascii=False)
    assert "permission_groups" not in servers["beta"], ".yml принят за файл словаря"
    assert "permission_groups" not in servers["gamma"]


@pytest.mark.ac("AC-240")
async def test_without_permissions_dir_the_card_value_is_used(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Соседнего каталога нет — прежнее поведение: значение берётся из карточки (R-C7.5)."""
    hub = await _hub_with(make_hub, _three_servers(), tmp_path / "catalog.yaml")
    servers = await _catalog_of(hub)
    assert servers["alpha"]["permission_groups"] == FROM_CARD
    assert "permission_groups" not in servers["beta"]
    assert "permission_groups" not in servers["gamma"]


@pytest.mark.ac("AC-240")
async def test_reload_picks_up_the_edited_file_without_restart(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Правка файла видна после POST /admin/catalog/reload (доп. R-C4)."""
    path = tmp_path / "catalog.yaml"
    directory = _permissions_dir(path)
    _write(directory / "alpha.yaml", copy.deepcopy(FROM_FILE))
    hub = await _hub_with(make_hub, _three_servers(), path, admin_token="adm")
    assert (await _catalog_of(hub))["alpha"]["permission_groups"]["version"] == 9

    _write(directory / "alpha.yaml", {"version": 10, "groups": [{"id": "переписано"}]})
    reloaded = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert reloaded.status_code == 200, reloaded.text

    after = (await _catalog_of(hub))["alpha"]["permission_groups"]
    assert after == {"version": 10, "groups": [{"id": "переписано"}]}


@pytest.mark.ac("AC-240")
def test_document_without_a_file_is_parsed_as_before(tmp_path: Path) -> None:
    """Каталог, разобранный не из файла, о соседнем каталоге не знает по определению (R-C7.5)."""
    from hub.catalog import parse_catalog

    path = tmp_path / "catalog.yaml"
    directory = _permissions_dir(path)
    _write(directory / "alpha.yaml", copy.deepcopy(FROM_FILE))
    catalog = parse_catalog(_three_servers())
    assert catalog.servers[0].model.permission_groups == FROM_CARD


# --- AC-241: непригодный файл словаря --------------------------------------


_BAD_FILES: list[tuple[str, str]] = [
    ("не разбирается как YAML", "groups: [unclosed\n  - {"),
    ("пуст (0 байт)", ""),
    ("содержит null", "null\n"),
    ("список", "- 1\n- 2\n"),
    ("строка", "просто строка\n"),
]


def _catalog_with_bad_file(tmp_path: Path, raw: str) -> Path:
    path = tmp_path / "catalog.yaml"
    _write(path, catalog_doc([_server("alpha")]))
    (_permissions_dir(path) / "alpha.yaml").write_text(raw, encoding="utf-8")
    return path


@pytest.mark.ac("AC-241")
@pytest.mark.parametrize(("title", "raw"), _BAD_FILES, ids=[c[0] for c in _BAD_FILES])
def test_unusable_file_breaks_the_start_with_its_path(
    tmp_path: Path, title: str, raw: str
) -> None:
    """Непригодный файл — ошибка загрузки, а не молчаливый пропуск (R-C7.6).

    Тихий пропуск отдал бы клиентам карточку без словаря разрешений — потерю, которую никто
    не заметит: файл существует и явно адресован этому серверу.
    """
    path = _catalog_with_bad_file(tmp_path, raw)
    with pytest.raises(Exception) as excinfo:
        create_app(Settings(**base_settings_kwargs(path)))
    message = str(excinfo.value)
    assert "permissions/alpha.yaml" in message, f"{title}: нет пути к файлу в {message!r}"


@pytest.mark.ac("AC-241")
@pytest.mark.parametrize(("title", "raw"), _BAD_FILES, ids=[c[0] for c in _BAD_FILES])
async def test_unusable_file_makes_reload_400_and_keeps_the_running_catalog(
    make_hub: HubFactory, tmp_path: Path, title: str, raw: str
) -> None:
    """Перечитывание отвечает 400, а действующий каталог не заменён (AC-20 не изменился)."""
    path = tmp_path / "catalog.yaml"
    hub = await _hub_with(make_hub, catalog_doc([_server("alpha")]), path, admin_token="adm")
    before = await _catalog_of(hub)
    assert "permission_groups" not in before["alpha"]

    (_permissions_dir(path) / "alpha.yaml").write_text(raw, encoding="utf-8")
    reloaded = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert reloaded.status_code == 400, f"{title}: {reloaded.text}"
    assert "permissions/alpha.yaml" in reloaded.text, title

    # Действующий каталог остался прежним: раздача коннекторов не сломана.
    assert await _catalog_of(hub) == before, title


@pytest.mark.ac("AC-241")
@pytest.mark.parametrize(("title", "raw"), _BAD_FILES, ids=[c[0] for c in _BAD_FILES])
def test_unusable_file_fails_cli_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], title: str, raw: str
) -> None:
    """``mcp-hub catalog validate`` печатает путь к файлу и завершается кодом 1 (доп. R-C5)."""
    path = _catalog_with_bad_file(tmp_path, raw)
    try:
        code = main(["catalog", "validate", "--path", str(path)])
    except SystemExit as exc:  # pragma: no cover - argparse сюда не приводит
        code = int(exc.code or 0)
    out = capsys.readouterr()
    printed = out.out + out.err
    assert code == 1, f"{title}: код {code}, вывод {printed!r}"
    assert "permissions/alpha.yaml" in printed, title
    assert "OK" not in printed, title


@pytest.mark.ac("AC-241")
def test_good_file_keeps_cli_validate_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пригодный файл CLI не ломает — иначе проверка выше падала бы на чём угодно."""
    path = tmp_path / "catalog.yaml"
    _write(path, catalog_doc([_server("alpha")]))
    _write(_permissions_dir(path) / "alpha.yaml", copy.deepcopy(FROM_FILE))
    code = main(["catalog", "validate", "--path", str(path)])
    assert code == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.ac("AC-241")
def test_directory_named_like_the_file_is_not_read_as_yaml(tmp_path: Path) -> None:
    """``permissions/<alias>.yaml`` каталогом, а не файлом, — это не файл словаря (R-C7.5)."""
    path = tmp_path / "catalog.yaml"
    (_permissions_dir(path) / "alpha.yaml").mkdir()
    app = _create_app(path, catalog_doc([_server("alpha")]))
    assert app.state.catalog.servers[0].model.permission_groups is None


# --- защита от выхода за каталог -------------------------------------------


@pytest.mark.ac("AC-240")
def test_alias_outside_the_allowed_form_never_reaches_the_filesystem(tmp_path: Path) -> None:
    """Файл ищется только по alias допустимого формата — выход за каталог невозможен (R-C7.5).

    Alias проверяется схемой позже, поэтому подстановка обязана сама отсекать имена вне
    ``ALIAS_RE``. Проверка сделана падающей: по пути, куда привела бы наивная склейка, лежит
    заведомо непригодный файл. Если бы его прочитали, ошибка была бы о файле словаря; правило
    требует, чтобы дело до чтения не дошло и ошибка была о формате alias.
    """
    path = tmp_path / "catalog.yaml"
    _permissions_dir(path)
    # tmp_path/pwned.yaml — то, что открыла бы склейка permissions/../pwned.yaml.
    (tmp_path / "pwned.yaml").write_text("[не, словарь", encoding="utf-8")

    message = _expect_load_error(path, catalog_doc([_server("../pwned")]), "alias")
    assert "permissions/" not in message, f"файл словаря всё-таки искали: {message}"
    assert "pwned.yaml" not in message, f"файл словаря всё-таки читали: {message}"


# --- AC-242: изоляция от логики Hub ----------------------------------------


# Волатильные заголовки: идентификатор MCP-сессии выдаёт мок по счётчику, поэтому между двумя
# прогонами он различается по устройству стенда, а не из-за сквозных полей.
VOLATILE_HEADERS = {"mcp-session-id", "x-request-id", "content-length"}


def _stable_headers(recorded: Any) -> dict[str, str]:
    return {k: v for k, v in recorded.headers.items() if k.lower() not in VOLATILE_HEADERS}


async def _run_scenario(hub: Hub, upstream: Any) -> dict[str, Any]:
    """Наблюдаемое поведение коннектора целиком: подключение, проксирование, страницы."""
    from tests.support import issue_hub_tokens

    await add_key(hub, "sk-ok", "u1")
    await web_login(hub, "u1")
    seen = len(upstream.requests)

    connected = await connect_with_token(hub, alias="tag", token="SESSION-1")
    assert connected.status_code == 200, connected.text
    body = dict(connected.json())
    body.pop("updated_at", None)  # метка времени к сквозным полям отношения не имеет

    rows = await hub.app.state.broker.load_connection("u1", "tag")
    tokens = await issue_hub_tokens(
        hub, user_id="u1", alias="tag", connection_id=rows.id, scope="tag:readonly"
    )
    listed = await hub.post(
        "/mcp/tag", content=jsonrpc_body("tools/list"), headers=mcp_headers(tokens["access_token"])
    )
    called = await hub.post(
        "/mcp/tag",
        content=jsonrpc_body("tools/call", {"name": "whoami", "arguments": {}}, request_id=2),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert listed.status_code == 200 and called.status_code == 200, listed.text

    return {
        "connect": body,
        "tools_list": listed.json(),
        "tools_call": called.json(),
        "server_page": (await hub.get("/ui/servers/tag")).text,
        "connections_page": (await hub.get("/ui/connections")).text,
        "upstream_headers": [_stable_headers(r) for r in upstream.requests[seen:]],
        "upstream_bodies": [r.content for r in upstream.requests[seen:]],
        "catalog": await _catalog_of(hub),
    }


@pytest.mark.ac("AC-242")
async def test_passthrough_fields_change_nothing_but_two_catalog_keys(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Добавление обоих полей не меняет ничего, кроме двух ключей /api/catalog (R-C7.4, R-C7.8)."""
    plain_server = user_token_facade("tag")
    rich_server = user_token_facade("tag")
    rich_server["permission_groups"] = copy.deepcopy(NESTED_GROUPS)
    rich_server["type"] = "messenger"

    hub_plain = await make_hub(
        catalog=catalog_doc([plain_server]), path=tmp_path / "plain.yaml",
        env=TAG_ENV, base_url="https://hub.test",
    )
    assert hub_plain.net is not None
    upstream = hub_plain.net.upstreams["tag"]
    plain = await _run_scenario(hub_plain, upstream)

    hub_rich = await make_hub(
        catalog=catalog_doc([rich_server]), path=tmp_path / "rich.yaml",
        env=TAG_ENV, base_url="https://hub.test",
    )
    rich = await _run_scenario(hub_rich, upstream)

    # Проксирование: ответы и то, что ушло в целевую систему, совпадают полностью.
    assert rich["tools_list"] == plain["tools_list"]
    assert rich["tools_call"] == plain["tools_call"]
    assert rich["upstream_headers"] == plain["upstream_headers"], "заголовки к upstream разошлись"
    assert rich["upstream_bodies"] == plain["upstream_bodies"]

    # Подключение и страницы: пресеты, группы, статус и HTML одинаковы.
    assert rich["connect"] == plain["connect"]
    assert rich["server_page"] == plain["server_page"], "витрина показывает сквозные поля"
    assert rich["connections_page"] == plain["connections_page"]

    # Единственное отличие — два ключа в элементе каталога.
    plain_card, rich_card = plain["catalog"]["tag"], rich["catalog"]["tag"]
    assert set(rich_card) - set(plain_card) == {"permission_groups", "type"}
    assert set(plain_card) - set(rich_card) == set()
    assert {k: v for k, v in rich_card.items() if k not in {"permission_groups", "type"}} == (
        plain_card
    )


# --- AC-243: секреты и подстановки внутри узла -----------------------------


SECRET_VALUE = "ЗНАЧЕНИЕ-СЕКРЕТА-НАРУЖУ-НЕ-ИДЁТ"
REF_NODE = {"$ref": "#/servers/alpha/permission_model"}


@pytest.mark.ac("AC-243")
def test_env_ref_inside_the_node_is_forbidden_in_the_card(tmp_path: Path) -> None:
    """(а) ``env:VAR`` внутри узла карточки — ошибка: узел уходит наружу целиком (R-C7.7, R-K3)."""
    document = catalog_doc(
        [_server("alpha", permission_groups={"groups": [{"secret": "env:TAG_OAUTH_CLIENT_SECRET"}]})]
    )
    message = _expect_load_error(
        tmp_path / "catalog.yaml", document, "permission_groups",
        env={"TAG_OAUTH_CLIENT_SECRET": SECRET_VALUE},
    )
    assert SECRET_VALUE not in message, "значение переменной попало в сообщение об ошибке"


@pytest.mark.ac("AC-243")
def test_env_ref_inside_the_node_is_forbidden_in_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """(б) то же для файла словаря — с путём к файлу и без значения переменной."""
    from tests.support import capture_all_levels, capture_json_logs, record_text

    path = tmp_path / "catalog.yaml"
    _write(path, catalog_doc([_server("alpha")]))
    _write(_permissions_dir(path) / "alpha.yaml",
           {"groups": [{"secret": "env:TAG_OAUTH_CLIENT_SECRET"}]})
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs, pytest.raises(Exception) as excinfo:
        create_app(
            Settings(**base_settings_kwargs(path)),
            catalog_env={"TAG_OAUTH_CLIENT_SECRET": SECRET_VALUE},
        )
    message = str(excinfo.value)
    assert "permissions/alpha.yaml" in message, message
    assert SECRET_VALUE not in message
    logged = "\n".join([record_text(r) for r in caplog.records] + json_logs.raw())
    assert SECRET_VALUE not in logged, "значение переменной попало в журнал"


@pytest.mark.ac("AC-243")
async def test_template_inside_the_node_stays_verbatim(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """(в) ``${VAR}`` внутри узла не подставляется даже при заданной переменной (решение 108)."""
    document = catalog_doc(
        [_server("alpha", permission_groups={"groups": [{"title": "${SOME_VAR}"}]})]
    )
    hub = await make_hub(
        catalog=document, path=tmp_path / "catalog.yaml",
        env={**TAG_ENV, "SOME_VAR": "подставлено"}, base_url="https://hub.test",
    )
    await seed_user_with_key(hub.app, "sk-ok")
    servers = await _catalog_of(hub)

    node = servers["alpha"]["permission_groups"]
    assert node["groups"][0]["title"] == "${SOME_VAR}", "строку внутри узла подставили"
    assert "подставлено" not in json.dumps(servers, ensure_ascii=False)


@pytest.mark.ac("AC-243")
def test_unset_template_inside_the_node_does_not_break_the_server(tmp_path: Path) -> None:
    """Незаданная переменная внутри узла не роняет загрузку и не делает сервер unconfigured.

    Ровно ради этого подстановка внутри узла и запрещена (решение 108): опечатка в словаре,
    которого Hub не понимает, иначе молча убрала бы сервер из выдачи.
    """
    document = catalog_doc(
        [_server("alpha", permission_groups={"groups": [{"title": "${НЕТ_ТАКОЙ_ПЕРЕМЕННОЙ}"}]})]
    )
    app = _create_app(tmp_path / "catalog.yaml", document, env={})
    entry = app.state.catalog.servers[0]
    assert entry.alias == "alpha"
    assert entry.status != "unconfigured", "сервер помечен unconfigured из-за узла"
    assert entry.model.permission_groups["groups"][0]["title"] == "${НЕТ_ТАКОЙ_ПЕРЕМЕННОЙ}"


@pytest.mark.ac("AC-243")
async def test_nested_ref_is_data_and_travels_verbatim(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """(г) вложенный внутрь узла ``$ref`` — просто данные и уходит клиенту дословно (R-C7.7)."""
    document = catalog_doc(
        [_server("alpha", permission_groups={"groups": [copy.deepcopy(REF_NODE)]})]
    )
    hub = await _hub_with(make_hub, document, tmp_path / "catalog.yaml")
    node = (await _catalog_of(hub))["alpha"]["permission_groups"]
    assert node["groups"][0] == REF_NODE, "вложенный $ref раскрыли вместо передачи как данных"


@pytest.mark.ac("AC-243")
def test_ref_in_place_of_the_field_is_an_error(tmp_path: Path) -> None:
    """(д) ``$ref`` на месте самого поля — ошибка: автор имел в виду ссылку R-C3 (R-C7.7)."""
    document = catalog_doc([_server("alpha", permission_groups=copy.deepcopy(REF_NODE))])
    _expect_load_error(tmp_path / "catalog.yaml", document,
                       "servers[0].permission_groups", "$ref")


@pytest.mark.ac("AC-243")
def test_external_ref_to_the_node_is_an_error(tmp_path: Path) -> None:
    """(е) ссылка извне на ``#/servers/<alias>/permission_groups`` — ошибка (R-C7.7)."""
    alpha = _server("alpha", permission_groups=copy.deepcopy(SIMPLE_GROUPS))
    beta = _server("beta", permission_model={"$ref": "#/servers/alpha/permission_groups"})
    _expect_load_error(tmp_path / "catalog.yaml", catalog_doc([alpha, beta]),
                       "permission_groups")


@pytest.mark.ac("AC-243")
async def test_substitutions_in_other_fields_still_work(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Правила R-C2/R-C3 в остальных полях карточки не ослаблены (AC-12, AC-14…AC-16)."""
    alpha = _server("alpha", permission_groups=copy.deepcopy(SIMPLE_GROUPS))
    alpha["mcp_url"] = "${SOME_URL}"
    beta = _server("beta", permission_model={"$ref": "#/servers/alpha/permission_model"})
    hub = await make_hub(
        catalog=catalog_doc([alpha, beta]), path=tmp_path / "catalog.yaml",
        env={"SOME_URL": "https://подставлено.test/mcp"}, base_url="https://hub.test",
    )
    await seed_user_with_key(hub.app, "sk-ok")
    servers = await _catalog_of(hub)

    # ${VAR} в обычном поле подставлен, $ref в обычном поле раскрыт.
    assert servers["alpha"]["mcp_url"] == "https://подставлено.test/mcp"
    assert servers["beta"]["permission_model"] == servers["alpha"]["permission_model"]


# --- ответ не делит память с каталогом -------------------------------------


@pytest.mark.ac("AC-239")
def test_public_view_does_not_share_the_node_with_the_catalog(tmp_path: Path) -> None:
    """Публичное представление отдаёт копию узла: правка ответа не меняет каталог (R-C7.2).

    Без копии узел ответа был бы тем же объектом, что и в каталоге, и любой, кто дописал бы в
    него ключ по дороге, испортил бы раздачу всем следующим запросам до перечитывания.
    """
    source = copy.deepcopy(NESTED_GROUPS)
    app = _create_app(
        tmp_path / "catalog.yaml",
        catalog_doc([_server("alpha", permission_groups=copy.deepcopy(source))]),
    )
    entry = app.state.catalog.servers[0]

    view = entry.public_view("https://hub.test")
    node = view["permission_groups"]
    assert node == source
    assert node is not entry.model.permission_groups, "узел ответа — тот же объект, что в каталоге"

    # Портим ответ так, как это сделал бы посторонний код на пути к клиенту.
    node["ИСПОРЧЕНО"] = True
    node["groups"].append({"id": "лишняя"})
    node["rest"]["limit"] = 999

    assert entry.model.permission_groups == source, "правка ответа изменила каталог"
    assert entry.public_view("https://hub.test")["permission_groups"] == source
