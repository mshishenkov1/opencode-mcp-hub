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
from hub.broker import TokenBroker, TokenRefresher
from hub.catalog import Catalog, load_catalog
from hub.clock import Clock, SystemClock
from hub.crypto import TokenCipher
from hub.db import Database, build_engine
from hub.errors import HubError
from hub.kv import KeyValueStore, create_kv_store
from hub.litellm import LiteLLMClient
from hub.logging_ import configure_logging
from hub.login import LoginService
from hub.metrics import Metrics
from hub.middleware import RequestContextMiddleware
from hub.oauth import OAuthServer
from hub.oidc import OIDCClient
from hub.proxy import CircuitBreaker, SessionStore, SseCounter, UpstreamClient
from hub.routes import (
    admin_router,
    api_router,
    cli_router,
    mcp_router,
    oauth_router,
    system_router,
    web_router,
)
from hub.settings import Settings
from hub.templating import Templates
from hub.websession import WebSessionService

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


async def _internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: необработанное исключение → 500 ``internal_error`` в едином формате (R-A7).

    Обработчик выполняется в ``ServerErrorMiddleware`` снаружи ``RequestContextMiddleware``, поэтому
    ``X-Request-ID`` и ``nosniff`` проставляются здесь (R-S4). Детали исключения в ответ не попадают —
    только в JSON-лог.
    """
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "unhandled_exception",
        exc_info=exc,
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "exc_type": type(exc).__name__,
        },
    )
    headers = {"X-Content-Type-Options": "nosniff"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return _error_response(
        request, HubError(500, "internal_error", "Внутренняя ошибка сервера", headers=headers)
    )


def create_app(
    settings: Settings | None = None,
    *,
    litellm_client: httpx.AsyncClient | None = None,
    http_client: httpx.AsyncClient | None = None,
    upstream_client: httpx.AsyncClient | None = None,
    oauth_client: httpx.AsyncClient | None = None,
    oidc_client: httpx.AsyncClient | None = None,
    kv: KeyValueStore | None = None,
    clock: Clock | None = None,
    catalog_env: Mapping[str, str] | None = None,
) -> FastAPI:
    """Создать приложение Hub.

    * ``settings=None`` — настройки читаются из окружения (``HUB_*``);
    * ``litellm_client`` — ``httpx.AsyncClient`` для LiteLLM (по умолчанию создаётся свой; тесты подменяют
      его здесь либо через ``app.state.litellm_client``);
    * ``http_client`` — общий клиент для остальных внешних вызовов (upstream MCP, AS целевых систем,
      OIDC); ``upstream_client`` / ``oauth_client`` / ``oidc_client`` подменяют его точечно. Все они
      доступны как ``app.state.upstream_client`` / ``oauth_client`` / ``oidc_client``;
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

    db = Database(build_engine(settings.database_url), auto_migrate=settings.db_auto_migrate)
    kv_store: KeyValueStore = kv or create_kv_store(settings.redis_url, app_clock)
    if kv is None and not settings.redis_url:
        # Общий KV — предпосылка работы нескольких реплик без sticky-сессий (R-P4, R-P10).
        logger.warning(
            "kv_in_memory",
            extra={
                "detail": (
                    "HUB_REDIS_URL не задан: KeyValueStore хранится в памяти процесса — реплики "
                    "Hub не делят denylist отозванных токенов, MCP-сессии, окна rate-limit и "
                    "состояние circuit-breaker. Для запуска в нескольких репликах задайте "
                    "HUB_REDIS_URL."
                )
            },
        )
    metrics = Metrics()

    owns_http_client = litellm_client is None
    litellm_http = litellm_client or httpx.AsyncClient(timeout=settings.litellm_timeout)

    owns_outbound_client = http_client is None
    outbound = http_client or httpx.AsyncClient(timeout=settings.upstream_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.db.init()
        await app.state.token_refresher.start()
        logger.info(
            "hub_started",
            extra={"version": __version__, "catalog_version": app.state.catalog.version},
        )
        try:
            yield
        finally:
            await app.state.token_refresher.stop()
            await app.state.kv.close()
            await app.state.db.dispose()
            if app.state.owns_http_client:
                await app.state.litellm_client.aclose()
            if app.state.owns_outbound_client:
                await app.state.outbound_client.aclose()

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
    app.state.litellm_client = litellm_http
    app.state.owns_http_client = owns_http_client
    app.state.litellm = litellm
    app.state.login = login

    # --- I-3: внешние HTTP-клиенты (подменяются тестами) ---
    app.state.outbound_client = outbound
    app.state.owns_outbound_client = owns_outbound_client
    app.state.upstream_client = upstream_client or outbound
    app.state.oauth_client = oauth_client or outbound
    app.state.oidc_client = oidc_client or outbound

    # --- I-3: сервисы ---
    app.state.templates = Templates()
    app.state.cipher = TokenCipher(settings.encryption_key.get_secret_value())
    app.state.web_sessions = WebSessionService(
        db=db,
        clock=app_clock,
        ttl=settings.web_session_ttl,
        secret_key=settings.secret_key.get_secret_value(),
        secure=settings.public_url.lower().startswith("https"),
    )
    app.state.oidc = OIDCClient(
        settings=settings,
        http=lambda: app.state.oidc_client,
        kv=kv_store,
        clock=app_clock,
    )
    app.state.broker = TokenBroker(
        settings=settings,
        db=db,
        kv=kv_store,
        clock=app_clock,
        cipher=app.state.cipher,
        metrics=metrics,
        http=lambda: app.state.oauth_client,
        catalog=lambda: app.state.catalog,
        catalog_env=lambda: app.state.catalog_env,
    )
    app.state.oauth = OAuthServer(
        settings=settings, db=db, kv=kv_store, clock=app_clock, metrics=metrics
    )
    app.state.token_refresher = TokenRefresher(
        settings=settings, broker=app.state.broker, db=db, clock=app_clock
    )
    app.state.sessions = SessionStore(kv_store, app_clock, settings)
    app.state.breaker = CircuitBreaker(kv_store, app_clock, settings)
    app.state.sse_counter = SseCounter(kv_store, settings)
    app.state.upstream = UpstreamClient(lambda: app.state.upstream_client, settings)

    async def _sessions_by_alias() -> dict[tuple[tuple[str, str], ...], float]:
        aliases = [s.alias for s in app.state.catalog.servers if s.model.mode == "facade"]
        counts = await app.state.sessions.active_by_alias(aliases)
        return {(("alias", alias),): value for alias, value in counts.items()}

    metrics.register_labeled_gauge(
        "hub_upstream_sessions_active",
        "Активные upstream-сессии MCP по серверам.",
        _sessions_by_alias,
    )

    app.add_exception_handler(HubError, _hub_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _internal_error_handler)

    app.include_router(system_router)
    app.include_router(cli_router)
    app.include_router(api_router)
    app.include_router(admin_router)
    app.include_router(web_router)
    app.include_router(oauth_router)
    app.include_router(mcp_router)

    app.add_middleware(RequestContextMiddleware, metrics=metrics)
    return app


__all__ = ["create_app"]
