#!/usr/bin/env bats
#
# Удаление и очистка пользовательских данных (N5-R1…N5-R4).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

@test "AC-85: --uninstall удаляет бинарник, .bak, блок профиля и CA, перечисляя каждый объект" {
  export SHELL=/bin/zsh
  mkdir -p "$(bin_dir_path)"
  write_fake_binary "$(bin_dir_path)/opencode" "1.17.9-magnit.0"
  oc_run --no-launch
  assert_status 0
  [ -f "$(bin_dir_path)/opencode.bak" ]
  oc_run --uninstall
  assert_status 0
  assert_output_contains "Бинарник: удалён ($(bin_dir_path)/opencode)"
  assert_output_contains "Резервная копия бинарника: удалён ($(bin_dir_path)/opencode.bak)"
  assert_output_contains "Блок в профиле: удалён ($HOME/.zshrc)"
  assert_output_contains "CA: удалён ($(config_dir_path)/tander-ca-bundle.pem)"
  assert_output_contains "Готово: OpenCode удалён."
  [ ! -e "$(bin_dir_path)/opencode" ]
  [ ! -e "$(bin_dir_path)/opencode.bak" ]
  [ ! -e "$(config_dir_path)/tander-ca-bundle.pem" ]
  refute_file_contains "$HOME/.zshrc" "# >>> opencode-magnit >>>"
  refute_file_contains "$HOME/.zshrc" "# <<< opencode-magnit <<<"
}

@test "AC-86: пользовательские строки профиля сохранены побайтово, маркеров в файле нет" {
  export SHELL=/bin/zsh
  printf 'export USER_ONE=1\nalias ll="ls -la"\n' >"$HOME/.zshrc"
  local original
  original=$(sha256_file "$HOME/.zshrc")
  oc_run --no-launch
  assert_status 0
  printf 'export USER_TWO=2\n' >>"$HOME/.zshrc"
  cat "$HOME/.zshrc" >"$BATS_TEST_TMPDIR/with-block"
  oc_run --uninstall
  assert_status 0
  [ -f "$HOME/.zshrc" ]
  refute_file_contains "$HOME/.zshrc" "opencode-magnit"
  assert_file_contains "$HOME/.zshrc" "export USER_ONE=1"
  assert_file_contains "$HOME/.zshrc" 'alias ll="ls -la"'
  assert_file_contains "$HOME/.zshrc" "export USER_TWO=2"
  # Исходные строки не изменились: файл без блока совпадает с «исходный + добавленная строка».
  printf 'export USER_ONE=1\nalias ll="ls -la"\nexport USER_TWO=2\n' >"$BATS_TEST_TMPDIR/expected"
  run diff "$BATS_TEST_TMPDIR/expected" "$HOME/.zshrc"
  assert_status 0
  [ -n "$original" ]
}

@test "AC-88: --uninstall без --purge сохраняет opencode.json, .bak, auth.json и каталог конфига" {
  mkdir -p "$(config_dir_path)" "$HOME/.local/share/opencode"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  printf '{"theme":"копия"}\n' >"$(config_dir_path)/opencode.json.bak"
  printf '{"key":"MARKER"}\n' >"$HOME/.local/share/opencode/auth.json"
  local a b c
  a=$(sha256_file "$(config_dir_path)/opencode.json")
  b=$(sha256_file "$(config_dir_path)/opencode.json.bak")
  c=$(sha256_file "$HOME/.local/share/opencode/auth.json")
  oc_run --no-launch
  assert_status 0
  oc_run --uninstall
  assert_status 0
  [ -d "$(config_dir_path)" ]
  [ "$(sha256_file "$(config_dir_path)/opencode.json")" = "$a" ]
  [ "$(sha256_file "$(config_dir_path)/opencode.json.bak")" = "$b" ]
  [ "$(sha256_file "$HOME/.local/share/opencode/auth.json")" = "$c" ]
}

@test "AC-89: --uninstall --purge удаляет ровно перечисленные каталоги, перечислив их в выводе" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf '{"key":"MARKER"}\n' >"$HOME/.local/share/opencode/auth.json"
  printf 'user\n' >"$HOME/keep-me.txt"
  oc_run --uninstall --purge
  assert_status 0
  assert_output_contains "Удаляются пользовательские данные:"
  assert_output_contains "  $(config_dir_path)"
  assert_output_contains "  $HOME/.local/share/opencode"
  [ ! -e "$(config_dir_path)" ]
  [ ! -e "$HOME/.local/share/opencode" ]
  [ -f "$HOME/keep-me.txt" ]
}

@test "AC-90: небезопасный путь /etc в purge_paths → список отвергнут целиком, код 2, ничего не удалено" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  # shellcheck disable=SC2016
  # Обоснование: подстановку раскрывает установщик, не тест.
  PKG_PURGE='/etc
${XDG_DATA_HOME}/opencode' write_manifest "$PKG"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --uninstall --purge
  assert_status 2
  assert_output_contains "Небезопасный путь в purge_paths: /etc"
  assert_output_contains "Список purge_paths отвергнут целиком, ничего не удалено"
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
  [ -d /etc ]
}

@test "AC-90: путь с .. в purge_paths → код 2, ничего не удалено" {
  oc_run --no-launch
  assert_status 0
  PKG_PURGE='~/../other' write_manifest "$PKG"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --uninstall --purge
  assert_status 2
  assert_output_contains "Небезопасный путь в purge_paths: ~/../other"
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-90: путь вне домашнего каталога (/tmp/x) в purge_paths → код 2, ничего не удалено" {
  oc_run --no-launch
  assert_status 0
  PKG_PURGE='/tmp/x' write_manifest "$PKG"
  mkdir -p "$TMPDIR/x"
  oc_run --uninstall --purge
  assert_status 2
  assert_output_contains "Небезопасный путь в purge_paths: /tmp/x"
  [ -x "$(bin_dir_path)/opencode" ]
}

@test "AC-91: purge_paths содержит сам домашний каталог → он не удалён, код 2" {
  oc_run --no-launch
  assert_status 0
  PKG_PURGE='~' write_manifest "$PKG"
  oc_run --uninstall --purge
  assert_status 2
  assert_output_contains "Небезопасный путь в purge_paths: ~"
  [ -d "$HOME" ]
  [ -x "$(bin_dir_path)/opencode" ]
}

@test "AC-92: --dry-run --uninstall --purge печатает список путей, ничего не удаляя" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf 'data\n' >"$HOME/.local/share/opencode/auth.json"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --dry-run --uninstall --purge
  assert_status 0
  assert_output_contains "План удаления OpenCode 1.17.9-magnit.1"
  assert_output_contains "Удаляются пользовательские данные:"
  assert_output_contains "  $(config_dir_path)"
  assert_output_contains "  $HOME/.local/share/opencode"
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-93: --uninstall на чистой системе печатает «не найден» по каждому объекту и завершается кодом 0" {
  oc_run --uninstall
  assert_status 0
  assert_output_contains "Бинарник: не найден"
  assert_output_contains "Резервная копия бинарника: не найден"
  assert_output_contains "Блок в профиле: не найден"
  assert_output_contains "CA: не найден"
  assert_output_contains "Готово: OpenCode удалён."
}

@test "AC-94: повторный --uninstall идемпотентен: код 0 и то же состояние" {
  oc_run --no-launch
  assert_status 0
  oc_run --uninstall
  assert_status 0
  snapshot_home "$BATS_TEST_TMPDIR/after1"
  oc_run --uninstall
  assert_status 0
  snapshot_home "$BATS_TEST_TMPDIR/after2"
  run diff "$BATS_TEST_TMPDIR/after1" "$BATS_TEST_TMPDIR/after2"
  assert_status 0
}
