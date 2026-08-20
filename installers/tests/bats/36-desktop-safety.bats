#!/usr/bin/env bats
#
# Безопасность установки Desktop: обязательность artifacts[].app_name при installer_type="dmg"
# и страховка перед разрушающими операциями (N5-P4, N5-R1).
#
# Модель угрозы: install_desktop подставляет app_name в rm -rf "<dest>/<app_name>", а при
# --system — через sudo. Отсутствующее, пустое, пробельное или «схлопывающееся» значение (".",
# "./OpenCode.app", "OpenCode.app/") дало бы целью сам каталог назначения, то есть rm -rf всего
# /Applications или $HOME/Applications. Такой манифест обязан отвергаться на разборе (код 2) до
# единой файловой операции — на всех путях исполнения, включая --check, --dry-run и --uninstall.
# Совместимость: bash 3.2 (N5-T5).

setup() {
  load helpers
  setup_sandbox
  PKG_DESKTOP=1 make_pkg
  # Каталог-жертва: имитирует содержимое каталога приложений, которое снёс бы rm -rf "<dest>/".
  VICTIM="$SANDBOX/victim"
  export VICTIM
  mkdir -p "$VICTIM" "$HOME/Applications"
  printf 'МАРКЕР ЖЕРТВЫ\n' >"$VICTIM/keep.txt"
  printf 'ЧУЖОЕ ПРИЛОЖЕНИЕ\n' >"$HOME/Applications/Other.marker"
}

set_app_name() {
  manifest_edit "s|\"app_name\": \"OpenCode.app\"|\"app_name\": \"$1\"|"
}

# Полное удаление поля app_name из desktop-артефакта (вместе с запятой предыдущей строки).
drop_app_name() {
  manifest_edit '/"app_name": "OpenCode.app"/d'
  manifest_edit 's|\("installer_type": "[a-z]*"\),|\1|'
}

snapshot_state() {
  local tag=$1
  snapshot_dir "$HOME" "$BATS_TEST_TMPDIR/$tag.home"
  snapshot_dir "$PREFIX_DIR" "$BATS_TEST_TMPDIR/$tag.prefix"
  snapshot_dir "$VICTIM" "$BATS_TEST_TMPDIR/$tag.victim"
}

assert_no_file_changes() {
  local tag
  snapshot_state after
  for tag in home prefix victim; do
    if ! diff "$BATS_TEST_TMPDIR/before.$tag" "$BATS_TEST_TMPDIR/after.$tag" >&2; then
      printf 'Установщик изменил каталог (%s), хотя манифест невалиден\n' "$tag" >&2
      return 1
    fi
  done
  return 0
}

# Каталог приложений пользователя и каталог-жертва не тронуты.
assert_app_dirs_intact() {
  [ -f "$VICTIM/keep.txt" ] || { printf 'Каталог-жертва пострадал\n' >&2; return 1; }
  [ -d "$HOME/Applications" ] || { printf 'Удалён каталог приложений пользователя\n' >&2; return 1; }
  [ -f "$HOME/Applications/Other.marker" ] || { printf 'Удалено содержимое каталога приложений пользователя\n' >&2; return 1; }
  return 0
}

# Один прогон установщика с невалидным app_name: код 2, названо поле, ничего не изменено.
# Аргументы: <ожидаемая причина в сообщении> <аргументы install.sh>...
assert_rejected() {
  local reason=$1
  shift
  snapshot_state before
  oc_run "$@"
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_output_contains "$reason"
  refute_output_contains "Манифест: корректен"
  refute_output_contains "Desktop: установлен"
  refute_output_contains "Desktop: удалён"
  assert_app_dirs_intact
  assert_no_file_changes
  assert_no_forbidden_calls
}

# ------------------------------------------------------------------ app_name отсутствует/пуст

@test "AC-138: dmg-артефакт без поля app_name → код 2 на установке, каталоги приложений целы" {
  drop_app_name
  assert_rejected "обязательное поле отсутствует или пусто" --no-launch
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-138: dmg-артефакт без app_name → код 2 при --check (а не «Манифест: корректен»)" {
  drop_app_name
  assert_rejected "обязательное поле отсутствует или пусто" --check
}

@test "AC-138: dmg-артефакт без app_name → код 2 при --dry-run, плана установки нет" {
  drop_app_name
  assert_rejected "обязательное поле отсутствует или пусто" --dry-run
  refute_output_contains "План установки OpenCode"
}

@test "AC-138: dmg-артефакт без app_name → код 2 при --uninstall, --dry-run --uninstall и --uninstall --purge" {
  drop_app_name
  assert_rejected "обязательное поле отсутствует или пусто" --uninstall
  assert_rejected "обязательное поле отсутствует или пусто" --dry-run --uninstall
  refute_output_contains "План удаления OpenCode"
  assert_rejected "обязательное поле отсутствует или пусто" --uninstall --purge
  refute_output_contains "Удаляются пользовательские данные:"
}

@test "AC-138: пустое app_name=\"\" → код 2 на всех шести режимах запуска" {
  set_app_name ""
  assert_rejected "обязательное поле отсутствует или пусто" --no-launch
  assert_rejected "обязательное поле отсутствует или пусто" --check
  assert_rejected "обязательное поле отсутствует или пусто" --dry-run
  assert_rejected "обязательное поле отсутствует или пусто" --uninstall
  assert_rejected "обязательное поле отсутствует или пусто" --dry-run --uninstall
  assert_rejected "обязательное поле отсутствует или пусто" --uninstall --purge
}

@test "AC-138: пробельное app_name=\"   \" равносильно пустому → код 2 на установке и при удалении" {
  set_app_name "   "
  assert_rejected "обязательное поле отсутствует или пусто" --no-launch
  assert_rejected "обязательное поле отсутствует или пусто" --uninstall
  assert_rejected "обязательное поле отсутствует или пусто" --dry-run --uninstall
}

@test "AC-138: app_name=\"\\t\" (табуляция) равносильно пустому → код 2" {
  set_app_name "\\t"
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_app_dirs_intact
  assert_no_file_changes
}

# ------------------------------------------------------------------ «схлопывающиеся» значения

@test "AC-135, AC-138: app_name=\".\" → код 2 на всех шести режимах (иначе целью стал бы сам каталог)" {
  set_app_name "."
  assert_rejected "недопустимый путь: ." --no-launch
  assert_rejected "недопустимый путь: ." --check
  assert_rejected "недопустимый путь: ." --dry-run
  assert_rejected "недопустимый путь: ." --uninstall
  assert_rejected "недопустимый путь: ." --dry-run --uninstall
  assert_rejected "недопустимый путь: ." --uninstall --purge
}

@test "AC-135, AC-138: app_name=\"./OpenCode.app\" → код 2 на всех шести режимах" {
  set_app_name "./OpenCode.app"
  assert_rejected "недопустимый путь: ./OpenCode.app" --no-launch
  assert_rejected "недопустимый путь: ./OpenCode.app" --check
  assert_rejected "недопустимый путь: ./OpenCode.app" --dry-run
  assert_rejected "недопустимый путь: ./OpenCode.app" --uninstall
  assert_rejected "недопустимый путь: ./OpenCode.app" --dry-run --uninstall
  assert_rejected "недопустимый путь: ./OpenCode.app" --uninstall --purge
}

@test "AC-135, AC-138: app_name=\"OpenCode.app/\" с завершающим слэшем → код 2 на всех шести режимах" {
  set_app_name "OpenCode.app/"
  assert_rejected "недопустимый путь: OpenCode.app/" --no-launch
  assert_rejected "недопустимый путь: OpenCode.app/" --check
  assert_rejected "недопустимый путь: OpenCode.app/" --dry-run
  assert_rejected "недопустимый путь: OpenCode.app/" --uninstall
  assert_rejected "недопустимый путь: OpenCode.app/" --dry-run --uninstall
  assert_rejected "недопустимый путь: OpenCode.app/" --uninstall --purge
}

@test "AC-135, AC-138: app_name=\"sub/OpenCode.app\" — имя приложения не может содержать разделитель" {
  set_app_name "sub/OpenCode.app"
  assert_rejected "недопустимый путь: sub/OpenCode.app" --no-launch
  assert_rejected "недопустимый путь: sub/OpenCode.app" --uninstall
}

@test "AC-135, AC-138: app_name=\"..\" → код 2 (родитель каталога назначения)" {
  set_app_name ".."
  assert_rejected "недопустимый путь: .." --no-launch
  assert_rejected "недопустимый путь: .." --uninstall
}

# ------------------------------------------------------------------ штатное значение не сломано

@test "AC-138: штатное app_name=OpenCode.app принимается: --dry-run и --check доходят до своих строк" {
  oc_run --dry-run
  assert_status 0
  assert_output_contains "/Applications/OpenCode.app"
  oc_run --check
  # Ничего не установлено → код 7 (расхождение), но манифест признан корректным.
  assert_status 7
  assert_output_contains "Манифест: корректен"
}

@test "AC-138: dmg без app_name отвергается ДО сверки целостности (код 2, а не 4)" {
  # Хеш CLI-артефакта заведомо неверный: если бы обязательность app_name проверялась после
  # verify_package, код был бы 4. Ожидается 2 — разбор манифеста завершается раньше.
  drop_app_name
  manifest_edit 's|^      "sha256": "................................................................"|      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  refute_output_contains "не совпадает"
}

@test "AC-138: installer_type≠dmg без app_name манифест не ломает (обязательность только для dmg)" {
  manifest_edit 's|"installer_type": "dmg"|"installer_type": "nsis"|'
  drop_app_name
  oc_run --dry-run
  assert_status 0
}

# ------------------------------------------------------------------ прямой вызов install_desktop
#
# Вторая линия защиты (defense in depth): даже если бы невалидное значение просочилось мимо
# manifest_load, install_desktop обязан отказать до первой разрушающей операции. Файл загружается
# как библиотека (OPENCODE_INSTALLER_SOURCE_ONLY=1); подменяются только границы среды —
# desktop_dest_dir (куда ставим), hdiutil (монтирование образа) и привилегированный запуск.
# MODE=stub — run_priv/ditto подменены журналирующими заглушками (привилегий нет вовсе);
# MODE=priv — run_priv настоящий (use_sudo=1), а sudo перехвачен ловушкой из install_trap_stubs,
# поэтому «привилегированных операций нет» проверяется наблюдаемо, а не по своему же моку.

write_desktop_driver() {
  cat >"$BATS_TEST_TMPDIR/drive.sh" <<'DRV'
#!/usr/bin/env bash
# shellcheck disable=SC2034
# Обоснование: глобальные переменные ниже читает загруженный через source install-posix.sh —
# присваивания здесь заменяют то, что в обычном запуске делают parse_args и manifest_load.
OPENCODE_INSTALLER_SOURCE_ONLY=1
export OPENCODE_INSTALLER_SOURCE_ONLY
# shellcheck source=/dev/null
. "$INSTALLERS_ROOT/common/install-posix.sh"

# Границы среды.
desktop_dest_dir() { printf '%s' "$DEST_DIR"; }
hdiutil() {
  printf 'hdiutil %s\n' "$*" >>"$OPS_LOG"
  # Имитация примонтированного образа: каталог приложения создаётся внутри той точки
  # монтирования, которую выбрал сам install_desktop (аргумент -mountpoint).
  local prev="" a mp=""
  for a in "$@"; do
    [ "$prev" != "-mountpoint" ] || mp=$a
    prev=$a
  done
  if [ "${1:-}" = "attach" ] && [ -n "$mp" ]; then
    mkdir -p "$mp/OpenCode.app" 2>/dev/null || true
  fi
  return 0
}
if [ "$MODE" = "stub" ]; then
  use_sudo=0
  run_priv() { printf 'run_priv %s\n' "$*" >>"$OPS_LOG"; return 0; }
  ditto() { printf 'ditto %s\n' "$*" >>"$OPS_LOG"; return 0; }
else
  use_sudo=1
  run_priv() { printf 'run_priv %s\n' "$*" >>"$OPS_LOG"; sudo "$@"; }
fi

opt_quiet=0
opt_no_desktop=0
platform=macos
pkg_root=$PKG
manifest_path="$PKG/common/manifest.json"
MF_version=1.17.9-magnit.1
MF_desktop_file=desktop/OpenCode.dmg
MF_desktop_type=dmg
MF_desktop_app=$APP_NAME
desktop_summary=""
desktop_failed=0

install_desktop
DRV
}

# Запуск драйвера: $1 — режим (stub|priv), $2 — значение app_name.
drive_install_desktop() {
  write_desktop_driver
  export MODE=$1 APP_NAME=$2
  export DEST_DIR="$SANDBOX/dest"
  export OPS_LOG="$BATS_TEST_TMPDIR/ops.log"
  mkdir -p "$DEST_DIR"
  printf 'ЧУЖОЕ ПРИЛОЖЕНИЕ\n' >"$DEST_DIR/Neighbour.marker"
  : >"$OPS_LOG"
  run bash "$BATS_TEST_TMPDIR/drive.sh"
}

ops_log() {
  cat "$BATS_TEST_TMPDIR/ops.log" 2>/dev/null || true
}

assert_no_ops() {
  local log
  log=$(ops_log)
  if [ -n "$log" ]; then
    printf 'install_desktop выполнил операции, хотя app_name невалиден:\n%s\n' "$log" >&2
    return 1
  fi
  [ -f "$DEST_DIR/Neighbour.marker" ] || { printf 'Каталог назначения опустошён\n' >&2; return 1; }
  [ -d "$DEST_DIR" ] || { printf 'Каталог назначения удалён\n' >&2; return 1; }
  return 0
}

@test "AC-138: install_desktop с пустым app_name — код 2, ни одной операции (заглушки run_priv/hdiutil/ditto)" {
  drive_install_desktop stub ""
  assert_status 2
  assert_output_contains "поле artifacts[].app_name"
  assert_no_ops
  assert_no_forbidden_calls
}

@test "AC-138: install_desktop с app_name=\".\" и \"OpenCode.app/\" — код 2, ни одной операции" {
  drive_install_desktop stub "."
  assert_status 2
  assert_output_contains "поле artifacts[].app_name"
  assert_no_ops
  drive_install_desktop stub "OpenCode.app/"
  assert_status 2
  assert_no_ops
}

@test "AC-138: install_desktop с пустым app_name не вызывает sudo (ловушка sudo пуста)" {
  drive_install_desktop priv ""
  assert_status 2
  assert_no_ops
  # Ловушка sudo из install_trap_stubs: ни одного привилегированного вызова.
  assert_no_forbidden_calls
}

@test "AC-138: install_desktop со штатным app_name копирует именно <dest>/OpenCode.app, а не <dest>" {
  drive_install_desktop stub "OpenCode.app"
  assert_status 0
  local log
  log=$(ops_log)
  printf '%s\n' "$log" | grep -F -q "run_priv ditto" || { printf 'Нет копирования:\n%s\n' "$log" >&2; return 1; }
  printf '%s\n' "$log" | grep -F -q "$DEST_DIR/OpenCode.app" || { printf 'Цель не <dest>/OpenCode.app:\n%s\n' "$log" >&2; return 1; }
  # Целью разрушающей операции никогда не становится сам каталог назначения.
  if printf '%s\n' "$log" | grep -E -q "run_priv rm -rf $DEST_DIR/?$"; then
    printf 'rm -rf нацелен на сам каталог назначения:\n%s\n' "$log" >&2
    return 1
  fi
  [ -f "$DEST_DIR/Neighbour.marker" ]
}

@test "AC-138: find_installed_desktop при пустом app_name не выдаёт сам каталог приложений" {
  source_installer
  MF_desktop_app=""
  run find_installed_desktop
  [ "$status" -ne 0 ]
  [ -z "$output" ]
  MF_desktop_app="."
  run find_installed_desktop
  [ "$status" -ne 0 ]
  MF_desktop_app="OpenCode.app/"
  run find_installed_desktop
  [ "$status" -ne 0 ]
  # Каталог $HOME/Applications существует и после трёх вызовов цел.
  [ -f "$HOME/Applications/Other.marker" ]
}

@test "AC-138: is_safe_app_name отвергает пробельное имя — обе линии защиты совпали" {
  # Пробельное значение отбивала только первая линия (manifest_load), а is_safe_app_name его
  # пропускал, и "<dest>/   " доходил до ditto/rm -rf. Паритет с Test-AppName (IsNullOrWhiteSpace)
  # проверяется здесь, а не читается из комментария (reports/review-i5-3.json, находка 4).
  source_installer
  local value
  for value in "   " "$(printf '\t')" "$(printf '\t \t')" " "; do
    run is_safe_app_name "$value"
    if [ "$status" -eq 0 ]; then
      printf 'is_safe_app_name принял пробельное значение [%s]\n' "$value" >&2
      return 1
    fi
    run desktop_target_path "/Applications" "$value"
    if [ "$status" -eq 0 ]; then
      printf 'desktop_target_path принял пробельное значение [%s] → %s\n' "$value" "$output" >&2
      return 1
    fi
  done
  # Контроль: штатное имя по-прежнему принимается.
  is_safe_app_name "OpenCode.app"
  [ "$(desktop_target_path "/Applications" "OpenCode.app")" = "/Applications/OpenCode.app" ]
}

@test "AC-138: install_desktop с пробельным app_name при обойдённом manifest_load — код 2, ни одной операции" {
  drive_install_desktop stub "   "
  assert_status 2
  assert_output_contains "поле artifacts[].app_name"
  assert_no_ops
  drive_install_desktop stub "$(printf '\t')"
  assert_status 2
  assert_no_ops
  assert_no_forbidden_calls
}

@test "AC-138: desktop_target_path отвергает значения, схлопывающие путь в сам каталог" {
  source_installer
  run desktop_target_path "/Applications" ""
  [ "$status" -ne 0 ]
  run desktop_target_path "/Applications" "."
  [ "$status" -ne 0 ]
  run desktop_target_path "/Applications" ".."
  [ "$status" -ne 0 ]
  run desktop_target_path "/Applications" "OpenCode.app/"
  [ "$status" -ne 0 ]
  run desktop_target_path "/Applications" "../victim"
  [ "$status" -ne 0 ]
  [ "$(desktop_target_path "/Applications" "OpenCode.app")" = "/Applications/OpenCode.app" ]
  [ "$(desktop_target_path "$HOME/Applications/" "OpenCode.app")" = "$HOME/Applications/OpenCode.app" ]
}

@test "AC-138: install_desktop со штатным app_name — привилегированный вызов ровно один и нацелен внутрь каталога" {
  # run_priv настоящий (use_sudo=1): все привилегированные вызовы уходят в ловушку sudo и
  # видны в forbidden.log. Ожидание: ditto в <dest>/OpenCode.app и ни одного rm по <dest>.
  drive_install_desktop priv "OpenCode.app"
  local log
  log=$(forbidden_log)
  printf '%s\n' "$log" | grep -F -q "sudo ditto" || { printf 'Нет sudo ditto:\n%s\n' "$log" >&2; return 1; }
  printf '%s\n' "$log" | grep -F -q "$DEST_DIR/OpenCode.app" || { printf 'Цель не <dest>/OpenCode.app:\n%s\n' "$log" >&2; return 1; }
  if printf '%s\n' "$log" | grep -E -q "sudo rm -rf $DEST_DIR/?$"; then
    printf 'rm -rf нацелен на сам каталог назначения:\n%s\n' "$log" >&2
    return 1
  fi
  [ -f "$DEST_DIR/Neighbour.marker" ]
}
