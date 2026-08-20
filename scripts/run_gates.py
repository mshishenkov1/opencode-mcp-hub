#!/usr/bin/env python3
"""Прогон quality gates G1–G6 (ТЗ §5). Команды и пороги — из pipeline.config.yaml.

Использование: run_gates.py [--run-id <id>] [--ci]

Пишет reports/gates-<run-id>.json и обновляет секцию gates в state.json.
Код выхода 0 — только если все шесть гейтов зелёные.
"""
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import yaml
except ImportError:
    print("нужен pyyaml: .venv/bin/pip install -e '.[dev]' (запускай через .venv/bin/python)",
          file=sys.stderr)
    sys.exit(2)


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)


def gate_g1_tests(cfg: dict) -> tuple[bool, str]:
    r = sh(cfg["commands"]["test"])
    skipped = 0
    m = re.search(r"(\d+) skipped", r.stdout + r.stderr)
    if m:
        skipped = int(m.group(1))
    justified = (ROOT / "reports" / "skip-justification.md").exists()
    if r.returncode != 0:
        return False, f"тесты красные (rc={r.returncode}): {tail(r.stdout)}"
    if skipped and not justified:
        return False, f"{skipped} skip без обоснования (нет reports/skip-justification.md)"
    return True, f"все тесты зелёные, skip: {skipped}" + (" (обоснованы)" if skipped else "")


def resolve_base(base: str) -> str | None:
    """Ревизия базовой ветки, существующая в этом клоне.

    На раннере actions/checkout создаёт только ветку прогона: даже при fetch-depth: 0
    локальной `main` нет, и diff-cover падает на неразрешимой ревизии с пустым stdout
    (гейт G2 был красным в CI и зелёным локально именно поэтому). Локально берём саму
    ветку, в CI — `origin/main`.
    """
    for ref in (base, f"origin/{base}"):
        if sh(f"git rev-parse --verify --quiet {ref}^{{commit}}").returncode == 0:
            return ref
    return None


def gate_g2_coverage_diff(cfg: dict) -> tuple[bool, str]:
    r = sh(cfg["commands"]["coverage"])
    if r.returncode != 0:
        return False, f"прогон с coverage упал: {tail(r.stdout)}"
    threshold = cfg["thresholds"]["coverage_diff"]
    base = resolve_base(cfg.get("base_branch", "main"))
    if base is None:
        return False, (f"базовая ветка {cfg.get('base_branch', 'main')} недоступна ни локально, "
                       "ни как origin/<ветка> — coverage-diff считать не от чего")
    cmd = cfg["commands"]["coverage_diff"].format(base=base, threshold=threshold)
    r = sh(cmd)
    m = re.search(r"Coverage: ([\d.]+)%", r.stdout)
    pct = m.group(1) + "%" if m else "n/a"
    if "No lines with coverage information" in r.stdout:
        return True, "нет новых/изменённых строк логики относительно базовой ветки"
    if r.returncode != 0:
        # stderr важен: неразрешимая ревизия и отсутствие coverage.xml видны только там.
        detail = tail(r.stdout) or tail(r.stderr)
        return False, f"coverage-diff {pct} < порога {threshold}% (база {base}): {detail}"
    return True, f"coverage-diff {pct} >= {threshold}% (база {base})"


def gate_g3_mutation(cfg: dict) -> tuple[bool, str]:
    threshold = cfg["thresholds"]["mutation_score"]
    r = sh(cfg["commands"]["mutation"])
    out = r.stdout + r.stderr
    # формат прогресс-строки mutmut: 🎉 killed  ⏰ timeout  🤔 suspicious  🙁 survived  🔇 skipped
    counts = {}
    for emoji, name in (("🎉", "killed"), ("⏰", "timeout"), ("🤔", "suspicious"),
                        ("🙁", "survived"), ("🔇", "skipped")):
        matches = re.findall(re.escape(emoji) + r"\s*(\d+)", out)
        counts[name] = int(matches[-1]) if matches else 0
    detected = counts["killed"] + counts["timeout"]
    undetected = counts["survived"] + counts["suspicious"]
    total = detected + undetected
    if total == 0:
        return False, ("не удалось получить статистику mutmut — проверь вручную: "
                       f"{tail(out)}")
    score = 100.0 * detected / total
    detail = (f"mutation score {score:.1f}% (killed {counts['killed']}, timeout {counts['timeout']}, "
              f"survived {counts['survived']}, suspicious {counts['suspicious']}), порог {threshold}%")
    return score >= threshold, detail


def gate_g4_static(cfg: dict) -> tuple[bool, str]:
    lint = sh(cfg["commands"]["lint"])
    typing = sh(cfg["commands"]["typecheck"])
    problems = []
    if lint.returncode != 0:
        problems.append(f"lint: {tail(lint.stdout)}")
    if typing.returncode != 0:
        problems.append(f"types: {tail(typing.stdout)}")
    return (not problems), ("; ".join(problems) or "линтер и типизация чистые")


def gate_g5_review() -> tuple[bool, str]:
    reviews = sorted((ROOT / "reports").glob("review-*.json"))
    if not reviews:
        return False, "нет ни одного отчёта review-agent (reports/review-*.json)"
    last = reviews[-1]
    try:
        verdict = json.loads(last.read_text(encoding="utf-8")).get("verdict")
    except Exception as e:
        return False, f"не читается {last.name}: {e}"
    return verdict == "approve", f"{last.name}: verdict={verdict}"


def gate_g6_traceability() -> tuple[bool, str]:
    r = sh(f"{sys.executable} scripts/check_ac_traceability.py")
    return r.returncode == 0, tail(r.stdout, 400)


def tail(text: str, n: int = 300) -> str:
    text = text.strip()
    return text[-n:].replace("\n", " | ") if text else "(пусто)"


def main(argv: list[str]) -> int:
    run_id = argv[argv.index("--run-id") + 1] if "--run-id" in argv else "manual"
    cfg = yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8"))

    gates = [
        ("G1", "Тесты", lambda: gate_g1_tests(cfg)),
        ("G2", "Coverage-diff", lambda: gate_g2_coverage_diff(cfg)),
        ("G3", "Mutation", lambda: gate_g3_mutation(cfg)),
        ("G4", "Статанализ", lambda: gate_g4_static(cfg)),
        ("G5", "Ревью", gate_g5_review),
        ("G6", "AC-трассировка", gate_g6_traceability),
    ]

    results = {}
    all_ok = True
    for gid, name, fn in gates:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"гейт упал с исключением: {e}"
        results[gid] = {"name": name, "passed": ok, "detail": detail}
        all_ok &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {gid} {name}: {detail}\n")

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    report = {"run_id": run_id, "ts": ts, "all_passed": all_ok, "gates": results}
    out = ROOT / "reports" / f"gates-{run_id}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    state_path = ROOT / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["gates"] = {gid: r["passed"] for gid, r in results.items()}
        state.setdefault("history", []).append(
            {"ts": ts, "event": f"gates: {'ALL PASS' if all_ok else 'FAIL'} -> {out.name}"})
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except Exception:
        pass

    print(f"{'ВСЕ ГЕЙТЫ ЗЕЛЁНЫЕ' if all_ok else 'ЕСТЬ КРАСНЫЕ ГЕЙТЫ'} -> {out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
