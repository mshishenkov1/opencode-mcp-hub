#!/usr/bin/env bats
#
# Идемпотентность, обновление, пропуск при совпадении, «другая версия» (N5-U1…N5-U4).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

# Кладёт в bin_dir «установленный» бинарник указанной версии (содержимое отличается от пакета).
install_previous_version() {
  local version=$1
  mkdir -p "$(bin_dir_path)"
  write_fake_binary "$(bin_dir_path)/opencode" "$version"
}

@test "AC-79: два одинаковых запуска подряд дают одинаковое состояние и код 0" {
  export SHELL=/bin/zsh
  mkdir -p "$(config_dir_path)"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  oc_run --no-launch
  assert_status 0
  snapshot_home "$BATS_TEST_TMPDIR/after1"
  oc_run --no-launch
  assert_status 0
  snapshot_home "$BATS_TEST_TMPDIR/after2"
  run diff "$BATS_TEST_TMPDIR/after1" "$BATS_TEST_TMPDIR/after2"
  assert_status 0
  [ "$(count_lines_with "$HOME/.zshrc" "# >>> opencode-magnit >>>")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "export PATH=\"$(bin_dir_path):")" = "1" ]
  run find "$(config_dir_path)" -name 'opencode.json.bak'
  [ "$(printf '%s\n' "$output" | wc -l | tr -d ' ')" = "1" ]
}

@test "AC-80: обновление сохраняет прежний бинарник в opencode.bak, каталог конфига не трогает" {
  mkdir -p "$(config_dir_path)"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  printf '{"theme":"старая копия"}\n' >"$(config_dir_path)/opencode.json.bak"
  local cfg_sha bak_sha old_sha
  cfg_sha=$(sha256_file "$(config_dir_path)/opencode.json")
  bak_sha=$(sha256_file "$(config_dir_path)/opencode.json.bak")
  install_previous_version "1.17.9-magnit.0"
  old_sha=$(sha256_file "$(bin_dir_path)/opencode")
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Бинарник: другая версия (установлена 1.17.9-magnit.0, в пакете 1.17.9-magnit.1)"
  [ "$(sha256_file "$(bin_dir_path)/opencode.bak")" = "$old_sha" ]
  [ "$(sha256_file "$(bin_dir_path)/opencode")" = "$(sha256_file "$PKG/bin/opencode")" ]
  [ "$(sha256_file "$(config_dir_path)/opencode.json")" = "$cfg_sha" ]
  [ "$(sha256_file "$(config_dir_path)/opencode.json.bak")" = "$bak_sha" ]
}

@test "AC-81: второе обновление хранит ровно одну предыдущую версию, файлов opencode.bak.1 нет" {
  install_previous_version "1.17.9-magnit.0"
  oc_run --no-launch
  assert_status 0
  # Второе обновление: «установлена» версия .2, в пакете — .1.
  install_previous_version "1.17.9-magnit.2"
  local prev_sha
  prev_sha=$(sha256_file "$(bin_dir_path)/opencode")
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$(bin_dir_path)/opencode.bak")" = "$prev_sha" ]
  run find "$(bin_dir_path)" -name 'opencode.bak.*'
  [ -z "$output" ]
  run find "$(bin_dir_path)" -name 'opencode*'
  [ "$(printf '%s\n' "$output" | wc -l | tr -d ' ')" = "2" ]
}

@test "AC-82: при обновлении пользовательские opencode.json и auth.json не изменяются побайтово" {
  mkdir -p "$(config_dir_path)" "$HOME/.local/share/opencode"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  printf '{"key":"MARKER"}\n' >"$HOME/.local/share/opencode/auth.json"
  local cfg auth
  cfg=$(sha256_file "$(config_dir_path)/opencode.json")
  auth=$(sha256_file "$HOME/.local/share/opencode/auth.json")
  install_previous_version "1.17.9-magnit.0"
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$(config_dir_path)/opencode.json")" = "$cfg" ]
  [ "$(sha256_file "$HOME/.local/share/opencode/auth.json")" = "$auth" ]
}

@test "AC-83: sha256 установленного бинарника совпадает → шаг пропущен, mtime не изменён, .bak не создан" {
  oc_run --no-launch
  assert_status 0
  local before
  before=$(mtime_of "$(bin_dir_path)/opencode")
  sleep 1
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Бинарник: уже установлен (1.17.9-magnit.1)"
  [ "$(mtime_of "$(bin_dir_path)/opencode")" = "$before" ]
  [ ! -e "$(bin_dir_path)/opencode.bak" ]
}

@test "AC-84: установка более старого пакета поверх более нового выполняется без слов «новее» и «старее»" {
  install_previous_version "1.17.9-magnit.3"
  oc_run --check
  assert_status 7
  assert_output_contains "Бинарник: другая версия (установлена 1.17.9-magnit.3, в пакете 1.17.9-magnit.1)"
  refute_output_contains "новее"
  refute_output_contains "старее"
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$(bin_dir_path)/opencode")" = "$(sha256_file "$PKG/bin/opencode")" ]
  run "$(bin_dir_path)/opencode" --version
  [ "$output" = "1.17.9-magnit.1" ]
  oc_run --check
  assert_status 0
}
