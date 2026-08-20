#!/usr/bin/env bash
# Резервная копия стенда Hub (D6-04): дамп Postgres + копия .env, с ротацией.
#
# Критичны только данные Postgres (пользователи, ключи, подключения, зашифрованные
# токены целевых систем, refresh-цепочки, аудит) и .env: без HUB_ENCRYPTION_KEY
# токены в дампе расшифровать невозможно. Redis не критичен — там кэш и сессии.
#
#   bash deploy/backup.sh                    # дамп в deploy/backups, 7 последних копий
#   BACKUP_KEEP=14 bash deploy/backup.sh     # хранить 14 копий
#   BACKUP_DIR=/mnt/backup bash deploy/backup.sh
#   BACKUP_PROJECT=hubi3 BACKUP_COMPOSE_FILES=docker-compose.yml bash deploy/backup.sh
#
# Переменные:
#   BACKUP_DIR            куда класть копии (по умолчанию deploy/backups)
#   BACKUP_KEEP           сколько копий хранить (по умолчанию 7)
#   BACKUP_COMPOSE_FILES  файлы compose через пробел
#                         (по умолчанию docker-compose.yml docker-compose.windows.yml)
#   BACKUP_PROJECT        имя проекта docker compose (-p); пусто — как у compose по умолчанию
#   BACKUP_DB / BACKUP_DB_USER   база и пользователь (по умолчанию hub/hub)
#
# ВОССТАНОВЛЕНИЕ (той же командой compose, что и запуск стенда):
#   docker compose -f docker-compose.yml -f docker-compose.windows.yml stop hub
#   docker compose ... exec -T postgres dropdb   -U hub --if-exists hub
#   docker compose ... exec -T postgres createdb -U hub hub
#   docker compose ... exec -T postgres pg_restore -U hub -d hub --no-owner \
#       < backups/hub-20260820-1200.dump
#   docker compose ... start hub
#   bash deploy/smoke.sh https://mcp-hub.corp.tander.ru
# Схема приводится к head миграциями при старте Hub (HUB_DB_AUTO_MIGRATE=true),
# поэтому дамп более старой версии восстанавливается штатно. Проверять
# восстановление на копии стенда, а не на боевом.
#
# Код возврата: 0 — копия снята и проверена, 1 — любая ошибка (годится для
# «Планировщика заданий» и cron: молчаливых провалов нет, всё пишется в stderr).

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEPLOY_DIR"

BACKUP_DIR="${BACKUP_DIR:-$DEPLOY_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
BACKUP_COMPOSE_FILES="${BACKUP_COMPOSE_FILES:-docker-compose.yml docker-compose.windows.yml}"
BACKUP_PROJECT="${BACKUP_PROJECT:-}"
BACKUP_DB="${BACKUP_DB:-hub}"
BACKUP_DB_USER="${BACKUP_DB_USER:-hub}"

if ! [[ "$BACKUP_KEEP" =~ ^[0-9]+$ ]] || [ "$BACKUP_KEEP" -lt 1 ]; then
  echo "BACKUP_KEEP должен быть целым числом ≥ 1, получено: $BACKUP_KEEP" >&2
  exit 1
fi

COMPOSE=(docker compose)
for f in $BACKUP_COMPOSE_FILES; do
  if [ ! -f "$f" ]; then
    echo "Не найден файл compose: $DEPLOY_DIR/$f (см. BACKUP_COMPOSE_FILES)" >&2
    exit 1
  fi
  COMPOSE+=(-f "$f")
done
[ -n "$BACKUP_PROJECT" ] && COMPOSE+=(-p "$BACKUP_PROJECT")

if [ -z "$("${COMPOSE[@]}" ps -q postgres 2>/dev/null)" ]; then
  echo "Контейнер postgres не запущен: снять дамп нечем." >&2
  echo "Проверьте: ${COMPOSE[*]} ps" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M)"
DUMP="$BACKUP_DIR/hub-$STAMP.dump"
TMP="$DUMP.part"
trap 'rm -f "$TMP"' EXIT

echo "Дамп базы $BACKUP_DB → $DUMP"
if ! "${COMPOSE[@]}" exec -T postgres \
    pg_dump -U "$BACKUP_DB_USER" -d "$BACKUP_DB" --format=custom > "$TMP"; then
  echo "pg_dump завершился с ошибкой — копия не создана." >&2
  exit 1
fi

# Проверка: непустой файл в формате custom (сигнатура PGDMP).
if [ ! -s "$TMP" ] || [ "$(head -c 5 "$TMP")" != "PGDMP" ]; then
  echo "Дамп пуст или не в формате custom — копия не создана." >&2
  exit 1
fi

mv "$TMP" "$DUMP"
chmod 600 "$DUMP"
trap - EXIT

# .env хранится рядом: без HUB_ENCRYPTION_KEY дамп бесполезен.
if [ -f "$DEPLOY_DIR/.env" ]; then
  cp "$DEPLOY_DIR/.env" "$BACKUP_DIR/env-$STAMP.bak"
  chmod 600 "$BACKUP_DIR/env-$STAMP.bak"
else
  echo "ПРЕДУПРЕЖДЕНИЕ: $DEPLOY_DIR/.env не найден, копия ключей не сделана." >&2
fi

# Ротация: оставляем BACKUP_KEEP самых свежих копий каждого вида.
rotate() {
  local pattern="$1" file count=0
  while IFS= read -r file; do
    count=$((count + 1))
    if [ "$count" -gt "$BACKUP_KEEP" ]; then
      rm -f -- "$file"
      echo "Удалена старая копия: $(basename "$file")"
    fi
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "$pattern" | sort -r)
}
rotate 'hub-*.dump'
rotate 'env-*.bak'

SIZE="$(wc -c < "$DUMP" | tr -d ' ')"
KEPT="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'hub-*.dump' | wc -l | tr -d ' ')"
echo "РЕЗУЛЬТАТ: OK — $DUMP ($SIZE байт), копий в $BACKUP_DIR: $KEPT (храним $BACKUP_KEEP)"
