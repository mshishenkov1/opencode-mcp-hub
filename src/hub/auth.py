"""Аутентификация Bearer по ключу LiteLLM: sha256 → api_keys → пользователь, кэш 60 с (R-L6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request

from hub.db import find_key_owner, to_iso
from hub.errors import unauthorized
from hub.login import sha256_hex

AUTH_CACHE_PREFIX = "keyauth:"
AUTH_CACHE_TTL = 60.0


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str | None
    groups: list[str]
    key_kind: str
    created_at: str | None  # ISO-8601 (UTC) ключа, которым выполнен запрос

    def to_me(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "key_kind": self.key_kind,
            "created_at": self.created_at,
        }


async def invalidate_key_cache(kv: Any, key_sha256: str) -> None:
    """Немедленно убрать положительный результат аутентификации ключа (R-L11.5, R-L12.5).

    Без сброса вышедший (или отозванный при повторном входе) ключ открывал бы ``/api/*`` ещё до
    истечения ``AUTH_CACHE_TTL``; это часть правила, а не оптимизация.
    """
    await kv.delete(AUTH_CACHE_PREFIX + key_sha256)


def extract_bearer(request: Request) -> str | None:
    """``Authorization: Bearer <key>``, иначе ``x-litellm-api-key`` (если оба — Authorization)."""
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        return None
    alt = request.headers.get("x-litellm-api-key")
    if alt and alt.strip():
        return alt.strip()
    return None


async def authenticate(request: Request) -> AuthUser:
    """FastAPI-зависимость: пользователь по ключу или 401 ``unauthorized``."""
    token = extract_bearer(request)
    if not token:
        raise unauthorized()
    state = request.app.state
    digest = sha256_hex(token)
    cache_key = AUTH_CACHE_PREFIX + digest
    cached = await state.kv.get(cache_key)
    if isinstance(cached, dict) and cached.get("user_id"):
        return AuthUser(
            user_id=str(cached["user_id"]),
            email=cached.get("email"),
            groups=list(cached.get("groups") or ["all"]),
            key_kind=str(cached.get("key_kind", "persistent")),
            created_at=cached.get("created_at"),
        )

    db = state.db
    await db.init()
    async with db.session() as session:
        found = await find_key_owner(session, digest)
    if found is None:
        raise unauthorized()
    api_key, user = found
    auth_user = AuthUser(
        user_id=user.user_id,
        email=user.email,
        groups=list(user.groups or ["all"]),
        key_kind=api_key.key_kind,
        created_at=to_iso(api_key.created_at),
    )
    await state.kv.set(
        cache_key,
        {
            "user_id": auth_user.user_id,
            "email": auth_user.email,
            "groups": auth_user.groups,
            "key_kind": auth_user.key_kind,
            "created_at": auth_user.created_at,
        },
        ttl=AUTH_CACHE_TTL,
    )
    return auth_user


async def authenticate_key_or_session(request: Request) -> AuthUser:
    """``/api/me/*``: ключ LiteLLM **либо** веб-сессия с CSRF для небезопасных методов (R-W6)."""
    from hub.db import User
    from hub.websession import CSRF_HEADER, check_csrf, session_token

    if extract_bearer(request) is not None:
        return await authenticate(request)
    state = request.app.state
    info = await state.web_sessions.load(session_token(request))
    if info is None:
        raise unauthorized()
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_csrf(info, request.headers.get(CSRF_HEADER))
    await state.db.init()
    async with state.db.session() as session:
        user = await session.get(User, info.user_id)
    return AuthUser(
        user_id=info.user_id,
        email=user.email if user else None,
        groups=list(user.groups) if user and user.groups else ["all"],
        key_kind="session",
        created_at=None,
    )


__all__ = [
    "AUTH_CACHE_PREFIX",
    "AUTH_CACHE_TTL",
    "AuthUser",
    "authenticate",
    "authenticate_key_or_session",
    "extract_bearer",
    "invalidate_key_cache",
]
