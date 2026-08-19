"""Hub как authorization server для facade-серверов (R-O1..R-O13).

RFC 8414 (метаданные AS), RFC 9728 (метаданные ресурса), RFC 7591 (динамическая регистрация),
PKCE S256, гранты ``authorization_code`` и ``refresh_token`` с ротацией и отзывом цепочки,
RFC 7009 (``/oauth/revoke``). Ни один токен не хранится в БД в открытом виде (R-O9).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, update

from hub.catalog import Catalog, ServerEntry
from hub.clock import Clock
from hub.crypto import InvalidJWT, jwt_decode, jwt_encode, random_token, sha256_hex, verify_pkce
from hub.db import Database, OAuthClient, OAuthCode, RefreshToken, to_naive_utc
from hub.kv import KeyValueStore
from hub.metrics import Metrics
from hub.settings import Settings

logger = logging.getLogger("hub.oauth")

JTI_DENYLIST_PREFIX = "jtiden:"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
CLIENT_NAME_MAX = 128
ALLOWED_GRANTS = {"authorization_code", "refresh_token"}

STATUS_ACTIVE = "active"
STATUS_ROTATED = "rotated"
STATUS_REVOKED = "revoked"


class OAuthError(Exception):
    """Ошибка OAuth в формате RFC 6749/7591 (``{error, error_description}``, R-O13)."""

    def __init__(self, status_code: int, error: str, description: str) -> None:
        super().__init__(f"{error}: {description}")
        self.status_code = status_code
        self.error = error
        self.description = description

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def naive_from_epoch(value: float) -> datetime:
    """Секунды epoch → naive UTC (в БД время хранится без часового пояса)."""
    return datetime.fromtimestamp(float(value), tz=UTC).replace(tzinfo=None)


def invalid_grant(description: str) -> OAuthError:
    return OAuthError(400, "invalid_grant", description)


@dataclass(frozen=True)
class AccessToken:
    """Выпущенный access-токен Hub."""

    value: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenClaims:
    """Проверенные claims access-токена (R-O12)."""

    subject: str
    alias: str
    scope: str
    connection_id: int | None
    client_id: str
    jti: str
    expires_at: float


def mask_to_regex(mask: str) -> re.Pattern[str]:
    """Маска ``HUB_OAUTH_ALLOWED_REDIRECTS``: ``*`` — любая последовательность символов (R-O3)."""
    parts = [re.escape(piece) for piece in mask.split("*")]
    return re.compile("^" + ".*".join(parts) + "$", re.IGNORECASE)


def redirect_uri_allowed(uri: str, masks: list[str]) -> bool:
    return any(mask_to_regex(mask).match(uri) for mask in masks)


def validate_redirect_uri(uri: Any, masks: list[str]) -> str:
    """Абсолютный URI без фрагмента; ``http`` — только для loopback; совпадение с маской (R-O3)."""
    if not isinstance(uri, str) or not uri.strip():
        raise OAuthError(400, "invalid_redirect_uri", "redirect_uri должен быть непустой строкой")
    parsed = urlsplit(uri)
    if not parsed.scheme or not parsed.netloc:
        raise OAuthError(400, "invalid_redirect_uri", f"redirect_uri должен быть абсолютным: {uri}")
    if parsed.fragment:
        raise OAuthError(
            400, "invalid_redirect_uri", "redirect_uri не должен содержать фрагмент (#…)"
        )
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "http":
        if host not in LOOPBACK_HOSTS:
            raise OAuthError(
                400,
                "invalid_redirect_uri",
                "схема http допустима только для локальных адресов 127.0.0.1, localhost, [::1]",
            )
    elif scheme != "https":
        raise OAuthError(400, "invalid_redirect_uri", f"недопустимая схема redirect_uri: {scheme}")
    if not redirect_uri_allowed(uri, masks):
        raise OAuthError(
            400, "invalid_redirect_uri", "redirect_uri не соответствует разрешённым маскам Hub"
        )
    return uri


def redirect_uri_matches(registered: list[str], candidate: str) -> bool:
    """Точное совпадение; для loopback допускается другой порт (RFC 8252, решение 46)."""
    if candidate in registered:
        return True
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        return False
    for known in registered:
        other = urlsplit(known)
        if (
            other.scheme.lower() == parsed.scheme.lower()
            and (other.hostname or "").lower() == host
            and other.path == parsed.path
            and other.query == parsed.query
        ):
            return True
    return False


class OAuthServer:
    """Регистрация клиентов, коды, токены Hub и denylist (R-O3..R-O12)."""

    def __init__(
        self, *, settings: Settings, db: Database, kv: KeyValueStore, clock: Clock, metrics: Metrics
    ) -> None:
        self.settings = settings
        self.db = db
        self.kv = kv
        self.clock = clock
        self.metrics = metrics

    # --- метаданные (R-O1, R-O2) -------------------------------------------

    def facade_servers(self, catalog: Catalog) -> list[ServerEntry]:
        return [s for s in catalog.servers if s.model.mode == "facade" and not s.unconfigured]

    def as_metadata(self, catalog: Catalog) -> dict[str, Any]:
        base = self.settings.public_url
        scopes: list[str] = []
        for entry in self.facade_servers(catalog):
            scopes.append(f"{entry.alias}:readonly")
            scopes.append(f"{entry.alias}:readwrite")
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": scopes,
        }

    def resource_metadata(self, entry: ServerEntry) -> dict[str, Any]:
        base = self.settings.public_url
        return {
            "resource": f"{base}/mcp/{entry.alias}",
            "authorization_servers": [base],
            "scopes_supported": [f"{entry.alias}:readonly", f"{entry.alias}:readwrite"],
            "bearer_methods_supported": ["header"],
            "resource_name": entry.model.title,
            "resource_documentation": entry.model.docs_url,
        }

    def resource_metadata_url(self, alias: str) -> str:
        return f"{self.settings.public_url}/.well-known/oauth-protected-resource/mcp/{alias}"

    def www_authenticate(self, alias: str, *, error: str | None = None) -> str:
        value = f'Bearer resource_metadata="{self.resource_metadata_url(alias)}"'
        if error:
            value += f', error="{error}"'
        return value

    # --- динамическая регистрация (R-O3) ------------------------------------

    async def register_client(self, payload: Any, *, ip: str | None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise OAuthError(400, "invalid_client_metadata", "Ожидается JSON-объект метаданных клиента")
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise OAuthError(
                400, "invalid_client_metadata", "redirect_uris: обязательный непустой массив строк"
            )
        auth_method = payload.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            raise OAuthError(
                400,
                "invalid_client_metadata",
                "token_endpoint_auth_method: поддерживается только 'none' (публичный клиент)",
            )
        response_types = payload.get("response_types", ["code"])
        if response_types != ["code"]:
            raise OAuthError(
                400, "invalid_client_metadata", "response_types: поддерживается только ['code']"
            )
        grant_types = payload.get("grant_types", ["authorization_code", "refresh_token"])
        if not isinstance(grant_types, list) or not grant_types or not set(grant_types) <= ALLOWED_GRANTS:
            raise OAuthError(
                400,
                "invalid_client_metadata",
                "grant_types: допустимы только authorization_code и refresh_token",
            )
        client_name = payload.get("client_name")
        if client_name is not None and (
            not isinstance(client_name, str) or len(client_name) > CLIENT_NAME_MAX
        ):
            raise OAuthError(
                400, "invalid_client_metadata", f"client_name: строка не длиннее {CLIENT_NAME_MAX}"
            )
        masks = self.settings.oauth_allowed_redirects
        validated = [validate_redirect_uri(uri, masks) for uri in redirect_uris]

        client_id = uuid.uuid4().hex
        now = to_naive_utc(self.clock.now())
        scope = payload.get("scope")
        await self.db.init()
        async with self.db.session() as session, session.begin():
            session.add(
                OAuthClient(
                    client_id=client_id,
                    client_name=client_name,
                    redirect_uris=validated,
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=str(scope) if isinstance(scope, str) else None,
                    created_at=now,
                    created_ip=ip,
                )
            )
        await self.db.audit(
            "oauth_client_registered",
            details={"client_id": client_id, "client_name": client_name},
            ts=self.clock.now(),
        )
        logger.info("oauth_client_registered", extra={"client_id": client_id})
        body: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(self.clock.time()),
            "redirect_uris": validated,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if client_name is not None:
            body["client_name"] = client_name
        if isinstance(scope, str):
            body["scope"] = scope
        return body

    async def get_client(self, client_id: str | None) -> OAuthClient | None:
        if not client_id:
            return None
        await self.db.init()
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(OAuthClient).where(OAuthClient.client_id == client_id).limit(1)
                )
            ).scalar_one_or_none()

    # --- коды авторизации (R-O7, R-O8) --------------------------------------

    async def issue_code(
        self,
        *,
        client_id: str,
        user_id: str,
        alias: str,
        connection_id: int | None,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
        resource: str | None,
    ) -> str:
        code = random_token()
        now = to_naive_utc(self.clock.now())
        await self.db.init()
        async with self.db.session() as session, session.begin():
            session.add(
                OAuthCode(
                    code_sha256=sha256_hex(code),
                    client_id=client_id,
                    user_id=user_id,
                    alias=alias,
                    connection_id=connection_id,
                    redirect_uri=redirect_uri,
                    code_challenge=code_challenge,
                    code_challenge_method="S256",
                    scope=scope,
                    resource=resource,
                    created_at=now,
                    expires_at=now + timedelta(seconds=self.settings.auth_code_ttl),
                )
            )
        await self.db.audit(
            "oauth_code_issued",
            user_id=user_id,
            alias=alias,
            details={"client_id": client_id, "scope": scope},
            ts=self.clock.now(),
        )
        return code

    async def _load_code(self, code: str) -> OAuthCode | None:
        await self.db.init()
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(OAuthCode).where(OAuthCode.code_sha256 == sha256_hex(code)).limit(1)
                )
            ).scalar_one_or_none()

    async def exchange_code(
        self, *, code: str, client_id: str, redirect_uri: str | None, code_verifier: str | None
    ) -> dict[str, Any]:
        """``grant_type=authorization_code`` (R-O8): проверки, отзыв цепочки при повторе кода."""
        row = await self._load_code(code)
        now = to_naive_utc(self.clock.now())
        if row is None:
            raise invalid_grant("Код авторизации недействителен или уже использован")
        if row.used_at is not None:
            # RFC 6819 / решение 45: повтор кода отзывает всю выданную по нему цепочку токенов.
            await self.revoke_chain(row.code_sha256)
            await self.db.audit(
                "oauth_refresh_reuse_detected",
                user_id=row.user_id,
                alias=row.alias,
                details={"client_id": row.client_id, "reason": "code_replay"},
                ts=self.clock.now(),
            )
            raise invalid_grant("Код авторизации недействителен или уже использован")
        if row.expires_at <= now:
            raise invalid_grant("Код авторизации недействителен или уже использован")
        if row.client_id != client_id:
            raise invalid_grant("Код выдан другому клиенту")
        if redirect_uri is not None and redirect_uri != row.redirect_uri:
            raise invalid_grant("redirect_uri не совпадает с указанным при выдаче кода")
        if not verify_pkce(code_verifier, row.code_challenge, row.code_challenge_method):
            raise invalid_grant("Проверка PKCE не пройдена")

        await self.db.init()
        async with self.db.session() as session, session.begin():
            await session.execute(
                update(OAuthCode).where(OAuthCode.id == row.id).values(used_at=now)
            )
        tokens = await self.issue_tokens(
            client_id=client_id,
            user_id=row.user_id,
            alias=row.alias,
            connection_id=row.connection_id,
            scope=row.scope,
            chain_id=row.code_sha256,
            grant="authorization_code",
        )
        return tokens

    # --- токены (R-O9, R-O10) -----------------------------------------------

    def make_access_token(
        self, *, user_id: str, alias: str, scope: str, connection_id: int | None, client_id: str
    ) -> AccessToken:
        issued = int(self.clock.time())
        jti = uuid.uuid4().hex
        expires = issued + self.settings.access_token_ttl
        claims = {
            "iss": self.settings.public_url,
            "sub": user_id,
            "aud": f"{self.settings.public_url}/mcp/{alias}",
            "scope": scope,
            "cid": connection_id,
            "client_id": client_id,
            "jti": jti,
            "iat": issued,
            "exp": expires,
        }
        token = jwt_encode(claims, self.settings.secret_key.get_secret_value())
        return AccessToken(value=token, jti=jti, expires_at=naive_from_epoch(expires))

    async def issue_tokens(
        self,
        *,
        client_id: str,
        user_id: str,
        alias: str,
        connection_id: int | None,
        scope: str,
        chain_id: str,
        parent_id: int | None = None,
        chain_expires_at: datetime | None = None,
        grant: str = "authorization_code",
    ) -> dict[str, Any]:
        access = self.make_access_token(
            user_id=user_id,
            alias=alias,
            scope=scope,
            connection_id=connection_id,
            client_id=client_id,
        )
        refresh = random_token()
        now = to_naive_utc(self.clock.now())
        expires_at = chain_expires_at or now + timedelta(seconds=self.settings.refresh_token_ttl)
        await self.db.init()
        async with self.db.session() as session, session.begin():
            session.add(
                RefreshToken(
                    token_sha256=sha256_hex(refresh),
                    chain_id=chain_id,
                    parent_id=parent_id,
                    client_id=client_id,
                    user_id=user_id,
                    connection_id=connection_id,
                    alias=alias,
                    scope=scope,
                    access_jti=access.jti,
                    access_exp=access.expires_at,
                    status=STATUS_ACTIVE,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
        self.metrics.counter(
            "hub_oauth_tokens_issued_total", "Выданные токены Hub.", {"grant": grant}
        )
        await self.db.audit(
            "oauth_token_issued",
            user_id=user_id,
            alias=alias,
            details={"client_id": client_id, "alias": alias, "grant": grant},
            ts=self.clock.now(),
        )
        return {
            "access_token": access.value,
            "token_type": "Bearer",
            "expires_in": self.settings.access_token_ttl,
            "refresh_token": refresh,
            "scope": scope,
        }

    async def _load_refresh(self, token: str) -> RefreshToken | None:
        await self.db.init()
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(RefreshToken)
                    .where(RefreshToken.token_sha256 == sha256_hex(token))
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def refresh_tokens(
        self, *, refresh_token: str, client_id: str, scope: str | None
    ) -> dict[str, Any]:
        """``grant_type=refresh_token`` (R-O10): ротация, отзыв цепочки при повторном использовании."""
        row = await self._load_refresh(refresh_token)
        if row is None:
            raise invalid_grant("Refresh-токен недействителен")
        if row.client_id != client_id:
            raise invalid_grant("Refresh-токен выдан другому клиенту")
        if row.status != STATUS_ACTIVE:
            await self.revoke_chain(row.chain_id)
            await self.db.audit(
                "oauth_refresh_reuse_detected",
                user_id=row.user_id,
                alias=row.alias,
                details={"client_id": client_id, "chain": "revoked"},
                ts=self.clock.now(),
            )
            logger.warning("oauth_refresh_reuse_detected", extra={"client_id": client_id})
            raise invalid_grant("Refresh-токен уже использован, цепочка токенов отозвана")
        now = to_naive_utc(self.clock.now())
        if row.expires_at <= now:
            raise invalid_grant("Срок действия refresh-токена истёк")
        new_scope = row.scope
        if scope is not None and scope.strip() and scope != row.scope:
            if scope != f"{row.alias}:readonly":
                raise OAuthError(400, "invalid_scope", "Расширение scope при обновлении недопустимо")
            new_scope = scope
        await self.db.init()
        async with self.db.session() as session, session.begin():
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.id == row.id)
                .values(status=STATUS_ROTATED, used_at=now)
            )
        return await self.issue_tokens(
            client_id=client_id,
            user_id=row.user_id,
            alias=row.alias,
            connection_id=row.connection_id,
            scope=new_scope,
            chain_id=row.chain_id,
            parent_id=row.id,
            chain_expires_at=row.expires_at,
            grant="refresh_token",
        )

    # --- отзыв (R-O11) -------------------------------------------------------

    async def deny_jti(self, jti: str | None, expires_at: datetime | None) -> None:
        if not jti:
            return
        ttl: float = float(self.settings.access_token_ttl)
        if expires_at is not None:
            ttl = max(
                1.0, (expires_at - to_naive_utc(self.clock.now())).total_seconds()
            )
        await self.kv.set(JTI_DENYLIST_PREFIX + jti, 1, ttl=ttl)

    async def is_denied(self, jti: str) -> bool:
        return await self.kv.get(JTI_DENYLIST_PREFIX + jti) is not None

    async def revoke_chain(self, chain_id: str) -> int:
        """Отозвать всю цепочку refresh-токенов и занести их access-``jti`` в denylist (R-O10)."""
        await self.db.init()
        now = to_naive_utc(self.clock.now())
        async with self.db.session() as session, session.begin():
            rows = list(
                (
                    await session.execute(
                        select(RefreshToken).where(RefreshToken.chain_id == chain_id)
                    )
                ).scalars()
            )
            for row in rows:
                row.status = STATUS_REVOKED
        for row in rows:
            if row.access_exp is None or row.access_exp > now:
                await self.deny_jti(row.access_jti, row.access_exp)
        return len(rows)

    async def revoke_token(self, token: str) -> bool:
        """Отзыв refresh- или access-токена (RFC 7009, R-O11). ``True`` — что-то отозвано."""
        row = await self._load_refresh(token)
        if row is not None:
            await self.revoke_chain(row.chain_id)
            return True
        try:
            claims = jwt_decode(token, self.settings.secret_key.get_secret_value())
        except InvalidJWT:
            return False
        jti = claims.get("jti")
        exp = claims.get("exp")
        expires_at = naive_from_epoch(exp) if isinstance(exp, int | float) else None
        await self.deny_jti(str(jti) if jti else None, expires_at)
        if isinstance(jti, str):
            await self.db.init()
            async with self.db.session() as session:
                related = (
                    await session.execute(
                        select(RefreshToken).where(RefreshToken.access_jti == jti).limit(1)
                    )
                ).scalar_one_or_none()
            if related is not None:
                await self.revoke_chain(related.chain_id)
        return True

    async def revoke_connection_tokens(self, connection_id: int) -> None:
        """Отозвать все токены Hub, выданные для подключения (R-B8)."""
        await self.db.init()
        async with self.db.session() as session:
            chains = list(
                (
                    await session.execute(
                        select(RefreshToken.chain_id)
                        .where(RefreshToken.connection_id == connection_id)
                        .distinct()
                    )
                ).scalars()
            )
        for chain_id in chains:
            await self.revoke_chain(chain_id)

    # --- проверка access-токена на горячем пути (R-O12) ---------------------

    async def verify_access_token(self, token: str, *, alias: str) -> TokenClaims:
        """Подпись → ``exp`` → ``aud`` → denylist. Ошибки — ``OAuthError`` 401/403 (R-O12)."""
        try:
            claims = jwt_decode(token, self.settings.secret_key.get_secret_value())
        except InvalidJWT as exc:
            raise OAuthError(401, "unauthorized", "Токен недействителен") from exc
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or float(exp) <= self.clock.time():
            raise OAuthError(401, "unauthorized", "Срок действия токена истёк")
        expected_aud = f"{self.settings.public_url}/mcp/{alias}"
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if expected_aud not in [str(a) for a in audiences]:
            raise OAuthError(403, "forbidden", "Токен выдан для другого сервера")
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise OAuthError(401, "unauthorized", "Токен недействителен")
        if await self.is_denied(jti):
            raise OAuthError(401, "unauthorized", "Токен отозван")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OAuthError(401, "unauthorized", "Токен недействителен")
        cid = claims.get("cid")
        scope = claims.get("scope")
        return TokenClaims(
            subject=subject,
            alias=alias,
            scope=str(scope) if isinstance(scope, str) else f"{alias}:readonly",
            connection_id=int(cid) if isinstance(cid, int) else None,
            client_id=str(claims.get("client_id") or ""),
            jti=jti,
            expires_at=float(exp),
        )


__all__ = [
    "JTI_DENYLIST_PREFIX",
    "STATUS_ACTIVE",
    "STATUS_REVOKED",
    "STATUS_ROTATED",
    "AccessToken",
    "OAuthError",
    "OAuthServer",
    "TokenClaims",
    "invalid_grant",
    "mask_to_regex",
    "naive_from_epoch",
    "redirect_uri_allowed",
    "redirect_uri_matches",
    "validate_redirect_uri",
]
