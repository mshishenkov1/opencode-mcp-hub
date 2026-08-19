"""Шаблоны страниц Hub (Jinja2 + HTMX, русский язык) — R-W6.

Все ответы страниц: ``text/html; charset=utf-8`` и ``Cache-Control: private, no-store``.
В HTML не попадают токены, секреты и внутренние адреса (``upstream_url``) — шаблоны получают
только подготовленные представления (R-T4, R-W6).
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, select_autoescape

HTML_CONTENT_TYPE = "text/html; charset=utf-8"
HTML_CACHE_CONTROL = "private, no-store"


def build_environment() -> Environment:
    return Environment(
        loader=PackageLoader("hub", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


class Templates:
    """Обёртка над Jinja2: рендер страницы и фрагмента HTMX."""

    def __init__(self, environment: Environment | None = None) -> None:
        self.env = environment or build_environment()

    def render(self, name: str, /, **context: Any) -> str:
        return self.env.get_template(name).render(**context)

    def page(
        self,
        name: str,
        /,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        **context: Any,
    ) -> HTMLResponse:
        response_headers = {"Cache-Control": HTML_CACHE_CONTROL}
        response_headers.update(headers or {})
        return HTMLResponse(
            content=self.render(name, **context),
            status_code=status_code,
            headers=response_headers,
            media_type=HTML_CONTENT_TYPE,
        )


__all__ = ["HTML_CACHE_CONTROL", "HTML_CONTENT_TYPE", "Templates", "build_environment"]
