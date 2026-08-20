#!/usr/bin/env bash
#
# installers/tests/trace-ac.sh — трассировка критериев приёмки I-5 на тесты.
#
# Источник критериев: installers/docs/acceptance-criteria.yaml (раздел criteria, AC-*;
# раздел hub_backlog_criteria с HAC-* к установщикам не относится и не учитывается).
# Источник покрытия: маркеры вида AC-NN в именах тестов installers/tests/bats/*.bats
# и installers/tests/pester/*.Tests.ps1.
#
# Использование:
#   trace-ac.sh              таблица «AC → тип → тесты» и итог
#   trace-ac.sh --markdown   та же таблица в виде markdown (для отчёта)
#
# Код выхода: 0 — каждый критерий типа unit/integration покрыт хотя бы одним тестом;
#             1 — есть непокрытые критерии unit/integration.
# Совместимость: bash 3.2 (N5-T5).

set -euo pipefail

CDPATH=''
self_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
installers_root=$(cd -- "$self_dir/.." && pwd -P)
criteria_file="$installers_root/docs/acceptance-criteria.yaml"
bats_dir="$installers_root/tests/bats"
pester_dir="$installers_root/tests/pester"

markdown=0
if [ "${1:-}" = "--markdown" ]; then
  markdown=1
fi

[ -f "$criteria_file" ] || {
  printf 'Не найден файл критериев: %s\n' "$criteria_file" >&2
  exit 2
}

work=$(mktemp -d "${TMPDIR:-/tmp}/trace-ac.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM

# 1. Критерии: "<id> <type>"
awk '
  /^hub_backlog_criteria:/ { exit }
  /^  - id: AC-/ { id = $3; next }
  /^    type: / { if (id != "") { print id, $2; id = "" } }
' "$criteria_file" >"$work/criteria"

# 2. Покрытие: "<id> <файл>" по маркерам в именах тестов
: >"$work/coverage"
for file in "$bats_dir"/*.bats; do
  [ -f "$file" ] || continue
  grep '^@test ' "$file" | grep -o 'AC-[0-9][0-9]*' | while IFS= read -r id; do
    printf '%s %s\n' "$id" "$(basename "$file")" >>"$work/coverage"
  done
done
for file in "$pester_dir"/*.Tests.ps1; do
  [ -f "$file" ] || continue
  grep -E "^[[:space:]]*(It|Context|Describe) " "$file" | grep -o 'AC-[0-9][0-9]*' | while IFS= read -r id; do
    printf '%s %s\n' "$id" "$(basename "$file")" >>"$work/coverage"
  done
done

# 3. Таблица
if [ "$markdown" -eq 1 ]; then
  printf '| AC | тип | тестов | файлы |\n'
  printf '|---|---|---|---|\n'
else
  printf '%-8s %-12s %-7s %s\n' "AC" "тип" "тестов" "файлы"
fi

uncovered=""
while read -r id type; do
  [ -n "$id" ] || continue
  count=$(grep -c "^$id " "$work/coverage" || true)
  files=$(grep "^$id " "$work/coverage" | awk '{ print $2 }' | sort -u | tr '\n' ' ' | sed -e 's/ $//' || true)
  [ -n "$files" ] || files="—"
  if [ "$markdown" -eq 1 ]; then
    printf '| %s | %s | %s | %s |\n' "$id" "$type" "$count" "$files"
  else
    printf '%-8s %-12s %-7s %s\n' "$id" "$type" "$count" "$files"
  fi
  if [ "$count" -eq 0 ]; then
    case $type in
      unit|integration) uncovered="$uncovered $id" ;;
    esac
  fi
done <"$work/criteria"

total=$(wc -l <"$work/criteria" | tr -d ' ')
covered=$(awk '{ print $1 }' "$work/coverage" | sort -u | wc -l | tr -d ' ')
printf '\nВсего критериев AC-*: %s; с тестами: %s\n' "$total" "$covered"

if [ -n "$uncovered" ]; then
  printf 'Без тестов (тип unit/integration):%s\n' "$uncovered" >&2
  exit 1
fi
printf 'Все критерии типа unit/integration покрыты.\n'
