"""``/cli/*``: вход через LiteLLM CLI-SSO (R-L1..R-L3, R-L8)."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hub.errors import HubError

router = APIRouter(prefix="/cli", tags=["cli"])

RATE_LIMIT_PREFIX = "rl:cli_start:"
RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW = 60.0
CLIENT_MAX_LEN = 128
POLL_SECRET_HEADER = "X-Hub-Poll-Secret"


async def _read_json_body(request: Request) -> Any:
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HubError(400, "invalid_request", "Тело запроса должно быть JSON") from exc


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


@router.post("/start")
async def cli_start(request: Request) -> JSONResponse:
    state = request.app.state
    ip = _client_ip(request)
    allowed, retry_after = await state.kv.rate_limit_hit(
        RATE_LIMIT_PREFIX + ip, state.clock.time(), RATE_LIMIT_WINDOW, RATE_LIMIT_MAX
    )
    if not allowed:
        raise HubError(
            429,
            "rate_limited",
            "Слишком много запросов на вход, повторите позже",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    body = await _read_json_body(request)
    if not isinstance(body, dict):
        raise HubError(400, "invalid_request", "Ожидается JSON-объект {client?: string}")
    unknown = set(body) - {"client"}
    if unknown:
        raise HubError(400, "invalid_request", "Неизвестные поля: " + ", ".join(sorted(map(str, unknown))))
    client = body.get("client")
    if client is not None and (not isinstance(client, str) or len(client) > CLIENT_MAX_LEN):
        raise HubError(400, "invalid_request", f"client: ожидается строка не длиннее {CLIENT_MAX_LEN}")

    result = await state.login.start(client)
    return JSONResponse(result)


@router.get("/poll/{login_id}")
async def cli_poll(login_id: str, request: Request) -> JSONResponse:
    state = request.app.state
    result = await state.login.poll(login_id, request.headers.get(POLL_SECRET_HEADER))
    return JSONResponse(result.body, status_code=result.status_code)


@router.post("/poll/{login_id}/team")
async def cli_choose_team(login_id: str, request: Request) -> JSONResponse:
    state = request.app.state
    try:
        body = await _read_json_body(request)
    except HubError:
        # Порядок проверок: 404/403 → 409 → 400 (см. R-L3); невалидный JSON проверяем внутри сервиса.
        body = None
    result = await state.login.choose_team(login_id, request.headers.get(POLL_SECRET_HEADER), body)
    return JSONResponse(result)
