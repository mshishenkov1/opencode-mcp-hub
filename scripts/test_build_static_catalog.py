"""Тесты сборщика статического каталога (``scripts/build-static-catalog.py``, правило S-C10).

Файл лежит рядом со скриптом, а не в ``tests/``: ``tests/`` — зона TEST-агента
(``pipeline.config.yaml``), а скрипт сборки в неё не входит. Прогон — явный:
``.venv/bin/python -m pytest scripts/test_build_static_catalog.py``; так же его зовёт задание
``static-catalog`` в ``.gitlab-ci.yml``.

Проверяется то, из-за чего файл раздаётся без авторизации и потому не прощает ошибок: конверт,
совпадающий с ``GET /api/catalog``; отсутствие секретов, внутренних адресов и неподставленных
переменных; и то, что sha256 рядом действительно описывает записанный файл.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "build_static_catalog", Path(__file__).with_name("build-static-catalog.py")
)
assert _SPEC and _SPEC.loader
bsc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bsc)


PUBLIC_URL = "https://hub.test"

NATIVE = {
    "alias": "tag",
    "title": "ТЭГ",
    "description": "Мессенджер",
    "owner": "AI Lab",
    "status": "beta",
    "audience": ["all"],
    "mode": "native",
    "mcp_url": "${TAG_MCP_URL}",
    "permission_model": {"kind": "consent", "presets": {"readonly": {"write_mode": "readonly"}}},
}

FACADE = {
    "alias": "gitlab",
    "title": "GitLab",
    "description": "Репозитории",
    "owner": "AI Lab",
    "status": "ga",
    "audience": ["all"],
    "mode": "facade",
    "upstream_url": "https://internal.mcp/gitlab",
    "auth": {
        "type": "oauth2",
        "authorize_url": "https://gl.test/oauth/authorize",
        "token_url": "https://gl.test/oauth/token",
        "client_id": "${GITLAB_CLIENT_ID}",
        "client_secret": "env:GITLAB_CLIENT_SECRET",
        "pkce": True,
        "scopes": {"readonly": ["read_api"], "readwrite": ["api"]},
    },
    "credential_headers": {"Authorization": "Bearer {{access_token}}"},
    "permission_model": {
        "kind": "header_groups",
        "header": "Enabled-Groups",
        "groups": [{"id": "core", "title": "Основное", "preset": "readonly"}],
    },
}

ENV = {"TAG_MCP_URL": "https://tag.test/mcp", "GITLAB_CLIENT_ID": "cid-1"}


def write_catalog(tmp_path: Path, servers: list[dict], version: int = 1) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"version": version, "servers": servers}, allow_unicode=True), encoding="utf-8")
    return path


def build(tmp_path: Path, servers: list[dict], *, permissions: Path | None = None, **kwargs):
    return bsc.build(
        catalog_path=write_catalog(tmp_path, servers),
        permissions_dir=permissions or (tmp_path / "нет-такого"),
        public_url=PUBLIC_URL,
        env=dict(ENV),
        **kwargs,
    )


# --- Конверт (S-C10 п.4) ---------------------------------------------------


def test_конверт_совпадает_с_ответом_hub(tmp_path: Path) -> None:
    document, _ = build(tmp_path, [NATIVE, FACADE])

    assert set(document) == {"version", "servers"}
    assert document["version"] == 1
    # Порядок серверов — как в файле (I-1 R-A3): витрина показывает их в этом порядке.
    assert [s["alias"] for s in document["servers"]] == ["tag", "gitlab"]


def test_карточка_несёт_поля_публичного_представления(tmp_path: Path) -> None:
    document, _ = build(tmp_path, [NATIVE, FACADE])
    tag, gitlab = document["servers"]

    assert tag["mcp_url"] == "https://tag.test/mcp"
    # Facade-карточка ведёт на Hub, а не на внутренний адрес апстрима.
    assert gitlab["mcp_url"] == f"{PUBLIC_URL}/mcp/gitlab"
    assert gitlab["permission_model"]["kind"] == "header_groups"
    assert gitlab["auth_kind"] == "oauth2"


def test_поля_connection_в_статическом_каталоге_нет(tmp_path: Path) -> None:
    # S-C10 п.4: у файла нет пользователя, а состояние карточки клиент считает по локальным данным.
    document, _ = build(tmp_path, [NATIVE, FACADE])
    assert all("connection" not in server for server in document["servers"])


# --- Секреты (R-C6, S-C10) -------------------------------------------------


def test_секреты_и_внутренние_адреса_наружу_не_попадают(tmp_path: Path) -> None:
    document, _ = build(tmp_path, [NATIVE, FACADE])
    text = json.dumps(document, ensure_ascii=False)

    for forbidden in ("client_secret", "GITLAB_CLIENT_SECRET", "env:", "internal.mcp", "credential_headers"):
        assert forbidden not in text, forbidden


def test_проверка_ловит_запрещённое_поле_в_словаре_разрешений(tmp_path: Path) -> None:
    # Словарь разрешений кладётся в карточку дословно, поэтому он — такой же вход, как catalog.yaml.
    # verify/exchange — блоки способа user_token: их содержимое наружу не отдаётся даже усечённым.
    permissions = tmp_path / "perm"
    permissions.mkdir()
    (permissions / "tag.yaml").write_text(
        yaml.safe_dump({"version": 1, "groups": [], "verify": {"url": "https://internal"}}), encoding="utf-8"
    )

    with pytest.raises(bsc.BuildError) as exc:
        build(tmp_path, [NATIVE], permissions=permissions)
    assert "verify" in str(exc.value)


def test_проверка_ловит_ссылку_на_секрет_и_неподставленную_переменную() -> None:
    problems = bsc.audit({"servers": [{"a": "env:SECRET", "b": "${MISSING}", "c": "ok"}]})
    assert len(problems) == 2
    assert any("env:VAR" in p for p in problems)
    assert any("${VAR}" in p for p in problems)


# --- Граница публикации способов подключения (R-U8.1, ревизия 4.4) ----------
#
# `public_view` этой ветки способов подключения ещё не отдаёт, поэтому проверка идёт не сквозняком,
# а на фикстурах: тело собрано ровно в той форме, в какой его отдаёт `ServerEntry.public_view` и
# `public_auth_methods` ветки i3. Так аудит проверяется до слияния — а слияние без него вырезало бы
# из статического каталога поля, без которых сборка без Hub не подключается вовсе.

#: Значение переменной окружения, подставленной в заголовок: после `${VAR}` оно неотличимо от
#: литерала, и поймать его можно только сравнением со значением переменной.
GW_SECRET = "gw-live-Zx91"
SECRET_VALUES = frozenset({GW_SECRET})

VERIFY_BLOCK = {
    "url": "https://tag.test/api/me",
    "method": "GET",
    "headers": {"Authorization": "Bearer {{access_token}}"},
    "expect_status": 200,
    "account_field": "login",
    "require_account": True,
}

EXCHANGE_BLOCK = {
    "url": "https://tag.test/api/tokens",
    "method": "POST",
    "headers": {"Authorization": "Bearer {{access_token}}"},
    "body": {"name": "opencode"},
    "expect_status": 201,
    "token_field": "token",
    "token_id_field": "id",
    "description": "Постоянный токен OpenCode",
    "revoke": {
        "url": "https://tag.test/api/tokens/revoke",
        "method": "POST",
        "headers": {"Authorization": "Bearer {{access_token}}"},
        "body": None,
        "expect_status": 204,
    },
}

OAUTH_METHOD = {
    "id": "oauth",
    "title": "OAuth",
    "type": "oauth2",
    "available": True,
    "unavailable_reason": None,
    "issues_permanent_token": False,
}

USER_TOKEN_METHOD = {
    "id": "token",
    "title": "Личный токен",
    "type": "user_token",
    "available": True,
    "unavailable_reason": None,
    "issues_permanent_token": True,
    # `secret: true` — признак «маскировать ввод», а не секрет: поле публиковалось и до ревизии.
    "field": {"label": "Токен", "hint": None, "docs_url": None, "secret": True,
              "placeholder": None, "min_length": 8, "max_length": 512},
    "verify": VERIFY_BLOCK,
    "exchange": EXCHANGE_BLOCK,
}

DIRECT_CARD = {
    "alias": "tag",
    "title": "ТЭГ",
    "status": "ga",
    "mode": "facade",
    "mcp_url": "https://hub.test/mcp/tag",
    "permission_model": {"kind": "consent", "presets": {}},
    "auth_kind": "user_token",
    "auth_methods": [USER_TOKEN_METHOD, OAUTH_METHOD],
    "upstream": {
        "url": "https://mcp-tag.corp/mcp",
        "credential_headers": {"Authorization": "Bearer {{access_token}}"},
        "static_headers": {"X-Client": "opencode"},
    },
}

OAUTH_CARD = {
    "alias": "gitlab",
    "title": "GitLab",
    "status": "ga",
    "mode": "facade",
    "mcp_url": "https://hub.test/mcp/gitlab",
    "permission_model": {"kind": "header_groups", "groups": [], "always": []},
    "auth_kind": "oauth2",
    "auth_methods": [dict(OAUTH_METHOD)],
}


def body(*servers: dict) -> dict:
    """Конверт `GET /api/catalog` с глубокой копией карточек: тест правит свою, а не общую."""
    return {"version": 7, "servers": [copy.deepcopy(s) for s in servers]}


def check(document: dict) -> list[str]:
    return bsc.audit(document, secret_values=SECRET_VALUES)


def test_публикуемые_поля_доступного_user_token_аудит_пропускает() -> None:
    # R-U8.1 пп. 2, 4: verify, exchange (вместе с revoke) и блок upstream карточки — это ровно то,
    # без чего приложение не подключится напрямую, и вырезать их сборщику больше нельзя.
    assert check(body(DIRECT_CARD, OAUTH_CARD)) == []


def test_подставленный_адрес_публикуется_а_неподставленный_ловится() -> None:
    # R-U8.1 п. 6: четыре адреса — единственное исключение, они идут наружу после подстановки.
    ok = body(DIRECT_CARD)
    ok["servers"][0]["upstream"]["url"] = "https://tag.test/mcp"
    assert check(ok) == []

    broken = body(DIRECT_CARD)
    broken["servers"][0]["auth_methods"][0]["verify"]["url"] = "${TAG_VERIFY_URL}"
    assert any("${VAR}" in p for p in check(broken))


@pytest.mark.parametrize(
    "where",
    [
        ("auth_methods", 0, "verify"),
        ("auth_methods", 0, "exchange"),
    ],
)
def test_блоки_способа_у_недоступного_user_token_запрещены(where: tuple) -> None:
    # R-U8.1 п. 3 (R-U1, решение 73): значения недоступного способа наружу не отдаются.
    document = body(DIRECT_CARD)
    document["servers"][0]["auth_methods"][0]["available"] = False
    problems = check(document)
    assert any(where[-1] in p and "запрещённое поле" in p for p in problems)


def test_блок_verify_у_способа_oauth2_запрещён_как_прежде() -> None:
    # R-U8.1 п. 1: у oauth2 граница не изменилась ни на одно поле — рядом лежит client_secret.
    document = body(DIRECT_CARD)
    document["servers"][0]["auth_methods"][1]["verify"] = copy.deepcopy(VERIFY_BLOCK)
    assert any("auth_methods[1].verify" in p for p in check(document))


def test_upstream_у_карточки_без_доступного_user_token_запрещён() -> None:
    # R-U8.1 п. 4: адрес цели отдаётся только карточке, объявившей доступный user_token.
    document = body(OAUTH_CARD)
    document["servers"][0]["upstream"] = {
        "url": "https://internal.mcp/gitlab",
        "credential_headers": {"Authorization": "Bearer {{access_token}}"},
    }
    problems = check(document)
    assert any("без доступного способа user_token" in p for p in problems)
    assert any("credential_headers" in p and "запрещённое поле" in p for p in problems)


# --- Аудит не ослаб --------------------------------------------------------


def test_client_secret_внутри_открытого_блока_ловится() -> None:
    document = body(DIRECT_CARD)
    document["servers"][0]["auth_methods"][0]["exchange"]["client_secret"] = "s3cr3t"
    assert any("client_secret" in p for p in check(document))


def test_ссылка_env_var_в_credential_headers_ловится() -> None:
    # AC-14: ни значение переменной, ни само имя с префиксом env: наружу не идут никогда.
    document = body(DIRECT_CARD)
    document["servers"][0]["upstream"]["credential_headers"]["X-Gw"] = "env:GW_SECRET"
    assert any("env:VAR" in p for p in check(document))


@pytest.mark.parametrize(
    "where",
    [
        ("upstream", "static_headers"),
        ("upstream", "credential_headers"),
        ("auth_methods", 0, "verify", "headers"),
        ("auth_methods", 0, "exchange", "headers"),
        ("auth_methods", 0, "exchange", "body"),
        ("auth_methods", 0, "exchange", "revoke", "headers"),
    ],
)
def test_значение_подставленной_переменной_в_заголовке_ловится(where: tuple) -> None:
    # Главный случай: `${GW_SECRET}` разворачивается ДО валидации схемы, поэтому в готовом теле
    # заголовок неотличим от литерала. Именно static_headers схема назначает местом для секретов.
    document = body(DIRECT_CARD)
    node = document["servers"][0]
    for part in where:
        node = node[part]
    node["X-Gw"] = GW_SECRET
    assert any("значение переменной окружения" in p for p in check(document))


def test_expiry_и_exchange_list_наружу_не_идут() -> None:
    # R-U8.1 п. 5: срок годности токена (R-U18) и список выпущенных (R-U15.3) исполняет Hub.
    with_expiry = body(DIRECT_CARD)
    with_expiry["servers"][0]["auth_methods"][0]["expiry"] = {"field": "expires_at"}
    assert any("expiry" in p for p in check(with_expiry))

    with_list = body(DIRECT_CARD)
    with_list["servers"][0]["auth_methods"][0]["exchange"]["list"] = {"url": "https://tag.test/x"}
    assert any("exchange.list" in p for p in check(with_list))


def test_имя_заголовка_групп_разрешений_наружу_не_идёт() -> None:
    # R-C6: permission_model.header остаётся закрытым; в словарях разрешений слово header законно.
    document = body(OAUTH_CARD)
    document["servers"][0]["permission_model"]["header"] = "Enabled-Groups"
    assert any("permission_model.header" in p for p in check(document))


def test_неизвестное_поле_открытого_блока_останавливает_сборку() -> None:
    # Перечень разрешённого (решение 120): поле, добавленное в public_view завтра, не утечёт молча.
    document = body(DIRECT_CARD)
    document["servers"][0]["upstream"]["proxy_secret"] = "abc"
    assert any("не входит в состав публикуемого блока" in p for p in check(document))


def test_послабление_field_secret_только_для_признака() -> None:
    # `secret: true` — булев признак маскировки ввода; строка с тем же именем остаётся запрещённой.
    document = body(DIRECT_CARD)
    document["servers"][0]["auth_methods"][0]["field"]["secret"] = "s3cr3t"
    assert any("field.secret" in p for p in check(document))


def test_ненастроенный_сервер_пропускается_с_причиной(tmp_path: Path) -> None:
    document, report = bsc.build(
        catalog_path=write_catalog(tmp_path, [NATIVE]),
        permissions_dir=tmp_path / "нет",
        public_url=PUBLIC_URL,
        env={},
    )
    assert document["servers"] == []
    assert "TAG_MCP_URL" in report[0]


# --- Отбор серверов --------------------------------------------------------


def test_ограниченная_аудитория_в_публичный_файл_не_попадает(tmp_path: Path) -> None:
    limited = {**NATIVE, "alias": "secret-one", "audience": ["ai-lab"]}
    document, report = build(tmp_path, [limited])

    assert document["servers"] == []
    assert "secret-one" in report[0]


def test_deprecated_включается_по_умолчанию_и_снимается_флагом(tmp_path: Path) -> None:
    old = {**NATIVE, "status": "deprecated"}

    included, _ = build(tmp_path, [old])
    excluded, report = build(tmp_path, [old], include_deprecated=False)

    assert [s["alias"] for s in included["servers"]] == ["tag"]
    assert excluded["servers"] == []
    assert "deprecated" in report[0]


# --- Словари разрешений (S-V20) --------------------------------------------


def test_словарь_разрешений_прикладывается_дословно_по_alias(tmp_path: Path) -> None:
    permissions = tmp_path / "perm"
    permissions.mkdir()
    dictionary = {
        "version": 1,
        "groups": [{"id": "read", "title": "Читать", "default": "allow", "tools": ["get_post"]}],
        "rest": {"title": "Остальное", "default": "ask"},
    }
    (permissions / "tag.yaml").write_text(yaml.safe_dump(dictionary, allow_unicode=True), encoding="utf-8")

    document, report = build(tmp_path, [NATIVE, FACADE], permissions=permissions)
    tag, gitlab = document["servers"]

    assert tag["permission_groups"] == dictionary
    # Словаря нет — поля нет вовсе: пустой словарь клиент разобрал бы как испорченный.
    assert "permission_groups" not in gitlab
    assert report == []


def test_словарь_без_сервера_называется_в_отчёте(tmp_path: Path) -> None:
    permissions = tmp_path / "perm"
    permissions.mkdir()
    (permissions / "нетакого.yaml").write_text(yaml.safe_dump({"version": 1, "groups": []}), encoding="utf-8")

    _, report = build(tmp_path, [NATIVE], permissions=permissions)
    assert any("нетакого.yaml" in line for line in report)


def test_испорченный_словарь_останавливает_сборку(tmp_path: Path) -> None:
    permissions = tmp_path / "perm"
    permissions.mkdir()
    (permissions / "tag.yaml").write_text("- список, а не объект\n", encoding="utf-8")

    with pytest.raises(bsc.BuildError):
        build(tmp_path, [NATIVE], permissions=permissions)


# --- Запись ----------------------------------------------------------------


def test_текст_детерминирован_и_не_экранирует_кириллицу(tmp_path: Path) -> None:
    document, _ = build(tmp_path, [NATIVE])
    text = bsc.render(document)

    assert text.endswith("\n")
    assert "ТЭГ" in text
    assert bsc.render(document) == text


def test_sha256_описывает_записанный_файл(tmp_path: Path) -> None:
    document, _ = build(tmp_path, [NATIVE, FACADE])
    out = tmp_path / "dist" / "catalog.json"

    digest = bsc.write(out, bsc.render(document))
    line = out.with_name("catalog.json.sha256").read_text(encoding="utf-8")

    assert digest == hashlib.sha256(out.read_bytes()).hexdigest()
    # Формат `sha256sum`: две пробельные позиции между суммой и именем, имя — без пути.
    assert line == f"{digest}  catalog.json\n"


def test_без_public_url_сборка_отказывает(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = bsc.main(["--catalog", str(write_catalog(tmp_path, [NATIVE])), "--public-url", ""])

    assert code == 2
    assert "public-url" in capsys.readouterr().err


def test_main_собирает_файл_и_печатает_сводку(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "dist" / "catalog.json"

    code = bsc.main(
        [
            "--catalog",
            str(write_catalog(tmp_path, [NATIVE, FACADE])),
            "--permissions",
            str(tmp_path / "нет"),
            "--public-url",
            PUBLIC_URL + "/",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    body = json.loads(out.read_text(encoding="utf-8"))
    assert [s["alias"] for s in body["servers"]] == ["tag", "gitlab"]
    # Хвостовой слэш адреса Hub не удваивается в mcp_url.
    assert body["servers"][1]["mcp_url"] == f"{PUBLIC_URL}/mcp/gitlab"
    assert "sha256" in capsys.readouterr().out


def test_ошибка_каталога_печатается_и_даёт_код_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "catalog.yaml"
    broken.write_text("version: 0\nservers: []\n", encoding="utf-8")

    code = bsc.main(["--catalog", str(broken), "--public-url", PUBLIC_URL, "--out", str(tmp_path / "c.json")])

    assert code == 2
    assert "version" in capsys.readouterr().err
