"""Клиент LiteLLM: CLI-SSO (``/sso/cli/start``, ``/sso/cli/poll``) и ``/key/generate``.

HTTP-клиент (``httpx.AsyncClient``) инжектируется — тесты подменяют его через ``respx``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

# R-L11.4: коды причин неудавшегося отзыва ключа (закрытый набор, он же уходит наружу).
REVOKE_NOT_PERMITTED = "not_permitted"
REVOKE_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
REVOKE_INVALID_RESPONSE = "invalid_response"
# R-L12.3: за один вход отзывается не более 20 алиасов одним запросом.
REVOKE_ALIAS_LIMIT = 20


class LiteLLMUnavailable(Exception):
    """Сеть/тайм-аут/5xx/невалидный ответ там, где он обязателен."""


@dataclass
class LiteLLMResponse:
    status_code: int
    body: Any  # разобранный JSON либо None, если тело не JSON

    @property
    def is_json_object(self) -> bool:
        return isinstance(self.body, dict)


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return None


class LiteLLMClient:
    """Тонкая обёртка над httpx. ``http`` — фабрика/геттер клиента, чтобы его можно было подменить
    после создания приложения (``app.state.litellm_client``)."""

    def __init__(self, base_url: str, http: Callable[[], httpx.AsyncClient], timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http
        self.timeout = timeout

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._http()
        url = f"{self.base_url}{path}"
        try:
            return await client.request(method, url, timeout=self.timeout, **kwargs)
        except httpx.HTTPError as exc:  # сеть, тайм-аут, протокол
            raise LiteLLMUnavailable(f"LiteLLM недоступен: {type(exc).__name__}") from exc

    async def cli_start(self) -> dict[str, Any]:
        """``POST /sso/cli/start`` → ``{login_id, poll_secret, user_code, expires_in?}``."""
        resp = await self._request("POST", "/sso/cli/start")
        if resp.status_code >= 500 or resp.status_code < 200 or resp.status_code >= 300:
            raise LiteLLMUnavailable(f"LiteLLM /sso/cli/start ответил {resp.status_code}")
        body = _parse_json(resp)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("login_id"), str)
            or not body.get("login_id")
            or not isinstance(body.get("poll_secret"), str)
            or not body.get("poll_secret")
        ):
            raise LiteLLMUnavailable("LiteLLM /sso/cli/start вернул невалидный ответ")
        return body

    async def cli_poll(self, login_id: str, poll_secret: str, team_id: str | None = None) -> LiteLLMResponse:
        """``GET /sso/cli/poll/{login_id}`` с заголовком ``x-litellm-cli-poll-secret``.

        5xx и сетевые ошибки → ``LiteLLMUnavailable``; остальное возвращается как есть.
        """
        params = {"team_id": team_id} if team_id else None
        resp = await self._request(
            "GET",
            f"/sso/cli/poll/{login_id}",
            headers={"x-litellm-cli-poll-secret": poll_secret},
            params=params,
        )
        if resp.status_code >= 500:
            raise LiteLLMUnavailable(f"LiteLLM /sso/cli/poll ответил {resp.status_code}")
        return LiteLLMResponse(resp.status_code, _parse_json(resp))

    async def key_generate(
        self,
        jwt: str,
        *,
        key_alias: str,
        metadata: dict[str, Any],
        team_id: str | None = None,
    ) -> LiteLLMResponse:
        """``POST /key/generate`` с ``Authorization: Bearer <JWT>``. 5xx/сеть → ``LiteLLMUnavailable``."""
        payload: dict[str, Any] = {"key_alias": key_alias, "metadata": metadata}
        if team_id:
            payload["team_id"] = team_id
        resp = await self._request(
            "POST",
            "/key/generate",
            headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 500:
            raise LiteLLMUnavailable(f"LiteLLM /key/generate ответил {resp.status_code}")
        return LiteLLMResponse(resp.status_code, _parse_json(resp))

    async def key_delete(
        self,
        credential: str,
        *,
        keys: list[str] | None = None,
        key_aliases: list[str] | None = None,
    ) -> LiteLLMResponse:
        """``POST /key/delete`` — отзыв ключей по значению (R-L11.4) или по алиасу (R-L12.3).

        В отличие от прочих вызовов, 5xx **не** превращается в исключение: исход отзыва
        классифицирует вызывающий по закрытой таблице (``revoke_error_for``). Сеть и таймаут
        отличить от ответа нельзя, поэтому они по-прежнему поднимают ``LiteLLMUnavailable``.
        """
        payload: dict[str, Any] = {}
        if keys is not None:
            payload["keys"] = list(keys)
        if key_aliases is not None:
            payload["key_aliases"] = list(key_aliases)
        resp = await self._request(
            "POST",
            "/key/delete",
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
            json=payload,
        )
        return LiteLLMResponse(resp.status_code, _parse_json(resp))


def revoke_error_for(status: int | None, body: Any) -> str | None:
    """Закрытая таблица исходов отзыва ключа (R-L11.4); ``None`` — ключ отозван.

    ``status is None`` — сеть или таймаут. 404 считается успехом: отзывать нечего.
    """
    if status is None:
        return REVOKE_UPSTREAM_UNAVAILABLE
    if status == 404:
        return None
    if 200 <= status < 300:
        return None if body is not None else REVOKE_INVALID_RESPONSE
    if status in (401, 403):
        return REVOKE_NOT_PERMITTED
    if status == 429 or status >= 500:
        return REVOKE_UPSTREAM_UNAVAILABLE
    return REVOKE_INVALID_RESPONSE


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Прочитать claims JWT без проверки подписи. Ошибка → ``{}``."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, UnicodeDecodeError, TypeError):
        return {}


__all__ = [
    "REVOKE_ALIAS_LIMIT",
    "REVOKE_INVALID_RESPONSE",
    "REVOKE_NOT_PERMITTED",
    "REVOKE_UPSTREAM_UNAVAILABLE",
    "LiteLLMClient",
    "LiteLLMResponse",
    "LiteLLMUnavailable",
    "decode_jwt_claims",
    "revoke_error_for",
]
