"""Общая логика зон записи для hooks. Источник истины — pipeline.config.yaml.

Если PyYAML недоступен системному python3, используются встроенные значения,
зеркалящие конфиг (при изменении зон в конфиге обнови и DEFAULTS).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "zones": {
        "orchestrator": {"allow": ["state.json", "reports/", "bugs/", "examples/"],
                         "deny": ["src/", "tests/"]},
        "spec": {"allow": ["spec.md", "acceptance-criteria.yaml", "reports/"], "deny": []},
        "dev": {"allow": ["src/", "disputes/", "pyproject.toml"], "deny": ["tests/"]},
        "test": {"allow": ["tests/", "bugs/", "reports/"], "deny": ["src/"]},
        "review": {"allow": ["reports/", "disputes/"], "deny": ["src/", "tests/"]},
    },
    "protected": ["pipeline.config.yaml", ".claude/", ".github/"],
}

# Каталоги временных файлов, куда запись разрешена любому агенту
TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")


def project_root(hook_input: dict) -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or hook_input.get("cwd") or "."
    return Path(root).resolve()


def load_config(root: Path) -> dict:
    cfg_path = root / "pipeline.config.yaml"
    try:
        import yaml  # type: ignore
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if "zones" in cfg and "protected" in cfg:
            return cfg
    except Exception:
        pass
    return DEFAULTS


def current_agent(root: Path) -> str:
    try:
        with open(root / "state.json", encoding="utf-8") as f:
            return json.load(f).get("current_agent") or "orchestrator"
    except Exception:
        return "orchestrator"


def _match(rel: str, prefix: str) -> bool:
    prefix = prefix.strip()
    if prefix.endswith("/"):
        return rel.startswith(prefix) or rel == prefix.rstrip("/")
    return rel == prefix


def check_path(raw_path: str, hook_input: dict) -> tuple[bool, str]:
    """Возвращает (разрешено, причина отказа)."""
    root = project_root(hook_input)
    p = Path(raw_path)
    if not p.is_absolute():
        p = root / p
    p = Path(os.path.normpath(p))

    # временные файлы — можно всем
    if str(p).startswith(TMP_PREFIXES):
        return True, ""

    try:
        rel = str(p.relative_to(root))
    except ValueError:
        return False, f"запись вне репозитория запрещена: {p}"

    cfg = load_config(root)
    for prot in cfg.get("protected", []):
        if _match(rel, prot):
            return False, (f"'{rel}' — защищённый путь (pipeline.config.yaml → protected). "
                           "Изменять его может только человек.")

    # служебный файл оркестрации доступен всегда (обновляется скриптами/хуками)
    if rel == "state.json":
        return True, ""

    agent = current_agent(root)
    zone = cfg.get("zones", {}).get(agent)
    if zone is None:
        return True, ""  # неизвестная роль — не блокируем, ловится diff-проверкой

    for d in zone.get("deny", []):
        if _match(rel, d):
            return False, (f"агенту '{agent}' запрещена запись в '{rel}' (deny-зона). "
                           "Если считаешь чужой артефакт неверным — верни возражение оркестратору.")
    for a in zone.get("allow", []):
        if _match(rel, a):
            return True, ""
    return False, (f"'{rel}' вне разрешённой зоны агента '{agent}' "
                   f"(allow: {zone.get('allow')}).")
