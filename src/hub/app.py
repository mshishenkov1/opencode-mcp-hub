"""Фабрика приложения ``create_app`` (R-K4, R-S5)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from hub import __version__
from hub.catalog import Catalog, load_catalog
from hub.clock import Clock, SystemClock
from hub.db import Database, build_engine
from hub.errors import HubError
from hub.kv import KeyValueStore, create_kv_store
from hub.litellm import LiteLLMClient
from hub.logging_ import configure_logging
from hub.login import LoginService
from hub.metrics import Metrics
from hub.middleware import RequestContextMiddleware
from hub.routes import admin_router, api_router, cli_router, system_router
from hub.settings import Settings

logger = logging.getLogger("hub.app")

_HTTP_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}
_HTTP_STATUS_MESSAGES = {
    404: "Ресурс не найден",
    405: "Метод не поддерживается",
}


def _is_cli(request: Request) -> bool:
    return request.url.path.startswith("/cli/")


def _error_response(request: Request, exc: HubError) -> JSONResponse:
    return JSONResponse(exc.to_body(cli=_is_cli(request)), status_code=exc.status_code, headers=exc.headers)


async def _hub_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HubError)
    return _error_response(request, exc)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    detail = "; ".join(
        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}" for err in exc.errors()
    )
    return _error_response(request, HubError(400, "invalid_request", f"Некорректный запрос: {detail}"))


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_STATUS_CODES.get(exc.status_code, "error")
    message = _HTTP_STATUS_MESSAGES.get(exc.status_code)
    if message is None and isinstance(exc.detail, str) and exc.detail:
        message = exc.detail
    headers = dict(exc.headers or {})
    return _error_response(request, HubError(exc.status_code, code, message, headers=headers))


def create_app(
    settings: Settings | None = None,
    *,
    litellm_client: httpx.AsyncClient | None = None,
    kv: KeyValueStore | None = None,
    clock: Clock | None = None,
    catalog_env: Mapping[str, str] | None = None,
) -> FastAPI:
    """Создать приложение Hub.

    * ``settings=None`` — настройки читаются из окружения (``HUB_*``);
    * ``litellm_client`` — ``httpx.AsyncClient`` для LiteLLM (по умолчанию создаётся свой; тесты подменяют
      его здесь либо через ``app.state.litellm_client``);
    * ``kv`` — KeyValueStore (по умолчанию по ``HUB_REDIS_URL``: Redis или in-memory);
    * ``clock`` — источник времени (по умолчанию системные часы);
    * ``catalog_env`` — окружение для ``${VAR}``/``env:VAR`` каталога (по умолчанию ``os.environ``).

    Ошибки конфигурации/каталога поднимаются сразу (``ConfigError``/``CatalogError``).
    """
    if settings is None:
        settings = Settings()
    configure_logging(settings.log_level_int)
    app_clock: Clock = clock or SystemClock()

    catalog: Catalog = load_catalog(settings.catalog_path, catalog_env)

    db = Database(build_engine(settings.database_url))
    kv_store: KeyValueStore = kv or create_kv_store(settings.redis_url, app_clock)
    metrics = Metrics()

    owns_http_client = litellm_client is None
    http_client = litellm_client or httpx.AsyncClient(timeout=settings.litellm_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.db.init()
        logger.info(
            "hub_started",
            extra={"version": __version__, "catalog_version": app.state.catalog.version},
        )
        try:
            yield
        finally:
            await app.state.kv.close()
            await app.state.db.dispose()
            if app.state.owns_http_client:
                await app.state.litellm_client.aclose()

    app = FastAPI(
        title="OpenCode MCP Hub",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    litellm = LiteLLMClient(
        settings.litellm_base_url,
        http=lambda: app.state.litellm_client,
        timeout=settings.litellm_timeout,
    )
    login = LoginService(
        kv=kv_store,
        db=db,
        clock=app_clock,
        litellm=litellm,
        session_ttl=settings.login_session_ttl,
        key_alias_prefix=settings.key_alias_prefix,
    )

    async def _active_sessions() -> float:
        return float(await login.active_sessions())

    metrics.register_gauge(
        "hub_login_sessions_active", "Число живых сессий входа через CLI-SSO.", _active_sessions
    )

    app.state.settings = settings
    app.state.clock = app_clock
    app.state.catalog = catalog
    app.state.catalog_env = catalog_env
    app.state.db = db
    app.state.kv = kv_store
    app.state.metrics = metrics
    app.state.litellm_client = http_client
    app.state.owns_http_client = owns_http_client
    app.state.litellm = litellm
    app.state.login = login

    app.add_exception_handler(HubError, _hub_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)

    app.include_router(system_router)
    app.include_router(cli_router)
    app.include_router(api_router)
    app.include_router(admin_router)

    app.add_middleware(RequestContextMiddleware, metrics=metrics)
    return app


__all__ = ["create_app"]
