"""``/.well-known/opencode`` (R-A5, R-A8): AC-58..AC-60."""

from __future__ import annotations

import json
import re

import pytest

from tests.conftest import Hub, HubFactory
from tests.support import catalog_doc, facade_server, native_server, write_catalog

ETAG_RE = re.compile(r'^"[0-9a-f]{16}"$')


# --- AC-58 -----------------------------------------------------------------


@pytest.mark.ac("AC-58")
async def test_wellknown_auth_provider_and_remote_config(make_hub: HubFactory) -> None:
    hub = await make_hub(
        public_url="https://hub.test",
        litellm_base_url="https://litellm.test",
        wellknown_env_name="MAGNIT_COPILOT_KEY",
    )
    resp = await hub.get("/.well-known/opencode")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=300"
    assert ETAG_RE.match(resp.headers["ETag"]), resp.headers["ETag"]
    body = resp.json()
    assert set(body) == {"auth", "config", "remote_config"}
    assert body["auth"] == {
        "command": ["opencode", "corp", "login", "--hub", "https://hub.test"],
        "env": "MAGNIT_COPILOT_KEY",
    }
    config = body["config"]
    assert config["$schema"] == "https://opencode.ai/config.json"
    assert config["autoupdate"] is False
    assert config["enabled_providers"] == ["magnit_prod"]
    provider = config["provider"]["magnit_prod"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["name"] == "LiteLLM Copilot prod"
    assert provider["options"] == {
        "baseURL": "https://litellm.test/v1",
        "apiKey": "{env:MAGNIT_COPILOT_KEY}",
    }
    assert provider["models"] == {
        "MagnitCopilot": {"name": "MagnitCopilot", "limit": {"context": 250000, "output": 8192}}
    }
    assert body["remote_config"] == {
        "url": "https://hub.test/remote-config",
        "headers": {"Authorization": "Bearer {env:MAGNIT_COPILOT_KEY}"},
    }


@pytest.mark.ac("AC-58")
async def test_wellknown_reflects_custom_provider_settings(make_hub: HubFactory) -> None:
    hub = await make_hub(
        litellm_base_url="https://llm.example/",
        litellm_model="Model-X",
        litellm_provider_id="prov",
        litellm_provider_name="Provider X",
        litellm_context_limit=1000,
        litellm_output_limit=100,
        wellknown_env_name="MY_KEY",
    )
    body = (await hub.get("/.well-known/opencode")).json()
    assert body["auth"]["env"] == "MY_KEY"
    assert body["config"]["enabled_providers"] == ["prov"]
    provider = body["config"]["provider"]["prov"]
    assert provider["name"] == "Provider X"
    assert provider["options"] == {"baseURL": "https://llm.example/v1", "apiKey": "{env:MY_KEY}"}
    assert provider["models"] == {
        "Model-X": {"name": "Model-X", "limit": {"context": 1000, "output": 100}}
    }
    assert body["remote_config"]["headers"] == {"Authorization": "Bearer {env:MY_KEY}"}


@pytest.mark.ac("AC-58")
async def test_wellknown_needs_no_auth_and_ignores_bad_bearer(hub: Hub) -> None:
    resp = await hub.get("/.well-known/opencode", headers={"Authorization": "Bearer sk-bad"})
    assert resp.status_code == 200


# --- AC-59 -----------------------------------------------------------------


CATALOG_ENV_NAMES = ("GL_SECRET", "GL_TOKEN", "GL_STATIC")
FORBIDDEN_FRAGMENTS = (
    "upstream",
    "client_secret",
    "credential_headers",
    "static_headers",
    *CATALOG_ENV_NAMES,
)


def _catalog_for_ac59() -> dict:  # type: ignore[type-arg]
    return catalog_doc(
        [
            facade_server(
                "gitlab",
                audience=["all"],
                credential_headers={"Authorization": "env:GL_TOKEN"},
                static_headers={"X-Static": "env:GL_STATIC"},
            ),
            native_server("tag", audience=["all"], mcp_url="https://tag.test/mcp"),
            native_server("b", audience=["devs"]),
            native_server("old", status="deprecated", audience=["all"]),
            native_server("u", status="beta", audience=["all"], mcp_url="${U_MCP_URL}"),
        ]
    )


@pytest.mark.ac("AC-59")
async def test_wellknown_mcp_entries_for_visible_servers_without_secrets(
    make_hub: HubFactory,
) -> None:
    catalog = _catalog_for_ac59()
    assert catalog["servers"][0]["auth"]["client_secret"] == "env:GL_SECRET"
    hub = await make_hub(
        catalog=catalog,
        wellknown_env_name="MAGNIT_COPILOT_KEY",
        env={"GL_SECRET": "very-secret", "GL_TOKEN": "tok-secret", "GL_STATIC": "static-secret"},
    )
    resp = await hub.get("/.well-known/opencode")
    assert resp.status_code == 200
    mcp = resp.json()["config"]["mcp"]
    assert set(mcp) == {"gitlab", "tag", "old"}
    assert mcp["gitlab"] == {
        "type": "remote",
        "url": "https://hub.test/mcp/gitlab",
        "enabled": False,
        "oauth": {},
    }
    assert mcp["tag"]["url"] == "https://tag.test/mcp"
    assert mcp["old"] == {
        "type": "remote",
        "url": "https://tag.test/mcp",
        "enabled": False,
        "oauth": {},
    }
    for entry in mcp.values():
        assert "headers" not in entry
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in resp.text, fragment
    for secret in ("very-secret", "tok-secret", "static-secret"):
        assert secret not in resp.text, secret
    # ревизия 1.1: ссылки каталога env:VAR не сериализуются — в config.mcp подстроки 'env:' нет
    assert "env:" not in json.dumps(mcp)


@pytest.mark.ac("AC-59")
async def test_wellknown_env_prefix_only_in_opencode_placeholders(make_hub: HubFactory) -> None:
    """Ревизия 1.1: единственные вхождения 'env:' — плейсхолдеры ``{env:<HUB_WELLKNOWN_ENV_NAME>}``
    в ``provider.*.options.apiKey`` и ``remote_config.headers.Authorization`` (AC-58)."""
    hub = await make_hub(catalog=_catalog_for_ac59(), wellknown_env_name="MAGNIT_COPILOT_KEY")
    resp = await hub.get("/.well-known/opencode")
    assert resp.status_code == 200
    body = resp.json()
    # плейсхолдеры присутствуют там, где положено (иначе проверка ниже была бы тривиальной)
    assert (
        body["config"]["provider"]["magnit_prod"]["options"]["apiKey"] == "{env:MAGNIT_COPILOT_KEY}"
    )
    assert body["remote_config"]["headers"]["Authorization"] == "Bearer {env:MAGNIT_COPILOT_KEY}"
    assert resp.text.count("{env:MAGNIT_COPILOT_KEY}") == 2
    stripped = resp.text.replace("{env:MAGNIT_COPILOT_KEY}", "")
    assert "env:" not in stripped
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in resp.text, fragment


@pytest.mark.ac("AC-59")
async def test_wellknown_env_prefix_check_follows_custom_env_name(make_hub: HubFactory) -> None:
    """При другом ``HUB_WELLKNOWN_ENV_NAME`` разрешены только плейсхолдеры с этим именем;
    имена переменных каталога (``env:VAR``) в теле по-прежнему отсутствуют."""
    hub = await make_hub(catalog=_catalog_for_ac59(), wellknown_env_name="CORP_KEY")
    resp = await hub.get("/.well-known/opencode")
    assert resp.status_code == 200
    assert resp.text.count("{env:CORP_KEY}") == 2
    assert "env:" not in resp.text.replace("{env:CORP_KEY}", "")
    for name in CATALOG_ENV_NAMES:
        assert name not in resp.text, name


@pytest.mark.ac("AC-59")
async def test_wellknown_mcp_uses_public_url_and_deprecated_included(make_hub: HubFactory) -> None:
    hub = await make_hub(
        catalog=catalog_doc(
            [
                facade_server("old-facade", status="deprecated"),
                native_server("hidden", audience=["ops"]),
            ]
        ),
        public_url="https://corp-hub.test/",
    )
    mcp = (await hub.get("/.well-known/opencode")).json()["config"]["mcp"]
    assert mcp == {
        "old-facade": {
            "type": "remote",
            "url": "https://corp-hub.test/mcp/old-facade",
            "enabled": False,
            "oauth": {},
        }
    }


# --- AC-60 -----------------------------------------------------------------


@pytest.mark.ac("AC-60")
async def test_wellknown_etag_304_and_changes_after_reload(make_hub: HubFactory) -> None:
    hub = await make_hub(catalog=catalog_doc([facade_server("gitlab")]), admin_token="adm")
    first = await hub.get("/.well-known/opencode")
    etag1 = first.headers["ETag"]
    assert ETAG_RE.match(etag1)

    not_modified = await hub.get("/.well-known/opencode", headers={"If-None-Match": etag1})
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["ETag"] == etag1
    assert not_modified.headers["Cache-Control"] == "public, max-age=300"
    assert not_modified.headers["X-Content-Type-Options"] == "nosniff"

    write_catalog(
        hub.catalog_path, catalog_doc([facade_server("gitlab"), native_server("tag")], version=2)
    )
    reload = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert reload.status_code == 200

    second = await hub.get("/.well-known/opencode")
    assert second.status_code == 200
    etag2 = second.headers["ETag"]
    assert ETAG_RE.match(etag2)
    assert etag2 != etag1
    assert "tag" in second.json()["config"]["mcp"]

    stale = await hub.get("/.well-known/opencode", headers={"If-None-Match": etag1})
    assert stale.status_code == 200
    assert stale.headers["ETag"] == etag2
    fresh = await hub.get("/.well-known/opencode", headers={"If-None-Match": etag2})
    assert fresh.status_code == 304


@pytest.mark.ac("AC-60")
async def test_wellknown_etag_stable_and_mismatch_returns_200(hub: Hub) -> None:
    a = await hub.get("/.well-known/opencode")
    b = await hub.get("/.well-known/opencode")
    assert a.headers["ETag"] == b.headers["ETag"]
    resp = await hub.get("/.well-known/opencode", headers={"If-None-Match": '"0000000000000000"'})
    assert resp.status_code == 200
    resp = await hub.get(
        "/.well-known/opencode", headers={"If-None-Match": f'"x", {a.headers["ETag"]}'}
    )
    assert resp.status_code == 304


@pytest.mark.ac("AC-60")
async def test_wellknown_etag_differs_between_settings(make_hub: HubFactory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    hub_a = await make_hub(wellknown_env_name="KEY_A")
    hub_b = await make_hub(wellknown_env_name="KEY_B", path=tmp_path / "b.yaml")
    etag_a = (await hub_a.get("/.well-known/opencode")).headers["ETag"]
    etag_b = (await hub_b.get("/.well-known/opencode")).headers["ETag"]
    assert etag_a != etag_b


@pytest.mark.ac("AC-60")
async def test_wellknown_if_none_match_star_and_weak(hub: Hub) -> None:
    etag = (await hub.get("/.well-known/opencode")).headers["ETag"]
    assert (
        await hub.get("/.well-known/opencode", headers={"If-None-Match": "*"})
    ).status_code == 304
    assert (
        await hub.get("/.well-known/opencode", headers={"If-None-Match": f"W/{etag}"})
    ).status_code == 304
