"""``/api/*`` и ``/remote-config`` (Bearer): витрина каталога, профиль, подключения (R-A2..R-A4, R-A6)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from hub.auth import AuthUser, authenticate
from hub.db import Connection, to_iso

router = APIRouter(tags=["api"])

_FALSE_VALUES = {"false", "0", "no"}


def _parse_include_deprecated(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() not in _FALSE_VALUES


async def _user_connections(request: Request, user_id: str) -> list[Connection]:
    db = request.app.state.db
    await db.init()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Connection).where(Connection.user_id == user_id).order_by(Connection.id)
            )
        ).scalars()
        return list(rows)


def _connection_view(conn: Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"status": "not_connected", "preset": None, "updated_at": None}
    return {"status": conn.status, "preset": conn.preset, "updated_at": to_iso(conn.updated_at)}


@router.get("/api/me")
async def api_me(user: Annotated[AuthUser, Depends(authenticate)]) -> JSONResponse:
    return JSONResponse(user.to_me())


@router.get("/api/catalog")
async def api_catalog(request: Request, user: Annotated[AuthUser, Depends(authenticate)]) -> JSONResponse:
    state = request.app.state
    catalog = state.catalog
    include_deprecated = _parse_include_deprecated(request.query_params.get("include_deprecated"))
    connections = {c.alias: c for c in await _user_connections(request, user.user_id)}
    servers = []
    for entry in catalog.visible_for(user.groups, include_deprecated=include_deprecated):
        view = entry.public_view(state.settings.public_url)
        view["connection"] = _connection_view(connections.get(entry.alias))
        servers.append(view)
    return JSONResponse({"version": catalog.version, "servers": servers})


@router.get("/api/me/connections")
async def api_me_connections(
    request: Request, user: Annotated[AuthUser, Depends(authenticate)]
) -> JSONResponse:
    items = [
        {
            "alias": c.alias,
            "status": c.status,
            "preset": c.preset,
            "groups": list(c.groups or []),
            "created_at": to_iso(c.created_at),
            "updated_at": to_iso(c.updated_at),
        }
        for c in await _user_connections(request, user.user_id)
    ]
    return JSONResponse(items)


@router.get("/remote-config")
async def remote_config(request: Request, user: Annotated[AuthUser, Depends(authenticate)]) -> JSONResponse:
    from hub.wellknown import build_remote_config

    connected = [c.alias for c in await _user_connections(request, user.user_id) if c.status == "connected"]
    return JSONResponse(build_remote_config(connected), headers={"Cache-Control": "private, no-store"})
