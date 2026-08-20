"""Мок upstream MCP-сервера для нагрузочных прогонов (D6-08, D6-10).

Отвечает как обычный MCP-сервер поверх streamable HTTP: ``initialize`` выдаёт
``Mcp-Session-Id``, ``tools/list`` — список инструментов, ``tools/call`` — результат.
Дополнительно реализован токен-эндпоинт целевой системы, чтобы брокер Hub мог
«обновлять» upstream-токены, не обращаясь к настоящему GitLab/Jira.

Ни один адрес, кроме localhost, в моке не используется: нагрузочный контур
физически не может попасть в боевые системы (D6-10).

Настройки (переменные окружения):
    MOCK_LATENCY_MS        базовая задержка ответа, мс (по умолчанию 5)
    MOCK_LATENCY_JITTER_MS случайная добавка к задержке, мс (по умолчанию 3)
    MOCK_TOOLS             сколько инструментов отдаёт tools/list (по умолчанию 30)
    MOCK_FAIL_RATE         доля ответов 500 (0…1, по умолчанию 0)
    MOCK_SESSION_TTL       сколько секунд помнить upstream-сессию (по умолчанию 3600)
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

LATENCY = float(os.environ.get("MOCK_LATENCY_MS", "5")) / 1000.0
JITTER = float(os.environ.get("MOCK_LATENCY_JITTER_MS", "3")) / 1000.0
TOOLS_COUNT = int(os.environ.get("MOCK_TOOLS", "30"))
FAIL_RATE = float(os.environ.get("MOCK_FAIL_RATE", "0"))
SESSION_TTL = float(os.environ.get("MOCK_SESSION_TTL", "3600"))
SESSION_HEADER = "Mcp-Session-Id"
CREDENTIAL_HEADERS = (
    "authorization",
    "x-atlassian-jira-personal-token",
    "x-atlassian-confluence-personal-token",
)
PROTOCOL_VERSION = "2025-06-18"

app = FastAPI(title="mock-upstream", docs_url=None, redoc_url=None, openapi_url=None)

_sessions: dict[str, float] = {}
_stats: dict[str, int] = {
    "initialize": 0,
    "tools/list": 0,
    "tools/call": 0,
    "notifications": 0,
    "token": 0,
    "delete": 0,
    "failed": 0,
    "unauthorized": 0,
}

# Имена инструментов повторяют группы каталога (core/code_review/devops/...),
# чтобы фильтрация инструментов в Hub работала на реалистичных данных.
_TOOL_PREFIXES = ("core", "code_review", "devops", "users", "repo_write", "issue_management")


def _tools() -> list[dict[str, Any]]:
    tools = []
    for i in range(TOOLS_COUNT):
        prefix = _TOOL_PREFIXES[i % len(_TOOL_PREFIXES)]
        tools.append(
            {
                "name": f"{prefix}_tool_{i:02d}",
                "description": f"Мок-инструмент {i} группы {prefix}",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": [],
                },
            }
        )
    return tools


async def _delay() -> None:
    if LATENCY or JITTER:
        await asyncio.sleep(LATENCY + (random.random() * JITTER if JITTER else 0.0))


def _drop_expired(now: float) -> None:
    if len(_sessions) < 10_000:
        return
    for key, created in list(_sessions.items()):
        if now - created > SESSION_TTL:
            _sessions.pop(key, None)


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "sessions": len(_sessions)})


@app.get("/stats")
async def stats() -> JSONResponse:
    return JSONResponse({**_stats, "sessions": len(_sessions)})


@app.post("/oauth/token")
async def oauth_token() -> JSONResponse:
    """Токен-эндпоинт «целевой системы»: и code, и refresh отдают новую пару."""
    _stats["token"] += 1
    await _delay()
    return JSONResponse(
        {
            "access_token": "mock-access-" + uuid.uuid4().hex,
            "refresh_token": "mock-refresh-" + uuid.uuid4().hex,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "read_api read_user",
        }
    )


@app.post("/oauth/revoke")
async def oauth_revoke() -> Response:
    return Response(status_code=200)


@app.delete("/mcp")
async def mcp_delete(request: Request) -> Response:
    _stats["delete"] += 1
    _sessions.pop(request.headers.get(SESSION_HEADER, ""), None)
    return Response(status_code=204)


@app.get("/mcp")
async def mcp_get(request: Request) -> Response:
    """Открытие SSE-канала: один комментарий-keepalive и закрытие."""
    session_id = request.headers.get(SESSION_HEADER, "")
    if session_id and session_id not in _sessions:
        return Response(status_code=404)
    await _delay()
    return Response(content=": keepalive\n\n", media_type="text/event-stream")


@app.post("/mcp")
async def mcp_post(request: Request) -> Response:
    body = await request.json()
    # Креды приходят так, как их подставляет каталог: Authorization для профиля
    # GitLab, X-Atlassian-*-Personal-Token для профиля Atlassian.
    if not any(request.headers.get(name) for name in CREDENTIAL_HEADERS):
        _stats["unauthorized"] += 1
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if FAIL_RATE and random.random() < FAIL_RATE:
        _stats["failed"] += 1
        return JSONResponse({"error": "upstream_failure"}, status_code=500)

    method = body.get("method") if isinstance(body, dict) else None
    request_id = body.get("id") if isinstance(body, dict) else None
    session_id = request.headers.get(SESSION_HEADER, "")
    now = time.monotonic()

    if method == "initialize":
        _stats["initialize"] += 1
        await _delay()
        _drop_expired(now)
        new_session = uuid.uuid4().hex
        _sessions[new_session] = now
        payload = _rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mock-upstream", "version": "1.0"},
            },
        )
        return JSONResponse(payload, headers={SESSION_HEADER: new_session})

    if isinstance(method, str) and method.startswith("notifications/"):
        _stats["notifications"] += 1
        return Response(status_code=202)

    # Все остальные методы требуют живой сессии — как настоящий MCP-сервер.
    if session_id and session_id not in _sessions:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "no session"}},
            status_code=404,
        )

    await _delay()
    if method == "tools/list":
        _stats["tools/list"] += 1
        return JSONResponse(_rpc_result(request_id, {"tools": _tools()}))

    if method == "tools/call":
        _stats["tools/call"] += 1
        params = body.get("params") or {}
        name = params.get("name", "unknown")
        return JSONResponse(
            _rpc_result(
                request_id,
                {"content": [{"type": "text", "text": f"мок-ответ {name}"}], "isError": False},
            )
        )

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    )
