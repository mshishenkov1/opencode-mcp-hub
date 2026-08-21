"""БД Hub: SQLAlchemy 2.x async, таблицы users / api_keys / connections / audit_log (R-S1, §6)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    make_url,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

# R-U13/R-U14: происхождение сохранённого токена целевой системы.
TOKEN_ORIGIN_ISSUED = "issued"
TOKEN_ORIGIN_SUBMITTED = "submitted"
# R-U14.1: закрытый набор причин, по которым обмен не состоялся.
TOKEN_ORIGIN_REASON_POLICY_DENIED = "policy_denied"
TOKEN_ORIGIN_REASON_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
TOKEN_ORIGIN_REASON_TOKEN_UNUSABLE = "token_unusable"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_naive_utc(value: datetime) -> datetime:
    """Хранить всегда naive UTC (SQLite не хранит tz)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    groups: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=lambda: ["all"])
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False, index=True)
    key_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    key_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    client: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("user_id", "alias", name="uq_connections_user_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_connected")
    preset: Mapped[str | None] = mapped_column(String(64), nullable=True)
    groups: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # --- I-3 (R-M3) ---
    needs_reauth_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    provider_account: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # --- I-4 (R-U4, решение 70): id способа подключения из auth_methods каталога ---
    auth_method: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class OAuthClient(Base):
    """Клиент MCP, зарегистрированный по RFC 7591 (R-M2)."""

    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    client_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redirect_uris: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    grant_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    response_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    token_endpoint_auth_method: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OAuthCode(Base):
    """Код авторизации Hub: хранится только sha256 (R-M2, R-O7)."""

    __tablename__ = "oauth_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("connections.id"), nullable=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(255), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(16), nullable=False, default="S256")
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RefreshToken(Base):
    """Refresh-токен Hub: sha256, цепочка ротации, связанный access-``jti`` (R-M2, R-O10)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    chain_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False)
    connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("connections.id"), nullable=True
    )
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    access_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    access_exp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UpstreamToken(Base):
    """Токены целевой системы: только в зашифрованном виде (R-M2, R-B3)."""

    __tablename__ = "upstream_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("connections.id"), nullable=False, unique=True, index=True
    )
    access_token_enc: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Bearer")
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    obtained_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # R-U17.4: происхождение сохранённого токена и следы обмена (ревизия 4).
    # ``issued_token_id`` — идентификатор выпущенного Hub'ом токена; не учётные данные,
    # хранится открытым, но наружу не отдаётся никогда.
    issued_token_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TOKEN_ORIGIN_SUBMITTED,
        server_default=TOKEN_ORIGIN_SUBMITTED,
    )
    token_origin_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # R-U18: верхняя граница срока годности присланного токена (только для origin=submitted).
    submitted_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebSession(Base):
    """Веб-сессия пользователя: sha256 идентификатора и CSRF-токена (R-M2, R-W1)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("users.user_id"), nullable=False, index=True
    )
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Consent(Base):
    """Сохранённое согласие пользователя клиенту на alias (R-M2, HUB_CONSENT=remember)."""

    __tablename__ = "consents"
    __table_args__ = (
        UniqueConstraint("user_id", "client_id", "alias", name="uq_consents_user_client_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.user_id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    preset: Mapped[str] = mapped_column(String(64), nullable=False)
    groups: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """SQLite по умолчанию не проверяет внешние ключи — включаем на каждое соединение.

    Без этого поведение SQLite (тесты, локальный запуск) расходится с PostgreSQL, где
    ограничения включены всегда, и нарушения FK остаются незамеченными.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def build_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    kwargs: dict[str, Any] = {"future": True}
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        if not url.database or url.database == ":memory:":
            kwargs["poolclass"] = StaticPool
    engine = create_async_engine(database_url, **kwargs)
    if is_sqlite:
        _enable_sqlite_foreign_keys(engine)
    return engine


class Database:
    """Обёртка над движком: ленивое приведение схемы, сессии, аудит, проверка готовности."""

    def __init__(self, engine: AsyncEngine, *, auto_migrate: bool = True) -> None:
        self.engine = engine
        self.auto_migrate = auto_migrate
        self._ready = False
        self._closed = False
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        """Довести схему до ``head`` миграциями Alembic — идемпотентно (R-M1, R-M5).

        При ``auto_migrate=False`` (``HUB_DB_AUTO_MIGRATE=false``) схема не создаётся автоматически:
        её приводит администратор командой ``mcp-hub db upgrade``.
        """
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if self.auto_migrate:
                from hub.migrate import upgrade

                await upgrade(self.engine)
            self._ready = True

    def session(self) -> AsyncSession:
        return AsyncSession(self.engine, expire_on_commit=False)

    async def ping(self) -> bool:
        if self._closed:
            return False
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - любая ошибка драйвера = БД недоступна
            return False

    async def audit(
        self,
        action: str,
        *,
        user_id: str | None = None,
        alias: str | None = None,
        details: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        await self.init()
        async with self.session() as session, session.begin():
            session.add(
                AuditLog(
                    ts=to_naive_utc(ts) if ts else utcnow(),
                    user_id=user_id,
                    action=action,
                    alias=alias,
                    details=details or {},
                )
            )

    async def dispose(self) -> None:
        """Закрыть движок; после этого ``ping()`` → False (``/ready`` → 503)."""
        self._closed = True
        self._ready = False
        await self.engine.dispose()


async def find_key_owner(session: AsyncSession, key_sha256: str) -> tuple[ApiKey, User] | None:
    stmt = (
        select(ApiKey, User)
        .join(User, User.user_id == ApiKey.user_id)
        .where(ApiKey.key_sha256 == key_sha256)
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row[0], row[1]


__all__ = [
    "TOKEN_ORIGIN_ISSUED",
    "TOKEN_ORIGIN_REASON_POLICY_DENIED",
    "TOKEN_ORIGIN_REASON_TOKEN_UNUSABLE",
    "TOKEN_ORIGIN_REASON_UPSTREAM_UNAVAILABLE",
    "TOKEN_ORIGIN_SUBMITTED",
    "ApiKey",
    "AuditLog",
    "Base",
    "Connection",
    "Consent",
    "Database",
    "OAuthClient",
    "OAuthCode",
    "RefreshToken",
    "UpstreamToken",
    "User",
    "WebSession",
    "build_engine",
    "find_key_owner",
    "to_iso",
    "to_naive_utc",
    "utcnow",
]
