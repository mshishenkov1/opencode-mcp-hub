#!/usr/bin/env bash
#
# installers/release.sh — сборка пакетов для портала из уже готовых локальных артефактов
# (installers/docs/spec.md, N5-B1…N5-B5).
#
# Скрипт не собирает форк OpenCode и не подписывает сборки: и то и другое выполняется
# отдельно, в конвейере форка (см. правила S-B9 и R-14 спецификации). Артефакты берутся
# только из локального каталога --artifacts: скачивания нет ни в одном режиме (N5-B5).
# Совместимость: bash 3.2 (N5-T5).

set -euo pipefail

EX_OK=0
EX_FAIL=1
EX_ARGS=2

MSG_ERR_NEED_ARG='Не задан обязательный аргумент: %s'
MSG_ERR_NEED_ENDPOINT='Не задан ни --hub-url, ни --catalog-url: пакету нужна хотя бы одна точка входа'
MSG_ERR_UNKNOWN_ARG='Неизвестный аргумент: %s'
MSG_ERR_NO_ARTIFACTS_DIR='Каталог артефактов не найден: %s'
MSG_ERR_NO_CA='Файл CA не найден: %s'
MSG_ERR_BAD_TARGET='Неизвестная цель сборки: %s'
MSG_ERR_NO_CLI='Для цели %s не найден CLI-артефакт. Ожидаются: %s или %s'
MSG_ERR_VERSION_MISMATCH='Версия артефакта не совпадает с --version: в имени %s, запрошена %s'
MSG_ERR_BAD_VERSION='Недопустимый формат --version: %s (ожидается <upstream>-magnit.<N>, например 1.17.9-magnit.1)'
MSG_ERR_NO_HASH_TOOL='Не найдена утилита вычисления sha256: sha256sum, shasum -a 256, openssl dgst -sha256'
MSG_ERR_SELFCHECK='Самопроверка пакета не пройдена: %s (код %s)'
MSG_ERR_SELFCHECK_HASH='Самопроверка пакета не пройдена: sha256 файла %s в архиве не совпадает'
MSG_ERR_SELFCHECK_JSON='Самопроверка пакета не пройдена: common/manifest.json в архиве не является валидным JSON'
MSG_ERR_NO_GH='Для публикации нужен установленный клиент gh'
# AC-271: «Desktop не собирали» и «собрали, но не для этой архитектуры» — разные вещи, и второе
# обязано быть названо дефектом в отчёте сборки, а не только штатной строкой установщика.
MSG_WARN_DESKTOP_ARCH='Предупреждение: dmg для %s не найден, хотя в каталоге есть dmg других архитектур (%s) — Desktop в пакет не вошёл'

MSG_BUILD='Сборка %s'
MSG_ARCHIVE='Архив: %s'
MSG_SUMS='Контрольные суммы: %s'
MSG_SELFCHECK_OK='Самопроверка: %s — план установки построен'
MSG_SELFCHECK_CROSS='Самопроверка: %s — целостность проверена, план не строится на этой ОС'
MSG_SELFCHECK_NOPWSH='Самопроверка: %s — проверен только манифест (pwsh недоступен)'
MSG_WARN_NO_PYTHON='Предупреждение: python3 недоступен — манифест %s проверен только на управляющие символы'
MSG_SELFCHECK_DROPPED='Архив не прошёл самопроверку и удалён из каталога сборки: %s'
MSG_NO_PUBLISH='публикация пропущена: добавьте --publish'
MSG_PUBLISH_DONE='Черновик релиза %s создан'

usage() {
  cat <<'HELP'
Сборка пакетов OpenCode Magnit для выкладки на портал.

Использование:
  release.sh --artifacts <каталог> --version <версия> --ca <файл>
             (--hub-url <адрес> | --catalog-url <адрес> | оба)
             [--out <каталог>] [--targets darwin-arm64,darwin-x64,linux-x64,windows-x64]
             [--signed] [--publish]

Аргументы:
  --artifacts <каталог>  локальный каталог с готовыми артефактами конвейера форка
  --version <версия>     версия сборки, например 1.17.9-magnit.1
  --ca <файл>            корпоративный CA-bundle в формате PEM
  --hub-url <адрес>      адрес Hub, попадает в манифест и в отчёт установщика
  --catalog-url <адрес>  адрес статического каталога коннекторов (сборка без Hub)
  --out <каталог>        куда класть архивы (по умолчанию installers/dist)
  --targets <список>     цели через запятую (по умолчанию все четыре)
  --signed               пометить сборку Desktop подписанной: установщик не снимает карантин
  --publish              создать черновик релиза с архивами; без флага публикации нет

Задать нужно хотя бы один из адресов --hub-url / --catalog-url: пакет без точки входа
бесполезен. В манифест попадает тот, что задан (или оба).

Границы (N5-B5):
  * скрипт не собирает форк OpenCode — сборка выполняется отдельно, в конвейере форка;
  * скрипт не подписывает сборки — подпись выполняется отдельно, решением ИБ;
  * скрипт ничего не загружает: вход — только локальный каталог --artifacts.
HELP
}

die() {
  local code=$1
  shift
  # shellcheck disable=SC2059
  # Обоснование: формат приходит константой из блока сообщений выше.
  printf -- "$1\n" "${@:2}" >&2
  exit "$code"
}

say() {
  # shellcheck disable=SC2059
  # Обоснование: формат приходит константой из блока сообщений выше.
  printf -- "$1\n" "${@:2}"
}

# --------------------------------------------------------------------------- аргументы
artifacts_dir=""
version=""
ca_file=""
hub_url=""
catalog_url=""
out_dir=""
targets="darwin-arm64,darwin-x64,linux-x64,windows-x64"
do_publish=0
# Подпись сборки Desktop (S-B17). Умолчание — «не подписана»: сертификатов ИБ пока нет, а
# умолчание «подписана» дало бы пакеты, установщик которых не снимает карантин, — на macOS 15+
# такое приложение не запускается вовсе. Флаг --signed переключит умолчание, когда подпись появится.
is_signed=0

while [ $# -gt 0 ]; do
  case $1 in
    --artifacts) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--artifacts"; }; artifacts_dir=$1 ;;
    --version) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--version"; }; version=$1 ;;
    --ca) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--ca"; }; ca_file=$1 ;;
    --hub-url) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--hub-url"; }; hub_url=$1 ;;
    --catalog-url) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--catalog-url"; }; catalog_url=$1 ;;
    --out) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--out"; }; out_dir=$1 ;;
    --targets) shift; [ $# -gt 0 ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--targets"; }; targets=$1 ;;
    --signed) is_signed=1 ;;
    --publish) do_publish=1 ;;
    --help|-h) usage; exit "$EX_OK" ;;
    *) usage >&2; die "$EX_ARGS" "$MSG_ERR_UNKNOWN_ARG" "$1" ;;
  esac
  shift
done

CDPATH=''
self_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)

[ -n "$artifacts_dir" ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--artifacts"; }
[ -n "$version" ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--version"; }
[ -n "$ca_file" ] || { usage >&2; die "$EX_ARGS" "$MSG_ERR_NEED_ARG" "--ca"; }
# Адрес точки входа: Hub, каталог коннекторов или оба (S-C10). Оба пустые — пакет собирать незачем.
if [ -z "$hub_url" ] && [ -z "$catalog_url" ]; then
  usage >&2
  die "$EX_ARGS" "$MSG_ERR_NEED_ENDPOINT"
fi
[ -d "$artifacts_dir" ] || die "$EX_ARGS" "$MSG_ERR_NO_ARTIFACTS_DIR" "$artifacts_dir"
[ -f "$ca_file" ] || die "$EX_ARGS" "$MSG_ERR_NO_CA" "$ca_file"

# --version обязана проходить собственную схему манифеста (N5-P3,
# ^[0-9]+\.[0-9]+\.[0-9]+-magnit\.[0-9]+$): иначе собрался бы пакет, не проходящий свою же схему.
case $version in
  *[!0-9A-Za-z._-]*) die "$EX_ARGS" "$MSG_ERR_BAD_VERSION" "$version" ;;
esac
if ! printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+-magnit\.[0-9]+$'; then
  die "$EX_ARGS" "$MSG_ERR_BAD_VERSION" "$version"
fi
if [ -z "$out_dir" ]; then
  out_dir="$self_dir/dist"
fi

artifacts_dir=$(cd -- "$artifacts_dir" && pwd -P)
ca_file=$(cd -- "$(dirname -- "$ca_file")" && pwd -P)/$(basename -- "$ca_file")

# --------------------------------------------------------------------------- утилиты
hash_tool=""
if command -v sha256sum >/dev/null 2>&1; then
  hash_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  hash_tool=shasum
elif command -v openssl >/dev/null 2>&1; then
  hash_tool=openssl
else
  die "$EX_FAIL" "$MSG_ERR_NO_HASH_TOOL"
fi

# Экранирование строки для вставки в JSON-манифест по RFC 8259 (N5-P3): обратный слэш,
# двойная кавычка и все управляющие символы < 0x20 — именованными escape-последовательностями
# (\b \f \n \r \t) либо в форме \u00XX. Без этого кавычка/слэш/перевод строки в --hub-url или в
# имени desktop-файла дали бы манифест, который отвергают строгие парсеры (ConvertFrom-Json в
# install.ps1, common/manifest.schema.json), хотя терпимый awk-парсер install-posix.sh его читает.
#
# Почему экранирование, а не отбраковка входа (ср. N5-B1 и проверку --version): --version обязана
# соответствовать шаблону схемы (^…-magnit\.[0-9]+$), поэтому её значение валидируется. Схема же
# описывает hub_url как произвольную непустую строку, и сужать контракт скрипт сборки не вправе —
# любое значение обязано попадать в манифест без потери и без порчи синтаксиса. Экранирование
# здесь единственная и полная линия защиты; собранный манифест дополнительно проверяется строгим
# парсером в selfcheck_package (N5-B2).
json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\b'/\\b}
  s=${s//$'\f'/\\f}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  # Остальные управляющие символы (0x01…0x1f без именованных выше) — редкий случай, поэтому
  # разбор посимвольно вынесен в awk и включается только при их наличии. LC_ALL=C делает
  # substr/length побайтовыми: многобайтные UTF-8 последовательности переносятся байт в байт.
  case $s in
    *[[:cntrl:]]*)
      s=$(printf '%s' "$s" | LC_ALL=C awk '
        BEGIN { for (i = 1; i < 32; i++) { esc[sprintf("%c", i)] = sprintf("\\u%04x", i) } }
        {
          out = ""
          n = length($0)
          for (i = 1; i <= n; i++) {
            c = substr($0, i, 1)
            out = out (c in esc ? esc[c] : c)
          }
          printf "%s", out
        }
      ')
      ;;
  esac
  printf '%s' "$s"
}

sha256_of() {
  case $hash_tool in
    sha256sum) sha256sum "$1" | awk '{ print $1 }' ;;
    shasum) shasum -a 256 "$1" | awk '{ print $1 }' ;;
    openssl) openssl dgst -sha256 "$1" | awk '{ print $NF }' ;;
    *) return 1 ;;
  esac
}

size_of() {
  wc -c <"$1" | tr -d ' '
}

host_os=""
case $(uname -s) in
  Darwin) host_os=darwin ;;
  Linux) host_os=linux ;;
  *) host_os=other ;;
esac
host_arch=""
case $(uname -m) in
  arm64|aarch64) host_arch=arm64 ;;
  x86_64|amd64) host_arch=x64 ;;
  *) host_arch=other ;;
esac

tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/opencode-release.XXXXXX")
cleanup() {
  rm -rf "$tmp_root"
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- поиск артефактов
# Возвращает путь к файлу CLI-бинарника для цели; при несовпадении версии или отсутствии — ошибка.
find_cli_binary() {
  local os=$1 arch=$2 bin_name=$3
  local zip_exact="$artifacts_dir/opencode-$os-$arch-$version.zip"
  local dir_layout="$artifacts_dir/opencode-$os-$arch/bin/$bin_name"
  local candidate found unpack

  if [ -f "$zip_exact" ]; then
    unpack="$tmp_root/unpack-$os-$arch"
    mkdir -p "$unpack"
    unzip -q -o "$zip_exact" -d "$unpack"
    found=$(find "$unpack" -type f -name "$bin_name" | head -1)
    if [ -z "$found" ]; then
      die "$EX_ARGS" "$MSG_ERR_NO_CLI" "$os-$arch" "$(basename "$zip_exact")" "opencode-$os-$arch/bin/$bin_name"
    fi
    printf '%s' "$found"
    return 0
  fi
  if [ -f "$dir_layout" ]; then
    printf '%s' "$dir_layout"
    return 0
  fi
  for candidate in "$artifacts_dir"/opencode-"$os"-"$arch"-*.zip; do
    [ -f "$candidate" ] || continue
    die "$EX_ARGS" "$MSG_ERR_VERSION_MISMATCH" "$(basename "$candidate")" "$version"
  done
  die "$EX_ARGS" "$MSG_ERR_NO_CLI" "$os-$arch" "opencode-$os-$arch-$version.zip" "opencode-$os-$arch/bin/$bin_name"
}

# Desktop собирается форком для обеих архитектур macOS (S-B16, ревизия 1.11) и для windows-x64.
#
# Прежняя редакция искала dmg только для darwin-arm64: пакет darwin-x64 собирался БЕЗ Desktop и
# молча — установщик печатал «Desktop: не входит в пакет», и это выглядело как штатная сборка.
# Архитектура выбирается по имени файла (artifactName electron-builder — `…-mac-<arch>.dmg`);
# если по имени не различить, берётся единственный dmg — так работают пакеты, собранные вручную
# одной архитектурой.
find_desktop_artifact() {
  local os=$1 arch=$2 candidate name only="" count=0 typed=0
  if [ "$os" = "darwin" ]; then
    for candidate in "$artifacts_dir"/*.dmg; do
      [ -f "$candidate" ] || continue
      count=$((count + 1))
      only=$candidate
      name=$(basename "$candidate")
      case $name in
        *arm64*|*x64*) typed=1 ;;
      esac
      case $name in
        *"$arch".dmg|*"$arch"-*.dmg)
          printf '%s' "$candidate"
          return 0
          ;;
      esac
    done
    # Ни в одном имени нет архитектуры и dmg ровно один — это ручная сборка, берём его.
    # Если архитектура в именах ЕСТЬ, но нужной среди них нет, Desktop в пакет не кладётся:
    # положить arm64-бандл в x64-пакет хуже, чем не положить ничего. Молча этого не делаем —
    # см. list_other_arch_dmgs и строку отчёта в build_target (AC-271).
    if [ "$count" -eq 1 ] && [ "$typed" -eq 0 ]; then
      printf '%s' "$only"
      return 0
    fi
  fi
  if [ "$os" = "windows" ]; then
    for candidate in "$artifacts_dir"/*.msi "$artifacts_dir"/*.exe; do
      [ -f "$candidate" ] || continue
      printf '%s' "$candidate"
      return 0
    done
  fi
  return 1
}

# Имена dmg с архитектурой в имени, лежащих в каталоге артефактов и НЕ подошедших запрошенной
# цели (AC-271).
#
# Отдельная функция, а не побочный результат find_desktop_artifact: та вызывается через $( ),
# то есть в подоболочке, и любое выставленное ею глобальное значение до вызывающего не доходит.
#
# Различить два случая обязательно. «Desktop не собирали» — штатная сборка пакета без Desktop.
# «Собрали, но не для этой архитектуры» — дефект: у пользователя он проявится штатной строкой
# установщика «Desktop: не входит в пакет», по которой ничего не понять.
list_other_arch_dmgs() {
  local arch=$1 candidate name out=""
  for candidate in "$artifacts_dir"/*.dmg; do
    [ -f "$candidate" ] || continue
    name=$(basename "$candidate")
    case $name in
      *arm64*|*x64*) : ;;
      *) continue ;;
    esac
    case $name in
      *"$arch".dmg|*"$arch"-*.dmg) continue ;;
    esac
    if [ -n "$out" ]; then
      out="$out, $name"
    else
      out="$name"
    fi
  done
  printf '%s' "$out"
}

# --------------------------------------------------------------------------- README пакета (N5-D3)
write_package_readme() {
  local dest=$1 os=$2 arch=$3 run_cmd=$4
  cat >"$dest" <<README
# OpenCode Magnit $version ($os-$arch)

Корпоративная сборка OpenCode: CLI/TUI, корпоративный CA и установщик.

## Установка

    $run_cmd

Установщик не спрашивает ключей и паролей, не меняет ваш opencode.json,
не требует прав администратора и не обращается в сеть.

## Проверка загрузки

Отпечатки всех архивов лежат в файле SHA256SUMS рядом с архивом на портале:

    sha256sum -c SHA256SUMS

## Подробнее

Инструкция пользователя: installers/docs/install-user.md в репозитории Hub
(или карточка загрузок на портале AI Lab).

Проверить установку: install.sh --check   (Windows: install.bat -Check)
Удалить: install.sh --uninstall           (Windows: install.bat -Uninstall)
README
}

# --------------------------------------------------------------------------- манифест (N5-P3)
write_manifest() {
  local dest=$1 os=$2 arch=$3 bin_name=$4
  local ca_sha=$5 cli_sha=$6 cli_size=$7
  local desktop_rel=$8 desktop_sha=$9 desktop_size=${10} desktop_type=${11} desktop_app=${12}
  local built_at purge_a purge_b desktop_block="" hub_url_json desktop_rel_json desktop_app_json
  local endpoint_block="" signed_json

  built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  desktop_rel_json=$(json_escape "$desktop_rel")
  desktop_app_json=$(json_escape "$desktop_app")

  # В манифест попадает тот адрес, что задан (S-C10): пустое поле хуже отсутствующего — схема
  # требует непустую строку, а установщик печатал бы «Hub: » вместо честного «адреса Hub нет».
  if [ -n "$hub_url" ]; then
    hub_url_json=$(json_escape "$hub_url")
    endpoint_block="$endpoint_block
  \"hub_url\": \"$hub_url_json\","
  fi
  if [ -n "$catalog_url" ]; then
    endpoint_block="$endpoint_block
  \"catalog_url\": \"$(json_escape "$catalog_url")\","
  fi

  # Подпись (S-B17): поле пишется всегда и явно. Отсутствие поля установщик трактует как
  # «не подписана», но «явно false» и «поля нет» — разные вещи для человека, читающего манифест.
  if [ "$is_signed" -eq 1 ]; then
    signed_json="true"
  else
    signed_json="false"
  fi
  if [ "$os" = "windows" ]; then
    purge_a='%USERPROFILE%\\.config\\opencode'
    purge_b='%USERPROFILE%\\.local\\share\\opencode'
  else
    # shellcheck disable=SC2016
    # Обоснование: подстановки раскрывает установщик при --purge (N5-R2), не shell сборки.
    purge_a='${XDG_CONFIG_HOME}/opencode'
    # shellcheck disable=SC2016
    # Обоснование: см. выше.
    purge_b='${XDG_DATA_HOME}/opencode'
  fi

  if [ -n "$desktop_rel" ]; then
    desktop_block=",
    {
      \"kind\": \"desktop\",
      \"file\": \"$desktop_rel_json\",
      \"sha256\": \"$desktop_sha\",
      \"size\": $desktop_size,
      \"installer_type\": \"$desktop_type\""
    if [ "$desktop_type" = "dmg" ]; then
      desktop_block="$desktop_block,
      \"app_name\": \"$desktop_app_json\""
    fi
    desktop_block="$desktop_block
    }"
  fi

  cat >"$dest" <<MANIFEST
{
  "schema": 1,
  "product": "opencode-magnit",
  "version": "$version",
  "os": "$os",
  "arch": "$arch",$endpoint_block
  "signed": $signed_json,
  "built_at": "$built_at",
  "source_release": "v$version",
  "ca": {
    "file": "certs/tander-ca-bundle.pem",
    "sha256": "$ca_sha",
    "install_name": "tander-ca-bundle.pem"
  },
  "artifacts": [
    {
      "kind": "cli",
      "file": "bin/$bin_name",
      "sha256": "$cli_sha",
      "size": $cli_size,
      "install_name": "$bin_name"
    }$desktop_block
  ],
  "purge_paths": [
    "$purge_a",
    "$purge_b"
  ]
}
MANIFEST
}

# --------------------------------------------------------------------------- самопроверка (N5-B2)
# Провал самопроверки не должен оставлять собранный архив в каталоге --out: битый пакет рядом с
# годными легко перепутать, а SHA256SUMS для него уже не создаётся (N5-B2).
selfcheck_fail() {
  local archive=$1
  shift
  rm -f "$archive"
  say "$MSG_SELFCHECK_DROPPED" "$archive"
  die "$EX_FAIL" "$@"
}

selfcheck_package() {
  local archive=$1 pkg_name=$2 os=$3 arch=$4 cli_sha=$5 ca_sha=$6
  local verify_dir="$tmp_root/verify-$os-$arch" root rc fake_home

  mkdir -p "$verify_dir"
  case $archive in
    *.tar.gz) tar -xzf "$archive" -C "$verify_dir" ;;
    *.zip) unzip -q -o "$archive" -d "$verify_dir" ;;
  esac
  root="$verify_dir/$pkg_name"

  if [ "$(sha256_of "$root/certs/tander-ca-bundle.pem")" != "$ca_sha" ]; then
    selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK_HASH" "certs/tander-ca-bundle.pem"
  fi
  if [ "$os" = "windows" ]; then
    if [ "$(sha256_of "$root/bin/opencode.exe")" != "$cli_sha" ]; then
      selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK_HASH" "bin/opencode.exe"
    fi
  else
    if [ "$(sha256_of "$root/bin/opencode")" != "$cli_sha" ]; then
      selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK_HASH" "bin/opencode"
    fi
  fi

  # Манифест — строгим парсером. Установщики его так не читают (awk-парсер install-posix.sh
  # терпим к сырым управляющим символам), поэтому синтаксическая порча манифеста без этой
  # проверки прошла бы самопроверку незамеченной. python3 требуется только на сборочной машине
  # и только здесь: в установщик такая зависимость не попадает (N5-P2).
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' \
      "$root/common/manifest.json" >/dev/null 2>&1; then
      selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK_JSON"
    fi
  else
    # Без python3 гарантия N5-B1/N5-B2 ослабевает — молча этого не делаем. Минимальная замена:
    # сырые управляющие символы внутри JSON запрещены RFC 8259 и означают провал экранирования
    # (bugs/BUG-I5-001) — ловим их побайтовым фильтром. Разрешён только перевод строки: сам
    # манифест печатается многострочным, отступы — пробелами.
    say "$MSG_WARN_NO_PYTHON" "$pkg_name"
    if [ "$(LC_ALL=C tr -d '\012\040-\377' <"$root/common/manifest.json" | wc -c | tr -d ' ')" != "0" ]; then
      selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK_JSON"
    fi
  fi

  fake_home="$tmp_root/fake-home-$os-$arch"
  mkdir -p "$fake_home"
  if [ "$os" = "windows" ]; then
    if command -v pwsh >/dev/null 2>&1; then
      rc=0
      pwsh -NoProfile -File "$root/install.ps1" -DryRun >/dev/null 2>&1 || rc=$?
      # Код 3 — «пакет для другой ОС»: на сборочной машине это ожидаемо (N5-I3).
      if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
        selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK" "$pkg_name" "$rc"
      fi
      say "$MSG_SELFCHECK_CROSS" "$pkg_name"
    else
      say "$MSG_SELFCHECK_NOPWSH" "$pkg_name"
    fi
    return 0
  fi

  rc=0
  HOME="$fake_home" SHELL=/bin/sh bash "$root/install.sh" --dry-run >/dev/null 2>&1 || rc=$?
  if [ "$os" = "$host_os" ] && [ "$arch" = "$host_arch" ]; then
    if [ "$rc" -ne 0 ]; then
      selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK" "$pkg_name" "$rc"
    fi
    say "$MSG_SELFCHECK_OK" "$pkg_name"
  else
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
      selfcheck_fail "$archive" "$MSG_ERR_SELFCHECK" "$pkg_name" "$rc"
    fi
    say "$MSG_SELFCHECK_CROSS" "$pkg_name"
  fi
}

# --------------------------------------------------------------------------- сборка одной цели
build_target() {
  local target=$1
  local os arch bin_name pkg_name stage cli_src archive run_cmd
  local ca_sha cli_sha cli_size
  local desktop_src="" desktop_rel="" desktop_sha="" desktop_size="0" desktop_type="" desktop_app=""
  local others=""

  case $target in
    darwin-arm64|darwin-x64|linux-x64|windows-x64) : ;;
    *) die "$EX_ARGS" "$MSG_ERR_BAD_TARGET" "$target" ;;
  esac
  os=${target%%-*}
  arch=${target#*-}

  bin_name="opencode"
  if [ "$os" = "windows" ]; then
    bin_name="opencode.exe"
  fi

  say "$MSG_BUILD" "$target"
  cli_src=$(find_cli_binary "$os" "$arch" "$bin_name")

  pkg_name="opencode-magnit-$os-$arch-$version"
  stage="$tmp_root/stage-$target/$pkg_name"
  mkdir -p "$stage/common" "$stage/bin" "$stage/certs"

  if [ "$os" = "windows" ]; then
    cp "$self_dir/windows/install.ps1" "$stage/install.ps1"
    cp "$self_dir/windows/install.bat" "$stage/install.bat"
    run_cmd='install.bat  (двойной клик или запуск из cmd)'
  else
    if [ "$os" = "darwin" ]; then
      cp "$self_dir/macos/install.sh" "$stage/install.sh"
    else
      cp "$self_dir/linux/install.sh" "$stage/install.sh"
    fi
    cp "$self_dir/common/install-posix.sh" "$stage/common/install-posix.sh"
    chmod 0755 "$stage/install.sh" "$stage/common/install-posix.sh"
    run_cmd="bash install.sh"
  fi

  cp "$cli_src" "$stage/bin/$bin_name"
  if [ "$os" != "windows" ]; then
    chmod 0755 "$stage/bin/$bin_name"
  fi
  cp "$ca_file" "$stage/certs/tander-ca-bundle.pem"

  if desktop_src=$(find_desktop_artifact "$os" "$arch"); then
    mkdir -p "$stage/desktop"
    cp "$desktop_src" "$stage/desktop/$(basename "$desktop_src")"
    desktop_rel="desktop/$(basename "$desktop_src")"
    desktop_sha=$(sha256_of "$stage/$desktop_rel")
    desktop_size=$(size_of "$stage/$desktop_rel")
    case $desktop_src in
      *.dmg) desktop_type="dmg"; desktop_app="OpenCode Magnit.app" ;;
      *.msi) desktop_type="msi" ;;
      *) desktop_type="nsis" ;;
    esac
  else
    desktop_src=""
    # Desktop для этой архитектуры собирали, но нужного бандла среди артефактов нет — называем
    # это дефектом здесь, в отчёте СБОРКИ. Иначе единственным следом останется штатная строка
    # установщика «Desktop: не входит в пакет» уже у пользователя (AC-271).
    if [ "$os" = "darwin" ]; then
      others=$(list_other_arch_dmgs "$arch")
      if [ -n "$others" ]; then
        say "$MSG_WARN_DESKTOP_ARCH" "$target" "$others"
      fi
    fi
  fi

  ca_sha=$(sha256_of "$stage/certs/tander-ca-bundle.pem")
  cli_sha=$(sha256_of "$stage/bin/$bin_name")
  cli_size=$(size_of "$stage/bin/$bin_name")

  write_manifest "$stage/common/manifest.json" "$os" "$arch" "$bin_name" \
    "$ca_sha" "$cli_sha" "$cli_size" \
    "$desktop_rel" "$desktop_sha" "$desktop_size" "$desktop_type" "$desktop_app"
  write_package_readme "$stage/README.md" "$os" "$arch" "$run_cmd"

  if [ "$os" = "windows" ]; then
    archive="$out_dir/$pkg_name.zip"
    rm -f "$archive"
    ( cd "$tmp_root/stage-$target" && zip -q -r -X "$archive" "$pkg_name" )
  else
    archive="$out_dir/$pkg_name.tar.gz"
    rm -f "$archive"
    ( cd "$tmp_root/stage-$target" && tar -czf "$archive" "$pkg_name" )
  fi
  say "$MSG_ARCHIVE" "$archive"
  selfcheck_package "$archive" "$pkg_name" "$os" "$arch" "$cli_sha" "$ca_sha"
  built_archives="$built_archives$archive
"
}

# --------------------------------------------------------------------------- основной ход
mkdir -p "$out_dir"
out_dir=$(cd -- "$out_dir" && pwd -P)
built_archives=""

old_ifs=$IFS
IFS=','
# shellcheck disable=SC2086
# Обоснование: разбиение списка целей по запятой — намеренное (bash 3.2, без массивов).
set -- $targets
IFS=$old_ifs
for target in "$@"; do
  [ -n "$target" ] || continue
  build_target "$target"
done

# SHA256SUMS в формате sha256sum: "<хеш>  <имя архива>" (N5-B3)
sums_file="$out_dir/SHA256SUMS"
: >"$sums_file"
printf '%s' "$built_archives" | while IFS= read -r archive; do
  [ -n "$archive" ] || continue
  printf '%s  %s\n' "$(sha256_of "$archive")" "$(basename "$archive")" >>"$sums_file"
done
say "$MSG_SUMS" "$sums_file"

if [ "$do_publish" -eq 1 ]; then
  if ! command -v gh >/dev/null 2>&1; then
    die "$EX_FAIL" "$MSG_ERR_NO_GH"
  fi
  gh release create "v$version" --draft --title "OpenCode Magnit $version" \
    --notes "Сборка $version. Отпечатки — в SHA256SUMS." \
    "$out_dir"/opencode-magnit-*.tar.gz "$out_dir"/opencode-magnit-*.zip "$sums_file"
  say "$MSG_PUBLISH_DONE" "v$version"
else
  say '%s' "$MSG_NO_PUBLISH"
fi
