#!/usr/bin/env bats
#
# Общий контракт: аргументы, режимы, коды выхода (N5-I1), справка и версия.

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

@test "AC-23: --help печатает справку со всеми флагами в stdout, код 0, домашний каталог не изменён" {
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --help
  assert_status 0
  for flag in --dry-run --check --uninstall --purge --system --prefix --no-desktop --no-launch --quiet --help --version; do
    assert_output_contains "$flag"
  done
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-24: --version печатает строку из манифеста и завершается кодом 0" {
  oc_run --version
  assert_status 0
  [ "$output" = "opencode-magnit 1.17.9-magnit.1 ($(host_os)-$(host_arch))" ]
}

@test "AC-25: неизвестный аргумент → справка в stderr, код 2, ничего не изменено" {
  snapshot_home "$BATS_TEST_TMPDIR/before"
  run bash -c "bash '$PKG/install.sh' --prefix '$PREFIX_DIR' --wat 2>&1 1>/dev/null"
  assert_status 2
  assert_output_contains "Неизвестный аргумент: --wat"
  assert_output_contains "Использование: install.sh"
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
  [ ! -d "$PREFIX_DIR" ]
}

@test "AC-26: --dry-run вместе с --check → взаимоисключающие режимы, код 2" {
  oc_run --dry-run --check
  assert_status 2
  assert_output_contains "Взаимоисключающие режимы: --dry-run, --check"
}

@test "AC-27: --check вместе с --uninstall → взаимоисключающие режимы, код 2" {
  oc_run --check --uninstall
  assert_status 2
  assert_output_contains "Взаимоисключающие режимы: --check, --uninstall"
}

@test "AC-28: --system вместе с --prefix → взаимоисключающие режимы, код 2" {
  run bash "$PKG/install.sh" --system --prefix /opt/x
  assert_status 2
  assert_output_contains "Взаимоисключающие режимы: --system, --prefix"
  [ ! -d /opt/x ]
}

@test "AC-95: --purge без --uninstall → сообщение и код 2, ничего не удалено" {
  oc_run --no-launch
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf 'data\n' >"$HOME/.local/share/opencode/keep.txt"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --purge
  assert_status 2
  assert_output_contains "--purge применяется только вместе с --uninstall"
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-17: --skip-hash-check не распознан → код 2, установка не выполнена" {
  # Пакет с заведомо неверным хешем: даже с «флагом пропуска» установки не происходит (N5-P5).
  manifest_edit 's|^      "sha256": ".|      "sha256": "0|'
  oc_run --skip-hash-check
  assert_status 2
  assert_output_contains "Неизвестный аргумент: --skip-hash-check"
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-17: --no-verify не распознан → код 2, установка не выполнена" {
  manifest_edit 's|^      "sha256": ".|      "sha256": "0|'
  oc_run --no-verify
  assert_status 2
  assert_output_contains "Неизвестный аргумент: --no-verify"
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-17: справки о флаге пропуска проверки хеша нет ни в --help, ни в исходнике" {
  oc_run --help
  assert_status 0
  refute_output_contains "skip-hash"
  refute_output_contains "no-verify"
  run grep -F -e "--skip-hash-check" -e "--no-verify" "$INSTALLERS_ROOT/common/install-posix.sh"
  [ "$status" -ne 0 ]
}

@test "AC-29: --dry-run --uninstall печатает план удаления, ничего не удалено, код 0" {
  oc_run --no-launch
  assert_status 0
  snapshot_home "$BATS_TEST_TMPDIR/before"
  oc_run --dry-run --uninstall
  assert_status 0
  assert_output_contains "План удаления OpenCode 1.17.9-magnit.1"
  assert_output_contains "Удалить: $(bin_dir_path)/opencode"
  assert_output_contains "Удалить блок из профиля: $HOME/.zshrc"
  [ -f "$(bin_dir_path)/opencode" ]
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-30: полный прогон при закрытом stdin завершается кодом 0 без ожидания ввода" {
  run bash "$PKG/install.sh" --prefix "$PREFIX_DIR" --no-launch </dev/null
  assert_status 0
  assert_output_contains "Готово: OpenCode 1.17.9-magnit.1 установлен."
  [ -x "$(bin_dir_path)/opencode" ]
}

@test "AC-26: разбор аргументов не зависит от корректности пакета — режимы проверяются первыми" {
  rm -f "$PKG/common/manifest.json"
  oc_run --dry-run --check
  assert_status 2
  assert_output_contains "Взаимоисключающие режимы"
}
