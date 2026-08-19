"""HTTP-маршруты Hub."""

from hub.routes.admin import router as admin_router
from hub.routes.api import router as api_router
from hub.routes.cli import router as cli_router
from hub.routes.system import router as system_router
from hub.routes.web import router as web_router

__all__ = ["admin_router", "api_router", "cli_router", "system_router", "web_router"]
