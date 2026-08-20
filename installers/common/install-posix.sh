#!/usr/bin/env bash
#
# installers/common/install-posix.sh
# Единственная реализация установщика OpenCode Magnit для macOS и Linux (N5-P1).
# Лончеры installers/macos/install.sh и installers/linux/install.sh логики не содержат:
# они лишь задают OPENCODE_INSTALLER_PLATFORM и передают сюда управление.
#
# Правила: installers/docs/spec.md — N5-P*, N5-I*, N5-U*, N5-R*, N5-C*, N5-T5, N5-T6.
# Совместимость: bash 3.2 (N5-T5) — без ассоциативных массивов, без чтения файла в массив
# одной командой, без смены регистра подстановкой параметра, без ссылочных локальных переменных.
# Оффлайн (N5-P6): загрузчиков и пакетных менеджеров здесь нет.
#
# Файл можно загрузить через `source` без выполнения действий, задав
# OPENCODE_INSTALLER_SOURCE_ONLY=1 — так тесты получают доступ к константам вывода и функциям
# (единый источник строк, N5-C2).

if [ "${OPENCODE_INSTALLER_SOURCE_ONLY:-0}" != "1" ]; then
  set -euo pipefail
fi

# =====================================================================================
# Константы: коды выхода (N5-I1)
# =====================================================================================
EX_OK=0
EX_FAIL=1
EX_ARGS=2
EX_ENV=3
EX_HASH=4
EX_PERM=5
# shellcheck disable=SC2034
# Обоснование: код 6 «файл занят» — часть общего контракта N5-I1; на POSIX он не возникает,
# но константа держится рядом с остальными кодами как единый источник значений для документации.
EX_BUSY=6
EX_DIFF=7

# =====================================================================================
# Константы: имена и маркеры
# =====================================================================================
OM_PRODUCT="opencode-magnit"
OM_SCHEMA="1"
OM_BLOCK_BEGIN="# >>> opencode-magnit >>>"
OM_BLOCK_END="# <<< opencode-magnit <<<"
OM_PROFILE_BAK_SUFFIX=".opencode-magnit.bak"
OM_ENV_NAME="NODE_EXTRA_CA_CERTS"

# Управляющие символы, недопустимые в пути: блок профиля построчный (N5-I5, N5-I13).
OM_LF=$'\n'
OM_CR=$'\r'

# =====================================================================================
# Константы: строки вывода — единый источник для тестов (N5-C2, N5-I12, N5-T3).
# Формулировки — часть контракта: правятся здесь, в тестах и в CI одновременно.
# =====================================================================================

# --- отчёт установки (N5-I12)
MSG_DONE='Готово: OpenCode %s установлен.'
MSG_SUM_BIN='  Бинарник: %s'
MSG_SUM_CA='  CA: %s'
MSG_SUM_DESKTOP='  Desktop: %s'
MSG_SUM_CONFIG='  Конфиг: %s (не изменялся)'
MSG_SUM_HUB='  Hub: %s'
MSG_NEXT='Дальше:'
MSG_NEXT_TERMINAL='  1. Откройте новый терминал (или выполните: source %s)'
MSG_NEXT_RUN='  2. Запустите: opencode'
MSG_NEXT_SSO='  Вход по корпоративному SSO предложат при первом запуске.'

# --- строки шагов установки
MSG_CA_SAME='CA: уже установлен'
MSG_CA_INSTALLED='CA: установлен (%s)'
MSG_CA_REPLACED='CA: обновлён (%s)'
MSG_BIN_SAME='Бинарник: уже установлен (%s)'
MSG_BIN_OTHER='Бинарник: другая версия (установлена %s, в пакете %s)'
MSG_BIN_INSTALLED='Бинарник: установлен (%s)'
MSG_BIN_BACKUP='Бинарник: предыдущая версия сохранена (%s)'
MSG_PROFILE_UPDATED='Профиль: блок обновлён (%s)'
MSG_PROFILE_SAME='Профиль: изменений не требуется (%s)'
MSG_PROFILE_BACKUP='Профиль: резервная копия (%s)'
MSG_PATH_ADDED='PATH: добавлен %s'
MSG_WARN_FOREIGN_ENV='Предупреждение: %s, строка %s задаёт %s другим значением — строка не изменена'
MSG_NOTE_GUI='Примечание: переменные окружения для GUI-приложений не задаются; Desktop берёт CA из каталога конфига'
MSG_CONFIG_KEPT='Конфиг: %s (не изменялся)'
MSG_CONFIG_BACKUP='Конфиг: создана резервная копия (%s)'
MSG_CONFIG_BACKUP_KEPT='Конфиг: резервная копия уже существует (%s)'

# --- Desktop (N5-I9, N5-I10)
MSG_DESKTOP_NONE='Desktop: не входит в пакет'
MSG_DESKTOP_SKIPPED='Desktop: пропущен (--no-desktop)'
MSG_DESKTOP_SAME='Desktop: уже установлен'
MSG_DESKTOP_INSTALLED='Desktop: установлен (%s)'
MSG_DESKTOP_MANUAL_AT='Desktop: запустите установщик вручную: %s'
MSG_DESKTOP_ERROR='Desktop: ошибка (код %s)'
MSG_DESKTOP_USER_DIR='Предупреждение: нет прав на %s, Desktop установлен в %s'

# --- --check (N5-C2), построчный контракт
MSG_CHECK_PKG='Пакет: opencode-magnit %s (%s-%s)'
MSG_CHECK_MANIFEST='Манифест: корректен'
MSG_CHECK_BIN_OK='Бинарник: установлен %s (%s)'
MSG_CHECK_BIN_OTHER='Бинарник: другая версия (установлена %s, в пакете %s)'
MSG_CHECK_BIN_NOVER='Бинарник: установлен, версия не определена (%s)'
MSG_CHECK_BIN_NONE='Бинарник: не установлен'
MSG_CHECK_PATH_YES='PATH: содержит %s'
MSG_CHECK_PATH_NO='PATH: не содержит %s'
MSG_CHECK_CA_OK='CA: установлен (%s)'
MSG_CHECK_CA_DIFF='CA: отличается от пакета (%s)'
MSG_CHECK_CA_NONE='CA: не установлен'
MSG_CHECK_ENV_OK='NODE_EXTRA_CA_CERTS: задан (%s)'
MSG_CHECK_ENV_OTHER='NODE_EXTRA_CA_CERTS: задан другой путь (%s)'
MSG_CHECK_ENV_NONE='NODE_EXTRA_CA_CERTS: не задан'
MSG_CHECK_DESKTOP_OK='Desktop: установлен (%s)'
MSG_CHECK_DESKTOP_NONE='Desktop: не установлен'
MSG_CHECK_CONFIG='Конфиг: %s (не изменяется установщиком)'
MSG_CHECK_HUB='Hub: %s'
MSG_CHECK_SUM_OK='Итог: всё установлено'
MSG_CHECK_SUM_NEED='Итог: требуется установка'

# --- --dry-run (N5-C1)
MSG_PLAN_HEAD='План установки OpenCode %s (%s-%s) — изменения не вносятся:'
MSG_PLAN_UNINSTALL_HEAD='План удаления OpenCode %s — изменения не вносятся:'
MSG_PLAN_STEP='  %s. %s'
MSG_PLAN_MKDIR='Создать каталог: %s'
MSG_PLAN_CA='Скопировать CA: %s -> %s'
MSG_PLAN_BIN='Скопировать бинарник: %s -> %s (через %s, chmod 0755)'
MSG_PLAN_BIN_BACKUP='Сохранить прежний бинарник: %s -> %s'
MSG_PLAN_PROFILE='Обновить блок в профиле: %s (%s=%s)'
MSG_PLAN_PATH='Добавить в PATH через профиль: %s'
MSG_PLAN_DESKTOP_DMG='Установить Desktop: %s -> %s/%s'
MSG_PLAN_CONFIG='Конфиг: %s не изменяется; резервная копия %s'
MSG_PLAN_CORP_STATUS='Показать состояние: %s corp status'
MSG_PLAN_REMOVE='Удалить: %s'
MSG_PLAN_REMOVE_BLOCK='Удалить блок из профиля: %s'
MSG_PLAN_ENV_UNSET='Снять переменную %s (значение наше)'

# --- удаление (N5-R1…N5-R3)
MSG_UNINSTALL_HEAD='Удаление OpenCode Magnit %s:'
MSG_UNINSTALL_REMOVED='%s: удалён (%s)'
MSG_UNINSTALL_ABSENT='%s: не найден'
MSG_UNINSTALL_FAILED='%s: не удалось удалить (%s)'
MSG_UNINSTALL_DONE='Готово: OpenCode удалён.'
MSG_PURGE_HEAD='Удаляются пользовательские данные:'
MSG_PURGE_ITEM='  %s'
MSG_PURGE_REMOVED='Данные: удалено (%s)'
MSG_PURGE_ABSENT='Данные: не найдено (%s)'
OBJ_BIN='Бинарник'
OBJ_BIN_BAK='Резервная копия бинарника'
OBJ_PROFILE='Блок в профиле'
OBJ_CA='CA'
OBJ_DESKTOP='Desktop'

# --- ошибки
MSG_ERR_UNKNOWN_ARG='Неизвестный аргумент: %s'
MSG_ERR_NEED_VALUE='Аргумент %s требует значения'
MSG_ERR_EXCLUSIVE='Взаимоисключающие режимы: %s'
MSG_ERR_PURGE_ALONE='--purge применяется только вместе с --uninstall'
MSG_ERR_MANIFEST_MISSING='Манифест не найден: %s'
MSG_ERR_MANIFEST_PARSE='Манифест не разбирается как JSON: %s'
MSG_ERR_MANIFEST_FIELD='Манифест %s: поле %s — %s'
MSG_ERR_FIELD_REQUIRED='обязательное поле отсутствует или пусто'
MSG_ERR_FIELD_SCHEMA='ожидается значение 1'
MSG_ERR_FIELD_PRODUCT='ожидается значение opencode-magnit'
MSG_ERR_FIELD_ENUM='недопустимое значение: %s'
MSG_ERR_FIELD_SHA='требуется 64 символа [0-9a-f]'
MSG_ERR_FIELD_PATH='недопустимый путь: %s'
MSG_ERR_FIELD_NOFILE='файл отсутствует в пакете: %s'
MSG_ERR_FIELD_CLI='требуется ровно один артефакт kind="cli", найдено %s'
MSG_ERR_FIELD_DESKTOP='допускается не более одного артефакта kind="desktop", найдено %s'
MSG_ERR_HASH_MISMATCH='Несовпадение sha256: %s (ожидался %s, фактический %s)'
MSG_ERR_NO_HASH_TOOL='Не найдена утилита вычисления sha256: sha256sum, shasum -a 256, openssl dgst -sha256'
MSG_ERR_OS_MISMATCH='Пакет собран для %s-%s, система — %s-%s'
MSG_ERR_OS_UNSUPPORTED='Неподдерживаемая система: uname -s = %s'
MSG_ERR_ARCH_UNSUPPORTED='Неподдерживаемая архитектура: uname -m = %s'
MSG_ERR_NO_WRITE='Нет прав на запись в каталог: %s'
MSG_ERR_NO_WRITE_HINT='Запустите установщик от имени владельца каталога или укажите --prefix <каталог>'
MSG_ERR_NEED_ROOT="нужны права root: запустите под root или без --system (установка в \$HOME/.local/bin)"
MSG_ERR_PURGE_UNSAFE='Небезопасный путь в purge_paths: %s (допустимы только пути внутри домашнего каталога)'
MSG_ERR_PURGE_REJECTED='Список purge_paths отвергнут целиком, ничего не удалено'
MSG_ERR_PATH_UNSAFE='Небезопасный путь (%s): %s'
MSG_VERSION_LINE='opencode-magnit %s (%s-%s)'

# =====================================================================================
# Состояние (значения по умолчанию)
# =====================================================================================
opt_dry_run=0
opt_check=0
opt_uninstall=0
opt_purge=0
opt_system=0
opt_prefix=""
opt_no_desktop=0
opt_no_launch=0
opt_quiet=0
opt_help=0
opt_show_version=0

pkg_root=""
manifest_path=""
platform=""
sys_os=""
sys_arch=""
hash_tool=""
config_dir=""
bin_dir=""
bin_target=""
bin_backup=""
ca_target=""
profile_file=""
profile_is_fish=0
use_sudo=0
plan_step=0
check_diff=0
desktop_summary=""
desktop_failed=0
profile_did_change=0

MANIFEST_KV=""
MF_version=""
MF_os=""
MF_arch=""
MF_hub_url=""
MF_ca_file=""
MF_ca_sha=""
MF_ca_name=""
MF_cli_file=""
MF_cli_sha=""
MF_cli_name=""
MF_desktop_file=""
MF_desktop_sha=""
MF_desktop_type=""
MF_desktop_app=""

# =====================================================================================
# Вывод
# =====================================================================================

# Форматирование без перевода строки. Единственная точка, где формат printf приходит переменной:
# шаблоны — константы из блока строк выше (единый источник строк для тестов, N5-C2).
fmt() {
  local f=$1
  shift
  # shellcheck disable=SC2059
  # Обоснование: формат намеренно передаётся константой-шаблоном, определённой в этом же файле.
  printf -- "$f" "$@"
}

_render() {
  local tpl=$1
  shift
  # shellcheck disable=SC2059
  # Обоснование: шаблоны вывода вынесены в константы (единый источник строк для тестов N5-C2),
  # поэтому формат printf приходит переменной. Все шаблоны заданы в этом файле, не извне.
  printf -- "$tpl\n" "$@"
}

out() { _render "$@"; }

say() {
  if [ "$opt_quiet" -eq 0 ]; then
    _render "$@"
  fi
}

die() {
  local code=$1
  shift
  _render "$@" >&2
  exit "$code"
}

print_help() {
  cat <<'HELP'
Установщик OpenCode Magnit (macOS/Linux).

Использование: install.sh [флаги]

Флаги:
  --dry-run          напечатать план шагов, ничего не менять
  --check            проверить состояние среды, ничего не менять
  --uninstall        удалить установленное
  --purge            вместе с --uninstall — удалить и пользовательские данные
  --system           ставить в /usr/local/bin (привилегированные шаги — через sudo)
  --prefix <каталог> корень установки: <каталог>/bin
  --no-desktop       пропустить шаг Desktop
  --no-launch        не выполнять финальный `opencode corp status`
  --quiet            печатать только итог и ошибки
  --help             показать эту справку
  --version          напечатать версию пакета из манифеста

Коды выхода: 0 успех, 1 ошибка выполнения, 2 аргументы или манифест, 3 несовместимая среда,
4 несовпадение sha256, 5 нет прав на запись, 6 файл занят, 7 --check нашёл расхождения.

Установщик работает только с файлами пакета: ничего не загружает и ключей не запрашивает.
HELP
}

# =====================================================================================
# Разбор аргументов (N5-I1)
# =====================================================================================
parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      --dry-run) opt_dry_run=1 ;;
      --check) opt_check=1 ;;
      --uninstall) opt_uninstall=1 ;;
      --purge) opt_purge=1 ;;
      --system) opt_system=1 ;;
      --prefix)
        if [ $# -lt 2 ]; then
          print_help >&2
          die "$EX_ARGS" "$MSG_ERR_NEED_VALUE" "--prefix"
        fi
        opt_prefix=$2
        shift
        ;;
      --prefix=*) opt_prefix=${1#--prefix=} ;;
      --no-desktop) opt_no_desktop=1 ;;
      --no-launch) opt_no_launch=1 ;;
      --quiet) opt_quiet=1 ;;
      --help|-h) opt_help=1 ;;
      --version) opt_show_version=1 ;;
      *)
        print_help >&2
        die "$EX_ARGS" "$MSG_ERR_UNKNOWN_ARG" "$1"
        ;;
    esac
    shift
  done

  if [ "$opt_dry_run" -eq 1 ] && [ "$opt_check" -eq 1 ]; then
    die "$EX_ARGS" "$MSG_ERR_EXCLUSIVE" "--dry-run, --check"
  fi
  if [ "$opt_check" -eq 1 ] && [ "$opt_uninstall" -eq 1 ]; then
    die "$EX_ARGS" "$MSG_ERR_EXCLUSIVE" "--check, --uninstall"
  fi
  if [ "$opt_system" -eq 1 ] && [ -n "$opt_prefix" ]; then
    die "$EX_ARGS" "$MSG_ERR_EXCLUSIVE" "--system, --prefix"
  fi
  if [ "$opt_purge" -eq 1 ] && [ "$opt_uninstall" -eq 0 ]; then
    die "$EX_ARGS" "$MSG_ERR_PURGE_ALONE"
  fi
}

# =====================================================================================
# Разбор JSON без внешних зависимостей (N5-P2: базовые утилиты — awk)
# Печатает строки "<путь><TAB><значение>"; для массивов дополнительно "<путь>.__len<TAB><N>".
# Ненулевой код возврата — документ не является корректным JSON.
# =====================================================================================
json_flatten() {
  awk '
    function ws(   c) {
      while (pos <= dlen) {
        c = substr(doc, pos, 1)
        if (c == " " || c == "\t" || c == "\n" || c == "\r") { pos++ } else { return }
      }
    }
    function emit(k, v) {
      gsub(/[\n\r\t]/, " ", k)
      gsub(/[\n\r\t]/, " ", v)
      print k "\t" v
    }
    function pstring(   res, c) {
      pos++
      res = ""
      while (pos <= dlen) {
        c = substr(doc, pos, 1)
        if (c == "\\") {
          pos++
          c = substr(doc, pos, 1)
          if (c == "n") { res = res " " }
          else if (c == "t") { res = res " " }
          else if (c == "r") { res = res " " }
          else if (c == "b" || c == "f") { res = res " " }
          else if (c == "u") { res = res "?"; pos = pos + 4 }
          else { res = res c }
          pos++
        } else if (c == "\"") {
          pos++
          return res
        } else {
          res = res c
          pos++
        }
      }
      fail = "unterminated string"
      return res
    }
    function pvalue(path,   c, num, str) {
      if (fail != "") { return }
      ws()
      if (pos > dlen) { fail = "unexpected end"; return }
      c = substr(doc, pos, 1)
      if (c == "{") { pobject(path); return }
      if (c == "[") { parray(path); return }
      if (c == "\"") { str = pstring(); if (fail == "") { emit(path, str) } ; return }
      if (substr(doc, pos, 4) == "true") { pos = pos + 4; emit(path, "true"); return }
      if (substr(doc, pos, 5) == "false") { pos = pos + 5; emit(path, "false"); return }
      if (substr(doc, pos, 4) == "null") { pos = pos + 4; emit(path, ""); return }
      num = ""
      while (pos <= dlen) {
        c = substr(doc, pos, 1)
        if (c ~ /[-+0-9eE.]/) { num = num c; pos++ } else { break }
      }
      if (num == "") { fail = "unexpected character"; return }
      emit(path, num)
    }
    function pobject(path,   key, c, prefix) {
      pos++
      ws()
      if (substr(doc, pos, 1) == "}") { pos++; return }
      while (1) {
        ws()
        if (substr(doc, pos, 1) != "\"") { fail = "expected key"; return }
        key = pstring()
        if (fail != "") { return }
        ws()
        if (substr(doc, pos, 1) != ":") { fail = "expected colon"; return }
        pos++
        if (path == "") { prefix = key } else { prefix = path "." key }
        pvalue(prefix)
        if (fail != "") { return }
        ws()
        c = substr(doc, pos, 1)
        if (c == ",") { pos++; continue }
        if (c == "}") { pos++; return }
        fail = "expected comma or brace"
        return
      }
    }
    function parray(path,   i, c) {
      pos++
      i = 0
      ws()
      if (substr(doc, pos, 1) == "]") { pos++; emit(path ".__len", 0); return }
      while (1) {
        pvalue(path "." i)
        if (fail != "") { return }
        i++
        ws()
        c = substr(doc, pos, 1)
        if (c == ",") { pos++; continue }
        if (c == "]") { pos++; emit(path ".__len", i); return }
        fail = "expected comma or bracket"
        return
      }
    }
    { doc = doc $0 "\n" }
    END {
      dlen = length(doc)
      pos = 1
      fail = ""
      ws()
      if (substr(doc, pos, 1) != "{") { exit 3 }
      pobject("")
      if (fail == "") {
        ws()
        if (pos <= dlen) { fail = "trailing data" }
      }
      if (fail != "") { exit 3 }
    }
  ' "$1"
}

mf_get() {
  local want=$1 k v
  while IFS=$'\t' read -r k v; do
    if [ "$k" = "$want" ]; then
      printf '%s' "$v"
      return 0
    fi
  done <<EOF
$MANIFEST_KV
EOF
  return 1
}

mf_has() {
  mf_get "$1" >/dev/null
}

mf_val() {
  mf_get "$1" || printf ''
}

# =====================================================================================
# Валидация манифеста (N5-P4)
# =====================================================================================
manifest_field_error() {
  die "$EX_ARGS" "$MSG_ERR_MANIFEST_FIELD" "$manifest_path" "$1" "$2"
}

is_sha256() {
  if [ "${#1}" -ne 64 ]; then
    return 1
  fi
  case $1 in
    *[!0-9a-fA-F]*) return 1 ;;
  esac
  return 0
}

# =====================================================================================
# Пути: единая нормализация перед любой файловой операцией (N5-P3, N5-P4, N5-R2)
# =====================================================================================
# Три уязвимости этого установщика подряд (обход каталога через app_name, пустое app_name,
# "${HOME}//" в purge_paths) имели одну общую причину: проверка безопасности выполнялась над
# сырой строкой, а файловая операция — над строкой, которую ядро нормализует само ("//" = "/",
# "/./" = "/", завершающий "/" отбрасывается). Поэтому нормализация вынесена в одну функцию:
# через неё обязан пройти КАЖДЫЙ путь до попадания в mkdir/cp/mv/rm/ditto, и все проверки
# ведутся уже над её результатом, а не над исходным текстом.
#
# path_normalize <путь> [база]:
#   - разворачивает ведущие "~" и "~/" в $HOME;
#   - относительный путь достраивает от «базы»; без базы относительный путь отвергается,
#     то есть результат всегда абсолютный;
#   - схлопывает повторяющиеся "/", отбрасывает сегменты "." и завершающие "/";
#   - отвергает любой сегмент ".." — лексический «подъём» здесь намеренно не выполняется:
#     он маскирует намерение вызывающего и на симлинках расходится с поведением ядра;
#   - отвергает путь с переводом строки или возвратом каретки: блок профиля построчный и его
#     маркеры распознаются по целой строке (N5-I5), поэтому такой путь не экранируется, а
#     признаётся небезопасным — вызывающий path_require превращает это в код 2 (N5-I13);
#   - печатает нормализованный абсолютный путь; код 1 — путь непригоден.
path_normalize() {
  local p=$1 base=${2:-} rest seg result='' tilde
  [ -n "$p" ] || return 1
  case $p in
    *"$OM_LF"*|*"$OM_CR"*) return 1 ;;
  esac
  tilde=$(printf '\176')
  case $p in
    "$tilde") p=$HOME ;;
    "$tilde/"*) p="$HOME/${p#"$tilde/"}" ;;
  esac
  case $p in
    /*) : ;;
    *)
      [ -n "$base" ] || return 1
      p="$base/$p"
      ;;
  esac
  case $p in
    /*) : ;;
    *) return 1 ;;
  esac
  rest=${p#/}
  while [ -n "$rest" ]; do
    case $rest in
      */*)
        seg=${rest%%/*}
        rest=${rest#*/}
        ;;
      *)
        seg=$rest
        rest=''
        ;;
    esac
    case $seg in
      ''|.) continue ;;
      ..) return 1 ;;
    esac
    result="$result/$seg"
  done
  if [ -z "$result" ]; then
    printf '/'
  else
    printf '%s' "$result"
  fi
}

# Строгая вложенность: child обязан лежать НИЖЕ parent — не совпадать с ним и не быть его
# родителем. Оба аргумента обязаны быть результатом path_normalize, иначе шаблон "$parent"/?*
# снова засчитает второй разделитель за содержательный символ (именно так обходилась проверка
# purge_paths).
path_is_inside() {
  local parent=$1 child=$2
  [ -n "$parent" ] || return 1
  [ -n "$child" ] || return 1
  [ "$child" != "$parent" ] || return 1
  case $parent in
    /) case $child in /?*) return 0 ;; esac ;;
    *) case $child in "$parent"/?*) return 0 ;; esac ;;
  esac
  return 1
}

# Обязательный путь: результат кладётся в глобальную REPLY — die внутри $( ) завершил бы только
# подоболочку и оставил вызывающего работать с непроверенным значением.
path_require() {
  local value=$1 what=$2 base=${3:-}
  if ! REPLY=$(path_normalize "$value" "$base"); then
    die "$EX_ARGS" "$MSG_ERR_PATH_UNSAFE" "$what" "$value"
  fi
}

# Объект строго внутри уже нормализованного каталога: "<dir>/<name>" плюс проверка вложенности.
path_require_child() {
  local dir=$1 name=$2 what=$3
  path_require "$dir/$name" "$what"
  if ! path_is_inside "$dir" "$REPLY"; then
    die "$EX_ARGS" "$MSG_ERR_PATH_UNSAFE" "$what" "$name"
  fi
}

# Форма пути внутри пакета: относительный, с разделителем "/", без "..", без буквы диска
# (N5-P3). Дополнительно отвергаются формы, бессмысленные как элемент внутри пакета и опасные
# при подстановке в файловые операции: "." и "./x" (самоссылка) и завершающий "/" — иначе
# "<каталог>/<значение>" схлопывается в сам каталог, а не в объект внутри него.
# Набор допустимых символов проверяется отдельно: у пути внутри пакета и у имени приложения он
# разный (пробел допустим только во втором).
is_safe_path_shape() {
  local p=$1
  [ -n "$p" ] || return 1
  case $p in
    /*) return 1 ;;
    *\\*) return 1 ;;
    ..|../*|*/../*|*/..) return 1 ;;
    .|./*|*/.|*/./*) return 1 ;;
    */) return 1 ;;
    [A-Za-z]:*) return 1 ;;
  esac
  return 0
}

# Белый список символов (N5-P4, N5-I13, ревизия 1.5). Значения ca.install_name,
# artifacts[].install_name и artifacts[].app_name попадают не только в файловые операции, но и в
# ГЕНЕРИРУЕМЫЙ КОД файла профиля (N5-I5), который оболочка пользователя исполняет при входе.
# Поэтому набор символов задан перечислением разрешённого, а не запрещённого: перечислять
# метасимволы поимённо — значит гарантированно что-то забыть, а забытый символ снова попадает в
# профиль. Диапазоны A-Z/a-z/0-9 раскрыты посимвольно намеренно: в bash bracket-выражение
# сравнивает по правилам локали, и "[A-Za-z]" в отдельных локалях захватывает буквы с
# диакритикой. Любой символ вне списка (в т.ч. управляющий, 0x7f и любой байт >= 0x80) даёт
# отказ.
is_safe_pkg_path() {
  local p=$1
  [ -n "$p" ] || return 1
  case $p in
    *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/-]*) return 1 ;;
  esac
  is_safe_path_shape "$p"
}

# Имя приложения (artifacts[].app_name) подставляется в "<dest>/<app_name>" и участвует в
# rm -rf (при --system — через sudo), поэтому проверяется строже пути внутри пакета: это ровно
# одно имя каталога — непустое, без разделителей пути и без самоссылок (N5-P4).
# Отличие набора символов от is_safe_pkg_path ровно одно: допускается пробел U+0020, но не
# первым и не последним символом («OpenCode Magnit.app»); разделитель "/" не допускается.
is_safe_app_name() {
  local p=$1
  # Пробельное значение равносильно пустому: "<dest>/   " — не объект внутри каталога назначения,
  # а мусор, попадание которого в rm -rf/ditto недопустимо. Проверка стоит здесь, а не только в
  # manifest_load, поэтому вторая линия защиты (desktop_target_path, find_installed_desktop) не
  # слабее первой, а комментарий про паритет с Test-AppName (IsNullOrWhiteSpace) стал истинным.
  [ -n "${p//[[:space:]]/}" ] || return 1
  case $p in
    *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._\ -]*) return 1 ;;
  esac
  case $p in
    ' '*|*' ') return 1 ;;
    */*) return 1 ;;
    .|..) return 1 ;;
  esac
  is_safe_path_shape "$p"
}

# Результат кладётся в глобальную REPLY: die внутри $( ) завершил бы только подоболочку.
mf_require() {
  local key=$1 name=$2
  REPLY=$(mf_val "$key")
  if [ -z "$REPLY" ]; then
    manifest_field_error "$name" "$MSG_ERR_FIELD_REQUIRED"
  fi
}

validate_pkg_file() {
  local value=$1 name=$2
  if ! is_safe_pkg_path "$value"; then
    manifest_field_error "$name" "$(fmt "$MSG_ERR_FIELD_PATH" "$value")"
  fi
  if [ ! -f "$pkg_root/$value" ]; then
    manifest_field_error "$name" "$(fmt "$MSG_ERR_FIELD_NOFILE" "$value")"
  fi
}

validate_sha_field() {
  local value=$1 name=$2
  if ! is_sha256 "$value"; then
    manifest_field_error "$name" "$MSG_ERR_FIELD_SHA"
  fi
}

to_lower() {
  printf '%s' "$1" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz'
}

manifest_load() {
  local raw="" value idx count kinds cli_count desktop_count kind

  manifest_path="$pkg_root/common/manifest.json"
  if [ ! -f "$manifest_path" ]; then
    die "$EX_ARGS" "$MSG_ERR_MANIFEST_MISSING" "$manifest_path"
  fi
  if ! raw=$(json_flatten "$manifest_path" 2>/dev/null); then
    die "$EX_ARGS" "$MSG_ERR_MANIFEST_PARSE" "$manifest_path"
  fi
  MANIFEST_KV=$raw

  value=$(mf_val "schema")
  if [ "$value" != "$OM_SCHEMA" ]; then
    manifest_field_error "schema" "$MSG_ERR_FIELD_SCHEMA"
  fi
  value=$(mf_val "product")
  if [ "$value" != "$OM_PRODUCT" ]; then
    manifest_field_error "product" "$MSG_ERR_FIELD_PRODUCT"
  fi

  mf_require "version" "version"; MF_version=$REPLY
  mf_require "os" "os"; MF_os=$REPLY
  mf_require "arch" "arch"; MF_arch=$REPLY
  mf_require "hub_url" "hub_url"; MF_hub_url=$REPLY
  mf_require "built_at" "built_at"
  mf_require "source_release" "source_release"

  case $MF_os in
    darwin|linux|windows) : ;;
    *) manifest_field_error "os" "$(fmt "$MSG_ERR_FIELD_ENUM" "$MF_os")" ;;
  esac
  case $MF_arch in
    arm64|x64) : ;;
    *) manifest_field_error "arch" "$(fmt "$MSG_ERR_FIELD_ENUM" "$MF_arch")" ;;
  esac

  mf_require "ca.file" "ca.file"; MF_ca_file=$REPLY
  mf_require "ca.sha256" "ca.sha256"; MF_ca_sha=$(to_lower "$REPLY")
  mf_require "ca.install_name" "ca.install_name"; MF_ca_name=$REPLY
  validate_sha_field "$MF_ca_sha" "ca.sha256"
  validate_pkg_file "$MF_ca_file" "ca.file"
  if ! is_safe_pkg_path "$MF_ca_name"; then
    manifest_field_error "ca.install_name" "$(fmt "$MSG_ERR_FIELD_PATH" "$MF_ca_name")"
  fi

  count=$(mf_val "artifacts.__len")
  if [ -z "$count" ]; then
    manifest_field_error "artifacts" "$MSG_ERR_FIELD_REQUIRED"
  fi
  cli_count=0
  desktop_count=0
  idx=0
  while [ "$idx" -lt "$count" ]; do
    kind=$(mf_val "artifacts.$idx.kind")
    value=$(mf_val "artifacts.$idx.file")
    if [ -z "$kind" ]; then
      manifest_field_error "artifacts.$idx.kind" "$MSG_ERR_FIELD_REQUIRED"
    fi
    if [ -z "$value" ]; then
      manifest_field_error "artifacts.$idx.file" "$MSG_ERR_FIELD_REQUIRED"
    fi
    validate_pkg_file "$value" "artifacts.$idx.file"
    kinds=$(to_lower "$(mf_val "artifacts.$idx.sha256")")
    validate_sha_field "$kinds" "artifacts.$idx.sha256"
    if [ "$kind" = "cli" ]; then
      cli_count=$((cli_count + 1))
      MF_cli_file=$value
      MF_cli_sha=$kinds
      MF_cli_name=$(mf_val "artifacts.$idx.install_name")
      if [ -z "$MF_cli_name" ]; then
        manifest_field_error "artifacts.$idx.install_name" "$MSG_ERR_FIELD_REQUIRED"
      fi
      if ! is_safe_pkg_path "$MF_cli_name"; then
        manifest_field_error "artifacts.$idx.install_name" "$(fmt "$MSG_ERR_FIELD_PATH" "$MF_cli_name")"
      fi
    elif [ "$kind" = "desktop" ]; then
      desktop_count=$((desktop_count + 1))
      MF_desktop_file=$value
      MF_desktop_sha=$kinds
      MF_desktop_type=$(mf_val "artifacts.$idx.installer_type")
      MF_desktop_app=$(mf_val "artifacts.$idx.app_name")
      # app_name попадает в файловые операции (rm -rf в /Applications или $dest, при --system —
      # через sudo), поэтому проходит проверку строже, чем file/install_name: одно имя каталога,
      # запрет "..", ведущего "/", обратного слэша, буквы диска, "." и разделителей пути.
      # Для installer_type="dmg" поле ОБЯЗАТЕЛЬНО и непусто (пробельное значение равносильно
      # пустому): install_desktop подставляет его в rm -rf "<dest>/<app_name>", и пустое значение
      # означало бы удаление самого каталога /Applications. Проверка выполняется в manifest_load,
      # то есть до любой файловой операции, на всех путях исполнения: установка, --check,
      # --dry-run, --uninstall и их комбинации (N5-P4, N5-R1).
      if [ "$MF_desktop_type" = "dmg" ] && [ -z "${MF_desktop_app//[[:space:]]/}" ]; then
        manifest_field_error "artifacts.$idx.app_name" "$MSG_ERR_FIELD_REQUIRED"
      fi
      if [ -n "$MF_desktop_app" ] && ! is_safe_app_name "$MF_desktop_app"; then
        manifest_field_error "artifacts.$idx.app_name" "$(fmt "$MSG_ERR_FIELD_PATH" "$MF_desktop_app")"
      fi
    fi
    idx=$((idx + 1))
  done

  if [ "$cli_count" -ne 1 ]; then
    manifest_field_error "artifacts" "$(fmt "$MSG_ERR_FIELD_CLI" "$cli_count")"
  fi
  if [ "$desktop_count" -gt 1 ]; then
    manifest_field_error "artifacts" "$(fmt "$MSG_ERR_FIELD_DESKTOP" "$desktop_count")"
  fi

  if ! mf_has "purge_paths.__len"; then
    manifest_field_error "purge_paths" "$MSG_ERR_FIELD_REQUIRED"
  fi
}

# =====================================================================================
# Среда: ОС, архитектура, утилита хеширования (N5-I3, N5-P5)
# =====================================================================================
normalize_os() {
  case $1 in
    Darwin) printf 'darwin' ;;
    Linux) printf 'linux' ;;
    *) return 1 ;;
  esac
}

normalize_arch() {
  case $1 in
    arm64|aarch64) printf 'arm64' ;;
    x86_64|amd64) printf 'x64' ;;
    *) return 1 ;;
  esac
}

check_system() {
  local uname_s uname_m
  uname_s=$(uname -s)
  uname_m=$(uname -m)
  if ! sys_os=$(normalize_os "$uname_s"); then
    die "$EX_ENV" "$MSG_ERR_OS_UNSUPPORTED" "$uname_s"
  fi
  if ! sys_arch=$(normalize_arch "$uname_m"); then
    die "$EX_ENV" "$MSG_ERR_ARCH_UNSUPPORTED" "$uname_m"
  fi
  if [ "$MF_os" != "$sys_os" ] || [ "$MF_arch" != "$sys_arch" ]; then
    die "$EX_ENV" "$MSG_ERR_OS_MISMATCH" "$MF_os" "$MF_arch" "$sys_os" "$sys_arch"
  fi
}

detect_hash_tool() {
  if command -v sha256sum >/dev/null 2>&1; then
    hash_tool=sha256sum
  elif command -v shasum >/dev/null 2>&1; then
    hash_tool=shasum
  elif command -v openssl >/dev/null 2>&1; then
    hash_tool=openssl
  else
    return 1
  fi
  return 0
}

require_hash_tool() {
  if ! detect_hash_tool; then
    die "$EX_ENV" "$MSG_ERR_NO_HASH_TOOL"
  fi
}

sha256_of() {
  local file=$1
  case $hash_tool in
    sha256sum) sha256sum "$file" | awk '{ print $1 }' ;;
    shasum) shasum -a 256 "$file" | awk '{ print $1 }' ;;
    openssl) openssl dgst -sha256 "$file" | awk '{ print $NF }' ;;
    *) return 1 ;;
  esac
}

# Обязательная проверка хеша (N5-P5): флага пропуска не существует.
verify_hash() {
  local file=$1 expected=$2 actual
  actual=$(to_lower "$(sha256_of "$file")")
  expected=$(to_lower "$expected")
  if [ "$actual" != "$expected" ]; then
    die "$EX_HASH" "$MSG_ERR_HASH_MISMATCH" "$file" "$expected" "$actual"
  fi
}

verify_package() {
  verify_hash "$pkg_root/$MF_ca_file" "$MF_ca_sha"
  verify_hash "$pkg_root/$MF_cli_file" "$MF_cli_sha"
  if [ -n "$MF_desktop_file" ] && [ "$opt_no_desktop" -eq 0 ]; then
    verify_hash "$pkg_root/$MF_desktop_file" "$MF_desktop_sha"
  fi
}

# =====================================================================================
# Раскладка путей (N5-I4, N5-I7)
# =====================================================================================
# Каждый путь раскладки проходит через path_normalize ДО первой файловой операции: формы,
# которые ядро схлопнуло бы в родительский каталог (--prefix "~//", XDG_CONFIG_HOME="$HOME//"),
# отвергаются здесь кодом 2, а не обнаруживаются постфактум по удалённому каталогу. Целевые
# файлы дополнительно проверяются на строгую вложенность в свой каталог (defense in depth):
# install_name из манифеста уже проверен is_safe_pkg_path, но подстановка в путь не должна
# зависеть от того, что проверка выше сохранится в неизменном виде.
compute_layout() {
  local xdg_config prefix_dir
  xdg_config=${XDG_CONFIG_HOME:-$HOME/.config}
  path_require "$xdg_config/opencode" "XDG_CONFIG_HOME"
  config_dir=$REPLY
  if [ -n "$opt_prefix" ]; then
    # Относительный --prefix достраивается от текущего каталога: дальше по коду путь только
    # абсолютный и только нормализованный.
    path_require "$opt_prefix" "--prefix" "$PWD"
    prefix_dir=$REPLY
    bin_dir="$prefix_dir/bin"
  elif [ "$opt_system" -eq 1 ]; then
    bin_dir="/usr/local/bin"
  else
    bin_dir="$HOME/.local/bin"
  fi
  path_require "$bin_dir" "каталог бинарника"
  bin_dir=$REPLY
  path_require_child "$bin_dir" "$MF_cli_name" "artifacts[].install_name"
  bin_target=$REPLY
  bin_backup="$bin_target.bak"
  path_require_child "$config_dir" "$MF_ca_name" "ca.install_name"
  ca_target=$REPLY
  profile_file=$(profile_path)
}

profile_path() {
  local shell_name=${SHELL##*/}
  case $shell_name in
    zsh) printf '%s/.zshrc' "$HOME" ;;
    bash)
      if [ "$platform" = "macos" ]; then
        printf '%s/.bash_profile' "$HOME"
      else
        printf '%s/.bashrc' "$HOME"
      fi
      ;;
    fish) printf '%s/.config/fish/conf.d/opencode-magnit.fish' "$HOME" ;;
    *) printf '%s/.profile' "$HOME" ;;
  esac
}

detect_profile_kind() {
  local shell_name=${SHELL##*/}
  if [ "$shell_name" = "fish" ]; then
    profile_is_fish=1
  else
    profile_is_fish=0
  fi
}

path_contains() {
  local dir=$1
  case ":$PATH:" in
    *":$dir:"*) return 0 ;;
  esac
  return 1
}

# =====================================================================================
# Профиль shell (N5-I5)
# =====================================================================================
# Замена всех вхождений подстроки, реализованная циклом, а не через ${var//from/to}: в bash 3.2
# (N5-T5) замена с ЗАКАВЫЧЕННОЙ правой частью подставляет кавычки буквально ("a'b" → a"'\''"b),
# а с незакавыченной — теряет обратный слэш в левой части (он становится экранирующим). Ни одна
# из форм не годится сразу для апострофа и для слэша, поэтому подстановка выполняется вручную.
str_replace_all() {
  local s=$1 from=$2 to=$3 out='' head
  while :; do
    case $s in
      *"$from"*)
        head=${s%%"$from"*}
        out="$out$head$to"
        s=${s#*"$from"}
        ;;
      *)
        out="$out$s"
        break
        ;;
    esac
  done
  printf '%s' "$out"
}

# Экранирование значения для ОДИНАРНЫХ кавычек POSIX-оболочки (N5-I13): внутри '…' специальных
# символов нет вовсе, кроме самого апострофа — он закрывает кавычку, поэтому заменяется на
# '\'' (закрыть, экранированный апостроф, открыть снова). Больше ничего не заменяется, не
# удаляется и не добавляется: значение обязано вернуться из профиля побайтово тем же.
sh_quote_value() {
  str_replace_all "$1" "'" "'\\''"
}

# Экранирование значения для одинарных кавычек fish (N5-I13): внутри '…' fish обрабатывает ровно
# два символа — обратный слэш и апостроф; $, обратная кавычка, ";" и скобки специального смысла
# там не имеют и не экранируются. Порядок замен важен: сначала слэш, иначе добавленный слэш
# экранирования апострофа был бы удвоен второй заменой.
fish_quote_value() {
  local s
  # shellcheck disable=SC1003
  # Обоснование: '\' и '\\' — это ровно один и ровно два обратных слэша, а не попытка
  # экранировать апостроф; внутри одинарных кавычек экранирования нет по определению.
  s=$(str_replace_all "$1" '\' '\\')
  str_replace_all "$s" "'" "\\'"
}

# Блок профиля — это ГЕНЕРИРУЕМЫЙ КОД: оболочка пользователя исполнит его при следующем входе,
# поэтому любое значение печатается в одинарных кавычках (N5-I13). Двойные кавычки остались
# ровно в одном месте — "$PATH" в POSIX-профиле: это намеренное раскрытие переменной оболочкой
# пользователя, а не подстановка нашего значения. Пути с переводом строки сюда не доходят:
# они отвергнуты в path_normalize кодом 2.
write_block_file() {
  local dest=$1 with_path=$2
  {
    printf '%s\n' "$OM_BLOCK_BEGIN"
    if [ "$profile_is_fish" -eq 1 ]; then
      printf "set -gx %s '%s'\n" "$OM_ENV_NAME" "$(fish_quote_value "$ca_target")"
      if [ "$with_path" -eq 1 ]; then
        printf "fish_add_path '%s'\n" "$(fish_quote_value "$bin_dir")"
      fi
    else
      printf "export %s='%s'\n" "$OM_ENV_NAME" "$(sh_quote_value "$ca_target")"
      if [ "$with_path" -eq 1 ]; then
        printf "export PATH='%s':\"\$PATH\"\n" "$(sh_quote_value "$bin_dir")"
      fi
    fi
    printf '%s\n' "$OM_BLOCK_END"
  } >"$dest"
}

profile_has_block() {
  [ -f "$1" ] || return 1
  grep -F -q -e "$OM_BLOCK_BEGIN" "$1"
}

# Номер строки с чужим присваиванием NODE_EXTRA_CA_CERTS вне нашего блока (N5-I5).
profile_foreign_env_line() {
  local file=$1
  [ -f "$file" ] || return 1
  awk -v b="$OM_BLOCK_BEGIN" -v e="$OM_BLOCK_END" -v name="$OM_ENV_NAME" -v target="$ca_target" '
    function ltrim(s) { sub(/^[ \t]+/, "", s); return s }
    # Присваивание в одном сегменте команды. Отбрасываются управляющие слова составной команды
    # (then/do/else) и объявляющие префиксы с их ключами: export, declare -x, typeset -x,
    # readonly, local, env NAME=... cmd. Остаток обязан НАЧИНАТЬСЯ с формы присваивания —
    # поэтому упоминание внутри строки (echo "NAME=...") присваиванием не считается.
    function is_assign_seg(seg,   s) {
      s = ltrim(seg)
      while (s ~ /^(then|do|else|elif|export|declare|typeset|readonly|local|env|command|builtin)([ \t]|$)/ ||
             s ~ /^-[A-Za-z]+([ \t]|$)/) {
        sub(/^[^ \t]+[ \t]*/, "", s)
      }
      if (index(s, name "=") == 1) { return 1 }                                    # NAME=...
      if (s ~ ("^setenv[ \t]+" name "([ \t]|$)")) { return 1 }                     # csh setenv NAME ...
      if (s ~ ("^set([ \t]+-[a-zA-Z]+)*[ \t]+" name "([ \t]|$)")) { return 1 }     # fish set -gx NAME
      return 0
    }
    $0 == b { inblk = 1; next }
    $0 == e { inblk = 0; next }
    inblk == 1 { next }
    {
      # N5-I5: чужим считается именно ПРИСВАИВАНИЕ переменной вне нашего блока, а не любое
      # упоминание. Строки-комментарии и текст с подстрокой NODE_EXTRA_CA_CERTS предупреждения
      # не вызывают. Строка разбивается на сегменты по разделителям команд, поэтому распознаётся
      # и присваивание внутри составной строки (if ...; then export NAME=...; fi).
      line = ltrim($0)
      if (substr(line, 1, 1) == "#") { next }
      is_assign = 0
      parts = split($0, seg, /[;&|(){}]/)
      for (i = 1; i <= parts; i++) {
        if (is_assign_seg(seg[i])) { is_assign = 1; break }
      }
      if (is_assign == 1 && index($0, target) == 0) { print NR; found = 1; exit }
    }
    END { if (found != 1) { exit 1 } }
  ' "$file"
}

apply_profile_block() {
  local with_path=$1
  local tmp_block tmp_new dir backup
  dir=$(dirname "$profile_file")
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
  fi
  tmp_block=$(mktemp "${TMPDIR:-/tmp}/opencode-block.XXXXXX")
  tmp_new=$(mktemp "${TMPDIR:-/tmp}/opencode-profile.XXXXXX")
  write_block_file "$tmp_block" "$with_path"

  if [ -f "$profile_file" ]; then
    if profile_has_block "$profile_file"; then
      # Блок правится НА МЕСТЕ: содержимое ПЕРВОГО маркированного блока заменяется новым, а все
      # последующие блоки (например, после ручного копирования) вырезаются. Гарантия N5-U1
      # «в профиле ровно один блок» держится, и при этом уже существующий блок не переезжает
      # в конец файла — порядок строк профиля пользователя сохраняется.
      awk -v b="$OM_BLOCK_BEGIN" -v e="$OM_BLOCK_END" -v bf="$tmp_block" '
        $0 == b {
          inblk = 1
          if (done != 1) {
            done = 1
            while ((getline bl < bf) > 0) { print bl }
            close(bf)
          }
          next
        }
        inblk == 1 && $0 == e { inblk = 0; next }
        inblk == 1 { next }
        { print }
      ' "$profile_file" >"$tmp_new"
    else
      cat "$profile_file" "$tmp_block" >"$tmp_new"
    fi
  else
    cat "$tmp_block" >"$tmp_new"
  fi

  if [ -f "$profile_file" ] && cmp -s "$tmp_new" "$profile_file"; then
    say "$MSG_PROFILE_SAME" "$profile_file"
    rm -f "$tmp_block" "$tmp_new"
    profile_did_change=0
    return 0
  fi
  profile_did_change=1

  backup="$profile_file$OM_PROFILE_BAK_SUFFIX"
  if [ -f "$profile_file" ] && ! profile_has_block "$profile_file" && [ ! -f "$backup" ]; then
    cp "$profile_file" "$backup"
    say "$MSG_PROFILE_BACKUP" "$backup"
  fi
  cat "$tmp_new" >"$profile_file"
  rm -f "$tmp_block" "$tmp_new"
  say "$MSG_PROFILE_UPDATED" "$profile_file"
  if [ "$with_path" -eq 1 ]; then
    say "$MSG_PATH_ADDED" "$bin_dir"
  fi
  return 0
}

remove_profile_block() {
  local tmp_new
  if [ ! -f "$profile_file" ] || ! profile_has_block "$profile_file"; then
    say "$MSG_UNINSTALL_ABSENT" "$OBJ_PROFILE"
    return 0
  fi
  tmp_new=$(mktemp "${TMPDIR:-/tmp}/opencode-profile.XXXXXX")
  awk -v b="$OM_BLOCK_BEGIN" -v e="$OM_BLOCK_END" '
    $0 == b { inblk = 1; next }
    inblk == 1 && $0 == e { inblk = 0; next }
    inblk == 1 { next }
    { print }
  ' "$profile_file" >"$tmp_new"
  cat "$tmp_new" >"$profile_file"
  rm -f "$tmp_new"
  say "$MSG_UNINSTALL_REMOVED" "$OBJ_PROFILE" "$profile_file"
}

# =====================================================================================
# Привилегии (N5-I7)
# =====================================================================================
run_priv() {
  if [ "$use_sudo" -eq 1 ]; then
    sudo "$@"
  else
    "$@"
  fi
}

prepare_privileges() {
  if [ "$opt_system" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      use_sudo=1
    else
      die "$EX_PERM" "$MSG_ERR_NEED_ROOT"
    fi
  fi
}

ensure_bin_dir_writable() {
  if [ -d "$bin_dir" ]; then
    if [ ! -w "$bin_dir" ] && [ "$use_sudo" -eq 0 ]; then
      out "$MSG_ERR_NO_WRITE_HINT" >&2
      die "$EX_PERM" "$MSG_ERR_NO_WRITE" "$bin_dir"
    fi
  else
    if ! run_priv mkdir -p "$bin_dir" 2>/dev/null; then
      out "$MSG_ERR_NO_WRITE_HINT" >&2
      die "$EX_PERM" "$MSG_ERR_NO_WRITE" "$bin_dir"
    fi
  fi
}

# =====================================================================================
# Версия установленного бинарника (N5-C2, N5-U2)
# =====================================================================================
installed_version() {
  local path=$1 value
  [ -x "$path" ] || return 1
  value=$("$path" --version 2>/dev/null | head -1) || return 1
  value=$(printf '%s' "$value" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

# =====================================================================================
# Шаги установки
# =====================================================================================
install_ca() {
  local current
  if [ ! -d "$config_dir" ]; then
    mkdir -p "$config_dir"
    chmod 0755 "$config_dir"
  fi
  if [ -f "$ca_target" ]; then
    current=$(to_lower "$(sha256_of "$ca_target")")
    if [ "$current" = "$MF_ca_sha" ]; then
      say "$MSG_CA_SAME"
      return 0
    fi
    cp "$pkg_root/$MF_ca_file" "$ca_target"
    chmod 0644 "$ca_target"
    say "$MSG_CA_REPLACED" "$ca_target"
    return 0
  fi
  cp "$pkg_root/$MF_ca_file" "$ca_target"
  chmod 0644 "$ca_target"
  say "$MSG_CA_INSTALLED" "$ca_target"
}

install_binary() {
  local current tmp_new old_version
  if [ -f "$bin_target" ]; then
    current=$(to_lower "$(sha256_of "$bin_target")")
    if [ "$current" = "$MF_cli_sha" ]; then
      say "$MSG_BIN_SAME" "$MF_version"
      return 0
    fi
    old_version=$(installed_version "$bin_target" || printf 'не определена')
    say "$MSG_BIN_OTHER" "$old_version" "$MF_version"
    if ! run_priv cp "$bin_target" "$bin_backup"; then
      die "$EX_PERM" "$MSG_ERR_NO_WRITE" "$bin_dir"
    fi
    say "$MSG_BIN_BACKUP" "$bin_backup"
  fi
  tmp_new="$bin_dir/.opencode.new.$$"
  # Привилегированный шаг мог не состояться (sudo отказал) — это отказ по правам, код 5 (N5-I7).
  if ! run_priv cp "$pkg_root/$MF_cli_file" "$tmp_new"; then
    die "$EX_PERM" "$MSG_ERR_NO_WRITE" "$bin_dir"
  fi
  if ! run_priv chmod 0755 "$tmp_new"; then
    die "$EX_PERM" "$MSG_ERR_NO_WRITE" "$bin_dir"
  fi
  if ! run_priv mv -f "$tmp_new" "$bin_target"; then
    die "$EX_PERM" "$MSG_ERR_NO_WRITE" "$bin_dir"
  fi
  say "$MSG_BIN_INSTALLED" "$bin_target"
}

desktop_dest_dir() {
  if [ -w "/Applications" ] || [ "$use_sudo" -eq 1 ]; then
    printf '/Applications'
  else
    printf '%s/Applications' "$HOME"
  fi
}

# Страховка перед разрушающими операциями с Desktop — вторая, независимая от manifest_load линия
# защиты (defense in depth). Возвращает путь установленного приложения, только если он строго
# внутри каталога назначения: не равен ему самому и не является его родителем. Пустое или
# «схлопывающееся» app_name дало бы "<dest>/" → rm -rf всего каталога /Applications.
desktop_target_path() {
  local dest=$1 app=$2 target
  is_safe_app_name "$app" || return 1
  dest=$(path_normalize "$dest") || return 1
  target=$(path_normalize "$dest/$app") || return 1
  path_is_inside "$dest" "$target" || return 1
  printf '%s' "$target"
}

desktop_installed_version() {
  local app=$1
  [ -d "$app" ] || return 1
  if command -v defaults >/dev/null 2>&1; then
    defaults read "$app/Contents/Info" CFBundleShortVersionString 2>/dev/null && return 0
  fi
  return 1
}

install_desktop() {
  local dest app_dest mount_point app_src app_version
  if [ -z "$MF_desktop_file" ]; then
    say "$MSG_DESKTOP_NONE"
    desktop_summary="не входит в пакет"
    return 0
  fi
  if [ "$opt_no_desktop" -eq 1 ]; then
    say "$MSG_DESKTOP_SKIPPED"
    desktop_summary="пропущен (--no-desktop)"
    return 0
  fi
  if [ "$MF_desktop_type" != "dmg" ] || [ "$platform" != "macos" ]; then
    say "$MSG_DESKTOP_MANUAL_AT" "$pkg_root/$MF_desktop_file"
    desktop_summary="$pkg_root/$MF_desktop_file"
    return 0
  fi

  dest=$(desktop_dest_dir)
  # Путь вычисляется и проверяется ДО первой файловой операции (в т.ч. до mkdir): при пустом или
  # небезопасном app_name манифест признаётся невалидным (код 2), и ни rm, ни ditto не выполняются.
  if ! app_dest=$(desktop_target_path "$dest" "$MF_desktop_app"); then
    manifest_field_error "artifacts[].app_name" "$(fmt "$MSG_ERR_FIELD_PATH" "$MF_desktop_app")"
  fi
  if [ "$dest" != "/Applications" ]; then
    say "$MSG_DESKTOP_USER_DIR" "/Applications" "$dest"
    mkdir -p "$dest"
  fi
  app_version=$(desktop_installed_version "$app_dest" || printf '')
  if [ -n "$app_version" ] && [ "$app_version" = "$MF_version" ]; then
    say "$MSG_DESKTOP_SAME"
    desktop_summary="$app_dest"
    return 0
  fi

  mount_point=$(mktemp -d "${TMPDIR:-/tmp}/opencode-dmg.XXXXXX")
  # Отмонтирование выполняется всегда, в том числе при ошибке копирования (N5-I9).
  trap 'hdiutil detach "$mount_point" -quiet >/dev/null 2>&1 || true; rmdir "$mount_point" >/dev/null 2>&1 || true' EXIT INT TERM
  if ! hdiutil attach -nobrowse -readonly -mountpoint "$mount_point" "$pkg_root/$MF_desktop_file" >/dev/null 2>&1; then
    trap - EXIT INT TERM
    rmdir "$mount_point" >/dev/null 2>&1 || true
    say "$MSG_DESKTOP_ERROR" "1"
    desktop_summary="ошибка (код 1)"
    desktop_failed=1
    return 0
  fi
  app_src="$mount_point/$MF_desktop_app"
  if [ -d "$app_dest" ]; then
    run_priv rm -rf "$app_dest"
  fi
  if run_priv ditto "$app_src" "$app_dest"; then
    say "$MSG_DESKTOP_INSTALLED" "$app_dest"
    desktop_summary="$app_dest"
  else
    say "$MSG_DESKTOP_ERROR" "1"
    desktop_summary="ошибка (код 1)"
    desktop_failed=1
  fi
  hdiutil detach "$mount_point" -quiet >/dev/null 2>&1 || true
  rmdir "$mount_point" >/dev/null 2>&1 || true
  trap - EXIT INT TERM
}

# Пользовательский конфиг не изменяется; конфликт = сам факт существования файла (N5-I11).
handle_user_config() {
  local cfg="$config_dir/opencode.json"
  local bak="$config_dir/opencode.json.bak"
  if [ -f "$cfg" ]; then
    if [ -f "$bak" ]; then
      say "$MSG_CONFIG_BACKUP_KEPT" "$bak"
    else
      cp "$cfg" "$bak"
      say "$MSG_CONFIG_BACKUP" "$bak"
    fi
  fi
  say "$MSG_CONFIG_KEPT" "$cfg"
}

print_report() {
  local profile_changed=$1
  out "$MSG_DONE" "$MF_version"
  out "$MSG_SUM_BIN" "$bin_target"
  out "$MSG_SUM_CA" "$ca_target"
  out "$MSG_SUM_DESKTOP" "$desktop_summary"
  out "$MSG_SUM_CONFIG" "$config_dir/opencode.json"
  out "$MSG_SUM_HUB" "$MF_hub_url"
  out "$MSG_NEXT"
  if [ "$profile_changed" -eq 1 ]; then
    out "$MSG_NEXT_TERMINAL" "$profile_file"
  fi
  out "$MSG_NEXT_RUN"
  out "$MSG_NEXT_SSO"
}

do_install() {
  local with_path=0 foreign_line profile_changed=0

  prepare_privileges
  ensure_bin_dir_writable
  detect_profile_kind

  install_ca
  install_binary

  if ! path_contains "$bin_dir"; then
    with_path=1
  fi
  if foreign_line=$(profile_foreign_env_line "$profile_file"); then
    say "$MSG_WARN_FOREIGN_ENV" "$profile_file" "$foreign_line" "$OM_ENV_NAME"
  fi
  apply_profile_block "$with_path"
  # «Откройте новый терминал» печатается, только если профиль или PATH действительно правились (N5-I12).
  if [ "$profile_did_change" -eq 1 ]; then
    profile_changed=1
  fi

  install_desktop
  handle_user_config
  say "$MSG_NOTE_GUI"

  print_report "$profile_changed"

  if [ "$opt_no_launch" -eq 0 ] && [ -x "$bin_target" ]; then
    # Код возврата corp status игнорируется: сразу после установки ключа ещё нет (N5-I12).
    "$bin_target" corp status || true
  fi

  if [ "$desktop_failed" -eq 1 ]; then
    exit "$EX_FAIL"
  fi
}

# =====================================================================================
# --check (N5-C2, N5-C3)
# =====================================================================================
env_state() {
  # Печатает: ok | other <путь> | none
  local current="${NODE_EXTRA_CA_CERTS:-}"
  if [ -f "$profile_file" ] && profile_has_block "$profile_file" &&
    grep -F -q -e "$ca_target" "$profile_file"; then
    printf 'ok'
    return 0
  fi
  if [ -n "$current" ]; then
    if [ "$current" = "$ca_target" ]; then
      printf 'ok'
    else
      printf 'other'
    fi
    return 0
  fi
  printf 'none'
}

do_check() {
  local version current state desktop_app

  out "$MSG_CHECK_PKG" "$MF_version" "$MF_os" "$MF_arch"
  out "$MSG_CHECK_MANIFEST"

  if [ -f "$bin_target" ]; then
    current=$(to_lower "$(sha256_of "$bin_target")")
    version=$(installed_version "$bin_target" || printf '')
    if [ "$current" = "$MF_cli_sha" ]; then
      if [ -n "$version" ]; then
        out "$MSG_CHECK_BIN_OK" "$version" "$bin_target"
      else
        out "$MSG_CHECK_BIN_NOVER" "$bin_target"
      fi
    elif [ -n "$version" ]; then
      out "$MSG_CHECK_BIN_OTHER" "$version" "$MF_version"
      check_diff=1
    else
      out "$MSG_CHECK_BIN_NOVER" "$bin_target"
      check_diff=1
    fi
  else
    out "$MSG_CHECK_BIN_NONE"
    check_diff=1
  fi

  if path_contains "$bin_dir"; then
    out "$MSG_CHECK_PATH_YES" "$bin_dir"
  else
    if [ -f "$profile_file" ] && profile_has_block "$profile_file" &&
      grep -F -q -e "$bin_dir" "$profile_file"; then
      out "$MSG_CHECK_PATH_YES" "$bin_dir"
    else
      out "$MSG_CHECK_PATH_NO" "$bin_dir"
      check_diff=1
    fi
  fi

  if [ -f "$ca_target" ]; then
    current=$(to_lower "$(sha256_of "$ca_target")")
    if [ "$current" = "$MF_ca_sha" ]; then
      out "$MSG_CHECK_CA_OK" "$ca_target"
    else
      out "$MSG_CHECK_CA_DIFF" "$ca_target"
      check_diff=1
    fi
  else
    out "$MSG_CHECK_CA_NONE"
    check_diff=1
  fi

  state=$(env_state)
  case $state in
    ok) out "$MSG_CHECK_ENV_OK" "$ca_target" ;;
    other) out "$MSG_CHECK_ENV_OTHER" "${NODE_EXTRA_CA_CERTS:-}"; check_diff=1 ;;
    *) out "$MSG_CHECK_ENV_NONE"; check_diff=1 ;;
  esac

  if [ -z "$MF_desktop_file" ]; then
    out "$MSG_DESKTOP_NONE"
  else
    desktop_app=$(find_installed_desktop || printf '')
    if [ -n "$desktop_app" ]; then
      out "$MSG_CHECK_DESKTOP_OK" "$desktop_app"
    else
      out "$MSG_CHECK_DESKTOP_NONE"
      check_diff=1
    fi
  fi

  out "$MSG_CHECK_CONFIG" "$config_dir/opencode.json"
  out "$MSG_CHECK_HUB" "$MF_hub_url"
  if [ "$check_diff" -eq 1 ]; then
    out "$MSG_CHECK_SUM_NEED"
    exit "$EX_DIFF"
  fi
  out "$MSG_CHECK_SUM_OK"
  exit "$EX_OK"
}

find_installed_desktop() {
  local app=$MF_desktop_app path
  # Та же страховка, что в install_desktop: путь удаления строится только из безопасного имени,
  # иначе Desktop считается ненайденным и ни одна файловая операция не выполняется (N5-P4).
  is_safe_app_name "$app" || return 1
  if path=$(desktop_target_path "/Applications" "$app") && [ -d "$path" ]; then
    printf '%s' "$path"
    return 0
  fi
  if path=$(desktop_target_path "$HOME/Applications" "$app") && [ -d "$path" ]; then
    printf '%s' "$path"
    return 0
  fi
  return 1
}

# =====================================================================================
# --dry-run (N5-C1)
# =====================================================================================
plan_line() {
  plan_step=$((plan_step + 1))
  local fmt=$1
  shift
  local text
  text=$(fmt "$fmt" "$@")
  out "$MSG_PLAN_STEP" "$plan_step" "$text"
}

do_plan_install() {
  local with_path=0
  detect_profile_kind
  out "$MSG_PLAN_HEAD" "$MF_version" "$MF_os" "$MF_arch"
  if [ ! -d "$config_dir" ]; then
    plan_line "$MSG_PLAN_MKDIR" "$config_dir"
  fi
  plan_line "$MSG_PLAN_CA" "$pkg_root/$MF_ca_file" "$ca_target"
  if [ ! -d "$bin_dir" ]; then
    plan_line "$MSG_PLAN_MKDIR" "$bin_dir"
  fi
  if [ -f "$bin_target" ]; then
    plan_line "$MSG_PLAN_BIN_BACKUP" "$bin_target" "$bin_backup"
  fi
  plan_line "$MSG_PLAN_BIN" "$pkg_root/$MF_cli_file" "$bin_target" "$bin_dir/.opencode.new.$$"
  plan_line "$MSG_PLAN_PROFILE" "$profile_file" "$OM_ENV_NAME" "$ca_target"
  if ! path_contains "$bin_dir"; then
    with_path=1
    plan_line "$MSG_PLAN_PATH" "$bin_dir"
  fi
  if [ -z "$MF_desktop_file" ]; then
    plan_line "$MSG_DESKTOP_NONE"
  elif [ "$opt_no_desktop" -eq 1 ]; then
    plan_line "$MSG_DESKTOP_SKIPPED"
  elif [ "$MF_desktop_type" = "dmg" ] && [ "$platform" = "macos" ]; then
    plan_line "$MSG_PLAN_DESKTOP_DMG" "$pkg_root/$MF_desktop_file" "$(desktop_dest_dir)" "$MF_desktop_app"
  else
    plan_line "$MSG_DESKTOP_MANUAL_AT" "$pkg_root/$MF_desktop_file"
  fi
  plan_line "$MSG_PLAN_CONFIG" "$config_dir/opencode.json" "$config_dir/opencode.json.bak"
  if [ "$opt_no_launch" -eq 0 ]; then
    plan_line "$MSG_PLAN_CORP_STATUS" "$bin_target"
  fi
  out "$MSG_CHECK_HUB" "$MF_hub_url"
  exit "$EX_OK"
}

do_plan_uninstall() {
  out "$MSG_PLAN_UNINSTALL_HEAD" "$MF_version"
  plan_line "$MSG_PLAN_REMOVE" "$bin_target"
  plan_line "$MSG_PLAN_REMOVE" "$bin_backup"
  plan_line "$MSG_PLAN_REMOVE_BLOCK" "$profile_file"
  plan_line "$MSG_PLAN_ENV_UNSET" "$OM_ENV_NAME"
  plan_line "$MSG_PLAN_REMOVE" "$ca_target"
  if [ -n "$MF_desktop_app" ]; then
    plan_line "$MSG_PLAN_REMOVE" "$(find_installed_desktop || printf '/Applications/%s' "$MF_desktop_app")"
  fi
  if [ "$opt_purge" -eq 1 ]; then
    purge_each plan
  fi
  exit "$EX_OK"
}

# =====================================================================================
# Удаление (N5-R1…N5-R4)
# =====================================================================================
# shellcheck disable=SC2016
# Обоснование: подстановки ${XDG_CONFIG_HOME}, ${XDG_DATA_HOME}, ${HOME} и «~» — это текст из
# манифеста (N5-P3), который раскрывается вручную, а не выражения shell.
expand_user_path() {
  local p=$1 xdg_config xdg_data tilde
  xdg_config=${XDG_CONFIG_HOME:-$HOME/.config}
  xdg_data=${XDG_DATA_HOME:-$HOME/.local/share}
  tilde=$(printf '\176')
  case $p in
    "$tilde") p=$HOME ;;
    "$tilde/"*) p="$HOME/${p#"$tilde/"}" ;;
  esac
  p=${p//'${XDG_CONFIG_HOME}'/$xdg_config}
  p=${p//'${XDG_DATA_HOME}'/$xdg_data}
  p=${p//'${HOME}'/$HOME}
  printf '%s' "$p"
}

# Раскрытие шаблона манифеста + единая нормализация. Печатает абсолютный нормализованный путь;
# код 1 — путь непригоден (пустой, относительный, с сегментом "..").
purge_path_resolve() {
  local raw=$1 expanded
  expanded=$(expand_user_path "$raw")
  path_normalize "$expanded"
}

# N5-R2: удалению подлежат только объекты СТРОГО внутри домашнего каталога — путь начинается с
# "$HOME/", не равен самому "$HOME" и не выходит за его пределы. Решение принимается по
# НОРМАЛИЗОВАННОМУ значению, поэтому "~/", "~//", "${HOME}//", "${HOME}///", "${HOME}/.",
# "${HOME}/./", "${HOME}/sub/.." — всё, что ядро схлопнуло бы в сам домашний каталог, —
# отвергается наравне с "~" и "/etc". Второй аргумент — результат purge_path_resolve (пустая
# строка означает, что нормализация не состоялась, то есть путь непригоден).
purge_path_is_safe() {
  local raw=$1 normalized=$2 home_dir
  case $raw in
    *".."*) return 1 ;;
  esac
  [ -n "$normalized" ] || return 1
  case $normalized in
    *".."*) return 1 ;;
  esac
  home_dir=$(path_normalize "$HOME") || return 1
  path_is_inside "$home_dir" "$normalized"
}

# Список проверяется целиком до любого удаления: одна небезопасная запись отвергает список (N5-R2).
validate_purge_paths() {
  local count idx raw normalized bad=0
  count=$(mf_val "purge_paths.__len")
  [ -n "$count" ] || count=0
  idx=0
  while [ "$idx" -lt "$count" ]; do
    raw=$(mf_val "purge_paths.$idx")
    normalized=$(purge_path_resolve "$raw") || normalized=''
    if ! purge_path_is_safe "$raw" "$normalized"; then
      out "$MSG_ERR_PURGE_UNSAFE" "$raw" >&2
      bad=1
    fi
    idx=$((idx + 1))
  done
  if [ "$bad" -eq 1 ]; then
    die "$EX_ARGS" "$MSG_ERR_PURGE_REJECTED"
  fi
}

# Вторая, независимая от validate_purge_paths линия защиты (defense in depth): ни один путь не
# попадает в rm -rf без повторной проверки прямо на месте удаления. Если валидация окажется
# обойдена или изменена, результатом будет отказ с кодом 2, а не удаление.
purge_each() {
  local mode=$1 count idx raw normalized
  count=$(mf_val "purge_paths.__len")
  [ -n "$count" ] || count=0
  if [ "$mode" = "plan" ]; then
    out "$MSG_PURGE_HEAD"
  else
    say "$MSG_PURGE_HEAD"
  fi
  idx=0
  while [ "$idx" -lt "$count" ]; do
    raw=$(mf_val "purge_paths.$idx")
    normalized=$(purge_path_resolve "$raw") || normalized=''
    if ! purge_path_is_safe "$raw" "$normalized"; then
      out "$MSG_ERR_PURGE_UNSAFE" "$raw" >&2
      die "$EX_ARGS" "$MSG_ERR_PURGE_REJECTED"
    fi
    out "$MSG_PURGE_ITEM" "$normalized"
    if [ "$mode" != "plan" ]; then
      if [ -e "$normalized" ]; then
        rm -rf "$normalized"
        say "$MSG_PURGE_REMOVED" "$normalized"
      else
        say "$MSG_PURGE_ABSENT" "$normalized"
      fi
    fi
    idx=$((idx + 1))
  done
}

remove_object() {
  local label=$1 path=$2
  if [ -e "$path" ]; then
    if run_priv rm -rf "$path"; then
      say "$MSG_UNINSTALL_REMOVED" "$label" "$path"
    else
      say "$MSG_UNINSTALL_FAILED" "$label" "$path"
    fi
  else
    say "$MSG_UNINSTALL_ABSENT" "$label"
  fi
}

do_uninstall() {
  local desktop_app
  detect_profile_kind
  if [ "$opt_system" -eq 1 ] && [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    use_sudo=1
  fi
  say "$MSG_UNINSTALL_HEAD" "$MF_version"
  remove_object "$OBJ_BIN" "$bin_target"
  remove_object "$OBJ_BIN_BAK" "$bin_backup"
  remove_profile_block
  remove_object "$OBJ_CA" "$ca_target"
  if [ -n "$MF_desktop_app" ]; then
    desktop_app=$(find_installed_desktop || printf '')
    if [ -n "$desktop_app" ]; then
      remove_object "$OBJ_DESKTOP" "$desktop_app"
    else
      say "$MSG_UNINSTALL_ABSENT" "$OBJ_DESKTOP"
    fi
  else
    say "$MSG_DESKTOP_NONE"
  fi
  if [ "$opt_purge" -eq 1 ]; then
    purge_each run
  fi
  out "$MSG_UNINSTALL_DONE"
  exit "$EX_OK"
}

# =====================================================================================
# Точка входа
# =====================================================================================
resolve_pkg_root() {
  local self_dir
  if [ -n "${OPENCODE_INSTALLER_PKG_ROOT:-}" ]; then
    pkg_root=$OPENCODE_INSTALLER_PKG_ROOT
  else
    CDPATH=''
    self_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    pkg_root=$(cd -- "$self_dir/.." && pwd -P)
  fi
  platform=${OPENCODE_INSTALLER_PLATFORM:-}
  if [ -z "$platform" ]; then
    case $(uname -s) in
      Darwin) platform=macos ;;
      *) platform=linux ;;
    esac
  fi
}

main() {
  desktop_summary=""
  desktop_failed=0

  parse_args "$@"
  if [ "$opt_help" -eq 1 ]; then
    print_help
    exit "$EX_OK"
  fi
  resolve_pkg_root
  manifest_load
  if [ "$opt_show_version" -eq 1 ]; then
    out "$MSG_VERSION_LINE" "$MF_version" "$MF_os" "$MF_arch"
    exit "$EX_OK"
  fi
  check_system
  require_hash_tool
  compute_layout

  if [ "$opt_uninstall" -eq 1 ]; then
    if [ "$opt_purge" -eq 1 ]; then
      validate_purge_paths
    fi
    if [ "$opt_dry_run" -eq 1 ]; then
      do_plan_uninstall
    fi
    do_uninstall
  fi

  verify_package

  if [ "$opt_check" -eq 1 ]; then
    do_check
  fi
  if [ "$opt_dry_run" -eq 1 ]; then
    do_plan_install
  fi
  do_install
}

if [ "${OPENCODE_INSTALLER_SOURCE_ONLY:-0}" != "1" ]; then
  main "$@"
fi
