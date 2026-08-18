#!/usr/bin/env python3
"""CLI для работы оркестратора со state.json.

Использование:
  state.py get [ключ]                — показать состояние или значение ключа
  state.py set <ключ> <значение>     — установить ключ (значение парсится как JSON, иначе строка)
  state.py agent <роль>              — установить current_agent (orchestrator|spec|dev|test|review)
  state.py stage <этап>              — установить stage
  state.py log "<событие>"           — добавить запись в history
  state.py sync-bugs                 — синхронизировать сводку bugs/ в state.json
"""
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state.json"
ROLES = {"orchestrator", "spec", "dev", "test", "review"}


def load() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log_event(state: dict, event: str) -> None:
    state.setdefault("history", []).append(
        {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "event": event}
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *args = argv
    state = load()

    if cmd == "get":
        print(json.dumps(state.get(args[0]) if args else state, ensure_ascii=False, indent=2))
        return 0

    if cmd == "set" and len(args) == 2:
        key, raw = args
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        state[key] = value
        log_event(state, f"set {key}={raw}")
    elif cmd == "agent" and len(args) == 1:
        if args[0] not in ROLES:
            print(f"неизвестная роль: {args[0]} (допустимо: {sorted(ROLES)})", file=sys.stderr)
            return 1
        state["current_agent"] = args[0]
        log_event(state, f"current_agent -> {args[0]}")
    elif cmd == "stage" and len(args) == 1:
        state["stage"] = args[0]
        log_event(state, f"stage -> {args[0]}")
    elif cmd == "log" and len(args) == 1:
        log_event(state, args[0])
    elif cmd == "sync-bugs":
        bugs = []
        for f in sorted((ROOT / "bugs").glob("BUG-*.json")):
            try:
                b = json.loads(f.read_text(encoding="utf-8"))
                bugs.append({k: b.get(k) for k in
                             ("id", "severity", "status", "iteration", "related_ac", "title")})
            except Exception as e:
                print(f"повреждённый баг-файл {f.name}: {e}", file=sys.stderr)
        state["bugs"] = bugs
        state["escalations"] = [b["id"] for b in bugs if b.get("status") == "escalated"]
        log_event(state, f"sync-bugs: {len(bugs)} багов, "
                         f"{sum(1 for b in bugs if b.get('status') == 'open')} открытых")
    else:
        print(__doc__)
        return 1

    save(state)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
