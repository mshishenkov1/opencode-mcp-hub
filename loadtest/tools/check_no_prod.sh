#!/usr/bin/env bash
# D6-10: нагрузочный контур не должен ходить в боевые системы.
#
# Проверяет грепом все файлы нагрузочного контура: каждый встреченный URL обязан
# указывать на хост из allow-list, а имена корпоративных доменов не должны
# встречаться вовсе. Вторая линия защиты — assertLocal() в k6 (loadtest/k6/lib/config.js)
# и _check_no_prod() в loadtest/tools/seed.py, они срабатывают уже во время запуска.
#
#   bash loadtest/tools/check_no_prod.sh            # проверить loadtest/
#   bash loadtest/tools/check_no_prod.sh path...    # проверить указанные файлы

set -uo pipefail

ALLOWED_HOSTS="localhost 127.0.0.1 ::1 [::1] 0.0.0.0 hub proxy mock-upstream postgres redis example.invalid"
# Признаки боевых систем: корпоративные домены и публичные хостинги целевых систем.
FORBIDDEN_PATTERNS='corp\.tander\.ru|tander\.ru|magnit\.ru|ailab-copilot|coderepo|jira\.corp|it-portal|gitlab\.platform|atlassian\.net|\.magnit\.'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

FILES=()
if [ $# -gt 0 ]; then
  FILES=("$@")
else
  # Всё, кроме служебных каталогов прогона и самого этого скрипта
  # (в нём перечислены запрещённые шаблоны). Из проверки исключаются артефакты
  # прогона (summary-*.json, seed.json) и package-lock.json (машинный файл с
  # адресами реестра пакетов), но НЕ конфигурация в JSON:
  # каталог или настройки в .json обязаны проходить проверку наравне с YAML.
  while IFS= read -r file; do
    [ "$file" = "$SELF" ] && continue
    FILES+=("$file")
  done < <(find "$ROOT" -type f \
    ! -path "*/.seed/*" ! -path "*/node_modules/*" \
    ! -name "summary-*.json" ! -name "seed.json" ! -name "package-lock.json" | sort)
fi

if [ ${#FILES[@]} -eq 0 ]; then
  echo "Нечего проверять: не найдено ни одного файла в $ROOT" >&2
  exit 1
fi

fail=0

echo "Проверяемые файлы: ${#FILES[@]}"
echo "Разрешённые хосты: $ALLOWED_HOSTS"
echo

# 1. Явные признаки боевых систем.
for file in "${FILES[@]}"; do
  if grep -nEi -- "$FORBIDDEN_PATTERNS" "$file"; then
    echo "ОШИБКА: $file содержит адрес боевой системы" >&2
    fail=1
  fi
done

# 2. Каждый URL — только на разрешённый хост.
for file in "${FILES[@]}"; do
  while IFS= read -r url; do
    host="${url#*://}"
    host="${host%%/*}"
    host="${host##*@}"
    # отбрасываем порт (кроме IPv6 в скобках)
    case "$host" in
      \[*\]*) host="${host%%]*}]" ;;
      *) host="${host%%:*}" ;;
    esac
    [ -z "$host" ] && continue
    found=0
    for allowed in $ALLOWED_HOSTS; do
      if [ "$host" = "$allowed" ]; then
        found=1
        break
      fi
    done
    if [ "$found" -eq 0 ]; then
      echo "ОШИБКА: $file → $url (хост $host не разрешён)" >&2
      fail=1
    fi
  done < <(grep -ohE 'https?://[^"'"'"'` )>,]+' "$file" | sed 's/[.,]$//' | sort -u)
done

if [ "$fail" -ne 0 ]; then
  echo >&2
  echo "РЕЗУЛЬТАТ: нагрузочный контур ссылается на внешние адреса — прогон запрещён" >&2
  exit 1
fi

echo "РЕЗУЛЬТАТ: OK — внешних адресов в нагрузочном контуре нет"
exit 0
