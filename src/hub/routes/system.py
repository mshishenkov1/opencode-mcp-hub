"""``/health``, ``/ready``, ``/metrics``, ``/.well-known/opencode`` (R-A1, R-A5, R-A8, R-S4)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from hub import __version__
from hub.metrics import PROMETHEUS_CONTENT_TYPE
from hub.wellknown import build_wellknown, dump_json, etag_for, if_none_match_matches

router = APIRouter(tags=["system"])

WELLKNOWN_CACHE_CONTROL = "public, max-age=300"


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "catalog_version": state.catalog.version,
            "time": state.clock.now().isoformat(),
        }
    )


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    catalog_ok = getattr(state, "catalog", None) is not None
    db_ok = await state.db.ping() if catalog_ok else False
    if catalog_ok and db_ok:
        return JSONResponse({"status": "ready"})
    reason = "каталог не загружен" if not catalog_ok else "база данных недоступна"
    return JSONResponse(
        {"status": "not_ready", "error": "not_ready", "message": reason}, status_code=503
    )


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    text = await request.app.state.metrics.render()
    return Response(content=text, media_type=PROMETHEUS_CONTENT_TYPE)


@router.get("/.well-known/opencode")
async def wellknown(request: Request) -> Response:
    state = request.app.state
    body = dump_json(build_wellknown(state.settings, state.catalog))
    etag = etag_for(body)
    headers = {"ETag": etag, "Cache-Control": WELLKNOWN_CACHE_CONTROL}
    if if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
