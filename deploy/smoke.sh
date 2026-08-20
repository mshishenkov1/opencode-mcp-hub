#!/usr/bin/env bash
# Проверка развёрнутого стенда Hub (D6-03).
# По одному запросу на проверку, итог — таблицей; ненулевой код возврата при любом провале.
#
#   ./smoke.sh                                   # стенд на https://localhost:8443
#   ./smoke.sh https://hub.corp.tander.ru        # базовый URL — позиционным параметром
#   ./smoke.sh -k https://localhost:8443         # -k: самоподписанный сертификат стенда
#   ./smoke.sh --tag-base https://localhost:8443/tag   # плюс проверки ТЭГ-MCP (профиль tag)
#   ./smoke.sh --api-key sk-...                  # плюс проверки /api/* с ключом LiteLLM
#   ./smoke.sh --external                        # плюс проверки, ходящие в LiteLLM
#
# Параметры (у каждого есть переменная окружения):
#   [BASE_URL] | --base URL | HUB_BASE       адрес Hub, по умолчанию https://localhost:8443
#   --tag-base URL | TAG_BASE                адрес ТЭГ-MCP; пусто — проверки пропускаются
#   --api-key KEY  | HUB_API_KEY             ключ LiteLLM; пусто — /api/* проверяется на 401
#   --aliases "a b"| SMOKE_ALIASES           facade-alias'ы; пусто — берутся из /.well-known/opencode
#   -k|--insecure  | SMOKE_INSECURE=1        не проверять TLS-сертификат
#   --external     | SMOKE_EXTERNAL=1        выполнять проверки, обращающиеся к LiteLLM
#   --timeout SEC  | SMOKE_TIMEOUT           таймаут одного запроса, с (по умолчанию 15)
#
# Без --external скрипт не обращается ни к одной внешней системе: проверяются только
# Hub и ТЭГ-MCP на самом стенде.

set -uo pipefail

HUB_BASE="${HUB_BASE:-https://localhost:8443}"
TAG_BASE="${TAG_BASE:-}"
HUB_API_KEY="${HUB_API_KEY:-}"
SMOKE_ALIASES="${SMOKE_ALIASES:-}"
SMOKE_INSECURE="${SMOKE_INSECURE:-0}"
SMOKE_EXTERNAL="${SMOKE_EXTERNAL:-0}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-15}"

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --base) HUB_BASE="${2:-}"; shift 2 ;;
    --tag-base) TAG_BASE="${2:-}"; shift 2 ;;
    --api-key) HUB_API_KEY="${2:-}"; shift 2 ;;
    --aliases) SMOKE_ALIASES="${2:-}"; shift 2 ;;
    --timeout) SMOKE_TIMEOUT="${2:-}"; shift 2 ;;
    -k|--insecure) SMOKE_INSECURE=1; shift ;;
    --external) SMOKE_EXTERNAL=1; shift ;;
    -*) echo "Неизвестный параметр: $1" >&2; usage >&2; exit 2 ;;
    *) HUB_BASE="$1"; shift ;;
  esac
done

HUB_BASE="${HUB_BASE%/}"
TAG_BASE="${TAG_BASE%/}"

if [ -z "$HUB_BASE" ]; then
  echo "Не задан базовый URL Hub" >&2
  exit 2
fi

CURL=(curl --silent --show-error --max-time "$SMOKE_TIMEOUT")
if [ "$SMOKE_INSECURE" = "1" ]; then
  CURL+=(--insecure)
fi

PASS=0
FAIL=0
SKIP=0
ROWS=()

# add_row <статус> <проверка> <ожидание> <факт>
add_row() {
  ROWS+=("$1|$2|$3|$4")
  case "$1" in
    OK) PASS=$((PASS + 1)) ;;
    FAIL) FAIL=$((FAIL + 1)) ;;
    SKIP) SKIP=$((SKIP + 1)) ;;
  esac
}

# http_code <метод> <url> [доп. аргументы curl...]
http_code() {
  local method="$1" url="$2"
  shift 2
  "${CURL[@]}" -o /dev/null -w '%{http_code}' -X "$method" "$@" "$url" 2>/dev/null || echo "000"
}

# body <метод> <url> [доп. аргументы curl...]
body() {
  local method="$1" url="$2"
  shift 2
  "${CURL[@]}" -X "$method" "$@" "$url" 2>/dev/null
}

# header <имя-заголовка> <url> — значение заголовка ответа (пусто, если нет)
header() {
  local name="$1" url="$2"
  shift 2
  "${CURL[@]}" -o /dev/null -D - "$@" "$url" 2>/dev/null \
    | tr -d '\r' \
    | awk -v want="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]'):" \
        'tolower($1)==want { $1=""; sub(/^ /, ""); print; exit }'
}

# check_code <название> <ожидаемые коды через запятую> <метод> <url> [доп. аргументы curl...]
check_code() {
  local name="$1" want="$2" method="$3" url="$4"
  shift 4
  local got
  got="$(http_code "$method" "$url" "$@")"
  case ",$want," in
    *",$got,"*) add_row OK "$name" "HTTP ${want//,/ или }" "HTTP $got" ;;
    *) add_row FAIL "$name" "HTTP ${want//,/ или }" "HTTP $got" ;;
  esac
}

# check_contains <название> <подстрока> <url>
check_contains() {
  local name="$1" needle="$2" url="$3"
  local out
  out="$(body GET "$url")"
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    add_row OK "$name" "тело содержит «$needle»" "найдено"
  else
    add_row FAIL "$name" "тело содержит «$needle»" "не найдено"
  fi
}

echo "Стенд:   $HUB_BASE"
[ -n "$TAG_BASE" ] && echo "ТЭГ-MCP: $TAG_BASE"
echo

# --- 1. Живость и готовность ------------------------------------------------
check_code "GET /health" 200 GET "$HUB_BASE/health"
check_contains "GET /health: status=ok" '"status":"ok"' "$HUB_BASE/health"
check_code "GET /ready" 200 GET "$HUB_BASE/ready"

# --- 2. Конфигурация клиента ------------------------------------------------
WELLKNOWN="$(body GET "$HUB_BASE/.well-known/opencode")"
if printf '%s' "$WELLKNOWN" | grep -q '"remote_config"'; then
  add_row OK "GET /.well-known/opencode" "JSON с remote_config" "получен"
else
  add_row FAIL "GET /.well-known/opencode" "JSON с remote_config" "нет remote_config"
fi

ETAG="$(header ETag "$HUB_BASE/.well-known/opencode")"
if [ -n "$ETAG" ]; then
  code="$(http_code GET "$HUB_BASE/.well-known/opencode" -H "If-None-Match: $ETAG")"
  if [ "$code" = "304" ]; then
    add_row OK "ETag /.well-known/opencode" "HTTP 304" "HTTP 304"
  else
    add_row FAIL "ETag /.well-known/opencode" "HTTP 304" "HTTP $code"
  fi
else
  add_row FAIL "ETag /.well-known/opencode" "заголовок ETag" "нет заголовка"
fi

# --- 3. Метаданные OAuth ----------------------------------------------------
AS_META="$(body GET "$HUB_BASE/.well-known/oauth-authorization-server")"
check_code "GET /.well-known/oauth-authorization-server" 200 GET \
  "$HUB_BASE/.well-known/oauth-authorization-server"

if [ -z "$SMOKE_ALIASES" ]; then
  # Источник истины по facade-серверам стенда — scopes_supported AS-метаданных:
  # "<alias>:readonly"/"<alias>:readwrite" на каждый настроенный facade-сервер.
  SMOKE_ALIASES="$(printf '%s' "$AS_META" \
    | grep -oE '"[A-Za-z0-9_-]+:readonly"' \
    | sed -E 's|"([A-Za-z0-9_-]+):readonly"|\1|' \
    | sort -u | tr '\n' ' ')"
fi
if [ -z "${SMOKE_ALIASES// /}" ]; then
  # Запасной источник: адреса вида <публичный-адрес>/mcp/<alias> в /.well-known/opencode
  # (native-серверы вроде ТЭГ имеют собственный URL и под шаблон не попадают).
  SMOKE_ALIASES="$(printf '%s' "$WELLKNOWN" \
    | grep -oE '"url":"[^"]*/mcp/[A-Za-z0-9_-]+"' \
    | sed -E 's|.*/mcp/([A-Za-z0-9_-]+)"$|\1|' \
    | sort -u | tr '\n' ' ')"
fi

if [ -z "${SMOKE_ALIASES// /}" ]; then
  # На стенде без выданных OAuth-приложений все facade-серверы unconfigured и в
  # метаданных не публикуются — это ожидаемое состояние, а не отказ (см. README-windows.md).
  add_row SKIP "Метаданные facade-серверов" "хотя бы один alias" \
    "нет настроенных facade-серверов (не заданы *_OAUTH_CLIENT_ID/SECRET)"
else
  for alias in $SMOKE_ALIASES; do
    check_code "AS-метаданные /mcp/$alias" 200 GET \
      "$HUB_BASE/.well-known/oauth-authorization-server/mcp/$alias"
    check_code "PRM /mcp/$alias" 200 GET \
      "$HUB_BASE/.well-known/oauth-protected-resource/mcp/$alias"
  done
  # Неизвестный alias обязан давать 404, а не 200
  check_code "PRM /mcp/__нет-такого__" 404 GET \
    "$HUB_BASE/.well-known/oauth-protected-resource/mcp/__нет-такого__"
fi

# --- 4. Каталог и права -----------------------------------------------------
if [ -n "$HUB_API_KEY" ]; then
  check_code "GET /api/catalog (с ключом)" 200 GET "$HUB_BASE/api/catalog" \
    -H "Authorization: Bearer $HUB_API_KEY"
  check_code "GET /api/me (с ключом)" 200 GET "$HUB_BASE/api/me" \
    -H "Authorization: Bearer $HUB_API_KEY"
  check_code "GET /remote-config (с ключом)" 200 GET "$HUB_BASE/remote-config" \
    -H "Authorization: Bearer $HUB_API_KEY"
else
  # Без ключа каталог обязан быть закрыт
  check_code "GET /api/catalog (без ключа)" 401 GET "$HUB_BASE/api/catalog"
  add_row SKIP "GET /api/catalog (с ключом)" "HTTP 200" "ключ не задан (--api-key)"
fi

# --- 5. Веб-интерфейс -------------------------------------------------------
# Страницы /ui/* без сессии отдают 302 на /auth/login?next=… (hub/web.py: login_redirect).
for ui_path in /ui/connections /ui/servers/gitlab; do
  ui_code="$(http_code GET "$HUB_BASE$ui_path")"
  ui_loc="$(header Location "$HUB_BASE$ui_path")"
  if [ "$ui_code" = "302" ] && [ "${ui_loc#/auth/login}" != "$ui_loc" ]; then
    add_row OK "GET $ui_path без сессии" "302 на /auth/login" "HTTP 302 → $ui_loc"
  else
    add_row FAIL "GET $ui_path без сессии" "302 на /auth/login" "HTTP $ui_code → ${ui_loc:-—}"
  fi
done

# --- 6. Метрики -------------------------------------------------------------
metrics_out="$(body GET "$HUB_BASE/metrics")"
metrics_n="$(printf '%s\n' "$metrics_out" | grep -c '^hub_')"
if [ "${metrics_n:-0}" -gt 0 ]; then
  add_row OK "GET /metrics" "серии hub_*" "$metrics_n строк"
else
  add_row FAIL "GET /metrics" "серии hub_*" "нет серий hub_*"
fi

# --- 7. ТЭГ-MCP (профиль tag) -----------------------------------------------
if [ -n "$TAG_BASE" ]; then
  check_code "tag-mcp GET /health" 200 GET "$TAG_BASE/health"
  check_code "tag-mcp PRM" 200 GET "$TAG_BASE/.well-known/oauth-protected-resource"
else
  add_row SKIP "tag-mcp /health" "HTTP 200" "адрес не задан (--tag-base)"
  add_row SKIP "tag-mcp PRM" "HTTP 200" "адрес не задан (--tag-base)"
fi

# --- 8. Проверки, обращающиеся в LiteLLM (только с --external) ---------------
# POST /cli/start и GET /auth/login при HUB_WEB_AUTH=litellm ходят в настоящий LiteLLM;
# без сетевого доступа к нему они дают 502, поэтому по умолчанию пропускаются.
if [ "$SMOKE_EXTERNAL" = "1" ]; then
  check_code "POST /cli/start" 200 POST "$HUB_BASE/cli/start" \
    -H 'Content-Type: application/json' -d '{"client":"smoke"}'
  check_code "GET /auth/login" 200,302 GET "$HUB_BASE/auth/login"
else
  add_row SKIP "POST /cli/start" "HTTP 200" "нужен --external (ходит в LiteLLM)"
  add_row SKIP "GET /auth/login" "HTTP 200 или 302" "нужен --external (ходит в LiteLLM)"
fi

# --- Итог -------------------------------------------------------------------
# pad <строка> <ширина> — выравнивание по числу символов (printf считает байты,
# а в названиях проверок кириллица).
pad() {
  local s="$1" w="$2" n stripped
  # ${#s} в локали C считает байты: убираем продолжающие байты UTF-8, тогда
  # длина совпадает с числом символов в любой локали.
  stripped="${s//[$'\x80'-$'\xbf']/}"
  n=$((w - ${#stripped}))
  if [ "$n" -gt 0 ]; then
    printf '%s%*s' "$s" "$n" ''
  else
    printf '%s' "$s"
  fi
}

W1=6
W2=44
W3=30
{
  printf '%s | %s | %s | %s\n' "$(pad ИТОГ $W1)" "$(pad ПРОВЕРКА $W2)" "$(pad ОЖИДАНИЕ $W3)" "ФАКТ"
  printf '%s-+-%s-+-%s-+-%s\n' \
    "$(pad '' $W1 | tr ' ' '-')" "$(pad '' $W2 | tr ' ' '-')" \
    "$(pad '' $W3 | tr ' ' '-')" "--------------------"
  for row in "${ROWS[@]}"; do
    IFS='|' read -r status name want got <<<"$row"
    printf '%s | %s | %s | %s\n' "$(pad "$status" $W1)" "$(pad "$name" $W2)" "$(pad "$want" $W3)" "$got"
  done
}
echo
echo "Успешно: $PASS, провалено: $FAIL, пропущено: $SKIP"

if [ "$FAIL" -gt 0 ]; then
  echo "РЕЗУЛЬТАТ: ПРОВАЛ" >&2
  exit 1
fi
echo "РЕЗУЛЬТАТ: OK"
exit 0
