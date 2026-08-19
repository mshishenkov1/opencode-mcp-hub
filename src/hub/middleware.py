"""ASGI-middleware: X-Request-ID, X-Content-Type-Options, метрики и JSON-лог запроса (R-A7, R-S4)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hub.logging_ import request_id_var
from hub.metrics import Metrics

logger = logging.getLogger("hub.http")

REQUEST_ID_HEADER = b"x-request-id"
REQUEST_ID_MAX_LEN = 128


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers") or []:
        if name.lower() == REQUEST_ID_HEADER:
            candidate = value.decode("latin-1").strip()
            if 0 < len(candidate) <= REQUEST_ID_MAX_LEN and candidate.isprintable():
                return candidate
            break
    return str(uuid.uuid4())


class RequestContextMiddleware:
    """Единый middleware вместо цепочки BaseHTTPMiddleware: заголовки, метрики, лог."""

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers") or [])
                headers = [
                    (k, v)
                    for k, v in headers
                    if k.lower() not in (REQUEST_ID_HEADER, b"x-content-type-options")
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                headers.append((b"x-content-type-options", b"nosniff"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            route = scope.get("route")
            path_template = getattr(route, "path", None) or scope.get("path", "")
            if route is None:
                path_template = "<unmatched>"
            method = scope.get("method", "")
            status = int(status_holder["status"])
            self.metrics.observe_request(method, path_template, status, duration)
            logger.info(
                "http_request method=%s path=%s status=%s request_id=%s",
                method,
                scope.get("path", ""),
                status,
                request_id,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": scope.get("path", ""),
                    "route": path_template,
                    "status": status,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
            request_id_var.reset(token)


__all__ = ["RequestContextMiddleware"]
