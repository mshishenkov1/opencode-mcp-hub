"""Вход в веб-интерфейс Hub через OIDC (Keycloak): метаданные, обмен кода, проверка id_token (R-W1).

HTTP-клиент инжектируется (``app.state.oidc_client``) — тесты подменяют его respx/MockTransport.
Метаданные и JWKS кэшируются в KV на ``KEYCLOAK_JWKS_TTL`` (ключи ``oidc:meta:*``, ``oidc:jwks:*``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet

from hub.clock import Clock
from hub.crypto import code_challenge_s256
from hub.kv import KeyValueStore
from hub.settings import Settings

logger = logging.getLogger("hub.oidc")

META_PREFIX = "oidc:meta:"
JWKS_PREFIX = "oidc:jwks:"
ALLOWED_ALGORITHMS = ["RS256", "RS512", "ES256", "PS256"]


class OIDCError(Exception):
    """Ошибка провайдера OIDC: метаданные, обмен кода, проверка id_token."""


class OIDCClient:
    def __init__(
        self,
        *,
        settings: Settings,
        http: Callable[[], httpx.AsyncClient],
        kv: KeyValueStore,
        clock: Clock,
    ) -> None:
        self.settings = settings
        self._http = http
        self.kv = kv
        self.clock = clock

    @property
    def issuer(self) -> str:
        return self.settings.keycloak_issuer.rstrip("/")

    async def metadata(self) -> dict[str, Any]:
        """``/.well-known/openid-configuration`` издателя (с кэшем в KV)."""
        cached = await self.kv.get(META_PREFIX + self.issuer)
        if isinstance(cached, dict):
            return cached
        url = f"{self.issuer}/.well-known/openid-configuration"
        data = await self._get_json(url)
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(data.get(field), str) or not data[field]:
                raise OIDCError(f"метаданные OIDC не содержат {field}")
        await self.kv.set(META_PREFIX + self.issuer, data, ttl=self.settings.keycloak_jwks_ttl)
        return data

    async def jwks(self) -> dict[str, Any]:
        cached = await self.kv.get(JWKS_PREFIX + self.issuer)
        if isinstance(cached, dict):
            return cached
        meta = await self.metadata()
        data = await self._get_json(str(meta["jwks_uri"]))
        await self.kv.set(JWKS_PREFIX + self.issuer, data, ttl=self.settings.keycloak_jwks_ttl)
        return data

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http().get(url, timeout=self.settings.litellm_timeout)
        except httpx.HTTPError as exc:
            raise OIDCError(f"провайдер OIDC недоступен: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise OIDCError(f"провайдер OIDC ответил {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OIDCError("провайдер OIDC вернул не JSON") from exc
        if not isinstance(data, dict):
            raise OIDCError("провайдер OIDC вернул неожиданный ответ")
        return data

    async def authorize_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_verifier: str
    ) -> str:
        meta = await self.metadata()
        params = {
            "client_id": self.settings.keycloak_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.settings.keycloak_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge_s256(code_verifier),
            "code_challenge_method": "S256",
        }
        endpoint = str(meta["authorization_endpoint"])
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        meta = await self.metadata()
        secret = self.settings.keycloak_client_secret
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.settings.keycloak_client_id,
            "client_secret": secret.get_secret_value() if secret else "",
            "code_verifier": code_verifier,
        }
        try:
            response = await self._http().post(
                str(meta["token_endpoint"]), data=payload, timeout=self.settings.litellm_timeout
            )
        except httpx.HTTPError as exc:
            raise OIDCError(f"провайдер OIDC недоступен: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise OIDCError(f"обмен кода на токены завершился ошибкой {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OIDCError("провайдер OIDC вернул не JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("id_token"), str):
            raise OIDCError("ответ провайдера OIDC не содержит id_token")
        return data

    async def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """Проверить подпись по JWKS издателя и claims ``iss``/``aud``/``exp``/``nonce`` (R-W1)."""
        jwks = await self.jwks()
        try:
            key_set = KeySet.import_key_set(cast(Any, jwks))
            decoded = jose_jwt.decode(id_token, key_set, algorithms=ALLOWED_ALGORITHMS)
        except Exception as exc:  # подпись, формат, неизвестный ключ
            raise OIDCError("подпись id_token не прошла проверку") from exc
        claims = dict(decoded.claims)
        issuer = str(claims.get("iss", "")).rstrip("/")
        if issuer != self.issuer:
            raise OIDCError("id_token выдан другим издателем")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.settings.keycloak_client_id not in [str(a) for a in audiences]:
            raise OIDCError("id_token выдан другому клиенту")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or float(exp) <= self.clock.time():
            raise OIDCError("срок действия id_token истёк")
        if str(claims.get("nonce", "")) != nonce:
            raise OIDCError("nonce id_token не совпадает с сохранённым")
        return claims


def user_id_from_claims(claims: dict[str, Any]) -> str | None:
    """``preferred_username`` → ``email`` → ``sub`` (первый непустой, решение 57)."""
    for field in ("preferred_username", "email", "sub"):
        value = claims.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


__all__ = ["JWKS_PREFIX", "META_PREFIX", "OIDCClient", "OIDCError", "user_id_from_claims"]
