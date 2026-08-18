#!/usr/bin/env python3
"""PreToolUse-hook для Bash: запрет опасных git-операций и best-effort контроль
записи в чужие зоны через редиректы/tee/sed -i/cp/mv (ТЗ §7).

Основная страховка от обхода — diff-проверка scripts/check_zone_diff.py.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _zones import check_path  # noqa: E402

GIT_RULES = [
    (r"git\s+push\b.*(\s--force\b|\s-f\b|--force-with-lease)", "force-push запрещён (только человек)"),
    (r"git\s+push\b.*\b(main|master)\b", "push в main запрещён (только человек)"),
    (r"git\s+push\b.*--delete", "удаление удалённых веток запрещено (только человек)"),
    (r"git\s+branch\s+(-D|-d)\b", "удаление веток запрещено (только человек)"),
    (r"rm\s+(-\w+\s+)*(/|~|\$HOME)(\s|$|/\*)", "удаление вне репозитория запрещено"),
]

PROTECTED_TOKEN = r"(pipeline\.config\.yaml|\.claude/|\.github/)"
WRITE_VERB = r"(\brm\b|\bmv\b|\bcp\b|\btee\b|\bsed\s+-i|\btruncate\b|\bchmod\b|>>?|\bln\b)"

# цели редиректов и tee — прогоняем через зонную проверку
REDIRECT_TARGET = re.compile(r"(?:>>?\s*|\btee\s+(?:-a\s+)?)([\w./~-]+)")


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cmd = (hook_input.get("tool_input") or {}).get("command") or ""

    for pattern, reason in GIT_RULES:
        if re.search(pattern, cmd):
            deny(reason)

    if re.search(WRITE_VERB, cmd) and re.search(PROTECTED_TOKEN, cmd):
        deny("команда изменяет защищённый путь (pipeline.config.yaml / .claude / .github) — только человек")

    for target in REDIRECT_TARGET.findall(cmd):
        if target.startswith(("/dev/", "-")):
            continue
        ok, reason = check_path(target, hook_input)
        if not ok:
            deny(f"редирект в чужую зону: {reason}")

    sys.exit(0)


def deny(reason: str) -> None:
    print(f"[guard_bash] ЗАПРЕЩЕНО: {reason}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
