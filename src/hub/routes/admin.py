"""``/admin/catalog/reload`` — перечитывание каталога по ``X-Admin-Token`` (R-C4)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hub.catalog import CatalogError, load_catalog
from hub.errors import HubError

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("hub.admin")

ADMIN_TOKEN_HEADER = "X-Admin-Token"


@router.post("/catalog/reload")
async def catalog_reload(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    if not settings.admin_enabled:
        raise HubError(404, "not_found", "Ресурс не найден")
    if not settings.is_admin_token(request.headers.get(ADMIN_TOKEN_HEADER)):
        raise HubError(403, "forbidden", "Неверный токен администратора")
    try:
        catalog = load_catalog(settings.catalog_path, state.catalog_env)
    except CatalogError as exc:
        logger.warning("catalog_reload_failed", extra={"reason": str(exc)})
        raise HubError(400, "catalog_invalid", str(exc)) from exc
    state.catalog = catalog  # атомарная замена ссылки
    await state.db.audit(
        "catalog_reloaded",
        details={"catalog_version": catalog.version, "servers": len(catalog.servers)},
        ts=state.clock.now(),
    )
    logger.info(
        "catalog_reloaded",
        extra={"catalog_version": catalog.version, "servers": len(catalog.servers)},
    )
    return JSONResponse({"status": "ok", "catalog_version": catalog.version, "servers": len(catalog.servers)})
