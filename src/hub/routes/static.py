"""Раздача статики страниц Hub из образа: htmx и его расширения, без публичных CDN (R-W6)."""

from __future__ import annotations

from functools import cache

from fastapi import APIRouter, Response

from hub.assets import STATIC_CACHE_CONTROL, STATIC_FILES, read_static
from hub.errors import HubError

router = APIRouter(tags=["static"])


@cache
def _content(name: str) -> bytes:
    return read_static(name)


@router.get("/static/{name}")
async def static_asset(name: str) -> Response:
    """Ресурс из ``hub/static``; имя вне белого списка — 404, выхода за каталог нет."""
    media_type = STATIC_FILES.get(name)
    if media_type is None:
        raise HubError(404, "not_found", "Ресурс не найден")
    return Response(
        content=_content(name),
        media_type=media_type,
        headers={"Cache-Control": STATIC_CACHE_CONTROL},
    )


__all__ = ["router"]
