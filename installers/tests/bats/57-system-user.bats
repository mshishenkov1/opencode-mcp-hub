#!/usr/bin/env bats
#
# Пользовательская часть при --system: исходный пользователь и владельцы (N5-I14, N5-I7).
#
# --system означает «бинарник в системный каталог», а не «вся установка от имени root».
# CA, каталог конфига, файл профиля и резервные копии относятся к дому ИСХОДНОГО пользователя
# и принадлежат ему. Опираться на $HOME нельзя: под sudo он указывает то на /var/root (/root),
# то на дом пользователя — в зависимости от env_reset. Тихая установка в дом root с кодом 0
# запрещена: пользователь остался бы без CA и без NODE_EXTRA_CA_CERTS, а установщик отчитался
# бы об успехе.
#
# Границы среды подменяются ловушками в PATH: id (эффективный uid, имя, группа), logname,
# getent/dscl (база пользователей), chown (владелец) и sudo (из install_trap_stubs). Логика
# выбора пользователя, раскладки путей и назначения владельцев проверяется как есть.
# Совместимость: bash 3.2 (N5-T5).

setup() {
  load helpers
  setup_sandbox
  make_pkg
  ALICE_HOME="$SANDBOX/users/alice"
  ROOT_HOME="$SANDBOX/users/root"
  CHOWN_LOG="$BATS_TEST_TMPDIR/chown.log"
  export ALICE_HOME ROOT_HOME CHOWN_LOG
  mkdir -p "$ALICE_HOME" "$ROOT_HOME"
  : >"$CHOWN_LOG"
  install_user_stubs
}

# Ловушки границы среды. Ответы задаются переменными окружения, каждый вызов журналируется.
install_user_stubs() {
  local dir="$BATS_TEST_TMPDIR/userstub"
  mkdir -p "$dir"

  cat >"$dir/id" <<'STUB'
#!/usr/bin/env bash
case ${1:-} in
  -u) printf '%s\n' "${FAKE_UID:-0}" ;;
  -un) printf '%s\n' "${FAKE_UNAME:-root}" ;;
  -gn) printf '%s\n' "${FAKE_GNAME:-staff}" ;;
  -g) printf '%s\n' "${FAKE_GID:-20}" ;;
  *) exit 1 ;;
esac
STUB

  cat >"$dir/logname" <<'STUB'
#!/usr/bin/env bash
[ -n "${FAKE_LOGNAME:-}" ] || exit 1
printf '%s\n' "$FAKE_LOGNAME"
STUB

  # База пользователей: одна запись — FAKE_DB_USER с домом FAKE_DB_HOME.
  cat >"$dir/getent" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" = passwd ] || exit 2
[ -n "${FAKE_DB_USER:-}" ] && [ "${2:-}" = "$FAKE_DB_USER" ] || exit 2
printf '%s:x:1000:1000::%s:/bin/sh\n' "$FAKE_DB_USER" "$FAKE_DB_HOME"
STUB

  cat >"$dir/dscl" <<'STUB'
#!/usr/bin/env bash
# macOS-ветка базы пользователей: отвечает тем же, что getent.
for arg in "$@"; do
  case $arg in
    /Users/*) user=${arg#/Users/} ;;
  esac
done
[ -n "${FAKE_DB_USER:-}" ] && [ "${user:-}" = "$FAKE_DB_USER" ] || exit 2
printf 'NFSHomeDirectory: %s\n' "$FAKE_DB_HOME"
STUB

  cat >"$dir/chown" <<STUB
#!/usr/bin/env bash
printf '%s\t%s\n' "\$1" "\$2" >>"$CHOWN_LOG"
exit 0
STUB

  chmod 0755 "$dir/id" "$dir/logname" "$dir/getent" "$dir/dscl" "$dir/chown"
  PATH="$dir:$PATH"
  export PATH
}

chown_log() {
  cat "$CHOWN_LOG" 2>/dev/null || true
}

assert_chowned() {
  local path=$1 owner=$2
  if ! grep -F -x -q "$owner	$path" "$CHOWN_LOG"; then
    printf 'Нет вызова chown %s %s. Журнал:\n%s\n' "$owner" "$path" "$(chown_log)" >&2
    return 1
  fi
  return 0
}

# ------------------------------------------------------------------ AC-148: выбор пользователя

write_resolver_driver() {
  cat >"$BATS_TEST_TMPDIR/resolve.sh" <<'DRV'
#!/usr/bin/env bash
# shellcheck disable=SC2034
# Обоснование: opt_system читает загруженный через source install-posix.sh — присваивание здесь
# заменяет то, что в обычном запуске делает parse_args.
OPENCODE_INSTALLER_SOURCE_ONLY=1
export OPENCODE_INSTALLER_SOURCE_ONLY
# shellcheck source=/dev/null
. "$INSTALLERS_ROOT/common/install-posix.sh"
opt_system=${OPT_SYSTEM:-1}
resolve_source_user
printf 'user=%s\n' "$src_user"
printf 'home=%s\n' "$user_home"
printf 'chown=%s\n' "$need_chown"
DRV
}

# Аргументы задаются переменными окружения ловушек; печатает результат резолвера.
run_resolver() {
  write_resolver_driver
  run bash "$BATS_TEST_TMPDIR/resolve.sh"
}

@test "AC-148 (а): uid != 0 — текущий пользователь, дом из \$HOME, chown не нужен" {
  export FAKE_UID=1000 FAKE_UNAME=alice
  export HOME="$ALICE_HOME"
  unset SUDO_USER
  run_resolver
  assert_status 0
  assert_output_contains "user=alice"
  assert_output_contains "home=$ALICE_HOME"
  assert_output_contains "chown=0"
  assert_no_forbidden_calls
  [ -z "$(chown_log)" ]
}

@test "AC-148 (б): uid = 0, SUDO_USER=alice, HOME=/var/root — дом берётся ИЗ БАЗЫ, а не из \$HOME" {
  export FAKE_UID=0 SUDO_USER=alice FAKE_DB_USER=alice FAKE_DB_HOME="$ALICE_HOME"
  export HOME="$ROOT_HOME"
  run_resolver
  assert_status 0
  assert_output_contains "user=alice"
  assert_output_contains "home=$ALICE_HOME"
  assert_output_contains "chown=1"
  refute_output_contains "home=$ROOT_HOME"
  assert_no_forbidden_calls
}

@test "AC-148 (в): uid = 0, SUDO_USER=alice, HOME совпадает с домом из базы — тот же результат" {
  export FAKE_UID=0 SUDO_USER=alice FAKE_DB_USER=alice FAKE_DB_HOME="$ALICE_HOME"
  export HOME="$ALICE_HOME"
  run_resolver
  assert_status 0
  assert_output_contains "user=alice"
  assert_output_contains "home=$ALICE_HOME"
  assert_output_contains "chown=1"
}

@test "AC-148 (г): uid = 0, SUDO_USER=root — имя берётся из logname" {
  export FAKE_UID=0 SUDO_USER=root FAKE_LOGNAME=alice
  export FAKE_DB_USER=alice FAKE_DB_HOME="$ALICE_HOME"
  export HOME="$ROOT_HOME"
  run_resolver
  assert_status 0
  assert_output_contains "user=alice"
  assert_output_contains "home=$ALICE_HOME"
  assert_output_contains "chown=1"
}

@test "AC-148 (д): uid = 0, SUDO_USER и logname пусты → код 5, сообщение и ни одного вызова sudo" {
  export FAKE_UID=0
  unset SUDO_USER
  export FAKE_LOGNAME=""
  export HOME="$ROOT_HOME"
  run_resolver
  assert_status 5
  assert_output_contains "--system"
  assert_output_contains "SUDO_USER"
  assert_output_contains "logname"
  assert_no_forbidden_calls
  [ -z "$(chown_log)" ]
}

@test "AC-148 (е): uid = 0, SUDO_USER=alice, но каталога дома нет → код 5" {
  export FAKE_UID=0 SUDO_USER=alice FAKE_DB_USER=alice FAKE_DB_HOME="$SANDBOX/users/ghost"
  export HOME="$ROOT_HOME"
  [ ! -d "$SANDBOX/users/ghost" ]
  run_resolver
  assert_status 5
  assert_output_contains "--system"
  assert_no_forbidden_calls
  [ -z "$(chown_log)" ]
}

@test "AC-148: без --system правило не применяется — root ставит в собственный дом" {
  export FAKE_UID=0 FAKE_UNAME=root OPT_SYSTEM=0
  unset SUDO_USER
  export HOME="$ROOT_HOME"
  run_resolver
  assert_status 0
  assert_output_contains "user=root"
  assert_output_contains "home=$ROOT_HOME"
  assert_output_contains "chown=0"
}

# ------------------------------------------------------------------ AC-149: полный прогон --system
#
# Единственная подмена сверх ловушек среды — каталог бинарника: compute_layout берёт для --system
# литерал /usr/local/bin, писать в который тест не имеет права. Значение, которое вычислил сам
# установщик, при этом проверяется (SYSTEM_BIN_DIR в выводе драйвера).

write_system_driver() {
  cat >"$BATS_TEST_TMPDIR/system.sh" <<'DRV'
#!/usr/bin/env bash
OPENCODE_INSTALLER_SOURCE_ONLY=1
export OPENCODE_INSTALLER_SOURCE_ONLY
# shellcheck source=/dev/null
. "$PKG/common/install-posix.sh"

parse_args --system --no-launch --no-desktop
resolve_pkg_root
manifest_load
check_system
require_hash_tool
resolve_source_user
compute_layout
printf 'SYSTEM_BIN_DIR=%s\n' "$bin_dir"
# Подмена каталога бинарника: /usr/local/bin недоступен тесту.
bin_dir=$FAKE_BIN
path_require "$bin_dir" "каталог бинарника"
bin_dir=$REPLY
path_require_child "$bin_dir" "$MF_cli_name" "artifacts[].install_name"
bin_target=$REPLY
bin_backup="$bin_target.bak"
require_tmpdir
verify_package
do_install
DRV
}

run_system_install() {
  write_system_driver
  export FAKE_BIN="$SANDBOX/fakebin"
  mkdir -p "$FAKE_BIN"
  export OPENCODE_INSTALLER_PKG_ROOT="$PKG" OPENCODE_INSTALLER_PLATFORM=$(host_platform)
  run bash "$BATS_TEST_TMPDIR/system.sh"
}

@test "AC-149: --system из сессии root — пользовательская часть в доме alice, владелец alice" {
  export FAKE_UID=0 FAKE_UNAME=root FAKE_GNAME=staff
  export SUDO_USER=alice FAKE_DB_USER=alice FAKE_DB_HOME="$ALICE_HOME"
  export HOME="$ROOT_HOME" SHELL=/bin/zsh
  # Профиль пользователя уже существует без нашего блока — появится резервная копия.
  printf 'export USER_ONE=1\n' >"$ALICE_HOME/.zshrc"
  snapshot_dir "$ROOT_HOME" "$BATS_TEST_TMPDIR/root.before"

  run_system_install
  assert_status 0

  # Раскладка сама по себе указывает на системный каталог — подмена его не «вылечила».
  assert_output_contains "SYSTEM_BIN_DIR=/usr/local/bin"
  assert_output_contains "Пользователь: alice ($ALICE_HOME)"

  local config_dir="$ALICE_HOME/.config/opencode"
  local ca="$config_dir/tander-ca-bundle.pem"
  local profile="$ALICE_HOME/.zshrc"
  local backup="$profile.opencode-magnit.bak"
  [ -f "$ca" ] || { printf 'CA не установлен в доме alice: %s\n' "$ca" >&2; return 1; }
  [ -d "$config_dir" ]
  [ -f "$backup" ] || { printf 'Нет резервной копии профиля: %s\n' "$backup" >&2; return 1; }
  assert_file_contains "$profile" "# >>> opencode-magnit >>>"
  assert_file_contains "$profile" "export NODE_EXTRA_CA_CERTS='$ca'"
  # В доме root не создано ничего.
  snapshot_dir "$ROOT_HOME" "$BATS_TEST_TMPDIR/root.after"
  diff "$BATS_TEST_TMPDIR/root.before" "$BATS_TEST_TMPDIR/root.after" >&2 || {
    printf 'В доме root что-то создано\n' >&2
    return 1
  }

  # Владелец назначен ровно объектам пользовательской части.
  assert_chowned "$config_dir" "alice:staff"
  assert_chowned "$ca" "alice:staff"
  assert_chowned "$profile" "alice:staff"
  assert_chowned "$backup" "alice:staff"

  # Ни один chown не относится к каталогу бинарника: /usr/local/bin остаётся root-овым.
  if grep -F -q "$FAKE_BIN" "$CHOWN_LOG"; then
    printf 'chown затронул каталог бинарника:\n%s\n' "$(chown_log)" >&2
    return 1
  fi
  # Процесс уже root — привилегии поднимать не через что: ловушка sudo не вызывалась.
  assert_no_forbidden_calls
  [ -x "$FAKE_BIN/opencode" ]
}

@test "AC-149: --system не под root — операции с bin_dir идут через sudo, пользовательская часть нет" {
  # Обратная ветка N5-I7: uid != 0 → use_sudo=1, и КАЖДАЯ операция с каталогом бинарника уходит
  # в ловушку sudo (она возвращает 97), а CA и профиль пишутся напрямую от имени пользователя.
  export FAKE_UID=1000 FAKE_UNAME=alice FAKE_GNAME=staff
  unset SUDO_USER
  export HOME="$ALICE_HOME" SHELL=/bin/zsh
  run_system_install
  # Ловушка sudo завершает копирование ошибкой — код не 0, и это ожидаемо.
  [ "$status" -ne 0 ]
  local log
  log=$(forbidden_log)
  printf '%s\n' "$log" | grep -F -q 'sudo ' || {
    printf 'Операции с каталогом бинарника не пошли через sudo:\n%s\n' "$log" >&2
    return 1
  }
  # Пользовательская часть выполнена без sudo и до отказа: CA уже на месте.
  [ -f "$ALICE_HOME/.config/opencode/tander-ca-bundle.pem" ]
  if printf '%s\n' "$log" | grep -F -q "$ALICE_HOME/.config"; then
    printf 'Пользовательская часть ушла в sudo:\n%s\n' "$log" >&2
    return 1
  fi
  # uid != 0 → владелец и так верный, chown не нужен.
  [ -z "$(chown_log)" ] || { printf 'Лишние вызовы chown:\n%s\n' "$(chown_log)" >&2; return 1; }
}

# ------------------------------------------------------------------ AC-151: --dry-run и --check

@test "AC-151: --dry-run --system и --check --system — пути в доме alice, ни одной файловой операции" {
  export FAKE_UID=0 FAKE_UNAME=root FAKE_GNAME=staff
  export SUDO_USER=alice FAKE_DB_USER=alice FAKE_DB_HOME="$ALICE_HOME"
  export HOME="$ROOT_HOME"
  snapshot_dir "$ALICE_HOME" "$BATS_TEST_TMPDIR/alice.before"
  snapshot_dir "$ROOT_HOME" "$BATS_TEST_TMPDIR/root.before"

  run bash "$PKG/install.sh" --dry-run --system
  assert_status 0
  assert_output_contains "Пользователь: alice ($ALICE_HOME)"
  assert_output_contains "$ALICE_HOME/.config/opencode/tander-ca-bundle.pem"
  assert_output_contains "/usr/local/bin/opencode"

  run bash "$PKG/install.sh" --check --system
  # Ничего не установлено → расхождение, код 7; манифест при этом корректен.
  assert_status 7
  assert_output_contains "Пользователь: alice ($ALICE_HOME)"
  assert_output_contains "Манифест: корректен"
  assert_output_contains "$ALICE_HOME/.config/opencode"

  snapshot_dir "$ALICE_HOME" "$BATS_TEST_TMPDIR/alice.after"
  snapshot_dir "$ROOT_HOME" "$BATS_TEST_TMPDIR/root.after"
  diff "$BATS_TEST_TMPDIR/alice.before" "$BATS_TEST_TMPDIR/alice.after" >&2
  diff "$BATS_TEST_TMPDIR/root.before" "$BATS_TEST_TMPDIR/root.after" >&2
  [ ! -e /usr/local/bin/opencode ] || skip "на этой машине /usr/local/bin/opencode существует вне теста"
  # Ловушки sudo и chown не вызывались ни разу.
  assert_no_forbidden_calls
  [ -z "$(chown_log)" ] || { printf 'Вызовы chown в режиме без изменений:\n%s\n' "$(chown_log)" >&2; return 1; }
}

@test "AC-151, AC-148 (д): --dry-run --system без определимого пользователя → код 5 и пустой план" {
  export FAKE_UID=0 FAKE_LOGNAME=""
  unset SUDO_USER
  export HOME="$ROOT_HOME"
  snapshot_dir "$ROOT_HOME" "$BATS_TEST_TMPDIR/root.before"
  run bash "$PKG/install.sh" --dry-run --system
  assert_status 5
  assert_output_contains "--system"
  refute_output_contains "План установки"
  snapshot_dir "$ROOT_HOME" "$BATS_TEST_TMPDIR/root.after"
  diff "$BATS_TEST_TMPDIR/root.before" "$BATS_TEST_TMPDIR/root.after" >&2
  assert_no_forbidden_calls
}

# ------------------------------------------------------------------ пригодность TMPDIR
#
# mktemp используется на шаге профиля — уже после установки CA и бинарника. Непригодный TMPDIR
# оборвал бы установку посередине системной английской диагностикой mktemp, оставив частичную
# установку. Поэтому пригодность каталога проверяется предусловием (N5-I1, код 1).

@test "AC-52: непригодный TMPDIR отвергается предусловием — код 1, русское сообщение, ничего не установлено" {
  export TMPDIR="$SANDBOX/no-such-tmp"
  [ ! -d "$TMPDIR" ]
  snapshot_home "$BATS_TEST_TMPDIR/home.before"
  oc_run --no-launch
  assert_status 1
  assert_output_contains "Каталог временных файлов недоступен для записи: $TMPDIR"
  assert_output_contains "TMPDIR"
  [ ! -e "$(bin_dir_path)/opencode" ]
  [ ! -e "$(config_dir_path)/tander-ca-bundle.pem" ]
  snapshot_home "$BATS_TEST_TMPDIR/home.after"
  diff "$BATS_TEST_TMPDIR/home.before" "$BATS_TEST_TMPDIR/home.after" >&2
}

@test "AC-52: TMPDIR без права записи отвергается предусловием, а --check и --dry-run не требуют его" {
  if is_root; then
    skip "прогон под root: снятый бит записи root не останавливает — проверка невозможна"
  fi
  export TMPDIR="$SANDBOX/ro-tmp"
  mkdir -p "$TMPDIR"
  chmod 0555 "$TMPDIR"
  oc_run --no-launch
  local install_status=$status
  # Режимы без изменений временных файлов не создают и обязаны работать.
  oc_run --check
  local check_status=$status
  oc_run --dry-run
  local plan_status=$status
  chmod 0755 "$TMPDIR"
  [ "$install_status" -eq 1 ] || { printf 'Ожидался код 1, получен %s\n' "$install_status" >&2; return 1; }
  [ "$check_status" -eq 7 ] || { printf '--check: ожидался код 7, получен %s\n' "$check_status" >&2; return 1; }
  [ "$plan_status" -eq 0 ] || { printf '--dry-run: ожидался код 0, получен %s\n' "$plan_status" >&2; return 1; }
}
