"""Сборка ответов ``/.well-known/opencode`` и ``/remote-config`` (R-A5, R-A6, R-A8)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hub.catalog import Catalog
from hub.settings import Settings

OPENCODE_SCHEMA = "https://opencode.ai/config.json"


def build_wellknown(settings: Settings, catalog: Catalog) -> dict[str, Any]:
    env_name = settings.wellknown_env_name
    provider_id = settings.litellm_provider_id
    mcp: dict[str, Any] = {}
    for server in catalog.servers:
        if server.unconfigured or "all" not in server.model.audience:
            continue
        mcp[server.alias] = {
            "type": "remote",
            "url": server.public_mcp_url(settings.public_url),
            "enabled": False,
            "oauth": {},
        }
    return {
        "auth": {"command": list(settings.wellknown_auth_command), "env": env_name},
        "config": {
            "$schema": OPENCODE_SCHEMA,
            "autoupdate": False,
            "enabled_providers": [provider_id],
            "provider": {
                provider_id: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": settings.litellm_provider_name,
                    "options": {
                        "baseURL": f"{settings.litellm_base_url}/v1",
                        "apiKey": f"{{env:{env_name}}}",
                    },
                    "models": {
                        settings.litellm_model: {
                            "name": settings.litellm_model,
                            "limit": {
                                "context": settings.litellm_context_limit,
                                "output": settings.litellm_output_limit,
                            },
                        }
                    },
                }
            },
            "mcp": mcp,
        },
        "remote_config": {
            "url": f"{settings.public_url}/remote-config",
            "headers": {"Authorization": f"Bearer {{env:{env_name}}}"},
        },
    }


def dump_json(payload: Any) -> bytes:
    """Детерминированная сериализация (для ETag)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:16] + '"'


def if_none_match_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        c = candidate.strip()
        if c == "*":
            return True
        c = c.removeprefix("W/")
        if c == etag:
            return True
    return False


def build_remote_config(connected_aliases: list[str]) -> dict[str, Any]:
    return {
        "config": {
            "mcp": {alias: {"enabled": True} for alias in connected_aliases},
            "permission": {},
            "tools": {},
        }
    }


__all__ = ["build_remote_config", "build_wellknown", "dump_json", "etag_for", "if_none_match_matches"]
