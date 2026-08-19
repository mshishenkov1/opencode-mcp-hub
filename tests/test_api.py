"""Аутентификация Bearer (R-L6) и API витрины (R-A1..R-A4, R-A6, R-A7, R-S4):
AC-48..AC-57, AC-61..AC-64."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hub.clock import ManualClock
from hub.db import Database, build_engine
from tests.conftest import Hub, HubFactory
from tests.support import (
    RecordingKeyValueStore,
    bearer,
    capture_json_logs,
    catalog_doc,
    execute,
    facade_server,
    insert_connection,
    insert_key,
    insert_user,
    mock_start,
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
    assert isinstance(body.get("message"), str) and body["message"]  # R-L6: {error, message, hint}
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
        {"Authorization": "Basic sk-ok"},
        {"Authorization": "Token sk-ok"},
    ],
    ids=[
        "no-header",
        "unknown-key",
        "basic",
        "bearer-empty",
        "bearer-space",
        "no-scheme",
        "basic-with-valid-key",
        "other-scheme-with-valid-key",
    ],
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
    # RFC 7235: между схемой и параметром допускается несколько пробелов
    spaced = await hub.get("/api/me", headers={"Authorization": "Bearer   sk-ok"})
    assert spaced.status_code == 200, spaced.text
    assert spaced.json()["user_id"] == "u1"
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
async def test_auth_result_cached_for_60s(make_hub: HubFactory, clock: ManualClock) -> None:
    kv = RecordingKeyValueStore(clock)
    hub = await make_hub(kv=kv)
    await seed_user_with_key(hub.app, "sk-ok")
    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200

    # spec §6: кэш под ключом keyauth:<sha256 ключа>, значение {user_id, email, key_kind, created_at};
    # сырой ключ LiteLLM в именах ключей KV не встречается
    cache_key = "keyauth:" + sha256_hex("sk-ok")
    cached = await hub.app.state.kv.get(cache_key)
    assert isinstance(cached, dict), cached
    assert cached["user_id"] == "u1"
    assert cached["email"] == "u1@corp.test"
    assert cached["key_kind"] == "persistent"
    assert isinstance(cached["created_at"], str) and cached["created_at"]
    assert await hub.app.state.kv.get("keyauth:sk-ok") is None
    assert cache_key in kv.written_keys
    assert all("sk-ok" not in key for key in kv.written_keys), kv.written_keys
    assert all(not key.startswith("keyauth:") or key == cache_key for key in kv.written_keys)

    await execute(hub.app, "DELETE FROM api_keys WHERE key_sha256 = :sha", sha=sha256_hex("sk-ok"))

    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200
    hub.clock.advance(59)
    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200
    hub.clock.advance(2)
    _assert_unauthorized(await hub.get("/api/me", headers=bearer("sk-ok")))
    assert await hub.app.state.kv.get(cache_key) is None


@pytest.mark.ac("AC-50")
@pytest.mark.ac("AC-52")
async def test_cached_auth_returns_same_profile_as_database_lookup(hub: Hub) -> None:
    """Ответ из кэша (второй запрос) совпадает с ответом по БД: user_id, email, key_kind, created_at."""
    created = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    await insert_user(hub.app, "u1", "u1@corp.test")
    await insert_key(hub.app, "jwt-1", "u1", key_kind="jwt", created_at=created)
    first = await hub.get("/api/me", headers=bearer("jwt-1"))
    assert first.status_code == 200
    assert first.json() == {
        "user_id": "u1",
        "email": "u1@corp.test",
        "key_kind": "jwt",
        "created_at": created.isoformat(),
    }
    # строка удалена — второй ответ может прийти только из кэша
    await execute(hub.app, "DELETE FROM api_keys WHERE key_sha256 = :sha", sha=sha256_hex("jwt-1"))
    second = await hub.get("/api/me", headers=bearer("jwt-1"))
    assert second.status_code == 200
    assert second.json() == first.json()


@pytest.mark.ac("AC-50")
@pytest.mark.ac("AC-55")
async def test_cached_auth_keeps_user_groups_for_audience_filter(make_hub: HubFactory) -> None:
    """Группы пользователя в кэше аутентификации: фильтр audience одинаков для запроса по БД и из кэша."""
    hub = await make_hub(
        catalog=catalog_doc(
            [
                native_server("a", audience=["all"]),
                native_server("b", audience=["devs"]),
                native_server("c", audience=["ops"]),
            ]
        )
    )
    await insert_user(hub.app, "u1", groups=["devs"])
    await insert_key(hub.app, "sk-dev", "u1")
    first = await hub.get("/api/catalog", headers=bearer("sk-dev"))
    assert [s["alias"] for s in first.json()["servers"]] == ["a", "b"]
    await execute(hub.app, "DELETE FROM api_keys WHERE key_sha256 = :sha", sha=sha256_hex("sk-dev"))
    await execute(hub.app, "UPDATE users SET groups = :g WHERE user_id = 'u1'", g='["ops"]')
    second = await hub.get("/api/catalog", headers=bearer("sk-dev"))
    assert second.status_code == 200
    assert [s["alias"] for s in second.json()["servers"]] == ["a", "b"]  # из кэша: группы ['devs']


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


# --- AC-63/AC-64: аварийный путь, редкие коды, JSON-логи (усиление после review-1) ---------


def _no_raise_client(hub: Hub) -> httpx.AsyncClient:
    """Клиент, не пробрасывающий исключения приложения в тест (ServerErrorMiddleware их повторно
    поднимает после отправки 500): нужен, чтобы увидеть ответ 500 как его видит клиент."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.app, raise_app_exceptions=False),
        base_url="http://hub.test",
    )


@pytest.mark.ac("AC-63")
@pytest.mark.ac("AC-64")
async def test_unhandled_exception_is_500_internal_error_in_uniform_format(
    hub: Hub, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    """БД отвалилась во время /api/me → 500 {error:'internal_error', message} в едином формате
    (R-A7), с nosniff и X-Request-ID клиента (R-S4); детали исключения — только в JSON-логе."""
    await seed_user_with_key(hub.app, "sk-ok")
    broken = Database(build_engine(f"sqlite+aiosqlite:///{tmp_path}/no/such/dir/hub.db"))
    original = hub.app.state.db
    hub.app.state.db = broken
    try:
        async with _no_raise_client(hub) as client:
            with capture_json_logs() as logs:
                resp = await client.get(
                    "/api/me", headers={**bearer("sk-ok"), "X-Request-ID": "req-500"}
                )
    finally:
        hub.app.state.db = original
        await broken.dispose()

    assert resp.status_code == 500
    assert resp.headers["Content-Type"].startswith("application/json")
    body = resp.json()
    assert body["error"] == "internal_error"
    assert isinstance(body["message"], str) and body["message"]
    assert "status" not in body  # вне /cli/*
    assert "Traceback" not in resp.text and "sqlite" not in resp.text.lower()
    assert resp.headers.get_list("x-content-type-options") == ["nosniff"]
    assert resp.headers.get_list("x-request-id") == ["req-500"]

    # запись об ошибке в JSON-логе: уровень, событие, request_id, метод, путь, тип исключения, трассировка
    errors = [r for r in logs.records() if r["level"] == "ERROR"]
    assert len(errors) == 1, logs.raw()
    err = errors[0]
    assert err["message"] == "unhandled_exception"
    assert err["logger"].startswith("hub")
    assert err["request_id"] == "req-500"
    assert err["method"] == "GET"
    assert err["path"] == "/api/me"
    assert err["exc_type"] == "OperationalError"
    assert "Traceback" in err["exc_info"] and "OperationalError" in err["exc_info"]
    # запись о самом запросе тоже есть и тоже несёт request_id и статус 500
    requests = [r for r in logs.records() if r["message"].startswith("http_request")]
    assert len(requests) == 1
    assert requests[0]["request_id"] == "req-500"
    assert requests[0]["status"] == 500
    assert requests[0]["route"] == "/api/me"

    # метрики учли запрос со статусом 500 и шаблоном маршрута
    metrics = (await hub.get("/metrics")).text
    assert 'hub_http_requests_total{method="GET",path="/api/me",status="500"} 1' in metrics
    # приложение работоспособно после аварии
    assert (await hub.get("/api/me", headers=bearer("sk-ok"))).status_code == 200


@pytest.mark.ac("AC-63")
async def test_method_not_allowed_and_disabled_docs_use_uniform_format(hub: Hub) -> None:
    method = await hub.post("/health")
    assert method.status_code == 405
    assert method.json() == {"error": "method_not_allowed", "message": "Метод не поддерживается"}
    assert method.headers["Allow"] == "GET"
    assert method.headers["X-Content-Type-Options"] == "nosniff"
    # служебные маршруты FastAPI отключены — это «неизвестный маршрут» (R-A7)
    for path in ("/docs", "/redoc", "/openapi.json"):
        resp = await hub.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"] == "not_found"
        assert resp.headers["Content-Type"].startswith("application/json")


@pytest.mark.ac("AC-64")
async def test_request_id_boundaries_empty_and_single_char(hub: Hub) -> None:
    empty = await hub.get("/health", headers={"X-Request-ID": ""})
    assert UUID_RE.match(empty.headers["X-Request-ID"]), empty.headers["X-Request-ID"]
    blank = await hub.get("/health", headers={"X-Request-ID": "   "})
    assert UUID_RE.match(blank.headers["X-Request-ID"]), blank.headers["X-Request-ID"]
    single = await hub.get("/health", headers={"X-Request-ID": "a"})
    assert single.headers["X-Request-ID"] == "a"
    assert single.headers.get_list("x-request-id") == ["a"]


@pytest.mark.ac("AC-64")
async def test_request_log_is_json_line_with_request_id_and_route(hub: Hub) -> None:
    """R-S4: JSON-логи, в записи о запросе — request_id; одна строка на запрос."""
    with capture_json_logs() as logs:
        resp = await hub.get("/cli/poll/some-id", headers={"X-Request-ID": "req-json"})
    assert resp.status_code == 404
    lines = logs.raw()
    assert len(lines) == 1, lines
    record = logs.records()[0]
    assert record["level"] == "INFO"
    assert record["logger"] == "hub.http"
    assert record["message"] == "http_request method=GET path=/cli/poll/some-id status=404 request_id=req-json"
    assert record["request_id"] == "req-json"
    assert record["method"] == "GET"
    assert record["path"] == "/cli/poll/some-id"
    assert record["route"] == "/cli/poll/{login_id}"  # шаблон маршрута, а не сырой путь
    assert record["status"] == 404
    assert isinstance(record["duration_ms"], float)
    assert 0 < record["duration_ms"] < 10_000
    assert record["duration_ms"] == round(record["duration_ms"], 3)
    assert ISO_RE.match(record["ts"]), record["ts"]
    # служебные атрибуты logging в JSON не попадают
    assert not {"levelno", "msg", "args", "pathname", "created"} & set(record)


@pytest.mark.ac("AC-64")
@pytest.mark.ac("AC-24")
async def test_request_id_propagates_into_nested_log_records(hub: Hub) -> None:
    """request_id попадает и в записи, которые пишутся внутри обработчика (не только в http_request)."""
    mock_start(hub.litellm)
    with capture_json_logs() as logs:
        resp = await hub.post(
            "/cli/start", json={"client": "opencode-fork/1.17.9"}, headers={"X-Request-ID": "req-login"}
        )
    assert resp.status_code == 200
    started = logs.find("login_started")
    assert len(started) == 1, logs.raw()
    assert started[0]["request_id"] == "req-login"
    assert started[0]["level"] == "INFO"
    assert started[0]["login_id"] == resp.json()["login_id"]
    assert started[0]["client"] == "opencode-fork/1.17.9"
    assert "ll-secret" not in "\n".join(logs.raw())
    assert resp.json()["poll_secret"] not in "\n".join(logs.raw())
    assert [r["request_id"] for r in logs.records()] == ["req-login"] * len(logs.records())


@pytest.mark.ac("AC-03")
@pytest.mark.ac("AC-64")
async def test_log_level_setting_filters_request_logs(make_hub: HubFactory) -> None:
    """HUB_LOG_LEVEL (по умолчанию INFO): при WARNING записи о запросах не пишутся, при INFO — пишутся."""
    quiet = await make_hub(log_level="WARNING")
    with capture_json_logs() as logs:
        assert (await quiet.get("/health")).status_code == 200
    assert not [r for r in logs.records() if r["message"].startswith("http_request")], logs.raw()

    verbose = await make_hub(log_level="INFO")
    with capture_json_logs() as logs:
        assert (await verbose.get("/health")).status_code == 200
    assert len([r for r in logs.records() if r["message"].startswith("http_request")]) == 1
