"""Брокер токенов целевых систем: OAuth по каталогу, шифрование, обновление фоном (R-B1..R-B9).

Все параметры OAuth берутся из ``catalog.yaml`` (``auth``); HTTP-клиент инжектируется
(``app.state.oauth_client``) — тесты подменяют его respx/MockTransport. Токены систем хранятся
только в зашифрованном виде (Fernet) и наружу не отдаются (R-B9).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from hub.catalog import AuthOAuth2, AuthUserToken, Catalog, ServerEntry
from hub.clock import Clock
from hub.crypto import TokenCipher, code_challenge_s256
from hub.db import Connection, Database, UpstreamToken, to_naive_utc, utcnow
from hub.kv import KeyValueStore
from hub.metrics import Metrics
from hub.proxy import resolve_header_value
from hub.settings import Settings

logger = logging.getLogger("hub.broker")

CONN_CACHE_PREFIX = "conn:"
REFRESH_LOCK_PREFIX = "refreshlock:"
REFRESH_LOCK_TTL = 30.0
REFRESH_WAIT_SECONDS = 5.0
REFRESH_WAIT_STEP = 0.05

STATUS_CONNECTED = "connected"
STATUS_NEEDS_REAUTH = "needs_reauth"
STATUS_NOT_CONNECTED = "not_connected"

REASON_NO_REFRESH = "Токен доступа истёк, обновление невозможно — нужна повторная авторизация"
REASON_REFRESH_FAILED = "Целевая система отклонила обновление токена — нужна повторная авторизация"
REASON_SCOPE_UPGRADE = "Нужно заново разрешить доступ в целевой системе: запрошены более широкие права"
# R-U6: у пользовательского токена нет refresh — единственный признак негодности 401 от upstream.
REASON_TOKEN_REJECTED = (
    "Токен целевой системы больше не действует — подключитесь заново, указав новый токен"
)


class BrokerError(Exception):
    """Базовая ошибка брокера токенов."""


class ServerUnconfigured(BrokerError):
    """Не заданы переменные окружения ``client_id``/``client_secret`` провайдера (R-B1)."""


class UpstreamAuthFailed(BrokerError):
    """Ошибка обмена/обновления на стороне целевой системы (R-B2)."""

    def __init__(self, message: str, *, invalid_grant: bool = False) -> None:
        super().__init__(message)
        self.invalid_grant = invalid_grant


class UserTokenRejected(BrokerError):
    """Целевая система не приняла пользовательский токен: 401/403 на проверке (R-U3)."""


class UpstreamUnavailable(BrokerError):
    """Целевая система недоступна на проверке токена: 5xx, сеть, таймаут, не-JSON (R-U3)."""


class NeedsReauth(BrokerError):
    """Подключение переведено в ``needs_reauth``: пригодного токена нет (R-B5)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class UpstreamTokens:
    """Ответ токен-эндпоинта целевой системы."""

    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    token_type: str = "Bearer"
    scopes: list[str] = field(default_factory=list)
    account: str | None = None


def _parse_token_response(payload: Any) -> UpstreamTokens:
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise UpstreamAuthFailed("ответ целевой системы не содержит access_token")
    expires_in = payload.get("expires_in")
    scope = payload.get("scope")
    scopes: list[str] = []
    if isinstance(scope, str) and scope.strip():
        scopes = scope.split()
    elif isinstance(scope, list):
        scopes = [str(s) for s in scope]
    refresh = payload.get("refresh_token")
    account = payload.get("account_id") or payload.get("user_id") or payload.get("username")
    return UpstreamTokens(
        access_token=str(payload["access_token"]),
        refresh_token=str(refresh) if isinstance(refresh, str) and refresh else None,
        expires_in=int(expires_in)
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
        else None,
        token_type=str(payload.get("token_type") or "Bearer"),
        scopes=scopes,
        account=str(account) if isinstance(account, str) and account else None,
    )


class TokenBroker:
    """Получение, хранение и обновление токенов целевых систем (R-B1..R-B9)."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        kv: KeyValueStore,
        clock: Clock,
        cipher: TokenCipher,
        metrics: Metrics,
        http: Callable[[], httpx.AsyncClient],
        catalog: Callable[[], Catalog],
        catalog_env: Callable[[], Mapping[str, str] | None],
    ) -> None:
        self.settings = settings
        self.db = db
        self.kv = kv
        self.clock = clock
        self.cipher = cipher
        self.metrics = metrics
        self._http = http
        self._catalog = catalog
        self._catalog_env = catalog_env

    # --- параметры провайдера (R-B1) ---------------------------------------

    @property
    def catalog(self) -> Catalog:
        return self._catalog()

    def provider(self, entry: ServerEntry) -> AuthOAuth2:
        auth = entry.model.auth
        if auth is not None:
            return auth
        # R-U1: сервер может объявлять способы списком — берём первый доступный OAuth-способ.
        methods = [m for m in entry.auth_methods if isinstance(m, AuthOAuth2)]
        oauth = next((m for m in methods if m.available), None) or next(iter(methods), None)
        if oauth is None:
            raise ServerUnconfigured(f"сервер {entry.alias}: не задан способ подключения oauth2")
        return oauth

    def client_secret(self, entry: ServerEntry) -> str:
        auth = self.provider(entry)
        value = auth.client_secret.get(self._catalog_env())
        if not value:
            logger.warning("provider_secret_missing", extra={"alias": entry.alias})
            raise ServerUnconfigured(f"сервер {entry.alias}: не задан client_secret провайдера")
        return value

    def scopes_for(self, entry: ServerEntry, preset: str) -> str:
        auth = self.provider(entry)
        scopes = auth.scopes.readwrite if preset == "readwrite" else auth.scopes.readonly
        return " ".join(scopes)

    def redirect_uri(self, alias: str) -> str:
        return f"{self.settings.public_url}/oauth/callback/{alias}"

    def authorize_url(
        self, entry: ServerEntry, *, preset: str, state: str, code_verifier: str | None
    ) -> str:
        auth = self.provider(entry)
        params = {
            "response_type": "code",
            "client_id": auth.client_id,
            "redirect_uri": self.redirect_uri(entry.alias),
            "state": state,
            "scope": self.scopes_for(entry, preset),
        }
        if auth.pkce and code_verifier:
            params["code_challenge"] = code_challenge_s256(code_verifier)
            params["code_challenge_method"] = "S256"
        separator = "&" if "?" in auth.authorize_url else "?"
        return f"{auth.authorize_url}{separator}{urlencode(params)}"

    # --- обмен и обновление (R-B2, R-B4, R-B6) ------------------------------

    async def _token_request(self, entry: ServerEntry, payload: dict[str, str]) -> UpstreamTokens:
        auth = self.provider(entry)
        payload = {**payload, "client_id": auth.client_id, "client_secret": self.client_secret(entry)}
        try:
            response = await self._http().post(
                auth.token_url, data=payload, timeout=self.settings.upstream_timeout
            )
        except httpx.HTTPError as exc:
            raise UpstreamAuthFailed(f"целевая система недоступна: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            body: Any = None
            try:
                body = response.json()
            except ValueError:
                body = None
            error = body.get("error") if isinstance(body, dict) else None
            raise UpstreamAuthFailed(
                f"целевая система ответила {response.status_code}",
                invalid_grant=(error == "invalid_grant" or 400 <= response.status_code < 500),
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamAuthFailed("целевая система вернула не JSON") from exc
        return _parse_token_response(data)

    async def exchange_code(
        self, entry: ServerEntry, *, code: str, code_verifier: str | None
    ) -> UpstreamTokens:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri(entry.alias),
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier
        return await self._token_request(entry, payload)

    async def refresh(self, entry: ServerEntry, refresh_token: str) -> UpstreamTokens:
        return await self._token_request(
            entry, {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def revoke(self, entry: ServerEntry, token: str, *, auth_method: str | None = None) -> None:
        """Best-effort отзыв токена в целевой системе (R-B8): ошибка не блокирует отключение.

        Для подключений ``user_token`` отзыв не выполняется никогда (R-U5, решение 66): личный
        токен пользователя нельзя отзывать за него и нельзя отправлять на OAuth-эндпоинт отзыва.
        """
        if entry.uses_user_token(auth_method):
            return
        auth = self.provider(entry)
        if not auth.revoke_url:
            return
        try:
            payload = {
                "token": token,
                "client_id": auth.client_id,
                "client_secret": self.client_secret(entry),
            }
            await self._http().post(
                auth.revoke_url, data=payload, timeout=self.settings.upstream_timeout
            )
        except (httpx.HTTPError, BrokerError) as exc:
            logger.info("upstream_revoke_failed", extra={"alias": entry.alias, "reason": str(exc)})

    # --- пользовательский токен (R-U3, R-U4) --------------------------------

    async def verify_user_token(
        self, entry: ServerEntry, method: AuthUserToken, token: str
    ) -> str | None:
        """Проверить токен запросом к целевой системе; вернуть ``provider_account`` (R-U3).

        Токен не логируется ни в каком виде: в журнал попадают alias, id способа и HTTP-код.
        """
        verify = method.verify
        environ = self._catalog_env()
        headers = {
            name: resolve_header_value(value, environ, token)
            for name, value in verify.headers.items()
        }
        try:
            response = await self._http().request(
                verify.method, verify.url, headers=headers, timeout=self.settings.upstream_timeout
            )
        except httpx.HTTPError as exc:
            logger.info(
                "user_token_verify_unavailable",
                extra={"alias": entry.alias, "auth_method": method.id, "reason": type(exc).__name__},
            )
            raise UpstreamUnavailable("целевая система недоступна") from exc
        status = response.status_code
        logger.info(
            "user_token_verified",
            extra={"alias": entry.alias, "auth_method": method.id, "status": status},
        )
        expected = verify.expect_status
        accepted = status == expected if expected is not None else 200 <= status < 300
        if not accepted:
            if status in (401, 403):
                raise UserTokenRejected("целевая система не приняла токен")
            raise UpstreamUnavailable(f"целевая система ответила {status}")
        if not verify.account_field:
            return None
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise UpstreamUnavailable("целевая система вернула не JSON") from exc
        if not isinstance(payload, dict):
            raise UpstreamUnavailable("целевая система вернула не JSON-объект")
        value = payload.get(verify.account_field)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    async def save_user_token(
        self, connection: Connection, token: str, *, token_type: str = "Bearer"
    ) -> None:
        """Сохранить пользовательский токен (R-U4): без refresh-токена и без срока действия."""
        await self.save_tokens(
            connection,
            UpstreamTokens(
                access_token=token,
                refresh_token=None,
                expires_in=None,
                token_type=token_type,
                scopes=[],
            ),
        )

    # --- подключения и кэш (R-O12, R-B3) ------------------------------------

    def cache_key(self, user_id: str, alias: str) -> str:
        return f"{CONN_CACHE_PREFIX}{user_id}:{alias}"

    async def invalidate_cache(self, user_id: str, alias: str) -> None:
        await self.kv.delete(self.cache_key(user_id, alias))

    async def load_connection(self, user_id: str, alias: str) -> Connection | None:
        await self.db.init()
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(Connection)
                    .where(Connection.user_id == user_id, Connection.alias == alias)
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def connection_state(self, user_id: str, alias: str) -> dict[str, Any] | None:
        """Состояние подключения из KV-кэша, при промахе — из БД (R-O12)."""
        cached = await self.kv.get(self.cache_key(user_id, alias))
        if isinstance(cached, dict):
            return cached
        conn = await self.load_connection(user_id, alias)
        if conn is None:
            return None
        token_expires_at: float | None = None
        await self.db.init()
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(UpstreamToken).where(UpstreamToken.connection_id == conn.id).limit(1)
                )
            ).scalar_one_or_none()
            has_refresh = bool(row and row.refresh_token_enc)
            has_token = row is not None
            access_token_enc = row.access_token_enc if row is not None else None
            if row is not None and row.expires_at is not None:
                token_expires_at = row.expires_at.replace(tzinfo=UTC).timestamp()
        state = {
            "connection_id": conn.id,
            "status": conn.status,
            "preset": conn.preset,
            "groups": list(conn.groups or []),
            "revision": conn.revision,
            "token_expires_at": token_expires_at,
            "has_token": has_token,
            "has_refresh": has_refresh,
            # Токен системы кладётся в кэш в том же зашифрованном виде, что и в БД (R-B3),
            # чтобы горячий путь /mcp/{alias} не ходил в БД (R-O12).
            "access_token_enc": access_token_enc,
            "needs_reauth_reason": conn.needs_reauth_reason,
            # R-U6: способ подключения определяет ветку обработки 401 от целевой системы.
            "auth_method": conn.auth_method,
        }
        await self.kv.set(
            self.cache_key(user_id, alias), state, ttl=self.settings.connection_cache_ttl
        )
        return state

    async def upsert_connection(
        self,
        *,
        user_id: str,
        alias: str,
        status: str | None = None,
        preset: str | None = None,
        groups: list[str] | None = None,
        needs_reauth_reason: str | None = None,
        clear_reason: bool = False,
        provider_account: str | None = None,
        auth_method: str | None = None,
        clear_auth_method: bool = False,
    ) -> Connection:
        """Создать/обновить подключение с инкрементом ``revision`` и сбросом кэша (R-M3)."""
        await self.db.init()
        now = to_naive_utc(self.clock.now())
        async with self.db.session() as session, session.begin():
            conn = (
                await session.execute(
                    select(Connection)
                    .where(Connection.user_id == user_id, Connection.alias == alias)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conn is None:
                conn = Connection(
                    user_id=user_id,
                    alias=alias,
                    status=status or STATUS_NOT_CONNECTED,
                    preset=preset,
                    groups=list(groups or []),
                    created_at=now,
                    updated_at=now,
                    revision=0,
                )
                session.add(conn)
            if status is not None:
                conn.status = status
            if preset is not None:
                conn.preset = preset
            if groups is not None:
                conn.groups = list(groups)
            if needs_reauth_reason is not None:
                conn.needs_reauth_reason = needs_reauth_reason
            elif clear_reason:
                conn.needs_reauth_reason = None
            if provider_account:
                conn.provider_account = provider_account
            if auth_method is not None:
                conn.auth_method = auth_method
            elif clear_auth_method:
                conn.auth_method = None
            conn.revision = int(conn.revision or 0) + 1
            conn.updated_at = now
            await session.flush()
            connection_id = conn.id
        await self.invalidate_cache(user_id, alias)
        result = await self.load_connection(user_id, alias)
        assert result is not None and result.id == connection_id
        return result

    async def save_tokens(
        self, connection: Connection, tokens: UpstreamTokens, *, keep_refresh: str | None = None
    ) -> None:
        """Сохранить токены системы в зашифрованном виде (R-B3, R-B6)."""
        await self.db.init()
        now = to_naive_utc(self.clock.now())
        expires_at = (
            now + timedelta(seconds=tokens.expires_in) if tokens.expires_in is not None else None
        )
        refresh = tokens.refresh_token or keep_refresh
        async with self.db.session() as session, session.begin():
            row = (
                await session.execute(
                    select(UpstreamToken)
                    .where(UpstreamToken.connection_id == connection.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                row = UpstreamToken(connection_id=connection.id, obtained_at=now)
                session.add(row)
            row.access_token_enc = self.cipher.encrypt(tokens.access_token)
            row.refresh_token_enc = self.cipher.encrypt(refresh) if refresh else None
            row.token_type = tokens.token_type
            row.scopes = list(tokens.scopes)
            row.expires_at = expires_at
            row.updated_at = now
            row.refresh_failed_at = None
            row.last_error = None
        await self.invalidate_cache(connection.user_id, connection.alias)

    async def delete_tokens(self, connection: Connection) -> str | None:
        """Удалить токены подключения; вернуть расшифрованный access-токен (для отзыва)."""
        await self.db.init()
        async with self.db.session() as session, session.begin():
            row = (
                await session.execute(
                    select(UpstreamToken)
                    .where(UpstreamToken.connection_id == connection.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            token = self.cipher.try_decrypt(row.access_token_enc)
            await session.delete(row)
        await self.invalidate_cache(connection.user_id, connection.alias)
        return token

    async def mark_needs_reauth(self, connection: Connection, reason: str) -> None:
        """Перевести подключение в ``needs_reauth`` (R-B5): токены Hub остаются валидными."""
        await self.db.init()
        now = to_naive_utc(self.clock.now())
        async with self.db.session() as session, session.begin():
            conn = await session.get(Connection, connection.id)
            if conn is None:  # pragma: no cover - подключение удалено параллельно
                return
            conn.status = STATUS_NEEDS_REAUTH
            conn.needs_reauth_reason = reason
            conn.revision = int(conn.revision or 0) + 1
            conn.updated_at = now
            row = (
                await session.execute(
                    select(UpstreamToken)
                    .where(UpstreamToken.connection_id == connection.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.refresh_failed_at = now
                row.last_error = "refresh_failed"
        await self.invalidate_cache(connection.user_id, connection.alias)
        await self.db.audit(
            "connection_needs_reauth",
            user_id=connection.user_id,
            alias=connection.alias,
            details={"reason": reason},
            ts=self.clock.now(),
        )
        logger.info(
            "connection_needs_reauth", extra={"alias": connection.alias, "reason": reason}
        )

    # --- пригодный access-токен целевой системы (R-B4, R-B5) ----------------

    def _is_valid(self, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return True
        return (expires_at - to_naive_utc(self.clock.now())).total_seconds() > 0

    async def _read_token_row(self, connection_id: int) -> UpstreamToken | None:
        await self.db.init()
        async with self.db.session() as session:
            return (
                await session.execute(
                    select(UpstreamToken).where(UpstreamToken.connection_id == connection_id).limit(1)
                )
            ).scalar_one_or_none()

    async def cached_access_token(self, state_record: dict[str, Any]) -> str | None:
        """Пригодный токен системы из KV-кэша подключения; ``None`` — нужно идти в БД (R-O12)."""
        enc = state_record.get("access_token_enc")
        if not isinstance(enc, str) or not enc:
            return None
        expires_at = state_record.get("token_expires_at")
        if isinstance(expires_at, int | float) and float(expires_at) - self.clock.time() <= 0:
            return None
        return self.cipher.try_decrypt(enc)

    async def access_token(self, connection: Connection, *, force_refresh: bool = False) -> str:
        """Пригодный access-токен целевой системы; при необходимости обновляется (R-B4).

        Провал обновления → ``NeedsReauth`` (подключение переведено в ``needs_reauth``).
        """
        row = await self._read_token_row(connection.id)
        if row is None:
            await self.mark_needs_reauth(connection, REASON_NO_REFRESH)
            raise NeedsReauth(REASON_NO_REFRESH)
        if not force_refresh and self._is_valid(row.expires_at):
            token = self.cipher.try_decrypt(row.access_token_enc)
            if token:
                return token
        return await self.refresh_connection(connection)

    async def refresh_connection(self, connection: Connection) -> str:
        """Обновить токен под блокировкой в KV; вернуть новый access-токен (R-B4)."""
        entry = self.catalog.get(connection.alias)
        if entry is None:
            raise NeedsReauth(REASON_REFRESH_FAILED)
        lock_key = f"{REFRESH_LOCK_PREFIX}{connection.id}"
        acquired = await self.kv.set_if_absent(lock_key, "1", ttl=REFRESH_LOCK_TTL)
        if not acquired:
            token = await self._wait_for_refresh(connection)
            if token is not None:
                return token
            await self.mark_needs_reauth(connection, REASON_REFRESH_FAILED)
            raise NeedsReauth(REASON_REFRESH_FAILED)
        try:
            row = await self._read_token_row(connection.id)
            if row is None:
                await self.mark_needs_reauth(connection, REASON_NO_REFRESH)
                raise NeedsReauth(REASON_NO_REFRESH)
            refresh_token = self.cipher.try_decrypt(row.refresh_token_enc)
            if not refresh_token:
                await self.mark_needs_reauth(connection, REASON_NO_REFRESH)
                raise NeedsReauth(REASON_NO_REFRESH)
            try:
                tokens = await self.refresh(entry, refresh_token)
            except (UpstreamAuthFailed, ServerUnconfigured) as exc:
                self.metrics.counter(
                    "hub_token_refresh_total",
                    "Обновления токенов целевых систем.",
                    {"alias": connection.alias, "result": "failed"},
                )
                logger.info(
                    "connection_refresh_failed",
                    extra={"alias": connection.alias, "reason": str(exc)},
                )
                await self.mark_needs_reauth(connection, REASON_REFRESH_FAILED)
                raise NeedsReauth(REASON_REFRESH_FAILED) from exc
            await self.save_tokens(connection, tokens, keep_refresh=refresh_token)
            await self._mark_refreshed(connection)
            self.metrics.counter(
                "hub_token_refresh_total",
                "Обновления токенов целевых систем.",
                {"alias": connection.alias, "result": "ok"},
            )
            await self.db.audit(
                "connection_refreshed",
                user_id=connection.user_id,
                alias=connection.alias,
                details={"rotated": bool(tokens.refresh_token)},
                ts=self.clock.now(),
            )
            return tokens.access_token
        finally:
            await self.kv.delete(lock_key)

    async def _wait_for_refresh(self, connection: Connection) -> str | None:
        """Дождаться чужого обновления (до 5 с) и вернуть обновлённый токен (R-B4)."""
        deadline = REFRESH_WAIT_SECONDS
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(REFRESH_WAIT_STEP)
            waited += REFRESH_WAIT_STEP
            row = await self._read_token_row(connection.id)
            if row is None:
                return None
            if self._is_valid(row.expires_at):
                token = self.cipher.try_decrypt(row.access_token_enc)
                if token:
                    return token
            if not await self.kv.get(f"{REFRESH_LOCK_PREFIX}{connection.id}"):
                row = await self._read_token_row(connection.id)
                if row is not None and self._is_valid(row.expires_at):
                    return self.cipher.try_decrypt(row.access_token_enc)
                return None
        return None

    async def _mark_refreshed(self, connection: Connection) -> None:
        await self.db.init()
        now = to_naive_utc(self.clock.now())
        async with self.db.session() as session, session.begin():
            conn = await session.get(Connection, connection.id)
            if conn is None:  # pragma: no cover
                return
            conn.last_refresh_at = now
            if conn.status == STATUS_NEEDS_REAUTH:
                conn.status = STATUS_CONNECTED
                conn.needs_reauth_reason = None
            conn.revision = int(conn.revision or 0) + 1
        await self.invalidate_cache(connection.user_id, connection.alias)

    # --- фоновое обновление (R-B4) ------------------------------------------

    async def due_connections(self) -> list[Connection]:
        """Подключения, у которых токен истекает раньше ``HUB_TOKEN_REFRESH_LEAD`` (R-B4)."""
        await self.db.init()
        threshold = to_naive_utc(self.clock.now()) + timedelta(
            seconds=self.settings.token_refresh_lead
        )
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(Connection, UpstreamToken)
                    .join(UpstreamToken, UpstreamToken.connection_id == Connection.id)
                    .where(
                        Connection.status == STATUS_CONNECTED,
                        UpstreamToken.refresh_token_enc.is_not(None),
                        UpstreamToken.expires_at.is_not(None),
                        UpstreamToken.expires_at <= threshold,
                    )
                )
            ).all()
        return [row[0] for row in rows]

    async def refresh_due(self) -> int:
        """Обновить все «созревшие» подключения; вернуть число успешных обновлений."""
        refreshed = 0
        for connection in await self.due_connections():
            try:
                await self.refresh_connection(connection)
                refreshed += 1
            except (NeedsReauth, BrokerError) as exc:
                logger.info(
                    "background_refresh_failed",
                    extra={"alias": connection.alias, "reason": str(exc)},
                )
        return refreshed


class TokenRefresher:
    """Фоновая задача обновления токенов (``HUB_TOKEN_REFRESH_ENABLED``, R-B4)."""

    def __init__(
        self, *, settings: Settings, broker: TokenBroker, db: Database, clock: Clock
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.db = db
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def run_once(self) -> int:
        return await self.broker.refresh_due()

    async def _loop(self) -> None:  # pragma: no cover - проверяется через run_once
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.token_refresh_interval
                )
                return
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception:  # фоновая задача не должна падать
                logger.exception("token_refresh_loop_failed")

    async def start(self) -> None:
        if not self.settings.token_refresh_enabled or self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # остановка приложения не должна падать из-за фоновой задачи
            logger.debug("token_refresh_stop_failed", exc_info=True)


def token_valid(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """R-B4: токен пригоден, если ``expires_at`` пуст или ещё не наступил."""
    if expires_at is None:
        return True
    return (expires_at - (now or utcnow())).total_seconds() > 0


__all__ = [
    "CONN_CACHE_PREFIX",
    "REASON_NO_REFRESH",
    "REASON_REFRESH_FAILED",
    "REASON_SCOPE_UPGRADE",
    "REASON_TOKEN_REJECTED",
    "REFRESH_LOCK_PREFIX",
    "STATUS_CONNECTED",
    "STATUS_NEEDS_REAUTH",
    "STATUS_NOT_CONNECTED",
    "BrokerError",
    "NeedsReauth",
    "ServerUnconfigured",
    "TokenBroker",
    "TokenRefresher",
    "UpstreamAuthFailed",
    "UpstreamTokens",
    "UpstreamUnavailable",
    "UserTokenRejected",
    "token_valid",
]
