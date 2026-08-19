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


__all__ = ["LiteLLMClient", "LiteLLMResponse", "LiteLLMUnavailable", "decode_jwt_claims"]
