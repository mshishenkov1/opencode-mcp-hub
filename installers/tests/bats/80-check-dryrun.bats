#!/usr/bin/env bats
#
# Диагностические режимы: --dry-run (N5-C1), --check (N5-C2, N5-C3), неизменность ФС (N5-C4),
# а также отказы по правам (N5-I7).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

teardown() {
  # Тесты, снимающие права, возвращают их обратно: иначе bats не уберёт временный каталог.
  chmod 0755 "$HOME" 2>/dev/null || true
  chmod 0755 "$PREFIX_DIR/bin" 2>/dev/null || true
}

@test "AC-96: --dry-run печатает нумерованный план с абсолютными путями, профилем и значением переменной" {
  export SHELL=/bin/zsh
  oc_run --dry-run
  assert_status 0
  assert_output_contains "План установки OpenCode 1.17.9-magnit.1 ($(host_os)-$(host_arch)) — изменения не вносятся:"
  assert_output_contains "  1. Создать каталог: $(config_dir_path)"
  assert_output_contains "Скопировать CA: $PKG/certs/tander-ca-bundle.pem -> $(config_dir_path)/tander-ca-bundle.pem"
  assert_output_contains "Создать каталог: $(bin_dir_path)"
  assert_output_contains "Скопировать бинарник: $PKG/bin/opencode -> $(bin_dir_path)/opencode"
  assert_output_contains "Обновить блок в профиле: $HOME/.zshrc (NODE_EXTRA_CA_CERTS=$(config_dir_path)/tander-ca-bundle.pem)"
  assert_output_contains "Добавить в PATH через профиль: $(bin_dir_path)"
  assert_output_contains "Hub: https://hub.test"
}

@test "AC-97: --dry-run не меняет файловую систему, код 0" {
  printf 'export USER_ONE=1\n' >"$HOME/.zshrc"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --dry-run
  assert_status 0
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
  [ ! -d "$PREFIX_DIR" ]
}

@test "AC-99: --dry-run --system показывает /usr/local/bin и не вызывает sudo" {
  run bash "$PKG/install.sh" --dry-run --system
  assert_status 0
  assert_output_contains "/usr/local/bin/opencode"
  assert_no_forbidden_calls
  [ ! -e /usr/local/bin/opencode ]
}

@test "AC-100: полностью установленный пакет → построчный отчёт --check и код 0" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  oc_run --check
  assert_status 0
  assert_output_contains "Пакет: opencode-magnit 1.17.9-magnit.1 ($(host_os)-$(host_arch))"
  assert_output_contains "Манифест: корректен"
  assert_output_contains "Бинарник: установлен 1.17.9-magnit.1 ($(bin_dir_path)/opencode)"
  assert_output_contains "PATH: содержит $(bin_dir_path)"
  assert_output_contains "CA: установлен ($(config_dir_path)/tander-ca-bundle.pem)"
  assert_output_contains "NODE_EXTRA_CA_CERTS: задан ($(config_dir_path)/tander-ca-bundle.pem)"
  assert_output_contains "Конфиг: $(config_dir_path)/opencode.json (не изменяется установщиком)"
  assert_output_contains "Hub: https://hub.test"
  assert_output_contains "Итог: всё установлено"
}

@test "AC-101: чистая система → строки «не установлен» и «Итог: требуется установка»" {
  oc_run --check
  assert_output_contains "Бинарник: не установлен"
  assert_output_contains "PATH: не содержит $(bin_dir_path)"
  assert_output_contains "CA: не установлен"
  assert_output_contains "NODE_EXTRA_CA_CERTS: не задан"
  assert_output_contains "Итог: требуется установка"
}

@test "AC-102: установлена другая версия → строка «Бинарник: другая версия»" {
  mkdir -p "$(bin_dir_path)"
  write_fake_binary "$(bin_dir_path)/opencode" "1.17.9-magnit.0"
  oc_run --check
  assert_status 7
  assert_output_contains "Бинарник: другая версия (установлена 1.17.9-magnit.0, в пакете 1.17.9-magnit.1)"
}

@test "AC-103: установленный бинарник без рабочего --version → «версия не определена», --check не падает" {
  mkdir -p "$(bin_dir_path)"
  write_noversion_binary "$(bin_dir_path)/opencode"
  oc_run --check
  assert_status 7
  assert_output_contains "Бинарник: установлен, версия не определена ($(bin_dir_path)/opencode)"
}

@test "AC-104: установленный CA отличается от пакетного → строка «CA: отличается от пакета»" {
  oc_run --no-launch
  assert_status 0
  printf 'другой CA\n' >"$(config_dir_path)/tander-ca-bundle.pem"
  oc_run --check
  assert_status 7
  assert_output_contains "CA: отличается от пакета ($(config_dir_path)/tander-ca-bundle.pem)"
}

@test "AC-105: сразу после установки --check видит переменную по блоку профиля, а не по окружению" {
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  [ -z "${NODE_EXTRA_CA_CERTS:-}" ]
  oc_run --check
  assert_status 0
  assert_output_contains "NODE_EXTRA_CA_CERTS: задан ($(config_dir_path)/tander-ca-bundle.pem)"
}

@test "AC-105: переменная окружения указывает на чужой путь и блока нет → «задан другой путь»" {
  export NODE_EXTRA_CA_CERTS=/other/ca.pem
  oc_run --check
  assert_status 7
  assert_output_contains "NODE_EXTRA_CA_CERTS: задан другой путь (/other/ca.pem)"
}

@test "AC-106: --check на полностью установленном пакете завершается кодом 0" {
  oc_run --no-launch
  assert_status 0
  oc_run --check
  assert_status 0
}

@test "AC-107: --check на чистой системе завершается кодом 7" {
  oc_run --check
  assert_status 7
  assert_output_contains "Итог: требуется установка"
}

@test "AC-108: Desktop не входит в пакет — расхождением не считается, код 0" {
  oc_run --no-launch
  assert_status 0
  oc_run --check
  assert_status 0
  assert_output_contains "Desktop: не входит в пакет"
  refute_output_contains "Desktop: не установлен"
}

@test "AC-109: домашний каталог доступен только на чтение → отчёт --check полный, код 7, ошибок прав нет" {
  chmod 0555 "$HOME"
  oc_run --check
  chmod 0755 "$HOME"
  assert_status 7
  assert_output_contains "Манифест: корректен"
  assert_output_contains "Бинарник: не установлен"
  assert_output_contains "Итог: требуется установка"
  refute_output_contains "Нет прав на запись"
}

@test "AC-110: --help, --version, --check и --dry-run не создают каталогов" {
  oc_run --help
  assert_status 0
  oc_run --version
  assert_status 0
  oc_run --check
  assert_status 7
  oc_run --dry-run
  assert_status 0
  [ ! -d "$HOME/.config" ]
  [ ! -d "$HOME/.config/opencode" ]
  [ ! -d "$HOME/.local/bin" ]
  [ ! -d "$PREFIX_DIR" ]
}

@test "AC-53: нет права записи в bin_dir → код 5 с именем каталога, sudo не вызывался" {
  mkdir -p "$(bin_dir_path)"
  write_fake_binary "$(bin_dir_path)/opencode" "1.17.9-magnit.0"
  local before
  before=$(sha256_file "$(bin_dir_path)/opencode")
  chmod 0555 "$(bin_dir_path)"
  oc_run --no-launch
  chmod 0755 "$(bin_dir_path)"
  assert_status 5
  assert_output_contains "Нет прав на запись в каталог: $(bin_dir_path)"
  assert_no_forbidden_calls
  [ "$(sha256_file "$(bin_dir_path)/opencode")" = "$before" ]
}

@test "AC-54: --system без sudo и не под root → код 5 с подсказкой про root и \$HOME/.local/bin" {
  local saved=$PATH
  PATH=$(min_path_dir sudo)
  run bash "$PKG/install.sh" --system --no-launch
  PATH=$saved
  assert_status 5
  assert_output_contains "нужны права root: запустите под root или без --system"
  assert_output_contains ".local/bin"
  [ ! -e /usr/local/bin/opencode ]
}
