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
