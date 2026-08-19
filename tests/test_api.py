"""Аутентификация Bearer (R-L6) и API витрины (R-A1..R-A4, R-A6, R-A7, R-S4):
AC-48..AC-57, AC-61..AC-64."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from hub.db import Database, build_engine
from tests.conftest import Hub, HubFactory
from tests.support import (
    bearer,
    catalog_doc,
    execute,
    facade_server,
    insert_connection,
    insert_key,
    insert_user,
    native_server,
    seed_user_with_key,
    sha256_hex,
)

UNAUTHORIZED_HINT = "выполните вход: opencode corp login"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _assert_unauthorized(resp) -> None:  # type: ignore[no-untyped-def]
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["error"] == "unauthorized"
    assert body["hint"] == UNAUTHORIZED_HINT
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


# --- AC-48 -----------------------------------------------------------------


@pytest.mark.ac("AC-48")
async def test_bearer_auth_valid_key_passes(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get("/api/me", headers=bearer("sk-ok"))
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "u1"


@pytest.mark.ac("AC-48")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer sk-bad"},
        {"Authorization": "Basic xxx"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "sk-ok"},
    ],
    ids=["no-header", "unknown-key", "basic", "bearer-empty", "bearer-space", "no-scheme"],
)
async def test_bearer_auth_rejects_missing_or_bad(hub: Hub, headers: dict[str, str]) -> None:
    await seed_user_with_key(hub.app, "sk-ok")
    _assert_unauthorized(await hub.get("/api/me", headers=headers))


@pytest.mark.ac("AC-48")
@pytest.mark.parametrize(
    "path", ["/api/me", "/api/catalog", "/api/me/connections", "/remote-config"]
)
async def test_all_bearer_endpoints_require_auth(hub: Hub, path: str) -> None:
    _assert_unauthorized(await hub.get(path))


@pytest.mark.ac("AC-48")
async def test_bearer_scheme_case_insensitive(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get("/api/me", headers={"Authorization": "bearer sk-ok"})
    assert resp.status_code == 200


# --- AC-49 -----------------------------------------------------------------


@pytest.mark.ac("AC-49")
async def test_x_litellm_api_key_header_accepted(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-ok", user_id="owner-1", email="o@corp.test")
    resp = await hub.get("/api/me", headers={"x-litellm-api-key": "sk-ok"})
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "owner-1"


@pytest.mark.ac("AC-49")
async def test_authorization_takes_precedence_over_x_litellm_api_key(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get(
        "/api/me", headers={"x-litellm-api-key": "sk-ok", "Authorization": "Bearer sk-bad"}
    )
    assert resp.status_code == 401
    resp = await hub.get(
        "/api/me", headers={"x-litellm-api-key": "sk-bad", "Authorization": "Bearer sk-ok"}
    )
    assert resp.status_code == 200


# --- AC-50 -----------------------------------------------------------------


@pytest.mark.ac("AC-50")
async def test_auth_result_cached_for_60s(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-ok")
    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200
    await execute(hub.app, "DELETE FROM api_keys WHERE key_sha256 = :sha", sha=sha256_hex("sk-ok"))

    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200
    hub.clock.advance(59)
    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200
    hub.clock.advance(2)
    _assert_unauthorized(await hub.get("/api/me", headers=bearer("sk-ok")))


@pytest.mark.ac("AC-50")
async def test_negative_auth_result_not_cached(hub: Hub) -> None:
    await insert_user(hub.app, "u1")
    _assert_unauthorized(await hub.get("/api/me", headers=bearer("sk-late")))
    await insert_key(hub.app, "sk-late", "u1")
    assert (await hub.get("/api/me", headers=bearer("sk-late"))).status_code == 200


# --- AC-51 -----------------------------------------------------------------


@pytest.mark.ac("AC-51")
async def test_health_and_ready(hub: Hub) -> None:
    health = await hub.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]
    assert body["catalog_version"] == 1
    assert ISO_RE.match(body["time"]), body["time"]
    assert body["time"] == hub.clock.now().isoformat()
    ready = await hub.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest.mark.ac("AC-51")
async def test_ready_503_when_db_disposed(hub: Hub) -> None:
    await hub.app.state.db.dispose()
    resp = await hub.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["error"] == "not_ready"
    assert (await hub.get("/health")).status_code == 200


@pytest.mark.ac("AC-51")
async def test_ready_503_when_db_engine_broken(hub: Hub, tmp_path) -> None:  # type: ignore[no-untyped-def]
    broken = Database(build_engine(f"sqlite+aiosqlite:///{tmp_path}/no/such/dir/hub.db"))
    original = hub.app.state.db
    hub.app.state.db = broken
    try:
        resp = await hub.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["error"] == "not_ready"
    finally:
        hub.app.state.db = original
        await broken.dispose()
    assert (await hub.get("/ready")).status_code == 200


# --- AC-52 -----------------------------------------------------------------


@pytest.mark.ac("AC-52")
async def test_api_me_reflects_key_used(hub: Hub) -> None:
    created_p = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    created_j = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
    await insert_user(hub.app, "u1", "u1@corp.test")
    await insert_key(hub.app, "sk-p", "u1", key_kind="persistent", created_at=created_p)
    await insert_key(
        hub.app,
        "jwt-1",
        "u1",
        key_kind="jwt",
        created_at=created_j,
        expires_at=created_j + timedelta(hours=1),
    )

    me_p = (await hub.get("/api/me", headers=bearer("sk-p"))).json()
    me_j = (await hub.get("/api/me", headers=bearer("jwt-1"))).json()
    assert set(me_p) == {"user_id", "email", "key_kind", "created_at"}
    assert me_p["user_id"] == me_j["user_id"] == "u1"
    assert me_p["email"] == me_j["email"] == "u1@corp.test"
    assert me_p["key_kind"] == "persistent"
    assert me_j["key_kind"] == "jwt"
    assert me_p["created_at"].startswith("2026-01-02T03:04:05")
    assert me_j["created_at"].startswith("2026-05-06T07:08:09")


# --- AC-53 -----------------------------------------------------------------


@pytest.mark.ac("AC-53")
async def test_api_catalog_requires_auth_and_has_connection_block(hub: Hub) -> None:
    _assert_unauthorized(await hub.get("/api/catalog"))
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get("/api/catalog", headers=bearer("sk-ok"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert [s["alias"] for s in body["servers"]] == ["gitlab", "tag"]
    for server in body["servers"]:
        assert server["connection"] == {
            "status": "not_connected",
            "preset": None,
            "updated_at": None,
        }


@pytest.mark.ac("AC-53")
async def test_api_catalog_preserves_file_order(make_hub: HubFactory) -> None:
    hub = await make_hub(
        catalog=catalog_doc([native_server("zeta"), native_server("alpha"), facade_server("mid")])
    )
    await seed_user_with_key(hub.app, "sk-ok")
    body = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in body["servers"]] == ["zeta", "alpha", "mid"]


# --- AC-54 -----------------------------------------------------------------


@pytest.mark.ac("AC-54")
async def test_include_deprecated_filter(make_hub: HubFactory) -> None:
    hub = await make_hub(
        catalog=catalog_doc([native_server("a"), native_server("old", status="deprecated")])
    )
    await seed_user_with_key(hub.app, "sk-ok")

    async def aliases(query: str = "") -> list[str]:
        resp = await hub.get(f"/api/catalog{query}", headers=bearer("sk-ok"))
        assert resp.status_code == 200, resp.text
        return [s["alias"] for s in resp.json()["servers"]]

    default = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()["servers"]
    assert [s["alias"] for s in default] == ["a", "old"]
    assert default[1]["status"] == "deprecated"
    assert await aliases("?include_deprecated=true") == ["a", "old"]
    assert await aliases("?include_deprecated=false") == ["a"]
    assert await aliases("?include_deprecated=0") == ["a"]
    assert await aliases("?include_deprecated=no") == ["a"]
    assert await aliases("?include_deprecated=FALSE") == ["a"]
    assert await aliases("?include_deprecated=1") == ["a", "old"]
    assert await aliases("?include_deprecated=") == ["a", "old"]


# --- AC-55 -----------------------------------------------------------------


@pytest.mark.ac("AC-55")
async def test_audience_filter(make_hub: HubFactory) -> None:
    hub = await make_hub(
        catalog=catalog_doc(
            [
                native_server("a", audience=["all"]),
                native_server("b", audience=["devs"]),
                native_server("c", audience=["devs", "all"]),
            ]
        )
    )
    await seed_user_with_key(hub.app, "sk-ok")
    body = (await hub.get("/api/catalog", headers=bearer("sk-ok"))).json()
    assert [s["alias"] for s in body["servers"]] == ["a", "c"]


@pytest.mark.ac("AC-55")
async def test_audience_intersection_with_user_groups(make_hub: HubFactory) -> None:
    hub = await make_hub(
        catalog=catalog_doc(
            [
                native_server("a", audience=["all"]),
                native_server("b", audience=["devs"]),
                native_server("c", audience=["ops"]),
            ]
        )
    )
    await insert_user(hub.app, "dev1", "d@corp.test", groups=["all", "devs"])
    await insert_key(hub.app, "sk-dev", "dev1")
    body = (await hub.get("/api/catalog", headers=bearer("sk-dev"))).json()
    assert [s["alias"] for s in body["servers"]] == ["a", "b"]


# --- AC-56 -----------------------------------------------------------------


@pytest.mark.ac("AC-56")
async def test_me_connections(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-u1", user_id="u1")
    await seed_user_with_key(hub.app, "sk-u2", user_id="u2", email="u2@corp.test")
    await insert_connection(
        hub.app, "u2", "gitlab", status="connected", preset="readonly", groups=["core"]
    )

    r1 = await hub.get("/api/me/connections", headers=bearer("sk-u1"))
    assert r1.status_code == 200
    assert r1.json() == []

    r2 = await hub.get("/api/me/connections", headers=bearer("sk-u2"))
    assert r2.status_code == 200
    items = r2.json()
    assert len(items) == 1
    item = items[0]
    assert set(item) == {"alias", "status", "preset", "groups", "created_at", "updated_at"}
    assert item["alias"] == "gitlab"
    assert item["status"] == "connected"
    assert item["preset"] == "readonly"
    assert item["groups"] == ["core"]
    assert ISO_RE.match(item["created_at"]) and ISO_RE.match(item["updated_at"])

    _assert_unauthorized(await hub.get("/api/me/connections"))


# --- AC-57 -----------------------------------------------------------------


@pytest.mark.ac("AC-57")
async def test_catalog_connection_block_reflects_rows(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-u2", user_id="u2", email="u2@corp.test")
    await insert_connection(hub.app, "u2", "gitlab", status="needs_reauth", preset="readwrite")
    body = (await hub.get("/api/catalog", headers=bearer("sk-u2"))).json()
    servers = {s["alias"]: s for s in body["servers"]}
    gitlab = servers["gitlab"]["connection"]
    assert gitlab["status"] == "needs_reauth"
    assert gitlab["preset"] == "readwrite"
    assert gitlab["updated_at"] is not None and ISO_RE.match(gitlab["updated_at"])
    assert servers["tag"]["connection"] == {
        "status": "not_connected",
        "preset": None,
        "updated_at": None,
    }


@pytest.mark.ac("AC-57")
async def test_catalog_connection_block_is_per_user(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-u1", user_id="u1")
    await seed_user_with_key(hub.app, "sk-u2", user_id="u2", email="u2@corp.test")
    await insert_connection(hub.app, "u2", "gitlab", status="connected", preset="readonly")
    body = (await hub.get("/api/catalog", headers=bearer("sk-u1"))).json()
    assert all(s["connection"]["status"] == "not_connected" for s in body["servers"])


# --- AC-61 -----------------------------------------------------------------


@pytest.mark.ac("AC-61")
async def test_remote_config_requires_bearer_and_is_empty_by_default(hub: Hub) -> None:
    _assert_unauthorized(await hub.get("/remote-config"))
    await seed_user_with_key(hub.app, "sk-ok")
    resp = await hub.get("/remote-config", headers=bearer("sk-ok"))
    assert resp.status_code == 200
    assert resp.json() == {"config": {"mcp": {}, "permission": {}, "tools": {}}}
    assert resp.headers["Cache-Control"] == "private, no-store"


# --- AC-62 -----------------------------------------------------------------


@pytest.mark.ac("AC-62")
async def test_remote_config_includes_connected_servers_only(hub: Hub) -> None:
    await seed_user_with_key(hub.app, "sk-u2", user_id="u2", email="u2@corp.test")
    await insert_connection(hub.app, "u2", "gitlab", status="connected", preset="readonly")
    await insert_connection(hub.app, "u2", "jira", status="needs_reauth", preset="readonly")
    await insert_connection(hub.app, "u2", "tag", status="not_connected", preset=None)
    resp = await hub.get("/remote-config", headers=bearer("sk-u2"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["mcp"] == {"gitlab": {"enabled": True}}
    assert body["config"]["permission"] == {}
    assert body["config"]["tools"] == {}
    assert resp.headers["Cache-Control"] == "private, no-store"


# --- AC-63 -----------------------------------------------------------------


@pytest.mark.ac("AC-63")
async def test_uniform_error_format_and_nosniff(hub: Hub) -> None:
    not_found = await hub.get("/nope")
    assert not_found.status_code == 404
    assert not_found.json()["error"] == "not_found"

    unauthorized = await hub.get("/api/me")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"] == "unauthorized"

    invalid = await hub.post(
        "/cli/start", content=b"{bad", headers={"Content-Type": "application/json"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "invalid_request"
    assert isinstance(invalid.json()["message"], str) and invalid.json()["message"]

    health = await hub.get("/health")
    assert health.status_code == 200

    for resp in (not_found, unauthorized, invalid, health):
        assert resp.headers["Content-Type"].startswith("application/json")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert not any(name.lower().startswith("access-control-") for name in resp.headers)


@pytest.mark.ac("AC-63")
async def test_error_codes_are_snake_case_and_have_no_status_outside_cli(hub: Hub) -> None:
    resp = await hub.get("/api/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert re.fullmatch(r"[a-z_]+", body["error"])
    assert "status" not in body
    method = await hub.post("/health")
    assert method.status_code == 405
    assert re.fullmatch(r"[a-z_]+", method.json()["error"])
    assert method.headers["X-Content-Type-Options"] == "nosniff"
    cors = await hub.client.options(
        "/api/me", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"}
    )
    assert not any(name.lower().startswith("access-control-") for name in cors.headers)


@pytest.mark.ac("AC-63")
async def test_metrics_and_wellknown_have_nosniff_too(hub: Hub) -> None:
    for path in ("/metrics", "/.well-known/opencode", "/ready"):
        resp = await hub.get(path)
        assert resp.headers["X-Content-Type-Options"] == "nosniff", path


# --- AC-64 -----------------------------------------------------------------


@pytest.mark.ac("AC-64")
async def test_request_id_preserved_and_logged(hub: Hub, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    caplog.clear()
    resp = await hub.get("/health", headers={"X-Request-ID": "req-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "req-123"
    request_logs = [
        r
        for r in caplog.records
        if r.name.startswith("hub")
        and (getattr(r, "request_id", None) == "req-123" or "req-123" in r.getMessage())
    ]
    assert request_logs, [r.getMessage() for r in caplog.records]
    assert any(
        "/health" in r.getMessage() or getattr(r, "path", None) == "/health" for r in request_logs
    )


@pytest.mark.ac("AC-64")
async def test_request_id_generated_when_missing(hub: Hub) -> None:
    first = await hub.get("/health")
    second = await hub.get("/health")
    assert UUID_RE.match(first.headers["X-Request-ID"]), first.headers["X-Request-ID"]
    assert UUID_RE.match(second.headers["X-Request-ID"])
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    uuid.UUID(first.headers["X-Request-ID"])


@pytest.mark.ac("AC-64")
async def test_request_id_too_long_is_replaced(hub: Hub) -> None:
    resp = await hub.get("/health", headers={"X-Request-ID": "x" * 129})
    assert resp.headers["X-Request-ID"] != "x" * 129
    assert UUID_RE.match(resp.headers["X-Request-ID"])
    resp = await hub.get("/health", headers={"X-Request-ID": "y" * 128})
    assert resp.headers["X-Request-ID"] == "y" * 128


@pytest.mark.ac("AC-64")
async def test_request_id_present_on_error_responses(hub: Hub) -> None:
    resp = await hub.get("/nope", headers={"X-Request-ID": "req-err"})
    assert resp.status_code == 404
    assert resp.headers["X-Request-ID"] == "req-err"
