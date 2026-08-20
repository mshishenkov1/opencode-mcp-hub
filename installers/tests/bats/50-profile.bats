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
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS='$(config_dir_path)/tander-ca-bundle.pem'"
  assert_file_contains "$HOME/.zshrc" "# <<< opencode-magnit <<<"
  # Ревизия 1.5 (N5-I13): значение печатается в ОДИНАРНЫХ кавычках; двойных кавычек вокруг
  # значения в блоке нет. Единственные допустимые двойные кавычки в блоке — вокруг "$PATH".
  local block
  block=$(sed -n '/# >>> opencode-magnit >>>/,/# <<< opencode-magnit <<</p' "$HOME/.zshrc")
  if printf '%s\n' "$block" | grep -F 'NODE_EXTRA_CA_CERTS' | grep -F -q '"'; then
    printf 'В строке NODE_EXTRA_CA_CERTS блока есть двойные кавычки:\n%s\n' "$block" >&2
    return 1
  fi
  return 0
}

@test "AC-42: повторный запуск оставляет ровно один блок и не дублирует строку PATH" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  oc_run --no-launch
  assert_status 0
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "# <<< opencode-magnit <<<")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "export PATH='$(bin_dir_path)':")" = "1" ]
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
  assert_file_contains "$f" "set -gx NODE_EXTRA_CA_CERTS '$(config_dir_path)/tander-ca-bundle.pem'"
  assert_file_contains "$f" "fish_add_path '$(bin_dir_path)'"
  # Ревизия 1.5 (N5-I13): в fish-профиле двойных кавычек нет вовсе.
  refute_file_contains "$f" '"'
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

# ------------------------------------------------------------------ схлопывание и позиция блока
#
# N5-I5/N5-U1: в профиле обязан остаться ровно один маркированный блок, и правится он НА МЕСТЕ —
# существующий блок не переезжает в конец файла, порядок строк пользователя сохраняется.

@test "AC-42, AC-141: два маркированных блока в профиле схлопываются в один, стоящий на месте первого" {
  export SHELL=/bin/zsh
  cat >"$HOME/.zshrc" <<'PROFILE'
export USER_ONE=1
# >>> opencode-magnit >>>
export NODE_EXTRA_CA_CERTS="/stale/ca.pem"
# <<< opencode-magnit <<<
export USER_TWO=2
# >>> opencode-magnit >>>
export NODE_EXTRA_CA_CERTS="/stale/ca.pem"
# <<< opencode-magnit <<<
export USER_THREE=3
PROFILE
  oc_run --no-launch
  assert_status 0
  # Ровно один блок.
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "# <<< opencode-magnit <<<")" = "1" ]
  [ "$(count_lines_with "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS=")" = "1" ]
  # Содержимое блока обновлено на актуальное значение, устаревшее ушло.
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS='$(config_dir_path)/tander-ca-bundle.pem'"
  refute_file_contains "$HOME/.zshrc" "/stale/ca.pem"
  # Позиция: блок остался на месте первого — между USER_ONE и USER_TWO, а не уехал в конец файла.
  local n_one n_block n_two n_three
  n_one=$(grep -n 'USER_ONE' "$HOME/.zshrc" | head -1 | cut -d: -f1)
  n_block=$(grep -n '# >>> opencode-magnit >>>' "$HOME/.zshrc" | head -1 | cut -d: -f1)
  n_two=$(grep -n 'USER_TWO' "$HOME/.zshrc" | head -1 | cut -d: -f1)
  n_three=$(grep -n 'USER_THREE' "$HOME/.zshrc" | head -1 | cut -d: -f1)
  [ "$n_one" -lt "$n_block" ] || { printf 'Блок оказался выше USER_ONE\n' >&2; return 1; }
  [ "$n_block" -lt "$n_two" ] || { printf 'Блок уехал ниже USER_TWO (правка не на месте)\n' >&2; return 1; }
  [ "$n_two" -lt "$n_three" ] || { printf 'Порядок пользовательских строк нарушен\n' >&2; return 1; }
  # Строки пользователя сохранены все три.
  assert_file_contains "$HOME/.zshrc" "export USER_ONE=1"
  assert_file_contains "$HOME/.zshrc" "export USER_TWO=2"
  assert_file_contains "$HOME/.zshrc" "export USER_THREE=3"
  [ "$(tail -1 "$HOME/.zshrc")" = "export USER_THREE=3" ]
}

@test "AC-42, AC-141: после схлопывания повторный запуск файл не меняет (идемпотентность сохранена)" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  # Ручное задвоение блока пользователем.
  local block
  block=$(sed -n '/# >>> opencode-magnit >>>/,/# <<< opencode-magnit <<</p' "$HOME/.zshrc")
  printf '%s\n' "$block" >>"$HOME/.zshrc"
  [ "$(block_count "$HOME/.zshrc")" = "2" ]
  oc_run --no-launch
  assert_status 0
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  local after_collapse
  after_collapse=$(sha256_file "$HOME/.zshrc")
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Профиль: изменений не требуется"
  [ "$(sha256_file "$HOME/.zshrc")" = "$after_collapse" ]
}

# ------------------------------------------------------------------ формы чужого присваивания
#
# N5-I5: предупреждение выдаётся именно на ПРИСВАИВАНИЕ чужого NODE_EXTRA_CA_CERTS вне нашего
# блока. Упоминание в комментарии или внутри строки присваиванием не считается.

# Профиль из переданных строк + прогон установщика.
run_with_profile() {
  printf '%s\n' "$@" >"$HOME/.zshrc"
  export SHELL=/bin/zsh
  oc_run --no-launch
}

assert_foreign_warning_at() {
  assert_status 0
  assert_output_contains "Предупреждение: $HOME/.zshrc, строка $1 задаёт NODE_EXTRA_CA_CERTS другим значением"
}

@test "AC-47, AC-141: declare -x и typeset -x с чужим значением → предупреждение с номером строки" {
  run_with_profile 'export USER_ONE=1' 'declare -x NODE_EXTRA_CA_CERTS=/other/ca.pem'
  assert_foreign_warning_at 2
  assert_file_contains "$HOME/.zshrc" "declare -x NODE_EXTRA_CA_CERTS=/other/ca.pem"
  run_with_profile 'export USER_ONE=1' 'export USER_TWO=2' 'typeset -x NODE_EXTRA_CA_CERTS=/other/ca.pem'
  assert_foreign_warning_at 3
}

@test "AC-47, AC-141: readonly и env NAME=... cmd с чужим значением → предупреждение с номером строки" {
  run_with_profile 'readonly NODE_EXTRA_CA_CERTS=/other/ca.pem'
  assert_foreign_warning_at 1
  run_with_profile '# комментарий' 'env NODE_EXTRA_CA_CERTS=/other/ca.pem some-cmd'
  assert_foreign_warning_at 2
}

@test "AC-47, AC-141: присваивание внутри составной строки (if ...; then export ...; fi) → предупреждение" {
  run_with_profile 'export USER_ONE=1' 'if true; then export NODE_EXTRA_CA_CERTS=/other/ca.pem; fi'
  assert_foreign_warning_at 2
}

@test "AC-47, AC-141: комментарий и echo с именем переменной предупреждения не вызывают" {
  run_with_profile \
    '# NODE_EXTRA_CA_CERTS=/other/ca.pem — заметка на будущее' \
    '#export NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'echo "NODE_EXTRA_CA_CERTS=/other/ca.pem"' \
    'echo "смотри переменную NODE_EXTRA_CA_CERTS"'
  assert_status 0
  refute_output_contains "задаёт NODE_EXTRA_CA_CERTS другим значением"
  # Все четыре строки пользователя на месте, блок дописан один.
  [ "$(block_count "$HOME/.zshrc")" = "1" ]
  assert_file_contains "$HOME/.zshrc" '# NODE_EXTRA_CA_CERTS=/other/ca.pem'
  assert_file_contains "$HOME/.zshrc" 'echo "NODE_EXTRA_CA_CERTS=/other/ca.pem"'
}

@test "AC-47, AC-141: наш собственный блок и наше же значение чужими не считаются" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  refute_output_contains "задаёт NODE_EXTRA_CA_CERTS другим значением"
  # Второй запуск: блок уже есть, строка внутри блока — не «чужая».
  oc_run --no-launch
  assert_status 0
  refute_output_contains "задаёт NODE_EXTRA_CA_CERTS другим значением"
  # Присваивание вне блока, но НАШИМ значением, тоже не чужое.
  local mine
  mine="$(config_dir_path)/tander-ca-bundle.pem"
  printf 'export NODE_EXTRA_CA_CERTS="%s"\n' "$mine" >>"$HOME/.zshrc"
  oc_run --no-launch
  assert_status 0
  refute_output_contains "задаёт NODE_EXTRA_CA_CERTS другим значением"
}

@test "AC-47, AC-141: profile_foreign_env_line — таблица форм: присваивание против упоминания" {
  source_installer
  ca_target="$HOME/.config/opencode/tander-ca-bundle.pem"
  profile_is_fish=0
  local f="$BATS_TEST_TMPDIR/prof"
  # Формы, которые обязаны считаться чужим присваиванием.
  local form
  for form in \
    'NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'export NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    '   export NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'declare -x NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'typeset -x NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'readonly NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'local NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'env NODE_EXTRA_CA_CERTS=/other/ca.pem node -v' \
    'setenv NODE_EXTRA_CA_CERTS /other/ca.pem' \
    'set -gx NODE_EXTRA_CA_CERTS /other/ca.pem' \
    'if true; then export NODE_EXTRA_CA_CERTS=/other/ca.pem; fi' \
    'true && export NODE_EXTRA_CA_CERTS=/other/ca.pem'
  do
    printf '%s\n' "$form" >"$f"
    if ! profile_foreign_env_line "$f" >/dev/null; then
      printf 'Форма НЕ распознана как чужое присваивание: %s\n' "$form" >&2
      return 1
    fi
  done
  # Формы, которые обязаны молчать.
  for form in \
    '# NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    '   # export NODE_EXTRA_CA_CERTS=/other/ca.pem' \
    'echo "NODE_EXTRA_CA_CERTS=/other/ca.pem"' \
    'echo NODE_EXTRA_CA_CERTS' \
    'alias showca="printenv NODE_EXTRA_CA_CERTS"' \
    'export OTHER_NODE_EXTRA_CA_CERTS_BACKUP=/other/ca.pem'
  do
    printf '%s\n' "$form" >"$f"
    if profile_foreign_env_line "$f" >/dev/null; then
      printf 'Ложное срабатывание на форме: %s\n' "$form" >&2
      return 1
    fi
  done
  # Наше собственное значение чужим не считается.
  printf 'export NODE_EXTRA_CA_CERTS="%s"\n' "$ca_target" >"$f"
  if profile_foreign_env_line "$f" >/dev/null; then
    printf 'Наше значение принято за чужое\n' >&2
    return 1
  fi
  # Номер строки — именно первой чужой.
  printf 'export A=1\nexport B=2\ndeclare -x NODE_EXTRA_CA_CERTS=/other/ca.pem\n' >"$f"
  [ "$(profile_foreign_env_line "$f")" = "3" ]
}
