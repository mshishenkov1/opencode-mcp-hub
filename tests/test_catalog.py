"""Каталог (R-C1..R-C6): AC-07..AC-20, AC-22, AC-23 (AC-21 — в test_cli.py)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from hub.app import create_app
from hub.settings import Settings
from tests.conftest import Hub, HubFactory, base_settings_kwargs
from tests.support import (
    audit_rows,
    bearer,
    catalog_doc,
    facade_server,
    native_server,
    seed_user_with_key,
    write_catalog,
)

REPO_CATALOG = Path(__file__).resolve().parents[1] / "catalog.yaml"
REPO_CATALOG_VARS = {
    "TAG_MCP_URL": "https://tag-mcp.test/mcp",
    "GITLAB_OAUTH_CLIENT_ID": "gl-client",
    "GITLAB_PLATFORM_OAUTH_CLIENT_ID": "glp-client",
    "JIRA_OAUTH_CLIENT_ID": "jira-client",
    "CONFLUENCE_OAUTH_CLIENT_ID": "conf-client",
}


def _create_app_with_catalog(
    path: Path, document: dict[str, Any] | str, env: dict[str, str] | None = None
) -> Any:
    write_catalog(path, document)
    settings = Settings(**base_settings_kwargs(path))
    return create_app(settings, catalog_env=env)


def _expect_catalog_error(
    tmp_path: Path,
    document: dict[str, Any] | str,
    *fragments: str,
    env: dict[str, str] | None = None,
) -> str:
    with pytest.raises(Exception) as excinfo:
        _create_app_with_catalog(tmp_path / "catalog.yaml", document, env)
    message = str(excinfo.value)
    for fragment in fragments:
        assert fragment in message, f"ожидалось {fragment!r} в сообщении: {message}"
    return message


# --- AC-07 -----------------------------------------------------------------


@pytest.mark.ac("AC-07")
async def test_repo_catalog_loads_at_start(
    make_hub: HubFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in REPO_CATALOG_VARS.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "repo-catalog.yaml"
    shutil.copy(REPO_CATALOG, path)
    hub: Hub = await make_hub(catalog=None, path=path)
    health = await hub.get("/health")
    assert health.status_code == 200
    assert health.json()["catalog_version"] == 1
    await seed_user_with_key(hub.app, "sk-ok")
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    aliases = [s["alias"] for s in catalog["servers"]]
    assert aliases == ["tag", "gitlab", "gitlab-platform", "jira", "confluence"]


@pytest.mark.ac("AC-07")
@pytest.mark.ac("AC-13")
async def test_repo_catalog_without_vars_hides_beta_servers(
    make_hub: HubFactory, tmp_path: Path
) -> None:
    """Каталог репозитория без ${…}: серверы каталога либо beta (→ unconfigured), либо стартуют;
    старт не должен падать из-за отсутствующих переменных beta-серверов."""
    path = tmp_path / "repo-catalog.yaml"
    shutil.copy(REPO_CATALOG, path)
    hub: Hub = await make_hub(catalog=None, path=path, env={})
    assert (await hub.get("/health")).json()["catalog_version"] == 1
    await seed_user_with_key(hub.app, "sk-ok")
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    for server in catalog["servers"]:
        assert "${" not in str(server)
    wellknown = (await hub.get("/.well-known/opencode")).json()
    assert "${" not in str(wellknown["config"]["mcp"])


# --- AC-08 ---------------------------------------------------------------------


@pytest.mark.ac("AC-08")
def test_missing_required_field_reports_path(tmp_path: Path) -> None:
    second = native_server("tag")
    del second["title"]
    _expect_catalog_error(
        tmp_path, catalog_doc([facade_server("gitlab"), second]), "servers[1].title"
    )


@pytest.mark.ac("AC-08")
@pytest.mark.parametrize(
    "field", ["description", "owner", "status", "audience", "mode", "permission_model"]
)
def test_missing_other_required_fields(tmp_path: Path, field: str) -> None:
    server = native_server("tag")
    del server[field]
    _expect_catalog_error(tmp_path, catalog_doc([server]), f"servers[0].{field}")


@pytest.mark.ac("AC-08")
def test_empty_required_string_rejected(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path, catalog_doc([native_server("tag", title="")]), "servers[0].title"
    )


@pytest.mark.ac("AC-08")
def test_unknown_field_rejected_strict_schema(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path, catalog_doc([native_server("tag", surprise=1)]), "servers[0].surprise"
    )


@pytest.mark.ac("AC-08")
def test_nested_auth_field_missing_reports_nested_path(tmp_path: Path) -> None:
    server = facade_server("gitlab")
    del server["auth"]["client_id"]
    _expect_catalog_error(tmp_path, catalog_doc([server]), "servers[0].auth.client_id")


@pytest.mark.ac("AC-08")
@pytest.mark.parametrize(
    "document, fragment",
    [
        ({"servers": []}, "version"),
        ({"version": 0, "servers": []}, "version"),
        ({"version": 1}, "servers"),
        ("- just\n- a list\n", "каталог"),
        ("version: 1\nservers: [\n", ""),
    ],
)
def test_top_level_schema_errors(tmp_path: Path, document: Any, fragment: str) -> None:
    _expect_catalog_error(tmp_path, document, fragment)


# --- AC-09 ---------------------------------------------------------------------


@pytest.mark.ac("AC-09")
@pytest.mark.parametrize(
    "alias", ["Bad_Alias", "-x", "1abc", "ABC", "with space", "a" * 33, "-abc", "", "a_b", "a-B"]
)
def test_invalid_alias_reports_path(tmp_path: Path, alias: str) -> None:
    _expect_catalog_error(tmp_path, catalog_doc([native_server(alias)]), "servers[0].alias")


@pytest.mark.ac("AC-09")
def test_duplicate_alias_reports_alias(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path, catalog_doc([facade_server("gitlab"), facade_server("gitlab")]), "gitlab"
    )


@pytest.mark.ac("AC-09")
@pytest.mark.parametrize(
    "alias", ["a", "a" * 32, "ab", "gitlab-platform2", "a-", "a0", "z" + "-" * 31]
)
def test_valid_aliases_accepted(tmp_path: Path, alias: str) -> None:
    """Ревизия 1.1: alias 1–32 символа (^[a-z][a-z0-9-]{0,31}$) — контрольные 'a' и 32 символа."""
    doc = catalog_doc([native_server(alias)])
    assert _create_app_with_catalog(tmp_path / "c.yaml", doc) is not None


@pytest.mark.ac("AC-09")
async def test_single_char_alias_visible_in_catalog_and_wellknown(make_hub: HubFactory) -> None:
    """Контрольный валидный alias 'a' проходит весь путь: загрузка → /api/catalog → well-known."""
    hub = await make_hub(catalog=catalog_doc([native_server("a"), native_server("a" * 32)]))
    await seed_user_with_key(hub.app, "sk-ok")
    listed = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in listed["servers"]] == ["a", "a" * 32]
    mcp = (await hub.get("/.well-known/opencode")).json()["config"]["mcp"]
    assert set(mcp) == {"a", "a" * 32}


# --- AC-10 ---------------------------------------------------------------------


@pytest.mark.ac("AC-10")
def test_native_without_mcp_url_invalid(tmp_path: Path) -> None:
    server = native_server("tag")
    del server["mcp_url"]
    _expect_catalog_error(tmp_path, catalog_doc([server]), "servers[0].mcp_url")


@pytest.mark.ac("AC-10")
@pytest.mark.parametrize("field", ["auth", "upstream_url", "credential_headers"])
def test_facade_without_required_field_invalid(tmp_path: Path, field: str) -> None:
    server = facade_server("gitlab")
    del server[field]
    _expect_catalog_error(tmp_path, catalog_doc([server]), f"servers[0].{field}")


@pytest.mark.ac("AC-10")
def test_facade_with_empty_credential_headers_invalid(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path,
        catalog_doc([facade_server("gitlab", credential_headers={})]),
        "servers[0].credential_headers",
    )


# --- AC-11 ---------------------------------------------------------------------


@pytest.mark.ac("AC-11")
@pytest.mark.parametrize(
    "overrides, path",
    [
        ({"status": "alpha"}, "servers[0].status"),
        ({"mode": "proxy"}, "servers[0].mode"),
        (
            {"permission_model": {"kind": "magic", "presets": {"x": {}}}},
            "servers[0].permission_model",
        ),
        ({"audience": []}, "servers[0].audience"),
    ],
)
def test_invalid_enum_values_rejected(tmp_path: Path, overrides: dict[str, Any], path: str) -> None:
    _expect_catalog_error(tmp_path, catalog_doc([native_server("tag", **overrides)]), path)


@pytest.mark.ac("AC-11")
def test_invalid_permission_group_preset_rejected(tmp_path: Path) -> None:
    server = facade_server("gitlab")
    server["permission_model"]["groups"][0]["preset"] = "full"
    _expect_catalog_error(tmp_path, catalog_doc([server]), "servers[0].permission_model")


@pytest.mark.ac("AC-11")
def test_duplicate_group_ids_rejected(tmp_path: Path) -> None:
    server = facade_server("gitlab")
    server["permission_model"]["groups"] = [
        {"id": "x", "title": "X", "preset": "readonly"},
        {"id": "x", "title": "X2", "preset": "readwrite"},
    ]
    _expect_catalog_error(tmp_path, catalog_doc([server]), "servers[0].permission_model")


@pytest.mark.ac("AC-11")
def test_permission_kinds_consent_and_tool_filter_accepted(tmp_path: Path) -> None:
    doc = catalog_doc(
        [
            native_server("one", permission_model={"kind": "consent", "presets": {"ro": {"x": 1}}}),
            native_server(
                "two", permission_model={"kind": "tool_filter", "presets": {"ro": {"tools": ["a"]}}}
            ),
        ]
    )
    assert _create_app_with_catalog(tmp_path / "c.yaml", doc) is not None
    _expect_catalog_error(
        tmp_path,
        catalog_doc(
            [native_server("two", permission_model={"kind": "tool_filter", "presets": {}})]
        ),
        "servers[0].permission_model",
    )


# --- AC-12 ---------------------------------------------------------------------


@pytest.mark.ac("AC-12")
async def test_var_substituted_from_environment(
    make_hub: HubFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MCP_URL", "https://x.test/mcp")
    hub: Hub = await make_hub(
        catalog=catalog_doc([native_server("tag", mcp_url="${TEST_MCP_URL}")])
    )
    await seed_user_with_key(hub.app, "sk-ok")
    body = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert body["servers"][0]["mcp_url"] == "https://x.test/mcp"


@pytest.mark.ac("AC-12")
def test_missing_var_for_ga_server_fails_with_name_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_MCP_URL", raising=False)
    _expect_catalog_error(
        tmp_path,
        catalog_doc([native_server("tag", mcp_url="${TEST_MCP_URL}")]),
        "TEST_MCP_URL",
        "servers[0].mcp_url",
    )


@pytest.mark.ac("AC-12")
def test_missing_var_for_deprecated_server_fails(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path,
        catalog_doc([native_server("old", status="deprecated", mcp_url="${NOPE_URL}")]),
        "NOPE_URL",
        env={},
    )


@pytest.mark.ac("AC-12")
def test_multiple_vars_in_one_string_and_nested(tmp_path: Path) -> None:
    server = facade_server("gitlab", upstream_url="https://${HOST}:${PORT}/mcp")
    server["auth"]["client_id"] = "${CID}"
    env = {"HOST": "up.test", "PORT": "8443", "CID": "cid-1"}
    app = _create_app_with_catalog(tmp_path / "c.yaml", catalog_doc([server]), env)
    entry = app.state.catalog.get("gitlab")
    assert entry.model.upstream_url == "https://up.test:8443/mcp"
    assert entry.model.auth.client_id == "cid-1"


@pytest.mark.ac("AC-12")
def test_missing_var_in_defaults_fails(tmp_path: Path) -> None:
    doc = catalog_doc([native_server("tag")], defaults={"label": "${MISSING_DEFAULT}"})
    _expect_catalog_error(tmp_path, doc, "MISSING_DEFAULT", env={})


# --- AC-13 ---------------------------------------------------------------------


@pytest.mark.ac("AC-13")
async def test_beta_server_with_missing_var_is_unconfigured_and_hidden(
    make_hub: HubFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAG_MCP_URL", raising=False)
    hub: Hub = await make_hub(
        catalog=catalog_doc(
            [native_server("tag", status="beta", mcp_url="${TAG_MCP_URL}"), facade_server("gitlab")]
        )
    )
    await seed_user_with_key(hub.app, "sk-ok")
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    aliases = [s["alias"] for s in catalog["servers"]]
    assert "tag" not in aliases
    assert "gitlab" in aliases
    wellknown = (await hub.get("/.well-known/opencode")).json()
    assert "tag" not in wellknown["config"]["mcp"]
    assert "gitlab" in wellknown["config"]["mcp"]


@pytest.mark.ac("AC-13")
async def test_beta_server_with_var_present_is_visible(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(
        catalog=catalog_doc([native_server("tag", status="beta", mcp_url="${TAG_MCP_URL}")]),
        env={"TAG_MCP_URL": "https://tag.test/mcp"},
    )
    await seed_user_with_key(hub.app, "sk-ok")
    catalog = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in catalog["servers"]] == ["tag"]
    assert catalog["servers"][0]["status"] == "beta"


# --- AC-14 ---------------------------------------------------------------------


@pytest.mark.ac("AC-14")
async def test_env_ref_not_required_and_never_serialized(
    make_hub: HubFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GL_SECRET", raising=False)
    server = facade_server("gitlab")
    server["auth"]["client_secret"] = "env:GL_SECRET"
    server["static_headers"] = {"X-Token": "env:GL_STATIC"}
    hub: Hub = await make_hub(catalog=catalog_doc([server]), admin_token="adm")
    await seed_user_with_key(hub.app, "sk-ok")
    responses = [
        await hub.get("/api/catalog", headers=bearer("sk-ok")),
        await hub.get("/.well-known/opencode"),
        await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"}),
        await hub.get("/health"),
        await hub.get("/metrics"),
    ]
    for resp in responses:
        assert resp.status_code == 200, resp.text
        assert "GL_SECRET" not in resp.text
        assert "GL_STATIC" not in resp.text
        assert "client_secret" not in resp.text


@pytest.mark.ac("AC-14")
def test_env_ref_value_not_in_repr(tmp_path: Path) -> None:
    server = facade_server("gitlab")
    server["auth"]["client_secret"] = "env:GL_SECRET"
    app = _create_app_with_catalog(
        tmp_path / "c.yaml", catalog_doc([server]), {"GL_SECRET": "very-secret"}
    )
    dumped = repr(app.state.catalog)
    assert "very-secret" not in dumped
    assert "env:GL_SECRET" not in dumped


# --- AC-15 ---------------------------------------------------------------------


@pytest.mark.ac("AC-15")
def test_env_ref_in_disallowed_field_is_schema_error(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path, catalog_doc([native_server("tag", mcp_url="env:SOME_URL")]), "servers[0].mcp_url"
    )


@pytest.mark.ac("AC-15")
@pytest.mark.parametrize(
    "mutate, path",
    [
        (lambda s: s.__setitem__("upstream_url", "env:UP"), "servers[0].upstream_url"),
        (lambda s: s["auth"].__setitem__("client_id", "env:CID"), "servers[0].auth.client_id"),
        (lambda s: s.__setitem__("title", "env:TITLE"), "servers[0].title"),
    ],
)
def test_env_ref_in_other_disallowed_fields(tmp_path: Path, mutate: Any, path: str) -> None:
    server = facade_server("gitlab")
    mutate(server)
    _expect_catalog_error(tmp_path, catalog_doc([server]), path)


@pytest.mark.ac("AC-15")
def test_env_ref_allowed_in_secret_fields(tmp_path: Path) -> None:
    server = facade_server("gitlab")
    server["auth"]["client_secret"] = "env:A"
    server["credential_headers"] = {"Authorization": "env:B"}
    server["static_headers"] = {"X-Y": "env:C"}
    assert _create_app_with_catalog(tmp_path / "c.yaml", catalog_doc([server])) is not None


# --- AC-16 ---------------------------------------------------------------------


@pytest.mark.ac("AC-16")
async def test_ref_resolved_one_level(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(
        catalog=catalog_doc(
            [
                facade_server("gitlab"),
                facade_server(
                    "gitlab-platform",
                    permission_model={"$ref": "#/servers/gitlab/permission_model"},
                ),
            ]
        )
    )
    await seed_user_with_key(hub.app, "sk-ok")
    servers = {
        s["alias"]: s
        for s in (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()["servers"]
    }
    pm_gitlab = servers["gitlab"]["permission_model"]
    pm_platform = servers["gitlab-platform"]["permission_model"]
    assert pm_platform == pm_gitlab
    assert pm_platform["kind"] == "header_groups"
    assert pm_platform["always"] == ["core"]
    assert [g["id"] for g in pm_platform["groups"]] == ["code_review", "repo_write", "admin"]


@pytest.mark.ac("AC-16")
def test_ref_target_with_var_is_substituted(tmp_path: Path) -> None:
    """Порядок: $ref → ${VAR} → схема: подстановка применяется и к скопированному значению."""
    src = native_server("src", mcp_url="${SRC_URL}")
    dst = native_server("dst", mcp_url={"$ref": "#/servers/src/mcp_url"})
    app = _create_app_with_catalog(
        tmp_path / "c.yaml", catalog_doc([src, dst]), {"SRC_URL": "https://s.test/mcp"}
    )
    assert app.state.catalog.get("dst").model.mcp_url == "https://s.test/mcp"


# --- AC-17 ---------------------------------------------------------------------


@pytest.mark.ac("AC-17")
def test_ref_to_unknown_alias_fails_with_path_and_ref(tmp_path: Path) -> None:
    doc = catalog_doc(
        [
            facade_server("gitlab"),
            facade_server("gp", permission_model={"$ref": "#/servers/nope/permission_model"}),
        ]
    )
    _expect_catalog_error(
        tmp_path, doc, "servers[1].permission_model", "#/servers/nope/permission_model"
    )


@pytest.mark.ac("AC-17")
def test_ref_to_field_that_is_itself_ref_fails(tmp_path: Path) -> None:
    doc = catalog_doc(
        [
            facade_server("gitlab"),
            facade_server("mid", permission_model={"$ref": "#/servers/gitlab/permission_model"}),
            facade_server("last", permission_model={"$ref": "#/servers/mid/permission_model"}),
        ]
    )
    _expect_catalog_error(
        tmp_path, doc, "servers[2].permission_model", "#/servers/mid/permission_model"
    )


@pytest.mark.ac("AC-17")
@pytest.mark.parametrize(
    "ref",
    [
        "#/servers/gitlab",
        "servers/gitlab/permission_model",
        "#/defaults/x",
        "#/servers/gitlab/nope",
    ],
)
def test_ref_bad_format_or_unknown_field_fails(tmp_path: Path, ref: str) -> None:
    doc = catalog_doc(
        [facade_server("gitlab"), facade_server("gp", permission_model={"$ref": ref})]
    )
    _expect_catalog_error(tmp_path, doc, "servers[1].permission_model", ref)


# --- AC-18 ---------------------------------------------------------------------


@pytest.mark.ac("AC-18")
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": "adm"}])
async def test_reload_disabled_without_admin_token(
    make_hub: HubFactory, headers: dict[str, str]
) -> None:
    hub: Hub = await make_hub()
    resp = await hub.post("/admin/catalog/reload", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.ac("AC-18")
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": ""}])
async def test_reload_forbidden_with_wrong_or_missing_token(
    make_hub: HubFactory, headers: dict[str, str]
) -> None:
    hub: Hub = await make_hub(admin_token="adm")
    resp = await hub.post("/admin/catalog/reload", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


@pytest.mark.ac("AC-18")
async def test_reload_empty_admin_token_setting_disables_endpoint(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(admin_token="")
    resp = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": ""})
    assert resp.status_code == 404


# --- AC-19 ---------------------------------------------------------------------


@pytest.mark.ac("AC-19")
async def test_reload_replaces_catalog_and_audits(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(catalog=catalog_doc([facade_server("gitlab")]), admin_token="adm")
    await seed_user_with_key(hub.app, "sk-ok")
    before = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in before["servers"]] == ["gitlab"]

    write_catalog(
        hub.catalog_path, catalog_doc([facade_server("gitlab"), native_server("tag")], version=2)
    )
    resp = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "catalog_version": 2, "servers": 2}

    after = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert after["version"] == 2
    assert [s["alias"] for s in after["servers"]] == ["gitlab", "tag"]
    assert (await hub.get("/health")).json()["catalog_version"] == 2
    rows = await audit_rows(hub.app, "catalog_reloaded")
    assert len(rows) == 1


# --- AC-20 ---------------------------------------------------------------------


@pytest.mark.ac("AC-20")
async def test_reload_invalid_file_keeps_old_catalog(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(catalog=catalog_doc([facade_server("gitlab")]), admin_token="adm")
    await seed_user_with_key(hub.app, "sk-ok")
    broken = native_server("tag")
    del broken["title"]
    write_catalog(hub.catalog_path, catalog_doc([facade_server("gitlab"), broken], version=2))
    resp = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "catalog_invalid"
    assert "servers[1].title" in body["message"]
    after = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert after["version"] == 1
    assert [s["alias"] for s in after["servers"]] == ["gitlab"]
    assert (await hub.get("/health")).json()["catalog_version"] == 1
    assert await audit_rows(hub.app, "catalog_reloaded") == []


@pytest.mark.ac("AC-20")
async def test_reload_deleted_file_keeps_old_catalog(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(catalog=catalog_doc([facade_server("gitlab")]), admin_token="adm")
    await seed_user_with_key(hub.app, "sk-ok")
    hub.catalog_path.unlink()
    resp = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "catalog_invalid"
    assert str(hub.catalog_path.name) in body["message"] or "файл" in body["message"].lower()
    after = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in after["servers"]] == ["gitlab"]


@pytest.mark.ac("AC-20")
async def test_reload_broken_yaml_keeps_old_catalog(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(catalog=catalog_doc([facade_server("gitlab")]), admin_token="adm")
    hub.catalog_path.write_text("version: 1\nservers: [\n", encoding="utf-8")
    resp = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "catalog_invalid"
    assert (await hub.get("/health")).json()["catalog_version"] == 1


# --- AC-22 ---------------------------------------------------------------------

PUBLIC_FIELDS = {
    "alias", "title", "description", "owner", "contact", "docs_url", "status", "mode",
    "mcp_url", "permission_model", "auth_kind",
}  # fmt: skip
FORBIDDEN_KEYS = {
    "upstream_url",
    "auth",
    "client_id",
    "client_secret",
    "credential_headers",
    "static_headers",
    "header",
}


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(str(k))
            keys |= _all_keys(v)
    elif isinstance(value, list):
        for v in value:
            keys |= _all_keys(v)
    return keys


@pytest.mark.ac("AC-22")
async def test_public_view_has_allowed_fields_only(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub()  # facade gitlab + native tag (mcp_url https://tag.test/mcp)
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    servers = {s["alias"]: s for s in resp.json()["servers"]}
    assert set(servers) == {"gitlab", "tag"}
    for server in servers.values():
        assert PUBLIC_FIELDS <= set(server)
        assert server["auth_kind"] == "oauth2"
        assert set(server) - PUBLIC_FIELDS == {"connection"}
    assert servers["gitlab"]["mcp_url"] == "https://hub.test/mcp/gitlab"
    assert servers["tag"]["mcp_url"] == "https://tag.test/mcp"
    assert not (_all_keys(resp.json()) & FORBIDDEN_KEYS)
    assert "internal.test" not in resp.text
    assert "hub-client-id" not in resp.text
    assert "static-value" not in resp.text


@pytest.mark.ac("AC-22")
async def test_public_permission_model_shapes(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(
        catalog=catalog_doc(
            [
                facade_server("gitlab"),
                native_server("tag"),
                native_server(
                    "tools",
                    permission_model={
                        "kind": "tool_filter",
                        "presets": {"ro": {"tools": ["a", "b"]}},
                    },
                ),
            ]
        )
    )
    await seed_user_with_key(hub.app, "sk-ok")
    servers = {
        s["alias"]: s
        for s in (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()["servers"]
    }
    assert servers["gitlab"]["permission_model"] == {
        "kind": "header_groups",
        "groups": [
            {"id": "code_review", "title": "Code review", "preset": "readonly"},
            {"id": "repo_write", "title": "Запись", "preset": "readwrite"},
            {"id": "admin", "title": "Админ", "preset": "none"},
        ],
        "always": ["core"],
    }
    assert servers["tag"]["permission_model"] == {
        "kind": "consent",
        "presets": {"readonly": {"write_mode": "readonly"}, "readwrite": {"write_mode": "confirm"}},
    }
    assert servers["tools"]["permission_model"] == {
        "kind": "tool_filter",
        "presets": {"ro": {"tools": ["a", "b"]}},
    }


@pytest.mark.ac("AC-22")
async def test_public_view_optional_contact_docs_null(make_hub: HubFactory) -> None:
    server = native_server("tag")
    del server["contact"]
    del server["docs_url"]
    hub: Hub = await make_hub(catalog=catalog_doc([server]))
    await seed_user_with_key(hub.app, "sk-ok")
    body = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert body["servers"][0]["contact"] is None
    assert body["servers"][0]["docs_url"] is None


# --- AC-23 ---------------------------------------------------------------------


@pytest.mark.ac("AC-23")
async def test_empty_catalog_valid(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(catalog={"version": 1, "servers": []})
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert resp.status_code == 200
    assert resp.json() == {"version": 1, "servers": []}
    wellknown = await hub.get("/.well-known/opencode")
    assert wellknown.status_code == 200
    assert wellknown.json()["config"]["mcp"] == {}
    assert (await hub.get("/health")).json()["catalog_version"] == 1


@pytest.mark.ac("AC-23")
async def test_catalog_with_defaults_and_null_servers_valid(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(catalog="version: 3\ndefaults:\n  x: 1\nservers:\n")
    assert (await hub.get("/health")).json()["catalog_version"] == 3
    assert (await hub.get("/.well-known/opencode")).json()["config"]["mcp"] == {}


# --- дополнительные граничные случаи схемы (R-C1..R-C3) ------------------------


@pytest.mark.ac("AC-08")
@pytest.mark.parametrize(
    "document, fragment",
    [
        ({"version": 1, "servers": "gitlab"}, "servers"),
        ({"version": 1, "servers": [1]}, "servers[0]"),
        ({"version": 1, "servers": [], "defaults": [1]}, "defaults"),
        ({"version": True, "servers": []}, "version"),
        ("", "пуст"),
        ("# только комментарий\n", "пуст"),
    ],
    ids=[
        "servers-str",
        "server-not-object",
        "defaults-list",
        "version-bool",
        "empty-file",
        "comment-only",
    ],
)
def test_more_top_level_schema_errors(tmp_path: Path, document: Any, fragment: str) -> None:
    _expect_catalog_error(tmp_path, document, fragment)


@pytest.mark.ac("AC-08")
def test_unreadable_catalog_path_is_error(tmp_path: Path) -> None:
    directory = tmp_path / "catalog.yaml"
    directory.mkdir()
    settings = Settings(**base_settings_kwargs(directory))
    with pytest.raises(Exception) as excinfo:
        create_app(settings)
    assert "catalog.yaml" in str(excinfo.value)


@pytest.mark.ac("AC-11")
def test_permission_model_without_kind_reports_kind_path(tmp_path: Path) -> None:
    _expect_catalog_error(
        tmp_path,
        catalog_doc([native_server("tag", permission_model={"presets": {"x": {}}})]),
        "servers[0].permission_model.kind",
    )


@pytest.mark.ac("AC-10")
@pytest.mark.parametrize(
    "mutate, path",
    [
        (lambda s: s["auth"].__setitem__("client_secret", 123), "servers[0].auth.client_secret"),
        (lambda s: s["auth"].__setitem__("pkce", "yes-please"), "servers[0].auth.pkce"),
        (lambda s: s["auth"].__setitem__("type", "basic"), "servers[0].auth.type"),
        (
            lambda s: s["auth"].__setitem__("scopes", {"readonly": ["a"]}),
            "servers[0].auth.scopes.readwrite",
        ),
        (
            lambda s: s.__setitem__("credential_headers", {"Authorization": 5}),
            "servers[0].credential_headers.Authorization",
        ),
        (lambda s: s.__setitem__("static_headers", {"X": ["a"]}), "servers[0].static_headers.X"),
        (lambda s: s["auth"].__setitem__("extra_field", 1), "servers[0].auth.extra_field"),
    ],
    ids=[
        "secret-int",
        "pkce-str",
        "auth-type",
        "scopes-missing-readwrite",
        "cred-header-int",
        "static-header-list",
        "auth-extra",
    ],
)
def test_facade_auth_field_type_errors(tmp_path: Path, mutate: Any, path: str) -> None:
    server = facade_server("gitlab")
    mutate(server)
    _expect_catalog_error(tmp_path, catalog_doc([server]), path)


@pytest.mark.ac("AC-17")
def test_ref_object_with_extra_keys_or_non_string_fails(tmp_path: Path) -> None:
    doc = catalog_doc(
        [
            facade_server("gitlab"),
            facade_server(
                "gp", permission_model={"$ref": "#/servers/gitlab/permission_model", "x": 1}
            ),
        ]
    )
    _expect_catalog_error(tmp_path, doc, "servers[1].permission_model")
    doc = catalog_doc([facade_server("gitlab"), facade_server("gp", permission_model={"$ref": 42})])
    _expect_catalog_error(tmp_path, doc, "servers[1].permission_model")


@pytest.mark.ac("AC-16")
def test_ref_nested_inside_field_value(tmp_path: Path) -> None:
    """$ref «на любой глубине»: внутри объекта auth."""
    src = facade_server("gitlab")
    dst = facade_server("gp")
    dst["auth"]["scopes"] = {"$ref": "#/servers/gitlab/auth"}  # значение поля auth целиком (объект)
    # $ref разрешён, но объект auth не подходит по схеме scopes → ошибка схемы с путём к полю
    _expect_catalog_error(tmp_path, catalog_doc([src, dst]), "servers[1].auth.scopes")
    dst = facade_server("gp")
    dst["permission_model"] = {
        "kind": "header_groups",
        "header": "X",
        "groups": {"$ref": "#/servers/gitlab/permission_model"},
    }
    _expect_catalog_error(tmp_path, catalog_doc([src, dst]), "servers[1].permission_model.groups")
    dst = native_server("dst", audience={"$ref": "#/servers/gitlab/audience"})
    app = _create_app_with_catalog(tmp_path / "c.yaml", catalog_doc([src, dst]))
    assert app.state.catalog.get("dst").model.audience == ["all"]
