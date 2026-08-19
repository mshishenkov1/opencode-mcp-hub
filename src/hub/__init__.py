"""OpenCode MCP Hub — каталог корпоративных MCP-серверов и вход через LiteLLM CLI-SSO."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("opencode-mcp-hub")
except PackageNotFoundError:  # pragma: no cover - пакет не установлен
    __version__ = "0.1.0"

__all__ = ["__version__"]
