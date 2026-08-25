"""Модель данных, миграции и KeyValueStore (R-M1..R-M5): AC-138..AC-142, AC-156."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from asgi_lifespan import LifespanManager
from sqlalchemy import event, inspect, text

import hub.migrate as migrate_module
from hub.app import create_app
from hub.cli import main
from hub.clock import ManualClock
from hub.db import build_engine
from hub.kv import InMemoryKeyValueStore
from hub.migrate import (
    ADVISORY_LOCK_ID,
    BASE_REVISION,
    _lock,
    alembic_config,
    current_revision,
    head_revision,
    upgrade,
)
from hub.settings import Settings
from tests.conftest import Hub, HubFactory, base_settings_kwargs
from tests.support import (
    CATALOG_ENV,
    RecordingKeyValueStore,
    authorize_params,
    bearer,
    exchange_code,
    execute,
    fetch_rows,
    i3_catalog,
    insert_key,
    insert_user,
    jsonrpc_body,
    litellm_http_client,
    make_litellm_router,
    mcp_headers,
    pkce_pair,
    provider_callback,
    query_of,
    register_client,
    sha256_hex,
    submit_consent,
    web_login,
)

I1_TABLES = ("users", "api_keys", "connections", "audit_log")
I3_TABLES = (
    "oauth_clients",
    "oauth_codes",
    "refresh_tokens",
    "upstream_tokens",
    "sessions",
    "consents",
)


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


def _file_settings(catalog_path: Path, db_file: Path, **overrides: Any) -> Settings:
    return Settings(
        **base_settings_kwargs(
            catalog_path, database_url=f"sqlite+aiosqlite:///{db_file}", **overrides
        )
    )


async def _introspect(app: Any) -> dict[str, Any]:
    async with app.state.db.engine.connect() as conn:

        def read(sync_conn: Any) -> dict[str, Any]:
            insp = inspect(sync_conn)
            tables = set(insp.get_table_names())
            return {
                "tables": tables,
                "columns": {t: {c["name"] for c in insp.get_columns(t)} for t in tables},
                "uniques": {
                    t: [sorted(u["column_names"]) for u in insp.get_unique_constraints(t)]
                    + [sorted(i["column_names"]) for i in insp.get_indexes(t) if i.get("unique")]
                    for t in tables
                },
            }

        return await conn.run_sync(read)


# --- AC-138 ----------------------------------------------------------------


@pytest.mark.ac("AC-138")
async def test_migrations_applied_at_startup(tmp_path: Path, catalog_path: Path) -> None:
    db_file = tmp_path / "hub.db"
    settings = _file_settings(catalog_path, db_file)
    app = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(app):
        await app.state.db.init()
        info = await _introspect(app)
        revision = (await fetch_rows(app, "SELECT version_num FROM alembic_version"))[0]
    assert revision["version_num"] == head_revision()
    assert set(I1_TABLES) <= info["tables"]
    assert set(I3_TABLES) <= info["tables"]


@pytest.mark.ac("AC-138")
async def test_restart_keeps_schema_and_data(tmp_path: Path, catalog_path: Path) -> None:
    db_file = tmp_path / "hub.db"
    settings = _file_settings(catalog_path, db_file)
    first = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(first):
        await insert_user(first, "persisted")
        before = await _introspect(first)
    second = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(second):
        after = await _introspect(second)
        rows = await fetch_rows(second, "SELECT user_id FROM users")
        revision = (await fetch_rows(second, "SELECT version_num FROM alembic_version"))[0]
    assert before["tables"] == after["tables"]
    assert [r["user_id"] for r in rows] == ["persisted"]
    assert revision["version_num"] == head_revision()


@pytest.mark.ac("AC-138")
async def test_auto_migrate_disabled_keeps_schema_empty(
    tmp_path: Path, catalog_path: Path
) -> None:
    db_file = tmp_path / "hub-noauto.db"
    settings = _file_settings(catalog_path, db_file, db_auto_migrate=False)
    app = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(app):
        info = await _introspect(app)
    assert not (set(I1_TABLES) & info["tables"])
    assert "alembic_version" not in info["tables"]


@pytest.mark.ac("AC-138")
def test_cli_db_upgrade_and_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_file = tmp_path / "cli.db"
    monkeypatch.setenv("HUB_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    assert main(["db", "upgrade"]) == 0
    capsys.readouterr()
    assert main(["db", "current"]) == 0
    printed = capsys.readouterr().out
    assert head_revision() in printed


# --- AC-139 ----------------------------------------------------------------


@pytest.mark.ac("AC-139")
async def test_new_tables_have_required_columns(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await hub.app.state.db.init()
    info = await _introspect(hub.app)
    expected = {
        "oauth_clients": {"client_id", "client_name", "redirect_uris", "created_at"},
        "oauth_codes": {
            "code_sha256",
            "code_challenge",
            "redirect_uri",
            "expires_at",
            "used_at",
            "scope",
            "resource",
        },
        "refresh_tokens": {
            "token_sha256",
            "chain_id",
            "parent_id",
            "status",
            "access_jti",
            "access_exp",
            "expires_at",
        },
        "upstream_tokens": {
            "connection_id",
            "access_token_enc",
            "refresh_token_enc",
            "expires_at",
            "scopes",
        },
        "sessions": {"session_sha256", "csrf_sha256", "user_id", "expires_at", "auth_method"},
        "consents": {"user_id", "client_id", "alias", "scope", "preset", "groups"},
    }
    for table, columns in expected.items():
        assert table in info["tables"], table
        assert columns <= info["columns"][table], (table, info["columns"][table])


@pytest.mark.ac("AC-139")
@pytest.mark.parametrize(
    ("table", "unique"),
    [
        ("oauth_clients", ["client_id"]),
        ("oauth_codes", ["code_sha256"]),
        ("refresh_tokens", ["token_sha256"]),
        ("upstream_tokens", ["connection_id"]),
        ("sessions", ["session_sha256"]),
        ("consents", ["alias", "client_id", "user_id"]),
    ],
)
async def test_new_tables_unique_constraints(
    make_hub: HubFactory, table: str, unique: list[str]
) -> None:
    hub = await _hub(make_hub)
    await hub.app.state.db.init()
    info = await _introspect(hub.app)
    assert unique in info["uniques"][table], (table, info["uniques"][table])


# --- AC-140 ----------------------------------------------------------------


@pytest.mark.ac("AC-140")
async def test_connections_extended_without_breaking_i1(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await hub.app.state.db.init()
    info = await _introspect(hub.app)
    connections = info["columns"]["connections"]
    assert {
        "id",
        "user_id",
        "alias",
        "status",
        "preset",
        "groups",
        "created_at",
        "updated_at",
    } <= connections
    assert {"needs_reauth_reason", "last_refresh_at", "revision", "provider_account"} <= connections
    assert ["alias", "user_id"] in info["uniques"]["connections"]
    assert ["key_sha256"] in info["uniques"]["api_keys"]
    assert info["columns"]["users"] == {"user_id", "email", "groups", "created_at", "updated_at"}
    assert info["columns"]["audit_log"] == {"id", "ts", "user_id", "action", "alias", "details"}
    assert info["columns"]["api_keys"] == {
        "id",
        "key_sha256",
        "user_id",
        "key_kind",
        "key_alias",
        "client",
        "created_at",
        "expires_at",
    }


@pytest.mark.ac("AC-140")
async def test_connection_revision_defaults_to_zero_and_grows(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    await insert_user(hub.app, "u1")
    await execute(
        hub.app,
        "INSERT INTO connections (user_id, alias, status, groups, created_at, updated_at) "
        "VALUES ('u1', 'gitlab', 'not_connected', '[]', '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
    )
    rows = await fetch_rows(hub.app, "SELECT revision FROM connections")
    assert rows[0]["revision"] == 0
    await hub.app.state.broker.upsert_connection(
        user_id="u1", alias="gitlab", status="connected", preset="readonly", groups=[]
    )
    rows = await fetch_rows(hub.app, "SELECT revision FROM connections")
    assert rows[0]["revision"] == 1


# --- AC-141 ----------------------------------------------------------------


@pytest.mark.ac("AC-141")
async def test_kv_atomic_primitives() -> None:
    clock = ManualClock(1_000_000.0)
    kv = InMemoryKeyValueStore(clock)
    assert await kv.set_if_absent("refreshlock:1", "owner", ttl=30) is True
    assert await kv.set_if_absent("refreshlock:1", "other", ttl=30) is False
    clock.advance(31)
    assert await kv.set_if_absent("refreshlock:1", "owner", ttl=30) is True

    assert await kv.incr("sse:u1", 1) == 1
    assert await kv.incr("sse:u1", 1) == 2
    assert await kv.decr("sse:u1", 1) == 1
    assert await kv.decr("sse:u1", 5) == 0


@pytest.mark.ac("AC-141")
async def test_revision2_kv_keys_and_ttls(make_hub: HubFactory, clock: ManualClock) -> None:
    kv = RecordingKeyValueStore(clock)
    hub = await _hub(make_hub, kv=kv, tools_cache_ttl=300, connection_cache_ttl=60)
    await web_login(hub)
    client_id = await register_client(hub)
    verifier, challenge = pkce_pair()

    started = await hub.get(
        "/oauth/authorize", params=authorize_params(client_id, challenge=challenge)
    )
    live_keys = {k for k in kv._data if kv._alive(k)}
    assert any(k.startswith("oauthtx:") for k in live_keys)
    tx_key = next(k for k in live_keys if k.startswith("oauthtx:"))
    assert kv._data[tx_key][1] - clock.monotonic() == pytest.approx(600, abs=1)

    page = await provider_callback(hub, started.headers["location"])
    redirect = await submit_consent(hub, page.text)
    code = query_of(redirect.headers["location"])["code"]
    tokens = (
        await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    ).json()

    headers = mcp_headers(tokens["access_token"])
    assert (
        await hub.post("/mcp/gitlab", content=jsonrpc_body("initialize"), headers=headers)
    ).status_code == 200
    assert (
        await hub.post(
            "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=2), headers=headers
        )
    ).status_code == 200
    await hub.post("/oauth/revoke", data={"token": tokens["access_token"]})

    live_keys = {k for k in kv._data if kv._alive(k)}
    assert any(k.startswith("jtiden:") for k in live_keys)
    assert "conn:u1:gitlab" in live_keys
    # R-M4 ревизии 2.1: alias входит в ключ виртуальной сессии (mcpsess:<alias>:<id>),
    # отдельного счётчика сессий (mcpsessn:*) в таблице ключей нет.
    session_keys = [k for k in live_keys if k.startswith("mcpsess:")]
    assert session_keys and all(k.startswith("mcpsess:gitlab:") for k in session_keys), session_keys
    assert all(len(k.split(":")) == 3 and k.split(":")[2] for k in session_keys), session_keys
    assert not [k for k in kv.written_keys if k.startswith("mcpsessn:")]
    assert any(k.startswith("toolscache:gitlab:") for k in live_keys)
    assert "cb:gitlab" in live_keys

    def ttl_of(key: str) -> float:
        return kv._data[key][1] - clock.monotonic()

    assert ttl_of("conn:u1:gitlab") == pytest.approx(60, abs=1)
    assert ttl_of("cb:gitlab") == pytest.approx(60, abs=1)  # HUB_CB_RESET × 2
    assert ttl_of(session_keys[0]) == pytest.approx(86400, abs=1)
    assert ttl_of(next(k for k in live_keys if k.startswith("toolscache:"))) == pytest.approx(
        300, abs=1
    )
    jti_key = next(k for k in live_keys if k.startswith("jtiden:"))
    assert ttl_of(jti_key) == pytest.approx(3600, abs=2)

    clock.advance(3601)
    assert await kv.get(jti_key) is None


# --- AC-142 ----------------------------------------------------------------


@pytest.mark.ac("AC-142")
async def test_i1_database_is_upgraded_without_data_loss(
    tmp_path: Path, catalog_path: Path
) -> None:
    db_file = tmp_path / "legacy.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    engine = build_engine(url)
    try:
        await upgrade(engine, "0001_i1_base")
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE alembic_version"))
    finally:
        await engine.dispose()

    settings = _file_settings(catalog_path, db_file)
    seeding = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    seeding.state.db.auto_migrate = False
    await seeding.state.db.init()
    await insert_user(seeding, "legacy-user", "legacy@corp.test")
    await insert_key(seeding, "sk-legacy", "legacy-user")
    await execute(
        seeding,
        "INSERT INTO connections (user_id, alias, status, preset, groups, created_at, updated_at) "
        "VALUES ('legacy-user','gitlab','connected','readonly','[]',"
        "'2026-01-01 00:00:00','2026-01-01 00:00:00')",
    )
    await seeding.state.db.audit("legacy_action", user_id="legacy-user")
    await seeding.state.db.dispose()

    app = create_app(settings, litellm_client=litellm_http_client(make_litellm_router()))
    async with LifespanManager(app):
        await app.state.db.init()
        info = await _introspect(app)
        revision = (await fetch_rows(app, "SELECT version_num FROM alembic_version"))[0]
        users = await fetch_rows(app, "SELECT user_id FROM users")
        keys = await fetch_rows(app, "SELECT key_sha256 FROM api_keys")
        connections = await fetch_rows(app, "SELECT alias, status FROM connections")
        audit = await fetch_rows(app, "SELECT action FROM audit_log")

        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://hub.test"
        ) as client:
            me = await client.get("/api/me", headers=bearer("sk-legacy"))

    assert revision["version_num"] == head_revision()
    assert set(I3_TABLES) <= info["tables"]
    assert [u["user_id"] for u in users] == ["legacy-user"]
    assert keys[0]["key_sha256"] == sha256_hex("sk-legacy")
    assert connections == [{"alias": "gitlab", "status": "connected"}]
    assert [a["action"] for a in audit] == ["legacy_action"]
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == "legacy-user"


@pytest.mark.ac("AC-142")
async def test_current_revision_is_none_for_empty_database(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        assert await current_revision(engine) is None
        await upgrade(engine)
        assert await current_revision(engine) == head_revision()
    finally:
        await engine.dispose()


# --- AC-142: ревизия 4 над существующими строками upstream_tokens ----------


PRE_REVISION_4 = "0003_i4_user_token"
NEW_COLUMNS = ("issued_token_id", "token_origin", "token_origin_reason", "submitted_expires_at")
LEGACY_CIPHERTEXT = "gAAAAA-legacy-ciphertext-not-a-real-token"


def _run_downgrade(connection: Any, revision: str) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.downgrade(cfg, revision)


async def _downgrade(engine: Any, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_run_downgrade, revision)


async def _upstream_columns(engine: Any) -> set[str]:
    async with engine.connect() as conn:
        return set(
            await conn.run_sync(
                lambda sync: {c["name"] for c in inspect(sync).get_columns("upstream_tokens")}
            )
        )


async def _rows(engine: Any, sql: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(text(sql))
        return [dict(row) for row in result.mappings().all()]


async def _seed_pre_revision_4(engine: Any) -> None:
    """Строка ``upstream_tokens`` ревизии до 4-й: шифртокен без сведений о происхождении."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (user_id, email, groups, created_at, updated_at) "
                "VALUES ('legacy-user','legacy@corp.test','[\"all\"]',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO connections (user_id, alias, status, preset, groups, "
                "created_at, updated_at, revision) VALUES ('legacy-user','tag','connected',"
                "'readonly','[]','2026-01-01 00:00:00','2026-01-01 00:00:00',1)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO upstream_tokens (connection_id, access_token_enc, refresh_token_enc, "
                "token_type, scopes, expires_at, obtained_at, updated_at) "
                "SELECT id, :enc, NULL, 'Bearer', '[]', NULL, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00' FROM connections"
            ),
            {"enc": LEGACY_CIPHERTEXT},
        )


@pytest.mark.ac("AC-142")
async def test_revision_4_keeps_previous_behaviour_for_existing_tokens(tmp_path: Path) -> None:
    """Существующие строки после апгрейда ведут себя как прежде (R-U17.4, решение 95).

    Прежнее поведение — это ``token_origin='submitted'`` и NULL в трёх остальных колонках:
    предупреждения о временном подключении нет (причина пуста, R-U16), отзыв при отключении
    не выполняется (R-U15.4 требует ``issued`` и непустой ``issued_token_id``), срока сессии нет.
    Шифртекст токена миграция не трогает.
    """
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'rev3.db'}")
    try:
        await upgrade(engine, PRE_REVISION_4)
        assert await current_revision(engine) == PRE_REVISION_4
        assert set(NEW_COLUMNS) & await _upstream_columns(engine) == set()
        await _seed_pre_revision_4(engine)

        await upgrade(engine)
        assert await current_revision(engine) == head_revision()
        assert set(NEW_COLUMNS) <= await _upstream_columns(engine)

        rows = await _rows(
            engine,
            "SELECT access_token_enc, token_type, issued_token_id, token_origin, "
            "token_origin_reason, submitted_expires_at FROM upstream_tokens",
        )
        assert len(rows) == 1, "строка не пережила апгрейд"
        row = rows[0]
        assert row["token_origin"] == "submitted"
        assert row["issued_token_id"] is None
        assert row["token_origin_reason"] is None
        assert row["submitted_expires_at"] is None
        # Шифртекст и прочие поля миграция не переписывает.
        assert row["access_token_enc"] == LEGACY_CIPHERTEXT
        assert row["token_type"] == "Bearer"
    finally:
        await engine.dispose()


@pytest.mark.ac("AC-142")
async def test_revision_4_downgrade_drops_columns_without_data_loss(tmp_path: Path) -> None:
    """Откат ревизии 4 снимает ровно четыре колонки и не теряет строк (R-M1)."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'rev4.db'}")
    try:
        await upgrade(engine, PRE_REVISION_4)
        await _seed_pre_revision_4(engine)
        await upgrade(engine)
        before = await _upstream_columns(engine)

        await _downgrade(engine, PRE_REVISION_4)
        assert await current_revision(engine) == PRE_REVISION_4
        after = await _upstream_columns(engine)
        assert before - after == set(NEW_COLUMNS)

        rows = await _rows(engine, "SELECT access_token_enc, token_type FROM upstream_tokens")
        assert rows == [{"access_token_enc": LEGACY_CIPHERTEXT, "token_type": "Bearer"}]

        # Повторный апгрейд возвращает прежний вид — миграция обратима.
        await upgrade(engine)
        assert await _upstream_columns(engine) == before
        assert (await _rows(engine, "SELECT token_origin FROM upstream_tokens")) == [
            {"token_origin": "submitted"}
        ]
    finally:
        await engine.dispose()


@pytest.mark.ac("AC-138")
def test_head_revision_is_i3() -> None:
    """Head — ревизия 4 I-4 (происхождение токена, R-U17.4), цепочка от базовой непрерывна."""
    assert head_revision() == "0004_i4_token_exchange"
    script = ScriptDirectory.from_config(alembic_config())
    chain = [rev.revision for rev in script.walk_revisions()][::-1]
    assert chain == [
        "0001_i1_base",
        "0002_i3_oauth",
        "0003_i4_user_token",
        "0004_i4_token_exchange",
    ]
    assert chain[0] == BASE_REVISION and chain[-1] == head_revision()
    for previous, current in itertools.pairwise(chain):
        assert script.get_revision(current).down_revision == previous


# --- AC-156 ----------------------------------------------------------------


class _RecordingConnection:
    """Мок соединения SQLAlchemy на границе БД: запоминает выполненные statements."""

    def __init__(self, dialect_name: str) -> None:
        self.dialect = type("_Dialect", (), {"name": dialect_name})()
        self.executed: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append((str(statement), params))
        return None


@pytest.mark.ac("AC-156")
def test_postgres_migrations_take_advisory_lock() -> None:
    """R-M1/R-N5: на PostgreSQL перед миграциями берётся advisory-блокировка транзакции."""
    connection = _RecordingConnection("postgresql")
    _lock(connection)
    assert len(connection.executed) == 1
    sql, params = connection.executed[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"key": ADVISORY_LOCK_ID}


@pytest.mark.ac("AC-156")
def test_sqlite_migrations_do_not_take_lock() -> None:
    connection = _RecordingConnection("sqlite")
    _lock(connection)
    assert connection.executed == []


@pytest.mark.ac("AC-156")
async def test_lock_is_taken_before_revisions_are_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блокировка запрашивается до применения ревизий; на SQLite pg-запросов нет (AC-138)."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}")
    statements: list[str] = []
    revision_at_lock: list[Any] = []
    original = migrate_module._lock

    def spy(connection: Any) -> None:
        revision_at_lock.append(inspect(connection).has_table("alembic_version"))
        original(connection)

    monkeypatch.setattr(migrate_module, "_lock", spy)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    try:
        await upgrade(engine)
        assert await current_revision(engine) == head_revision()
    finally:
        await engine.dispose()

    assert revision_at_lock == [False], "блокировка взята не до применения ревизий"
    assert not [sql for sql in statements if "pg_advisory" in sql]
