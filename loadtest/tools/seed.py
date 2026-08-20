"""Подготовка данных для нагрузочного прогона (D6-08).

Создаёт в базе нагрузочного стенда пользователей, ключи LiteLLM, подключения к
серверам каталога, «токены целевой системы» (мок) и цепочки refresh-токенов Hub,
после чего пишет их в JSON для k6. Ни одного обращения во внешние системы:
используются только модули Hub и его собственная база.

    python loadtest/tools/seed.py --users 50 --out loadtest/.seed/seed.json

Переменные окружения — те же, что у Hub (HUB_DATABASE_URL, HUB_SECRET_KEY,
HUB_ENCRYPTION_KEY, HUB_CATALOG_PATH, HUB_PUBLIC_URL, HUB_REDIS_URL): скрипт
обязан видеть ту же базу и те же ключи, что и проверяемый Hub.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hub.app import create_app
from hub.broker import STATUS_CONNECTED, UpstreamTokens
from hub.db import ApiKey, User
from hub.login import sha256_hex

ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1", "hub", "mock-upstream", "postgres", "redis")


def _check_no_prod(settings: Any) -> None:
    """Страховка D6-10: сеять данные можно только против локального стенда."""
    from urllib.parse import urlparse

    for name, url in (
        ("HUB_PUBLIC_URL", settings.public_url),
        ("HUB_DATABASE_URL", settings.database_url),
    ):
        host = urlparse(url.replace("postgresql+asyncpg", "postgresql")).hostname
        if host and host not in ALLOWED_HOSTS:
            raise SystemExit(
                f"{name}={url}: хост {host} не входит в список разрешённых {ALLOWED_HOSTS}. "
                "Нагрузочные данные разрешено сеять только в локальный стенд."
            )


async def _seed(args: argparse.Namespace) -> dict[str, Any]:
    app = create_app()
    state = app.state
    _check_no_prod(state.settings)

    aliases = [a.strip() for a in args.aliases.split(",") if a.strip()]
    known = {entry.alias for entry in state.catalog.servers if entry.model.mode == "facade"}
    missing = [a for a in aliases if a not in known]
    if missing:
        raise SystemExit(
            f"В каталоге {state.settings.catalog_path} нет facade-серверов {missing}; "
            f"есть {sorted(known)}"
        )

    db = state.db
    broker = state.broker
    oauth = state.oauth
    await db.init()

    client = await oauth.register_client(
        {
            "client_name": "loadtest",
            "redirect_uris": ["http://127.0.0.1:9999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        ip="127.0.0.1",
    )
    client_id = str(client["client_id"])

    users: list[dict[str, Any]] = []
    for index in range(args.users):
        user_id = f"{args.prefix}{index:05d}"
        api_key = f"sk-loadtest-{index:05d}-{secrets.token_hex(8)}"
        async with db.session() as session, session.begin():
            existing = await session.get(User, user_id)
            if existing is None:
                session.add(User(user_id=user_id, email=f"{user_id}@example.invalid", groups=["all"]))
                # Явный flush: связь users → api_keys задана только внешним ключом,
                # без relationship(), поэтому порядок вставки нужно задать самим.
                await session.flush()
            session.add(
                ApiKey(
                    key_sha256=sha256_hex(api_key),
                    user_id=user_id,
                    key_kind="persistent",
                    key_alias=f"loadtest-{index:05d}",
                    client="loadtest",
                )
            )

        entry_tokens: dict[str, Any] = {}
        for alias in aliases:
            entry = state.catalog.get(alias)
            assert entry is not None
            groups = args.groups.split(",") if args.groups else []
            connection = await broker.upsert_connection(
                user_id=user_id,
                alias=alias,
                status=STATUS_CONNECTED,
                preset=args.preset,
                groups=groups,
                clear_reason=True,
            )
            await broker.save_tokens(
                connection,
                UpstreamTokens(
                    access_token="mock-access-" + uuid.uuid4().hex,
                    refresh_token="mock-refresh-" + uuid.uuid4().hex,
                    expires_in=args.token_ttl,
                    scopes=["read_api", "read_user"],
                ),
            )
            issued = await oauth.issue_tokens(
                client_id=client_id,
                user_id=user_id,
                alias=alias,
                connection_id=connection.id,
                scope=f"{alias}:{args.preset}",
                chain_id=uuid.uuid4().hex,
            )
            entry_tokens[alias] = {
                "access_token": issued["access_token"],
                "refresh_token": issued["refresh_token"],
            }
        users.append({"user_id": user_id, "api_key": api_key, "tokens": entry_tokens})

    await state.kv.close()
    await db.dispose()
    await state.litellm_client.aclose()
    if state.outbound_client is not state.litellm_client:
        await state.outbound_client.aclose()

    return {
        "hub_base": state.settings.public_url,
        "client_id": client_id,
        "aliases": aliases,
        "preset": args.preset,
        "users": users,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Сеет данные нагрузочного стенда Hub")
    parser.add_argument("--users", type=int, default=50, help="сколько пользователей завести")
    parser.add_argument("--aliases", default="gitlab,jira", help="facade-alias'ы через запятую")
    parser.add_argument("--preset", default="readonly", help="пресет прав подключения")
    parser.add_argument("--groups", default="code_review,devops", help="группы прав через запятую")
    parser.add_argument("--token-ttl", type=int, default=86400, help="TTL мок-токена системы, с")
    parser.add_argument("--prefix", default="loadtest-user-", help="префикс user_id")
    parser.add_argument(
        "--out", default="loadtest/.seed/seed.json", help="куда положить JSON для k6"
    )
    args = parser.parse_args()

    data = asyncio.run(_seed(args))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Записано {len(data['users'])} пользователей × {len(data['aliases'])} подключений "
        f"в {out} (client_id={data['client_id']}, hub={data['hub_base']})"
    )


if __name__ == "__main__":
    if not os.environ.get("HUB_DATABASE_URL"):
        raise SystemExit("HUB_DATABASE_URL не задан: нужны те же настройки, что у Hub")
    main()
