#!/usr/bin/env bats
#
# Файл профиля shell и NODE_EXTRA_CA_CERTS на macOS/Linux (N5-I5, N5-I7).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

block_count() {
  count_lines_with "$1" "# >>> opencode-magnit >>>"
}

@test "AC-41: SHELL=zsh и отсутствующий ~/.zshrc → создан файл с одним блоком и строкой NODE_EXTRA_CA_CERTS" {
  export SHELL=/bin/zsh
  [ ! -e "$HOME/.zshrc" ]
  oc_run --no-launch
  assert_status 0
  [ -f "$HOME/.zshrc" ]
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  assert_file_contains "$HOME/.zshrc" "# >>> opencode-magnit >>>"
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS=\"$(config_dir_path)/tander-ca-bundle.pem\""
  assert_file_contains "$HOME/.zshrc" "# <<< opencode-magnit <<<"
}

@test "AC-42: повторный запуск оставляет ровно один блок и не дублирует строку PATH" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  oc_run --no-launch
  assert_status 0
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "# <<< opencode-magnit <<<")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "export PATH=\"$(bin_dir_path):")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS=")" = "1" ]
}

@test "AC-43: блок уже актуален → файл профиля не переписывается, mtime не изменился" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  local before
  before=$(mtime_of "$HOME/.zshrc")
  sleep 1
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Профиль: изменений не требуется"
  [ "$(mtime_of "$HOME/.zshrc")" = "$before" ]
}

@test "AC-44: существующий профиль без блока → ровно один .opencode-magnit.bak с исходным содержимым" {
  export SHELL=/bin/zsh
  printf 'export USER_ONE=1\nalias ll="ls -la"\n' >"$HOME/.zshrc"
  local original
  original=$(sha256_file "$HOME/.zshrc")
  oc_run --no-launch
  assert_status 0
  [ -f "$HOME/.zshrc.opencode-magnit.bak" ]
  [ "$(sha256_file "$HOME/.zshrc.opencode-magnit.bak")" = "$original" ]
  local bak_after_first
  bak_after_first=$(sha256_file "$HOME/.zshrc.opencode-magnit.bak")
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$HOME/.zshrc.opencode-magnit.bak")" = "$bak_after_first" ]
  run find "$HOME" -name '.zshrc*.bak' -o -name '.zshrc.opencode-magnit.bak*'
  [ "$(printf '%s\n' "$output" | wc -l | tr -d ' ')" = "1" ]
  assert_file_contains "$HOME/.zshrc" "alias ll="
}

@test "AC-45: SHELL=bash на linux-ветке правит ~/.bashrc и только его" {
  PKG_PLATFORM=linux make_pkg
  export SHELL=/bin/bash
  oc_run --no-launch
  assert_status 0
  [ -f "$HOME/.bashrc" ]
  [ "$(block_count "$HOME/.bashrc")" = "1" ]
  [ ! -e "$HOME/.bash_profile" ]
  [ ! -e "$HOME/.profile" ]
  [ ! -e "$HOME/.zshrc" ]
}

@test "AC-45: SHELL=bash на macos-ветке правит ~/.bash_profile и только его" {
  PKG_PLATFORM=macos make_pkg
  export SHELL=/bin/bash
  oc_run --no-launch
  assert_status 0
  [ -f "$HOME/.bash_profile" ]
  [ "$(block_count "$HOME/.bash_profile")" = "1" ]
  [ ! -e "$HOME/.bashrc" ]
  [ ! -e "$HOME/.profile" ]
}

@test "AC-45: пустой SHELL → правится ~/.profile и только он" {
  export SHELL=""
  oc_run --no-launch
  assert_status 0
  [ -f "$HOME/.profile" ]
  [ "$(block_count "$HOME/.profile")" = "1" ]
  [ ! -e "$HOME/.zshrc" ]
  [ ! -e "$HOME/.bashrc" ]
  [ ! -e "$HOME/.bash_profile" ]
}

@test "AC-46: SHELL=fish → conf.d/opencode-magnit.fish с set -gx, прочие профили не изменены" {
  export SHELL=/usr/local/bin/fish
  oc_run --no-launch
  assert_status 0
  local f="$HOME/.config/fish/conf.d/opencode-magnit.fish"
  [ -f "$f" ]
  [ "$(block_count "$f")" = "1" ]
  assert_file_contains "$f" "set -gx NODE_EXTRA_CA_CERTS \"$(config_dir_path)/tander-ca-bundle.pem\""
  assert_file_contains "$f" "fish_add_path \"$(bin_dir_path)\""
  [ ! -e "$HOME/.zshrc" ]
  [ ! -e "$HOME/.bashrc" ]
  [ ! -e "$HOME/.profile" ]
}

@test "AC-47: чужая строка NODE_EXTRA_CA_CERTS вне блока сохранена, в отчёте предупреждение с номером строки" {
  export SHELL=/bin/zsh
  printf 'export USER_ONE=1\nexport NODE_EXTRA_CA_CERTS=/other/ca.pem\nexport USER_TWO=2\n' >"$HOME/.zshrc"
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Предупреждение: $HOME/.zshrc, строка 2 задаёт NODE_EXTRA_CA_CERTS другим значением"
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS=/other/ca.pem"
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  # Наш блок дописан в конец файла: последняя строка — маркер конца блока.
  [ "$(tail -1 "$HOME/.zshrc")" = "# <<< opencode-magnit <<<" ]
}

@test "AC-48: bin_dir уже в PATH → в блоке нет строки export PATH, строка NODE_EXTRA_CA_CERTS есть" {
  export SHELL=/bin/zsh
  PATH="$(bin_dir_path):$PATH"
  oc_run --no-launch
  assert_status 0
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS="
  refute_file_contains "$HOME/.zshrc" "export PATH="
  refute_output_contains "PATH: добавлен"
}
