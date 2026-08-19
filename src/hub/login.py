"""Вход через LiteLLM CLI-SSO и постоянный ключ (R-L1..R-L10)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hub.clock import Clock
from hub.db import ApiKey, AuditLog, Database, User, to_naive_utc
from hub.errors import HubError
from hub.kv import KeyValueStore
from hub.litellm import LiteLLMClient, LiteLLMResponse, LiteLLMUnavailable, decode_jwt_claims

logger = logging.getLogger("hub.login")

SESSION_PREFIX = "login:"
POLL_THROTTLE_SECONDS = 2.0
STATE_PENDING = "pending"
STATE_TEAM_SELECTION = "team_selection"
STATE_KEY_PENDING = "key_pending"

MSG_LOGIN_EXPIRED = "Сессия входа не найдена или истекла"
MSG_LITELLM_UNAVAILABLE = "LiteLLM недоступен, повторите попытку позже"
MSG_LITELLM_INVALID = "LiteLLM вернул неожиданный ответ"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


@dataclass
class PollResult:
    status_code: int
    body: dict[str, Any]


def _err(status_code: int, error: str, message: str | None = None) -> HubError:
    return HubError(status_code, error, message)


def _unavailable() -> HubError:
    return _err(502, "litellm_unavailable", MSG_LITELLM_UNAVAILABLE)


def _invalid_response() -> HubError:
    return _err(502, "litellm_invalid_response", MSG_LITELLM_INVALID)


def _login_expired() -> HubError:
    return _err(404, "login_expired", MSG_LOGIN_EXPIRED)


def _extract_teams(body: dict[str, Any]) -> list[dict[str, str]]:
    details = body.get("team_details")
    teams: list[dict[str, str]] = []
    if isinstance(details, list) and details:
        for item in details:
            if isinstance(item, dict) and item.get("team_id"):
                tid = str(item["team_id"])
                alias = item.get("team_alias")
                teams.append({"team_id": tid, "team_alias": str(alias) if alias else tid})
        return teams
    raw = body.get("teams")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item:
                teams.append({"team_id": item, "team_alias": item})
            elif isinstance(item, dict) and item.get("team_id"):
                tid = str(item["team_id"])
                alias = item.get("team_alias")
                teams.append({"team_id": tid, "team_alias": str(alias) if alias else tid})
    return teams


class LoginService:
    """Состояние сессий входа хранится в KeyValueStore под ``login:<login_id>``."""

    def __init__(
        self,
        *,
        kv: KeyValueStore,
        db: Database,
        clock: Clock,
        litellm: LiteLLMClient,
        session_ttl: int,
        key_alias_prefix: str,
    ) -> None:
        self.kv = kv
        self.db = db
        self.clock = clock
        self.litellm = litellm
        self.session_ttl = session_ttl
        self.key_alias_prefix = key_alias_prefix

    # ------------------------------------------------------------------ helpers

    async def _load(self, login_id: str) -> dict[str, Any] | None:
        data = await self.kv.get(SESSION_PREFIX + login_id)
        if not isinstance(data, dict):
            return None
        if float(data.get("expires_at", 0)) <= self.clock.time():
            await self.kv.delete(SESSION_PREFIX + login_id)
            return None
        return data

    async def _save(self, login_id: str, session: dict[str, Any]) -> None:
        ttl = float(session["expires_at"]) - self.clock.time()
        if ttl <= 0:
            await self.kv.delete(SESSION_PREFIX + login_id)
            return
        await self.kv.set(SESSION_PREFIX + login_id, session, ttl=ttl)

    async def _drop(self, login_id: str) -> None:
        await self.kv.delete(SESSION_PREFIX + login_id)

    async def active_sessions(self) -> int:
        return await self.kv.count_prefix(SESSION_PREFIX)

    async def _authorize(self, login_id: str, poll_secret: str | None) -> dict[str, Any]:
        session = await self._load(login_id)
        if session is None:
            raise _login_expired()
        expected = str(session.get("poll_secret", ""))
        if not poll_secret or not hmac.compare_digest(
            expected.encode("utf-8"), poll_secret.encode("utf-8", errors="replace")
        ):
            raise _err(403, "forbidden")
        return session

    # ------------------------------------------------------------------ R-L1

    async def start(self, client: str | None) -> dict[str, Any]:
        try:
            upstream = await self.litellm.cli_start()
        except LiteLLMUnavailable as exc:
            logger.warning("litellm_cli_start_failed", extra={"reason": str(exc)})
            raise _unavailable() from exc

        ttl = self.session_ttl
        expires_in = upstream.get("expires_in")
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool) and expires_in > 0:
            ttl = min(ttl, int(expires_in))

        login_id = str(uuid.uuid4())
        poll_secret = secrets.token_urlsafe(32)
        now = self.clock.time()
        session = {
            "poll_secret": poll_secret,
            "litellm_login_id": upstream["login_id"],
            "litellm_poll_secret": upstream["poll_secret"],
            "client": client,
            "state": STATE_PENDING,
            "teams": [],
            "team_id": None,
            "jwt": None,
            "user_id": None,
            "email": None,
            "last_call_at": None,
            "last_response": None,
            "jwt_warned": False,
            "created_at": now,
            "expires_at": now + ttl,
        }
        await self._save(login_id, session)
        await self.db.audit(
            "login_started",
            details={"client": client, "login_id": login_id},
            ts=self.clock.now(),
        )
        logger.info("login_started", extra={"login_id": login_id, "client": client})
        return {
            "login_id": login_id,
            "poll_secret": poll_secret,
            "browser_url": (
                f"{self.litellm.base_url}/sso/key/generate?source=litellm-cli&key={upstream['login_id']}"
            ),
            "user_code": upstream.get("user_code"),
            "expires_in": ttl,
        }

    # ------------------------------------------------------------------ R-L2/R-L10

    async def poll(self, login_id: str, poll_secret: str | None) -> PollResult:
        session = await self._authorize(login_id, poll_secret)

        if session.get("state") == STATE_TEAM_SELECTION and not session.get("team_id"):
            return PollResult(200, {"status": "team_selection_required", "teams": session.get("teams") or []})

        now = self.clock.time()
        last_call = session.get("last_call_at")
        cached = session.get("last_response")
        if last_call is not None and cached and (now - float(last_call)) < POLL_THROTTLE_SECONDS:
            return PollResult(int(cached["code"]), dict(cached["body"]))

        session["last_call_at"] = now
        try:
            if session.get("state") == STATE_KEY_PENDING:
                result = await self._issue_key(login_id, session)
            else:
                result = await self._sso_poll(login_id, session, depth=0)
        except HubError as exc:
            if exc.status_code == 404 and exc.error == "login_expired":
                raise
            body = exc.to_body(cli=True)
            if await self._load(login_id) is not None:
                session["last_response"] = {"code": exc.status_code, "body": body}
                await self._save(login_id, session)
            return PollResult(exc.status_code, body)

        if result.body.get("status") != "ready" and await self._load(login_id) is not None:
            session["last_response"] = {"code": result.status_code, "body": result.body}
            await self._save(login_id, session)
        return result

    async def _sso_poll(self, login_id: str, session: dict[str, Any], *, depth: int) -> PollResult:
        try:
            resp = await self.litellm.cli_poll(
                session["litellm_login_id"], session["litellm_poll_secret"], session.get("team_id")
            )
        except LiteLLMUnavailable as exc:
            logger.warning("litellm_poll_failed", extra={"login_id": login_id, "reason": str(exc)})
            raise _unavailable() from exc

        if 400 <= resp.status_code < 500:
            logger.info(
                "litellm_poll_rejected", extra={"login_id": login_id, "upstream_status": resp.status_code}
            )
            await self._drop(login_id)
            raise _login_expired()
        return await self._handle_poll_body(login_id, session, resp, depth=depth)

    async def _handle_poll_body(
        self, login_id: str, session: dict[str, Any], resp: LiteLLMResponse, *, depth: int
    ) -> PollResult:
        body = resp.body
        if not isinstance(body, dict) or not isinstance(body.get("status"), str):
            raise _invalid_response()
        status = body["status"]
        if status == "pending":
            session["state"] = STATE_PENDING
            return PollResult(200, {"status": "pending"})
        if status != "ready":
            raise _invalid_response()

        key = body.get("key")
        has_key = isinstance(key, str) and bool(key)
        if body.get("requires_team_selection") is True or not has_key:
            teams = _extract_teams(body)
            if not teams:
                raise _invalid_response()
            if len(teams) == 1:
                if depth >= 1:
                    raise _invalid_response()
                session["team_id"] = teams[0]["team_id"]
                session["teams"] = teams
                return await self._sso_poll(login_id, session, depth=depth + 1)
            session["state"] = STATE_TEAM_SELECTION
            session["teams"] = teams
            session["team_id"] = None
            await self._save(login_id, session)
            return PollResult(200, {"status": "team_selection_required", "teams": teams})

        assert isinstance(key, str)
        claims = decode_jwt_claims(key)
        user_id = body.get("user_id") or claims.get("user_id") or claims.get("sub")
        if not user_id or not isinstance(user_id, str):
            raise _invalid_response()
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            email = user_id if "@" in user_id else None
        team_id = body.get("team_id") or session.get("team_id") or None
        session.update(
            {
                "state": STATE_KEY_PENDING,
                "jwt": key,
                "user_id": user_id,
                "email": email,
                "team_id": str(team_id) if team_id else None,
            }
        )
        await self._save(login_id, session)
        return await self._issue_key(login_id, session)

    # ------------------------------------------------------------------ R-L4/R-L5

    async def _issue_key(self, login_id: str, session: dict[str, Any]) -> PollResult:
        jwt = session["jwt"]
        user_id = session["user_id"]
        now = self.clock.now()
        key_alias = f"{self.key_alias_prefix}-{user_id}-{now:%Y%m%d-%H%M}"
        metadata: dict[str, Any] = {"source": "opencode-mcp-hub"}
        if session.get("client"):
            metadata["client"] = session["client"]
        team_id = session.get("team_id")

        try:
            resp = await self.litellm.key_generate(
                jwt, key_alias=key_alias, metadata=metadata, team_id=team_id
            )
        except LiteLLMUnavailable as exc:
            logger.warning("litellm_key_generate_failed", extra={"login_id": login_id, "reason": str(exc)})
            raise _unavailable() from exc

        expires_in: int | None = None
        expires_at: datetime | None = None
        if 200 <= resp.status_code < 300:
            key = resp.body.get("key") if isinstance(resp.body, dict) else None
            if not isinstance(key, str) or not key:
                logger.warning("litellm_key_generate_no_key", extra={"login_id": login_id})
                raise _unavailable()
            key_kind = "persistent"
        elif 400 <= resp.status_code < 500:
            key = jwt
            key_kind = "jwt"
            claims = decode_jwt_claims(jwt)
            exp = claims.get("exp")
            if isinstance(exp, int | float) and not isinstance(exp, bool):
                expires_in = max(0, int(exp - self.clock.time()))
                expires_at = datetime.fromtimestamp(float(exp), tz=UTC)
            if not session.get("jwt_warned"):
                session["jwt_warned"] = True
                logger.warning(
                    "key_generate_fallback_jwt",
                    extra={"login_id": login_id, "upstream_status": resp.status_code, "user_id": user_id},
                )
        else:  # pragma: no cover - 3xx не ожидается
            raise _unavailable()

        await self._persist(
            key=key,
            key_kind=key_kind,
            key_alias=key_alias,
            user_id=user_id,
            email=session.get("email"),
            client=session.get("client"),
            team_id=team_id,
            expires_at=expires_at,
        )
        await self._drop(login_id)
        logger.info(
            "login_completed",
            extra={"login_id": login_id, "user_id": user_id, "key_kind": key_kind, "team_id": team_id},
        )
        body: dict[str, Any] = {
            "status": "ready",
            "key": key,
            "key_kind": key_kind,
            "user": {"user_id": user_id, "email": session.get("email")},
            "team_id": team_id,
        }
        if key_kind == "jwt":
            body["expires_in"] = expires_in
        return PollResult(200, body)

    async def _persist(
        self,
        *,
        key: str,
        key_kind: str,
        key_alias: str,
        user_id: str,
        email: str | None,
        client: str | None,
        team_id: str | None,
        expires_at: datetime | None,
    ) -> None:
        await self.db.init()
        now = to_naive_utc(self.clock.now())
        async with self.db.session() as session, session.begin():
            user = await session.get(User, user_id)
            if user is None:
                user = User(user_id=user_id, email=email, groups=["all"], created_at=now, updated_at=now)
                session.add(user)
            else:
                user.email = email
                if not user.groups:
                    user.groups = ["all"]
                user.updated_at = now
            session.add(
                ApiKey(
                    key_sha256=sha256_hex(key),
                    user_id=user_id,
                    key_kind=key_kind,
                    key_alias=key_alias,
                    client=client,
                    created_at=now,
                    expires_at=to_naive_utc(expires_at) if expires_at else None,
                )
            )
            session.add(
                AuditLog(
                    ts=now,
                    user_id=user_id,
                    action="login_completed",
                    alias=None,
                    details={
                        "key_kind": key_kind,
                        "key_alias": key_alias,
                        "team_id": team_id,
                        "client": client,
                    },
                )
            )

    # ------------------------------------------------------------------ R-L3

    async def choose_team(self, login_id: str, poll_secret: str | None, body: Any) -> dict[str, Any]:
        session = await self._authorize(login_id, poll_secret)
        if session.get("state") != STATE_TEAM_SELECTION or session.get("team_id"):
            raise _err(409, "team_selection_not_required", "Сессия не ожидает выбора команды")
        if not isinstance(body, dict) or not isinstance(body.get("team_id"), str) or not body["team_id"]:
            raise _err(400, "invalid_request", "Ожидается JSON-объект {team_id: string}")
        team_id = body["team_id"]
        allowed = {t["team_id"] for t in session.get("teams") or []}
        if team_id not in allowed:
            raise _err(400, "invalid_team", "Команда не входит в список доступных")
        session["team_id"] = team_id
        session["state"] = STATE_PENDING
        session["last_call_at"] = None
        session["last_response"] = None
        await self._save(login_id, session)
        return {"status": "pending"}


__all__ = ["SESSION_PREFIX", "LoginService", "PollResult", "sha256_hex"]
