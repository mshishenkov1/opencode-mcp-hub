"""Веб-сессии Hub и CSRF-токены (R-W1, R-W6).

Идентификатор сессии и CSRF-токен наружу отдаются только в cookie; в БД хранятся их sha256.
CSRF-токен выводится из идентификатора сессии (HMAC на ``HUB_SECRET_KEY``) — он привязан к сессии
и не хранится в открытом виде нигде.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Request, Response
from sqlalchemy import delete, select

from hub.clock import Clock
from hub.crypto import random_token, sha256_hex
from hub.db import Database, WebSession, to_naive_utc
from hub.errors import HubError

SESSION_COOKIE = "hub_session"
CSRF_COOKIE = "hub_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"


@dataclass(frozen=True)
class WebSessionInfo:
    """Активная веб-сессия пользователя."""

    id: int
    user_id: str
    auth_method: str
    csrf_token: str
    expires_at: datetime


def forbidden_csrf() -> HubError:
    return HubError(403, "forbidden", "Отсутствует или неверный CSRF-токен")


class WebSessionService:
    """Создание, чтение и удаление веб-сессий; проверка CSRF (R-W1, R-W6)."""

    def __init__(self, *, db: Database, clock: Clock, ttl: int, secret_key: str, secure: bool) -> None:
        self.db = db
        self.clock = clock
        self.ttl = ttl
        self._secret_key = secret_key
        self.secure = secure

    # --- CSRF ---------------------------------------------------------------

    def csrf_for(self, session_token: str) -> str:
        digest = hmac.new(
            self._secret_key.encode("utf-8"), f"csrf:{session_token}".encode(), hashlib.sha256
        )
        return digest.hexdigest()

    # --- жизненный цикл сессии ---------------------------------------------

    async def create(self, user_id: str, auth_method: str) -> tuple[str, str]:
        """Создать сессию: возвращает ``(идентификатор сессии, CSRF-токен)``."""
        token = random_token()
        csrf = self.csrf_for(token)
        now = to_naive_utc(self.clock.now())
        expires_at = now + timedelta(seconds=self.ttl)
        await self.db.init()
        async with self.db.session() as session, session.begin():
            session.add(
                WebSession(
                    session_sha256=sha256_hex(token),
                    csrf_sha256=sha256_hex(csrf),
                    user_id=user_id,
                    auth_method=auth_method,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
        return token, csrf

    async def load(self, token: str | None) -> WebSessionInfo | None:
        if not token:
            return None
        await self.db.init()
        digest = sha256_hex(token)
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(WebSession).where(WebSession.session_sha256 == digest).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.expires_at <= to_naive_utc(self.clock.now()):
                return None
            return WebSessionInfo(
                id=row.id,
                user_id=row.user_id,
                auth_method=row.auth_method,
                csrf_token=self.csrf_for(token),
                expires_at=row.expires_at,
            )

    async def delete(self, token: str | None) -> None:
        if not token:
            return
        await self.db.init()
        async with self.db.session() as session, session.begin():
            await session.execute(
                delete(WebSession).where(WebSession.session_sha256 == sha256_hex(token))
            )

    # --- cookie -------------------------------------------------------------

    def set_cookies(self, response: Response, token: str, csrf: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=self.ttl,
            httponly=True,
            samesite="lax",
            secure=self.secure,
            path="/",
        )
        # Double-submit: значение читается скриптом страницы и уходит в заголовок X-CSRF-Token.
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            max_age=self.ttl,
            httponly=False,
            samesite="lax",
            secure=self.secure,
            path="/",
        )

    def clear_cookies(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def check_csrf(info: WebSessionInfo, provided: str | None) -> None:
    """Сверить CSRF-токен запроса с токеном сессии; несовпадение → 403 (R-W6)."""
    if not provided or not hmac.compare_digest(
        sha256_hex(info.csrf_token), sha256_hex(provided.strip())
    ):
        raise forbidden_csrf()


__all__ = [
    "CSRF_COOKIE",
    "CSRF_FIELD",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "WebSessionInfo",
    "WebSessionService",
    "check_csrf",
    "forbidden_csrf",
    "session_token",
]
