#!/usr/bin/env python3
"""G6: трассировка критериев приёмки на тесты.

Каждый AC из acceptance-criteria.yaml должен иметь минимум один тест,
помеченный @pytest.mark.ac("AC-XX"). Печатает таблицу AC -> тесты.
Ненулевой код выхода, если есть непокрытые AC или маркеры на несуществующие AC.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AC_ID = re.compile(r"^\s*-\s+id:\s*[\"']?(AC-\d+)[\"']?", re.M)
MARKER = re.compile(r"pytest\.mark\.ac\(\s*[\"'](AC-\d+)[\"']\s*\)")
TEST_DEF = re.compile(r"^(?:async\s+)?def\s+(test_\w+)", re.M)


def main() -> int:
    ac_file = ROOT / "acceptance-criteria.yaml"
    if not ac_file.exists():
        print("acceptance-criteria.yaml не найден — этап SPEC не выполнен")
        return 1
    ac_ids = AC_ID.findall(ac_file.read_text(encoding="utf-8"))
    if not ac_ids:
        print("в acceptance-criteria.yaml нет ни одного критерия с id AC-NN")
        return 1

    mapping: dict[str, list[str]] = {ac: [] for ac in ac_ids}
    unknown: list[tuple[str, str]] = []

    for test_file in sorted((ROOT / "tests").rglob("test_*.py")):
        text = test_file.read_text(encoding="utf-8")
        # сопоставляем маркеры с ближайшим следующим def test_
        for m in MARKER.finditer(text):
            tail = text[m.end():]
            d = TEST_DEF.search(tail)
            test_name = f"{test_file.relative_to(ROOT)}::{d.group(1)}" if d else str(
                test_file.relative_to(ROOT))
            if m.group(1) in mapping:
                mapping[m.group(1)].append(test_name)
            else:
                unknown.append((m.group(1), test_name))

    print(f"{'AC':<8} {'тестов':<7} тесты")
    for ac in ac_ids:
        tests = mapping[ac]
        print(f"{ac:<8} {len(tests):<7} {', '.join(tests) if tests else '— НЕ ПОКРЫТ —'}")

    ok = True
    missing = [ac for ac in ac_ids if not mapping[ac]]
    if missing:
        print(f"\nG6 FAIL: без тестов: {', '.join(missing)}")
        ok = False
    if unknown:
        print("\nG6 FAIL: маркеры на несуществующие AC:")
        for ac, t in unknown:
            print(f"  {ac} <- {t}")
        ok = False
    if ok:
        print(f"\nG6 OK: все {len(ac_ids)} критериев покрыты тестами")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
