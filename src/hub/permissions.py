"""Права подключения: наборы групп, заголовок групп, фильтр инструментов (R-P2, R-P8, R-B7)."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from hub.catalog import (
    PermissionHeaderGroups,
    PermissionToolFilter,
    ServerEntry,
)

PRESETS = ("readonly", "readwrite")


@dataclass(frozen=True)
class ToolFilter:
    """Итоговые наборы масок для пользователя (R-P8)."""

    allow: tuple[str, ...]
    deny: tuple[str, ...]

    def allows(self, tool: str) -> bool:
        if any(fnmatch.fnmatchcase(tool, mask) for mask in self.deny):
            return False
        return any(fnmatch.fnmatchcase(tool, mask) for mask in self.allow)

    def filter_tools(self, tools: list[Any]) -> list[Any]:
        result = []
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or self.allows(name):
                result.append(tool)
        return result


def group_ids(entry: ServerEntry) -> dict[str, str]:
    """``id`` → ``preset`` для всех групп ``permission_model`` (пустой словарь для иных видов)."""
    model = entry.model.permission_model
    if not isinstance(model, PermissionHeaderGroups):
        return {}
    return {group.id: group.preset for group in model.groups}


def normalize_groups(entry: ServerEntry, preset: str, requested: list[str]) -> list[str]:
    """Отфильтровать выбранные группы: только известные, без ``none`` и без ``readwrite``
    при пресете ``readonly``; порядок — каталога, без дублей (R-B7, решение 49)."""
    model = entry.model.permission_model
    if not isinstance(model, PermissionHeaderGroups):
        return []
    wanted = set(requested)
    result: list[str] = []
    for group in model.groups:
        if group.id not in wanted or group.preset == "none":
            continue
        if preset == "readonly" and group.preset == "readwrite":
            continue
        if group.id in model.always:
            continue
        result.append(group.id)
    return result


def unknown_groups(entry: ServerEntry, requested: list[str]) -> list[str]:
    known = group_ids(entry)
    return [gid for gid in requested if gid not in known]


def denied_groups(entry: ServerEntry, requested: list[str]) -> list[str]:
    """Группы с ``preset: none`` — их нельзя выбрать никогда (R-B7)."""
    known = group_ids(entry)
    return [gid for gid in requested if known.get(gid) == "none"]


def enabled_groups(entry: ServerEntry, preset: str, groups: list[str]) -> list[str]:
    """Итоговый список групп для заголовка: сначала ``always``, затем выбранные (R-P2, решение 50)."""
    model = entry.model.permission_model
    if not isinstance(model, PermissionHeaderGroups):
        return []
    selected = set(normalize_groups(entry, preset, groups))
    result: list[str] = []
    for gid in model.always:
        if gid not in result:
            result.append(gid)
    for group in model.groups:
        if group.id in selected and group.id not in result:
            result.append(group.id)
    return result


def groups_header(entry: ServerEntry, preset: str, groups: list[str]) -> tuple[str, str] | None:
    """``(имя заголовка, значение)`` для ``permission_model.kind == header_groups`` (R-P2)."""
    model = entry.model.permission_model
    if not isinstance(model, PermissionHeaderGroups):
        return None
    return model.header, ",".join(enabled_groups(entry, preset, groups))


def tool_filter(entry: ServerEntry, preset: str, groups: list[str]) -> ToolFilter:
    """Итоговый фильтр инструментов пользователя (R-P8)."""
    model = entry.model.permission_model
    allow: list[str] = []
    deny: list[str] = []
    if isinstance(model, PermissionHeaderGroups):
        extra = model.tool_filter
        if extra is not None:
            allow.extend(extra.allow)
            deny.extend(extra.deny)
        by_id = {group.id: group for group in model.groups}
        for gid in enabled_groups(entry, preset, groups):
            group = by_id.get(gid)
            if group is not None and group.tools:
                allow.extend(group.tools)
    elif isinstance(model, PermissionToolFilter):
        chosen = model.presets.get(preset)
        if chosen is not None:
            allow.extend(chosen.tools)
    if not allow:
        allow = ["*"]
    return ToolFilter(allow=tuple(dict.fromkeys(allow)), deny=tuple(dict.fromkeys(deny)))


def preset_requires_reauth(current: str | None, requested: str) -> bool:
    """``readonly → readwrite`` требует повторной авторизации в целевой системе (R-B7)."""
    return requested == "readwrite" and (current or "readonly") != "readwrite"


__all__ = [
    "PRESETS",
    "ToolFilter",
    "denied_groups",
    "enabled_groups",
    "group_ids",
    "groups_header",
    "normalize_groups",
    "preset_requires_reauth",
    "tool_filter",
    "unknown_groups",
]
