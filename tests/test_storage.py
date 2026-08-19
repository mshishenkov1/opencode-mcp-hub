"""Хранилище, кэш, наблюдаемость (R-S1..R-S4): AC-65..AC-68."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hub.app import create_app
from hub.clock import ManualClock
from hub.kv import create_kv_store
from hub.settings import Settings
from tests.conftest import Hub, HubFactory, base_settings_kwargs
from tests.support import (
    audit_rows,
    execute,
    fetch_rows,
    insert_connection,
    insert_key,
    insert_user,
    litellm_http_client,
    make_jwt,
    make_litellm_router,
    mock_key_generate,
    mock_poll,
    mock_start,
    ready_body,
    sha256_hex,
    teams_body,
    write_catalog,
)

EXPECTED_COLUMNS = {
    "users": {"user_id", "email", "groups", "created_at", "updated_at"},
    "api_keys": {
        "id",
        "key_sha256",
        "user_id",
        "key_kind",
        "key_alias",
        "client",
        "created_at",
        "expires_at",
    },
    "connections": {
        "id",
        "user_id",
        "alias",
        "status",
        "preset",
        "groups",
        "created_at",
        "updated_at",
    },
    "audit_log": {"id", "ts", "user_id", "action", "alias", "details"},
}


# --- AC-65 -----------------------------------------------------------------


@pytest.mark.ac("AC-65")
async def test_schema_created_at_startup_in_sqlite_file(tmp_path: Path, catalog_path: Path) -> None:
    db_file = tmp_path / "hub.db"
    settings = Settings(
        **base_settings_kwargs(catalog_path, database_url=f"sqlite+aiosqlite:///{db_file}")
    )
    app = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(app):
        assert db_file.exists()
        async with app.state.db.engine.connect() as conn:

            def introspect(sync_conn: Any) -> dict[str, Any]:
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                columns = {
                    t: {c["name"] for c in insp.get_columns(t)}
                    for t in EXPECTED_COLUMNS
                    if t in tables
                }
                uniques: dict[str, list[list[str]]] = {}
                for t in EXPECTED_COLUMNS:
                    if t not in tables:
                        continue
                    cols: list[list[str]] = []
                    cols += [sorted(u["column_names"]) for u in insp.get_unique_constraints(t)]
                    cols += [
                        sorted(i["column_names"]) for i in insp.get_indexes(t) if i.get("unique")
                    ]
                    uniques[t] = cols
                return {"tables": tables, "columns": columns, "uniques": uniques}

            info = await conn.run_sync(introspect)
    assert set(EXPECTED_COLUMNS) <= info["tables"]
    for table, expected in EXPECTED_COLUMNS.items():
        assert expected <= info["columns"][table], (table, info["columns"][table])
    assert ["key_sha256"] in info["uniques"]["api_keys"]
    assert ["alias", "user_id"] in info["uniques"]["connections"]


@pytest.mark.ac("AC-65")
async def test_unique_constraints_enforced(hub: Hub) -> None:
    await insert_user(hub.app, "u1")
    await insert_key(hub.app, "sk-dup", "u1")
    with pytest.raises(IntegrityError):
        await insert_key(hub.app, "sk-dup", "u1")
    await insert_connection(hub.app, "u1", "gitlab")
    with pytest.raises(IntegrityError):
        await insert_connection(hub.app, "u1", "gitlab", status="needs_reauth")
    # другой пользователь с тем же alias — допустимо
    await insert_user(hub.app, "u2")
    await insert_connection(hub.app, "u2", "gitlab")


@pytest.mark.ac("AC-65")
async def test_schema_creation_is_idempotent_across_restarts(
    tmp_path: Path, catalog_path: Path
) -> None:
    db_file = tmp_path / "hub.db"
    settings = Settings(
        **base_settings_kwargs(catalog_path, database_url=f"sqlite+aiosqlite:///{db_file}")
    )
    app1 = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(app1):
        await insert_user(app1, "persisted")
    app2 = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(app2):
        rows = await fetch_rows(app2, "SELECT user_id FROM users")
    assert [r["user_id"] for r in rows] == ["persisted"]


# --- AC-66 -----------------------------------------------------------------


@pytest.mark.ac("AC-66")
async def test_inmemory_kv_respects_ttl() -> None:
    clock = ManualClock(1_000.0)
    kv = create_kv_store("", clock)  # HUB_REDIS_URL пуст → in-memory
    await kv.set("k", {"v": 1}, ttl=5)
    assert await kv.get("k") == {"v": 1}
    clock.advance(4.9)
    assert await kv.get("k") == {"v": 1}
    clock.advance(1.1)  # 6 с
    assert await kv.get("k") is None

    await kv.set("forever", "x")  # без TTL, без delete/expire
    clock.advance(10_000)
    assert await kv.get("forever") == "x"

    await kv.set("k2", [1, 2], ttl=100)
    assert await kv.get("k2") == [1, 2]
    await kv.delete("k2")
    assert await kv.get("k2") is None
    await kv.delete("never-existed")
    assert await kv.get("missing") is None
    await kv.close()


@pytest.mark.ac("AC-66")
async def test_inmemory_kv_ttl_boundary_and_overwrite() -> None:
    clock = ManualClock(0.0)
    kv = create_kv_store("", clock)
    await kv.set("k", "v", ttl=5)
    clock.advance(5)  # ровно TTL — значение уже недоступно
    assert await kv.get("k") is None
    await kv.set("k", "v1", ttl=5)
    await kv.set("k", "v2", ttl=50)  # перезапись продлевает TTL
    clock.advance(10)
    assert await kv.get("k") == "v2"
    await kv.set("k", "v3", ttl=0)  # неположительный TTL — значение недоступно
    assert await kv.get("k") is None


@pytest.mark.ac("AC-66")
async def test_inmemory_kv_returns_copies() -> None:
    kv = create_kv_store("", ManualClock(0.0))
    await kv.set("k", {"a": [1]})
    value = await kv.get("k")
    value["a"].append(2)
    assert await kv.get("k") == {"a": [1]}


# --- AC-67 -----------------------------------------------------------------


@pytest.mark.ac("AC-67")
async def test_metrics_exposes_requests_latency_and_sessions(hub: Hub) -> None:
    assert (await hub.get("/health")).status_code == 200
    mock_start(hub.litellm)
    assert (await hub.post("/cli/start", json={"client": "c"})).status_code == 200

    resp = await hub.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    text = resp.text
    assert "hub_http_requests_total{" in text
    line = next(
        (
            ln
            for ln in text.splitlines()
            if ln.startswith("hub_http_requests_total{") and 'path="/health"' in ln
        ),
        None,
    )
    assert line is not None, text
    assert 'method="GET"' in line
    assert 'status="200"' in line
    assert re.search(r"\s1$", line), line
    assert "hub_http_request_duration_seconds_bucket" in text
    assert re.search(
        r'hub_http_request_duration_seconds_bucket\{[^}]*path="/cli/start"[^}]*le="\+Inf"\} 1', text
    )
    assert re.search(r"^hub_login_sessions_active 1$", text, re.MULTILINE), text


@pytest.mark.ac("AC-67")
async def test_metrics_path_label_is_route_template(hub: Hub) -> None:
    await hub.get("/cli/poll/some-id", headers={"X-Hub-Poll-Secret": "x"})
    text = (await hub.get("/metrics")).text
    assert 'path="/cli/poll/{login_id}"' in text
    assert 'path="/cli/poll/some-id"' not in text
    assert re.search(
        r'hub_http_requests_total\{method="GET",path="/cli/poll/\{login_id\}",status="404"\} 1',
        text,
    )


@pytest.mark.ac("AC-67")
async def test_metrics_active_sessions_gauge_tracks_lifecycle(hub: Hub) -> None:
    def gauge(text: str) -> int:
        m = re.search(r"^hub_login_sessions_active (\d+)", text, re.MULTILINE)
        assert m
        return int(m.group(1))

    assert gauge((await hub.get("/metrics")).text) == 0
    mock_start(hub.litellm)
    s1 = (await hub.post("/cli/start")).json()
    s2 = (await hub.post("/cli/start")).json()
    assert gauge((await hub.get("/metrics")).text) == 2
    hub.clock.advance(601)  # обе истекли
    assert gauge((await hub.get("/metrics")).text) == 0
    assert (await hub.poll(s1["login_id"], s1["poll_secret"])).status_code == 404
    assert (await hub.poll(s2["login_id"], s2["poll_secret"])).status_code == 404


# --- AC-68 -----------------------------------------------------------------


@pytest.mark.ac("AC-68")
async def test_audit_log_has_no_secrets(make_hub: HubFactory) -> None:
    hub = await make_hub(admin_token="ADM-TOKEN-XYZ")
    jwt = make_jwt({"sub": "u1", "email": "u1@corp.test", "exp": int(hub.clock.time()) + 3600})
    mock_start(hub.litellm)
    start = (await hub.post("/cli/start", json={"client": "c1"})).json()
    mock_poll(hub.litellm, ready_body(jwt, team_id="t2"), team_id="t2")
    mock_poll(hub.litellm, teams_body(("t1", "A"), ("t2", "B")))
    assert (await hub.poll(start["login_id"], start["poll_secret"])).json()[
        "status"
    ] == "team_selection_required"
    assert (
        await hub.choose_team(start["login_id"], start["poll_secret"], {"team_id": "t2"})
    ).status_code == 200
    mock_key_generate(hub.litellm, "sk-test-5")
    ready = await hub.poll(start["login_id"], start["poll_secret"])
    assert ready.status_code == 200 and ready.json()["key"] == "sk-test-5"
    write_catalog(hub.catalog_path, {"version": 2, "servers": []})
    assert (
        await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "ADM-TOKEN-XYZ"})
    ).status_code == 200

    rows = await audit_rows(hub.app)
    actions = [r["action"] for r in rows]
    for expected in ("login_started", "login_completed", "catalog_reloaded"):
        assert expected in actions, actions
    secrets = [
        "sk-test-5",
        jwt,
        start["poll_secret"],
        "ll-secret",
        "ADM-TOKEN-XYZ",
        sha256_hex("sk-test-5"),
    ]
    for row in rows:
        serialized = json.dumps(row, default=str, ensure_ascii=False)
        for secret in secrets:
            assert secret not in serialized, (row["action"], secret)
    completed = next(r for r in rows if r["action"] == "login_completed")
    assert completed["details"]["key_kind"] == "persistent"
    assert completed["details"]["team_id"] == "t2"


@pytest.mark.ac("AC-68")
async def test_audit_details_are_json_objects(hub: Hub) -> None:
    mock_start(hub.litellm)
    await hub.post("/cli/start", json={"client": "c1"})
    rows = await audit_rows(hub.app, "login_started")
    assert isinstance(rows[0]["details"], dict)
    assert rows[0]["details"]["client"] == "c1"
    assert rows[0]["ts"] is not None
    await execute(hub.app, "DELETE FROM audit_log")
    assert await audit_rows(hub.app) == []
