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
    make_url,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


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


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


def build_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    kwargs: dict[str, Any] = {"future": True}
    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if not url.database or url.database == ":memory:":
            kwargs["poolclass"] = StaticPool
    return create_async_engine(database_url, **kwargs)


class Database:
    """Обёртка над движком: ленивое создание схемы, сессии, аудит, проверка готовности."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._ready = False
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        """``create_all`` — идемпотентно."""
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self._ready = True

    def session(self) -> AsyncSession:
        return AsyncSession(self.engine, expire_on_commit=False)

    async def ping(self) -> bool:
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
    "ApiKey",
    "AuditLog",
    "Base",
    "Connection",
    "Database",
    "User",
    "build_engine",
    "find_key_owner",
    "to_iso",
    "to_naive_utc",
    "utcnow",
]
