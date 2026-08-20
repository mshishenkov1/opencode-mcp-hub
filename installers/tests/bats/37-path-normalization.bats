#!/usr/bin/env bats
#
# Единая нормализация путей и строгая вложенность purge_paths (N5-P3, N5-P4, N5-R2).
#
# Модель угрозы (review-i5-3, blocker): проверка безопасности выполнялась над СЫРОЙ строкой, а
# файловая операция — над строкой, которую ядро нормализует само ("//" = "/", "/./" = "/",
# завершающий "/" отбрасывается). Поэтому purge_paths=["${HOME}//"] проходил проверку «не равен
# самому $HOME» и приводил к rm -rf "$HOME//" — удалению домашнего каталога целиком с кодом 0.
# Здесь проверяется наблюдаемое поведение: КАЖДАЯ форма, которую ядро схлопнуло бы в сам
# домашний каталог, отвергается кодом 2 до единой файловой операции, домашний каталог и
# канарейки внутри него целы, а штатное значение по-прежнему работает.
#
# Файл проверяет три уровня:
#   1) таблица path_normalize / path_is_inside / path_require_child (прямой вызов, N5-P3);
#   2) сквозные прогоны install.sh --uninstall --purge на опасных формах (N5-R2);
#   3) defense in depth: purge_each вызывается напрямую в обход validate_purge_paths, с
#      заглушкой rm — небезопасное значение не должно дойти до удаления.
# Совместимость: bash 3.2 (N5-T5).

setup() {
  load helpers
  setup_sandbox
  make_pkg
  # Канарейки внутри домашнего каталога (AC-143): то, что снёс бы rm -rf "$HOME//".
  mkdir -p "$HOME/Documents/very-important" "$HOME/.ssh"
  printf 'КАНАРЕЙКА В КОРНЕ ДОМАШНЕГО КАТАЛОГА\n' >"$HOME/canary.txt"
  printf 'ВАЖНЫЕ ДАННЫЕ ПОЛЬЗОВАТЕЛЯ\n' >"$HOME/Documents/very-important/data.txt"
  printf 'PRIVATE KEY CANARY\n' >"$HOME/.ssh/id_rsa"
  CANARY_ROOT_SHA=$(sha256_file "$HOME/canary.txt")
  CANARY_DEEP_SHA=$(sha256_file "$HOME/Documents/very-important/data.txt")
}

# Домашний каталог существует, обе канарейки на месте и побайтово не изменились (AC-143).
assert_home_intact() {
  [ -d "$HOME" ] || { printf 'Домашний каталог удалён: %s\n' "$HOME" >&2; return 1; }
  [ -f "$HOME/canary.txt" ] || { printf 'Удалена канарейка canary.txt\n' >&2; return 1; }
  [ -f "$HOME/Documents/very-important/data.txt" ] || { printf 'Удалена канарейка Documents/very-important/data.txt\n' >&2; return 1; }
  [ -f "$HOME/.ssh/id_rsa" ] || { printf 'Удалена канарейка .ssh/id_rsa\n' >&2; return 1; }
  [ "$(sha256_file "$HOME/canary.txt")" = "$CANARY_ROOT_SHA" ] || { printf 'Канарейка canary.txt изменена\n' >&2; return 1; }
  [ "$(sha256_file "$HOME/Documents/very-important/data.txt")" = "$CANARY_DEEP_SHA" ] || { printf 'Канарейка data.txt изменена\n' >&2; return 1; }
  return 0
}

# ------------------------------------------------------------------ 1. таблица нормализации

@test "AC-135, AC-143: path_normalize схлопывает эквивалентные формы домашнего каталога к одному значению" {
  source_installer
  local home_norm form
  home_norm=$(path_normalize "$HOME")
  [ "$home_norm" = "$HOME" ]
  # Всё, что ядро схлопнуло бы в сам домашний каталог, даёт РОВНО его нормализованное значение —
  # значит, «не равен $HOME» решается уже после нормализации и обойти его задвоением нельзя.
  # shellcheck disable=SC2088
  # Обоснование: "~" здесь — не путь для shell, а проверяемое значение: его разворачивает
  # сам установщик (path_normalize/expand_user_path). Раскрытие тильдой сломало бы проверку.
  for form in "$HOME//" "$HOME///" "$HOME/./" "$HOME/." "$HOME/" "~" "~/" "~//"; do
    run path_normalize "$form"
    assert_status 0
    if [ "$output" != "$home_norm" ]; then
      printf 'Форма %s нормализована в %s, ожидалось %s\n' "$form" "$output" "$home_norm" >&2
      return 1
    fi
  done
}

@test "AC-135, AC-143: path_normalize отвергает сегмент \"..\" и относительный путь без базы" {
  source_installer
  local form
  # shellcheck disable=SC2088
  # Обоснование: "~" здесь — не путь для shell, а проверяемое значение: его разворачивает
  # сам установщик (path_normalize/expand_user_path). Раскрытие тильдой сломало бы проверку.
  for form in "$HOME/sub/../" "$HOME/sub/.." "~/../other" ".." "../x" "$HOME/../$(basename "$HOME")"; do
    run path_normalize "$form"
    if [ "$status" -eq 0 ]; then
      printf 'path_normalize принял небезопасную форму %s → %s\n' "$form" "$output" >&2
      return 1
    fi
  done
  # Относительный путь без базы — тоже отказ: результат обязан быть абсолютным.
  run path_normalize "rel/path"
  [ "$status" -ne 0 ]
  run path_normalize ""
  [ "$status" -ne 0 ]
  # С базой относительный путь достраивается и нормализуется.
  [ "$(path_normalize "rel/path" "/base")" = "/base/rel/path" ]
  [ "$(path_normalize "./x" "/base")" = "/base/x" ]
  run path_normalize "../up" "/base"
  [ "$status" -ne 0 ]
}

@test "AC-135, AC-143: path_normalize приводит повторяющиеся разделители и \".\" к канонической форме" {
  source_installer
  [ "$(path_normalize "/a//b/./c/")" = "/a/b/c" ]
  [ "$(path_normalize "/a/./././b")" = "/a/b" ]
  [ "$(path_normalize "//")" = "/" ]
  [ "$(path_normalize "/")" = "/" ]
}

@test "AC-135, AC-143: path_is_inside — строго ниже родителя, не сам родитель и не его сосед" {
  source_installer
  path_is_inside "/home/u" "/home/u/x"
  path_is_inside "/" "/etc"
  run path_is_inside "/home/u" "/home/u"
  [ "$status" -ne 0 ]
  run path_is_inside "/home/u" "/home/user2"
  [ "$status" -ne 0 ]
  run path_is_inside "/home/u" "/home"
  [ "$status" -ne 0 ]
  run path_is_inside "/home/u" "/other/x"
  [ "$status" -ne 0 ]
  run path_is_inside "/" "/"
  [ "$status" -ne 0 ]
}

@test "AC-135, AC-143: path_require_child не выпускает цель за пределы своего каталога" {
  source_installer
  path_require_child "/opt/x/bin" "opencode" "тест"
  [ "$REPLY" = "/opt/x/bin/opencode" ]
  # Отказ выполняется через die → отдельный процесс, поэтому проверяется кодом выхода.
  run path_require_child "/opt/x/bin" ".." "тест"
  assert_status 2
  run path_require_child "/opt/x/bin" "." "тест"
  assert_status 2
  run path_require_child "/opt/x/bin" "../evil" "тест"
  assert_status 2
  run path_require_child "/opt/x/bin" "" "тест"
  assert_status 2
}

# ------------------------------------------------------------------ 2. purge_paths сквозным прогоном

# Один прогон --uninstall --purge на опасном значении purge_paths: код 2, названо само значение,
# ни одной файловой операции, домашний каталог и канарейки целы.
assert_purge_rejected() {
  local raw=$1
  PKG_PURGE="$raw" write_manifest "$PKG"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --uninstall --purge
  assert_status 2
  assert_output_contains "Небезопасный путь в purge_paths: $raw"
  assert_output_contains "Список purge_paths отвергнут целиком, ничего не удалено"
  refute_output_contains "Данные: удалено"
  refute_output_contains "Готово: OpenCode удалён."
  assert_home_intact
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
  assert_no_forbidden_calls
}

# shellcheck disable=SC2016
# Обоснование: ${HOME} и ${XDG_CONFIG_HOME} — текст манифеста (N5-P3), подстановку выполняет
# установщик, а не shell теста. Именно эта форма и обходила проверку до фикса.

@test "AC-91, AC-143: purge_paths=\"\${HOME}//\" → код 2, домашний каталог и канарейки целы" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2016
  assert_purge_rejected '${HOME}//'
  [ -x "$(bin_dir_path)/opencode" ]
}

@test "AC-91, AC-143: purge_paths=\"~//\" → код 2, домашний каталог цел" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2088
  # Обоснование: "~" здесь — не путь для shell, а проверяемое значение: его разворачивает
  # сам установщик (path_normalize/expand_user_path). Раскрытие тильдой сломало бы проверку.
  assert_purge_rejected '~//'
}

@test "AC-91, AC-143: purge_paths=\"\${HOME}///\" → код 2, домашний каталог цел" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2016
  assert_purge_rejected '${HOME}///'
}

@test "AC-91, AC-143: purge_paths=\"\${HOME}/./\" и \"\${HOME}/.\" → код 2, домашний каталог цел" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2016
  assert_purge_rejected '${HOME}/./'
  # shellcheck disable=SC2016
  assert_purge_rejected '${HOME}/.'
}

@test "AC-91, AC-143: purge_paths=\"\${HOME}/sub/../\" → код 2, домашний каталог цел" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2016
  assert_purge_rejected '${HOME}/sub/../'
}

@test "AC-91, AC-143: purge_paths=\"\${HOME}\" и \"~\" (сам домашний каталог) → код 2" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2016
  assert_purge_rejected '${HOME}'
  assert_purge_rejected '~'
}

@test "AC-90, AC-143: purge_paths вне домашнего каталога и относительный путь → код 2" {
  oc_run --no-launch
  assert_status 0
  assert_purge_rejected '/etc'
  [ -d /etc ]
  # shellcheck disable=SC2088
  # Обоснование: "~" здесь — не путь для shell, а проверяемое значение: его разворачивает
  # сам установщик (path_normalize/expand_user_path). Раскрытие тильдой сломало бы проверку.
  assert_purge_rejected '~/../other'
  # Относительный путь нормализовать не от чего: результат обязан быть абсолютным.
  assert_purge_rejected 'relative/opencode'
  assert_purge_rejected '.config/opencode'
}

@test "AC-91, AC-143: одна опасная форма в списке отвергает весь список — соседний штатный путь цел" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf '{"key":"MARKER"}\n' >"$HOME/.local/share/opencode/auth.json"
  # shellcheck disable=SC2016
  # Обоснование: подстановку раскрывает установщик, не тест.
  PKG_PURGE='${XDG_DATA_HOME}/opencode
${HOME}//' write_manifest "$PKG"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --uninstall --purge
  assert_status 2
  # shellcheck disable=SC2016
  assert_output_contains 'Небезопасный путь в purge_paths: ${HOME}//'
  assert_home_intact
  [ -f "$HOME/.local/share/opencode/auth.json" ]
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-91, AC-143: опасная форма отвергается и в --dry-run --uninstall --purge (план не печатается)" {
  oc_run --no-launch
  assert_status 0
  # shellcheck disable=SC2016
  PKG_PURGE='${HOME}//' write_manifest "$PKG"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --dry-run --uninstall --purge
  assert_status 2
  refute_output_contains "Удаляются пользовательские данные:"
  assert_home_intact
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-89, AC-143: штатный \${XDG_CONFIG_HOME}/opencode после фикса по-прежнему удаляется" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf '{"key":"MARKER"}\n' >"$HOME/.local/share/opencode/auth.json"
  oc_run --uninstall --purge
  assert_status 0
  assert_output_contains "  $(config_dir_path)"
  assert_output_contains "Данные: удалено ($(config_dir_path))"
  [ ! -e "$(config_dir_path)" ]
  [ ! -e "$HOME/.local/share/opencode" ]
  assert_home_intact
}

@test "AC-89, AC-143: путь с задвоенным разделителем ВНУТРИ домашнего каталога нормализуется и удаляется как один объект" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf 'data\n' >"$HOME/.local/share/opencode/auth.json"
  # shellcheck disable=SC2016
  # Обоснование: подстановку раскрывает установщик, не тест.
  PKG_PURGE='${HOME}//.local//share/./opencode' write_manifest "$PKG"
  oc_run --uninstall --purge
  assert_status 0
  # В выводе — нормализованный путь, а не исходная строка с задвоенными разделителями.
  assert_output_contains "  $HOME/.local/share/opencode"
  refute_output_contains "$HOME//.local"
  [ ! -e "$HOME/.local/share/opencode" ]
  assert_home_intact
}

# ------------------------------------------------------------------ 3. defense in depth: purge_each
#
# Вторая линия защиты: даже при обойдённой validate_purge_paths ни один путь не попадает в rm -rf
# без повторной проверки. Файл загружается как библиотека (OPENCODE_INSTALLER_SOURCE_ONLY=1),
# purge_each вызывается напрямую, mf_val подменён источником значения, а rm — журналирующей
# заглушкой: «ничего не удалено» проверяется наблюдаемо, по пустому журналу и целым канарейкам.

write_purge_driver() {
  cat >"$BATS_TEST_TMPDIR/purge-drive.sh" <<'DRV'
#!/usr/bin/env bash
# shellcheck disable=SC2034
# Обоснование: opt_quiet читает загруженный через source install-posix.sh — присваивание здесь
# заменяет то, что в обычном запуске делает parse_args.
OPENCODE_INSTALLER_SOURCE_ONLY=1
export OPENCODE_INSTALLER_SOURCE_ONLY
# shellcheck source=/dev/null
. "$INSTALLERS_ROOT/common/install-posix.sh"

# Источник значений purge_paths в обход manifest_load и validate_purge_paths.
mf_val() {
  case $1 in
    purge_paths.__len) printf '1' ;;
    purge_paths.0) printf '%s' "$RAW_PATH" ;;
    *) printf '' ;;
  esac
}

# Граница среды: единственная разрушающая операция purge_each.
rm() { printf 'rm %s\n' "$*" >>"$OPS_LOG"; return 0; }

opt_quiet=0
purge_each run
DRV
}

# Прямой вызов purge_each с заданным сырым значением purge_paths.
drive_purge_each() {
  write_purge_driver
  export RAW_PATH=$1
  export OPS_LOG="$BATS_TEST_TMPDIR/purge-ops.log"
  : >"$OPS_LOG"
  run bash "$BATS_TEST_TMPDIR/purge-drive.sh"
}

purge_ops_log() {
  cat "$BATS_TEST_TMPDIR/purge-ops.log" 2>/dev/null || true
}

assert_no_purge_ops() {
  local log
  log=$(purge_ops_log)
  if [ -n "$log" ]; then
    printf 'purge_each дошёл до удаления, хотя путь небезопасен:\n%s\n' "$log" >&2
    return 1
  fi
  return 0
}

@test "AC-143: purge_each при обойдённой валидации отвергает \"\${HOME}//\" — код 2, ни одного rm" {
  # shellcheck disable=SC2016
  drive_purge_each '${HOME}//'
  assert_status 2
  # shellcheck disable=SC2016
  assert_output_contains 'Небезопасный путь в purge_paths: ${HOME}//'
  assert_output_contains "Список purge_paths отвергнут целиком, ничего не удалено"
  assert_no_purge_ops
  assert_home_intact
}

@test "AC-143: purge_each отвергает все схлопывающиеся формы и пути вне \$HOME — ни одного rm" {
  local form
  # shellcheck disable=SC2016,SC2088
  for form in '~//' '${HOME}///' '${HOME}/./' '${HOME}/.' '${HOME}/sub/../' '${HOME}' '~' '/etc' '~/../other' 'relative/opencode' '.config/opencode'; do
    drive_purge_each "$form"
    if [ "$status" -ne 2 ]; then
      printf 'purge_each вернул %s на форме %s, ожидался код 2\nВывод:\n%s\n' "$status" "$form" "$output" >&2
      return 1
    fi
    assert_no_purge_ops || return 1
  done
  assert_home_intact
}

@test "AC-143: purge_each со штатным путём внутри \$HOME доходит до rm ровно по нормализованному значению" {
  mkdir -p "$HOME/.local/share/opencode"
  # shellcheck disable=SC2016
  drive_purge_each '${XDG_DATA_HOME}/opencode'
  assert_status 0
  local log
  log=$(purge_ops_log)
  printf '%s\n' "$log" | grep -F -q -- "rm -rf $HOME/.local/share/opencode" || {
    printf 'Штатный путь не дошёл до удаления:\n%s\n' "$log" >&2
    return 1
  }
  # Целью не стал ни сам домашний каталог, ни его форма с задвоенным разделителем.
  if printf '%s\n' "$log" | grep -E -q -- "rm -rf $HOME/*$"; then
    printf 'rm нацелен на сам домашний каталог:\n%s\n' "$log" >&2
    return 1
  fi
}

# ------------------------------------------------------------------ 4. нормализация --prefix и bin_dir

@test "AC-55, AC-143: относительный --prefix достраивается от текущего каталога и работает" {
  run bash -c "cd '$SANDBOX' && bash '$PKG/install.sh' --prefix rel-opt --no-launch"
  assert_status 0
  [ -x "$SANDBOX/rel-opt/bin/opencode" ]
  # Каталог с относительным именем в корне не создан.
  [ ! -e "/rel-opt" ]
}

@test "AC-143: --prefix с сегментом \"..\" отвергается кодом 2 до единой файловой операции" {
  snapshot_home "$BATS_TEST_TMPDIR/before"
  run bash -c "cd '$SANDBOX' && bash '$PKG/install.sh' --prefix '../escape' --no-launch"
  assert_status 2
  assert_output_contains "Небезопасный путь (--prefix): ../escape"
  [ ! -e "$SANDBOX/../escape" ]
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-143: --prefix в схлопывающейся форме нормализуется, а не создаёт лишних каталогов" {
  oc_run_prefix_forms() {
    run bash "$PKG/install.sh" --prefix "$1" --no-launch --quiet
  }
  oc_run_prefix_forms "$PREFIX_DIR//"
  assert_status 0
  [ -x "$PREFIX_DIR/bin/opencode" ]
  rm -rf "$PREFIX_DIR"
  oc_run_prefix_forms "$PREFIX_DIR/./"
  assert_status 0
  [ -x "$PREFIX_DIR/bin/opencode" ]
}

@test "AC-143: XDG_CONFIG_HOME в схлопывающейся форме нормализуется, CA ложится внутрь \$HOME" {
  XDG_CONFIG_HOME="$HOME//cfg" oc_run --no-launch
  assert_status 0
  [ -f "$HOME/cfg/opencode/tander-ca-bundle.pem" ]
  assert_output_contains "$HOME/cfg/opencode/tander-ca-bundle.pem"
  refute_output_contains "$HOME//cfg"
  assert_home_intact
}

@test "AC-143: XDG_CONFIG_HOME с сегментом \"..\" отвергается кодом 2" {
  XDG_CONFIG_HOME="$HOME/../escape" oc_run --no-launch
  assert_status 2
  assert_output_contains "Небезопасный путь (XDG_CONFIG_HOME)"
  [ ! -e "$SANDBOX/escape" ]
}
