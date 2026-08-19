"""CLI ``mcp-hub``: ``serve`` (uvicorn) и ``catalog validate`` (R-C5, R-S5)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from hub import __version__
from hub.catalog import CatalogError, load_catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="OpenCode MCP Hub — каталог MCP-серверов и вход через LiteLLM CLI-SSO.",
    )
    parser.add_argument("--version", action="version", version=f"mcp-hub {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    serve = sub.add_parser("serve", help="запустить HTTP-сервер Hub (uvicorn)")
    serve.add_argument("--host", default="0.0.0.0", help="адрес прослушивания (по умолчанию 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    serve.add_argument("--reload", action="store_true", help="перезапуск при изменении кода (разработка)")
    serve.add_argument("--workers", type=int, default=None, help="число процессов uvicorn")

    catalog = sub.add_parser(
        "catalog",
        help="операции с каталогом MCP-серверов",
        description="Операции с каталогом MCP-серверов: validate [--path <файл>] — проверка схемы.",
    )
    catalog_sub = catalog.add_subparsers(dest="catalog_command", metavar="<subcommand>")
    catalog_sub.required = True
    validate = catalog_sub.add_parser(
        "validate", help="проверить схему каталога: validate [--path <файл>]"
    )
    validate.add_argument(
        "--path",
        default=None,
        help="путь к каталогу (по умолчанию HUB_CATALOG_PATH или ./catalog.yaml)",
    )

    db = sub.add_parser(
        "db",
        help="миграции БД (Alembic)",
        description="Миграции БД: upgrade [--revision head] — применить, current — текущая ревизия.",
    )
    db_sub = db.add_subparsers(dest="db_command", metavar="<subcommand>")
    db_sub.required = True
    upgrade = db_sub.add_parser("upgrade", help="применить миграции до указанной ревизии")
    upgrade.add_argument("--revision", default="head", help="целевая ревизия (по умолчанию head)")
    db_sub.add_parser("current", help="напечатать текущую ревизию БД")
    return parser


def cmd_catalog_validate(path: str | None) -> int:
    catalog_path = path or os.environ.get("HUB_CATALOG_PATH") or "./catalog.yaml"
    try:
        catalog = load_catalog(catalog_path)
    except CatalogError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    unconfigured = [s.alias for s in catalog.unconfigured()]
    suffix = f" (unconfigured: {', '.join(unconfigured)})" if unconfigured else ""
    print(f"OK: version={catalog.version}, servers={len(catalog.servers)}{suffix}")
    return 0


def _database_url() -> str:
    return os.environ.get("HUB_DATABASE_URL") or "sqlite+aiosqlite:///./hub.db"


def cmd_db(db_command: str, revision: str = "head") -> int:
    """``mcp-hub db upgrade|current`` (R-M1)."""
    import asyncio

    from hub.db import build_engine
    from hub.migrate import MigrationError, current_revision, upgrade

    async def run() -> int:
        engine = build_engine(_database_url())
        try:
            if db_command == "upgrade":
                await upgrade(engine, revision)
                current = await current_revision(engine)
                print(f"OK: ревизия БД {current or 'не задана'}")
                return 0
            current = await current_revision(engine)
            print(current or "нет ревизии (схема не размечена)")
            return 0
        finally:
            await engine.dispose()

    try:
        return asyncio.run(run())
    except MigrationError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


def cmd_serve(host: str, port: int, reload: bool, workers: int | None) -> int:
    import uvicorn

    uvicorn.run(
        "hub.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        log_level=os.environ.get("HUB_LOG_LEVEL", "info").lower(),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args.host, args.port, args.reload, args.workers)
    if args.command == "catalog" and args.catalog_command == "validate":
        return cmd_catalog_validate(args.path)
    if args.command == "db":
        return cmd_db(args.db_command, getattr(args, "revision", "head"))
    parser.print_help()
    return 2


def entrypoint() -> None:  # pragma: no cover - обёртка для console_scripts
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
