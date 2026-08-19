"""Миграции Alembic Hub: применение при старте и из CLI (R-M1, R-M5).

Каталог версий — внутри пакета (``hub/migrations``); ``alembic.ini`` не нужен, конфигурация
собирается программно. Миграции выполняются на соединении async-движка приложения через
``run_sync`` — отдельного (синхронного) драйвера БД не требуется.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("hub.migrate")

MIGRATIONS_PATH = Path(__file__).resolve().parent / "migrations"
BASE_REVISION = "0001_i1_base"
# Таблицы, созданные ``create_all`` в I-1: их наличие без alembic_version = БД ревизии I-1 (R-M5).
I1_TABLES = ("users", "api_keys", "connections", "audit_log")


class MigrationError(RuntimeError):
    """Ошибка применения миграций: приложение не поднимается (R-M1)."""


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_PATH))
    return cfg


def head_revision() -> str:
    script = ScriptDirectory.from_config(alembic_config())
    return script.get_current_head() or ""


def _current(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _needs_stamp(connection: Connection) -> bool:
    """БД создана кодом I-1 (``create_all``) и не размечена Alembic — нужен ``stamp`` (R-M5)."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return False
    return all(name in tables for name in I1_TABLES)


def _run_upgrade(connection: Connection, revision: str) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    if _needs_stamp(connection):
        logger.info("db_stamp_base", extra={"revision": BASE_REVISION})
        command.stamp(cfg, BASE_REVISION)
    command.upgrade(cfg, revision)


def _read_current(connection: Connection) -> str | None:
    return _current(connection)


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    """Довести схему БД до ``revision`` (по умолчанию ``head``).

    Ошибка самой миграции → ``MigrationError`` с именем ревизии (приложение не поднимается, R-M1).
    Ошибки уровня соединения с БД пробрасываются как есть — их обрабатывает общий контур
    недоступности БД (``/ready`` → 503, R-A7/R-S4).
    """
    async with engine.begin() as conn:
        try:
            await conn.run_sync(_run_upgrade, revision)
        except Exception as exc:  # ошибка применения ревизии останавливает старт
            raise MigrationError(
                f"Не удалось применить миграции БД (ревизия {revision}): {type(exc).__name__}: {exc}"
            ) from exc


async def current_revision(engine: AsyncEngine) -> str | None:
    """Текущая ревизия БД (``None`` — схема не размечена)."""
    async with engine.connect() as conn:
        return await conn.run_sync(_read_current)


__all__: list[str] = [
    "BASE_REVISION",
    "MIGRATIONS_PATH",
    "MigrationError",
    "alembic_config",
    "current_revision",
    "head_revision",
    "upgrade",
]
