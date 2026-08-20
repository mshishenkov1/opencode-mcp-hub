# shellcheck shell=bash
#
# installers/tests/bats/helpers.bash — общие функции тестов установщика (N5-T1).
#
# Изоляция: каждый тест работает в собственном $BATS_TEST_TMPDIR, $HOME подменён, корень
# установки задаётся --prefix, $SHELL задаётся явно. Тесты не вызывают sudo, не пишут вне
# временного каталога и не обращаются в сеть (сетевые утилиты подменены заглушками-ловушками).
# Хеши в фикстурных манифестах считаются на лету: захардкоженных sha256 нет (кроме тестов на
# заведомо неверный хеш).
# Совместимость: bash 3.2 (N5-T5).

INSTALLERS_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
REPO_ROOT=$(cd -- "$INSTALLERS_ROOT/.." && pwd -P)
FIXTURES_DIR="$INSTALLERS_ROOT/tests/fixtures"
export INSTALLERS_ROOT REPO_ROOT FIXTURES_DIR

# Утилиты, из которых собирается урезанный PATH (см. min_path_dir).
OM_TOOL_LIST="bash sh awk sed grep egrep fgrep cat cp mv rm mkdir rmdir chmod mktemp uname id \
find head tail cmp diff dirname basename wc sort ls date touch stat tr env printenv sudo \
shasum sha256sum openssl unzip zip tar gzip defaults hdiutil ditto python3 xxd od"

# ------------------------------------------------------------------ базовые утилиты

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{ print $1 }'
  else
    shasum -a 256 "$1" | awk '{ print $1 }'
  fi
}

mode_of() {
  if [ "$(uname -s)" = "Darwin" ]; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

mtime_of() {
  if [ "$(uname -s)" = "Darwin" ]; then
    stat -f '%m' "$1"
  else
    stat -c '%Y' "$1"
  fi
}

host_os() {
  case $(uname -s) in
    Darwin) printf 'darwin' ;;
    Linux) printf 'linux' ;;
    *) printf 'other' ;;
  esac
}

host_arch() {
  case $(uname -m) in
    arm64|aarch64) printf 'arm64' ;;
    x86_64|amd64) printf 'x64' ;;
    *) printf 'other' ;;
  esac
}

host_platform() {
  if [ "$(host_os)" = "darwin" ]; then
    printf 'macos'
  else
    printf 'linux'
  fi
}

# Значение os/arch, заведомо не совпадающее с системным (для проверок кода 3).
other_os() {
  if [ "$(host_os)" = "darwin" ]; then printf 'linux'; else printf 'darwin'; fi
}

other_arch() {
  if [ "$(host_arch)" = "arm64" ]; then printf 'x64'; else printf 'arm64'; fi
}

# ------------------------------------------------------------------ песочница

# Заглушки сетевых утилит и sudo: любой вызов фиксируется в файле-ловушке и завершается ошибкой.
# Так «оффлайновость» (N5-P6) и «sudo не вызывался» (N5-I7) проверяются наблюдаемо.
install_trap_stubs() {
  local dir="$BATS_TEST_TMPDIR/stub" tool
  mkdir -p "$dir"
  for tool in curl wget nc git npm brew apt apt-get winget choco gh sudo; do
    cat >"$dir/$tool" <<STUB
#!/usr/bin/env bash
printf '%s %s\n' "$tool" "\$*" >>"$BATS_TEST_TMPDIR/forbidden.log"
exit 97
STUB
    chmod 0755 "$dir/$tool"
  done
  : >"$BATS_TEST_TMPDIR/forbidden.log"
  PATH="$dir:$PATH"
  export PATH
}

forbidden_log() {
  cat "$BATS_TEST_TMPDIR/forbidden.log" 2>/dev/null || true
}

assert_no_forbidden_calls() {
  local log
  log=$(forbidden_log)
  if [ -n "$log" ]; then
    printf 'Вызваны запрещённые утилиты:\n%s\n' "$log" >&2
    return 1
  fi
  return 0
}

# Урезанный PATH: только перечисленные в OM_TOOL_LIST утилиты, кроме указанных в аргументах.
# Печатает путь к созданному каталогу.
min_path_dir() {
  local dir="$BATS_TEST_TMPDIR/minbin.$$.$RANDOM" tool src skip excluded=" $* "
  mkdir -p "$dir"
  for tool in $OM_TOOL_LIST; do
    case $excluded in
      *" $tool "*) continue ;;
    esac
    skip=0
    src=$(PATH="/usr/bin:/bin:/usr/sbin:/sbin" command -v "$tool" 2>/dev/null) || skip=1
    [ "$skip" -eq 0 ] || continue
    ln -sf "$src" "$dir/$tool"
  done
  printf '%s' "$dir"
}

setup_sandbox() {
  # Пути канонизируются (pwd -P): установщик печатает пути после realpath, и на macOS
  # /var/folders/... — символьная ссылка на /private/var/folders/...
  SANDBOX=$(cd -- "$BATS_TEST_TMPDIR" && pwd -P)
  export SANDBOX
  export HOME="$SANDBOX/home"
  export PREFIX_DIR="$SANDBOX/opt"
  export PKG="$SANDBOX/pkg"
  export TMPDIR="$SANDBOX/tmp"
  mkdir -p "$HOME" "$TMPDIR"
  export SHELL=/bin/zsh
  unset XDG_CONFIG_HOME XDG_DATA_HOME NODE_EXTRA_CA_CERTS OPENCODE_TEST_MARKER
  unset OPENCODE_INSTALLER_PLATFORM OPENCODE_INSTALLER_PKG_ROOT OPENCODE_INSTALLER_SOURCE_ONLY
  # bin_dir пакета не должен случайно оказаться в PATH хоста.
  install_trap_stubs
}

# ------------------------------------------------------------------ фикстурный пакет

# Фикстурный «бинарник»: shell-скрипт, печатающий версию (N5-T1).
write_fake_binary() {
  local dest=$1 version=$2
  sed -e "s/@@VERSION@@/$version/" "$FIXTURES_DIR/opencode-fake.sh" >"$dest"
  chmod 0755 "$dest"
}

write_noversion_binary() {
  local dest=$1
  cp "$FIXTURES_DIR/opencode-noversion.sh" "$dest"
  chmod 0755 "$dest"
}

# Манифест фикстурного пакета (N5-P3). Хеши считаются от фактических файлов пакета.
# Переопределения через переменные окружения: PKG_OS, PKG_ARCH, PKG_VERSION, PKG_HUB,
# PKG_DESKTOP (1 — добавить артефакт desktop), PKG_SHA_UPPER (1 — хеши заглавными),
# PKG_PURGE (список путей purge_paths, по одному в строке).
write_manifest() {
  local dir=$1
  local os=${PKG_OS:-$(host_os)}
  local arch=${PKG_ARCH:-$(host_arch)}
  local ver=${PKG_VERSION:-1.17.9-magnit.1}
  local hub=${PKG_HUB:-https://hub.test}
  local bin_name=opencode
  local ca_sha cli_sha cli_size desktop_block="" purge_block="" item first=1 list

  [ "$os" != "windows" ] || bin_name=opencode.exe
  ca_sha=$(sha256_file "$dir/certs/tander-ca-bundle.pem")
  cli_sha=$(sha256_file "$dir/bin/$bin_name")
  cli_size=$(wc -c <"$dir/bin/$bin_name" | tr -d ' ')
  if [ "${PKG_SHA_UPPER:-0}" = "1" ]; then
    ca_sha=$(printf '%s' "$ca_sha" | tr 'a-f' 'A-F')
    cli_sha=$(printf '%s' "$cli_sha" | tr 'a-f' 'A-F')
  fi

  if [ "${PKG_DESKTOP:-0}" = "1" ]; then
    local d_sha d_size
    d_sha=$(sha256_file "$dir/desktop/OpenCode.dmg")
    d_size=$(wc -c <"$dir/desktop/OpenCode.dmg" | tr -d ' ')
    desktop_block=",
    {
      \"kind\": \"desktop\",
      \"file\": \"desktop/OpenCode.dmg\",
      \"sha256\": \"$d_sha\",
      \"size\": $d_size,
      \"installer_type\": \"dmg\",
      \"app_name\": \"OpenCode.app\"
    }"
  fi

  # shellcheck disable=SC2016
  # Обоснование: ${XDG_CONFIG_HOME} и ${XDG_DATA_HOME} — текст манифеста (N5-P3), который
  # раскрывает установщик, а не shell теста.
  list=${PKG_PURGE:-'${XDG_CONFIG_HOME}/opencode
${XDG_DATA_HOME}/opencode'}
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    if [ "$first" -eq 1 ]; then
      purge_block="    \"$item\""
      first=0
    else
      purge_block="$purge_block,
    \"$item\""
    fi
  done <<PURGE
$list
PURGE

  cat >"$dir/common/manifest.json" <<MANIFEST
{
  "schema": 1,
  "product": "opencode-magnit",
  "version": "$ver",
  "os": "$os",
  "arch": "$arch",
  "hub_url": "$hub",
  "built_at": "2026-08-18T09:41:07Z",
  "source_release": "v$ver",
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
$purge_block
  ]
}
MANIFEST
}

# Собирает распакованный пакет (N5-P2) в каталоге $1 (по умолчанию $PKG).
make_pkg() {
  local dir=${1:-$PKG}
  local platform=${PKG_PLATFORM:-$(host_platform)}
  local ver=${PKG_VERSION:-1.17.9-magnit.1}
  mkdir -p "$dir/common" "$dir/bin" "$dir/certs"
  cp "$INSTALLERS_ROOT/$platform/install.sh" "$dir/install.sh"
  cp "$INSTALLERS_ROOT/common/install-posix.sh" "$dir/common/install-posix.sh"
  chmod 0755 "$dir/install.sh" "$dir/common/install-posix.sh"
  cp "$FIXTURES_DIR/ca-sample.pem" "$dir/certs/tander-ca-bundle.pem"
  write_fake_binary "$dir/bin/opencode" "$ver"
  if [ "${PKG_DESKTOP:-0}" = "1" ]; then
    mkdir -p "$dir/desktop"
    printf 'FIXTURE dmg payload\n' >"$dir/desktop/OpenCode.dmg"
  fi
  printf '# OpenCode Magnit %s\n' "$ver" >"$dir/README.md"
  write_manifest "$dir"
}

# Точечная правка сгенерированного манифеста sed-выражением (формат манифеста фиксирован).
manifest_edit() {
  local expr=$1 dir=${2:-$PKG} tmp
  tmp=$(mktemp "$TMPDIR/manifest.XXXXXX")
  sed -e "$expr" "$dir/common/manifest.json" >"$tmp"
  cat "$tmp" >"$dir/common/manifest.json"
  rm -f "$tmp"
}

# ------------------------------------------------------------------ фикстурные артефакты CI форка

# Каталог артефактов для release.sh: zip-архивы вида opencode-<os>-<arch>-<ver>.zip с bin/<имя>
# и CA-файл. Аргументы: <каталог> <версия> <цель>...
make_artifacts() {
  local dir=$1 ver=$2
  shift 2
  local target os arch bin stage
  mkdir -p "$dir"
  for target in "$@"; do
    os=${target%%-*}
    arch=${target#*-}
    bin=opencode
    [ "$os" != "windows" ] || bin=opencode.exe
    stage="$dir/.stage-$target"
    mkdir -p "$stage/bin"
    write_fake_binary "$stage/bin/$bin" "$ver"
    ( cd "$stage" && zip -q -r "$dir/opencode-$os-$arch-$ver.zip" bin )
    rm -rf "$stage"
  done
  cp "$FIXTURES_DIR/ca-sample.pem" "$dir/tander-ca-bundle.pem"
}

release_run() {
  run bash "$INSTALLERS_ROOT/release.sh" "$@"
}

sums_check() {
  local dir=$1
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$dir" && sha256sum -c SHA256SUMS )
  else
    ( cd "$dir" && shasum -a 256 -c SHA256SUMS )
  fi
}

# Значение поля манифеста верхнего уровня (строка) — без внешних зависимостей.
manifest_field() {
  local file=$1 key=$2
  sed -n "s/^  \"$key\": \"\{0,1\}\([^\",]*\)\"\{0,1\},\{0,1\}$/\1/p" "$file" | head -1
}

# sha256 артефакта заданного kind из манифеста собранного пакета.
manifest_artifact_sha() {
  local file=$1 kind=$2
  awk -v kind="\"kind\": \"$kind\"" '
    index($0, kind) > 0 { found = 1 }
    found == 1 && index($0, "\"sha256\"") > 0 {
      line = $0
      sub(/^[^:]*: "/, "", line)
      sub(/".*$/, "", line)
      print line
      exit
    }
  ' "$file"
}

# ------------------------------------------------------------------ запуск установщика

# Запуск установщика пакета с изолированным корнем установки.
oc_run() {
  run bash "$PKG/install.sh" --prefix "$PREFIX_DIR" "$@"
}

# Запуск без --prefix (проверки умолчания $HOME/.local/bin).
oc_run_default() {
  run bash "$PKG/install.sh" "$@"
}

bin_dir_path() {
  printf '%s/bin' "$PREFIX_DIR"
}

config_dir_path() {
  printf '%s/opencode' "${XDG_CONFIG_HOME:-$HOME/.config}"
}

# ------------------------------------------------------------------ снимок ФС

# Снимок каталога без меток времени: путь, тип, права, содержимое (sha256).
snapshot_dir() {
  local root=$1 out=$2 p
  : >"$out"
  [ -d "$root" ] || return 0
  ( cd "$root" && find . -print ) | LC_ALL=C sort | while IFS= read -r p; do
    if [ -L "$root/$p" ]; then
      printf '%s\tlink\t%s\n' "$p" "$(readlink "$root/$p")" >>"$out"
    elif [ -d "$root/$p" ]; then
      printf '%s\tdir\t%s\n' "$p" "$(mode_of "$root/$p")" >>"$out"
    elif [ -f "$root/$p" ]; then
      printf '%s\tfile\t%s\t%s\n' "$p" "$(mode_of "$root/$p")" "$(sha256_file "$root/$p")" >>"$out"
    else
      printf '%s\tother\n' "$p" >>"$out"
    fi
  done
  return 0
}

snapshot_home() {
  snapshot_dir "$HOME" "$1"
}

# ------------------------------------------------------------------ утверждения
#
# shellcheck disable=SC2154
# Обоснование: переменные $status и $output задаёт команда `run` самого bats — присваивания
# в этом файле нет и быть не может.

assert_status() {
  local want=$1
  if [ "$status" -ne "$want" ]; then
    printf 'Ожидался код выхода %s, получен %s\nВывод:\n%s\n' "$want" "$status" "$output" >&2
    return 1
  fi
  return 0
}

assert_output_contains() {
  case $output in
    *"$1"*) return 0 ;;
  esac
  printf 'В выводе нет подстроки: %s\nВывод:\n%s\n' "$1" "$output" >&2
  return 1
}

refute_output_contains() {
  case $output in
    *"$1"*)
      printf 'В выводе найдена нежелательная подстрока: %s\nВывод:\n%s\n' "$1" "$output" >&2
      return 1
      ;;
  esac
  return 0
}

assert_file_contains() {
  if grep -F -q -e "$2" "$1"; then
    return 0
  fi
  printf 'В файле %s нет подстроки: %s\n' "$1" "$2" >&2
  return 1
}

refute_file_contains() {
  if grep -F -q -e "$2" "$1"; then
    printf 'В файле %s найдена нежелательная подстрока: %s\n' "$1" "$2" >&2
    return 1
  fi
  return 0
}

count_lines_with() {
  grep -F -c -e "$2" "$1" 2>/dev/null || printf '0'
}

# Число строк вывода установщика, содержащих подстроку.
count_output_lines_with() {
  printf '%s\n' "$output" | grep -F -c -e "$1" || true
}

# Загрузка install-posix.sh как библиотеки: константы и функции без выполнения (N5-T1).
source_installer() {
  OPENCODE_INSTALLER_SOURCE_ONLY=1
  export OPENCODE_INSTALLER_SOURCE_ONLY
  # shellcheck source=/dev/null
  . "$INSTALLERS_ROOT/common/install-posix.sh"
}
