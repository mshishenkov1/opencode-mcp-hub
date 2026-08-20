#!/usr/bin/env bats
#
# Целостность пакета и среда: sha256 (N5-P5), ОС/архитектура (N5-I3), оффлайновость (N5-P6).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

@test "AC-13: изменённый bin/opencode → код 4 с именем файла и обоими хешами, бинарник не установлен" {
  local expected
  expected=$(sha256_file "$PKG/bin/opencode")
  printf '\n# подмена после генерации манифеста\n' >>"$PKG/bin/opencode"
  local actual
  actual=$(sha256_file "$PKG/bin/opencode")
  oc_run --no-launch
  assert_status 4
  assert_output_contains "Несовпадение sha256"
  assert_output_contains "$PKG/bin/opencode"
  assert_output_contains "$expected"
  assert_output_contains "$actual"
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-14: CA не соответствует ca.sha256 → код 4, CA не скопирован, профиль не изменён" {
  printf 'подмена CA\n' >>"$PKG/certs/tander-ca-bundle.pem"
  printf 'export USER_LINE=1\n' >"$HOME/.zshrc"
  local before
  before=$(sha256_file "$HOME/.zshrc")
  oc_run --no-launch
  assert_status 4
  assert_output_contains "Несовпадение sha256"
  assert_output_contains "tander-ca-bundle.pem"
  [ ! -e "$(config_dir_path)/tander-ca-bundle.pem" ]
  [ "$(sha256_file "$HOME/.zshrc")" = "$before" ]
}

@test "AC-98: --dry-run на пакете с изменённым bin/opencode → код 4" {
  printf '\n# подмена\n' >>"$PKG/bin/opencode"
  oc_run --dry-run
  assert_status 4
  assert_output_contains "Несовпадение sha256"
  [ ! -d "$(config_dir_path)" ]
}

@test "AC-15: нет ни sha256sum, ни shasum, ни openssl → код 3 с перечислением утилит" {
  local saved=$PATH
  PATH=$(min_path_dir sha256sum shasum openssl)
  run bash "$PKG/install.sh" --prefix "$PREFIX_DIR" --no-launch
  PATH=$saved
  assert_status 3
  assert_output_contains "sha256sum"
  assert_output_contains "shasum -a 256"
  assert_output_contains "openssl dgst -sha256"
  [ ! -e "$(bin_dir_path)/opencode" ]
  [ ! -d "$(config_dir_path)" ]
}

@test "AC-16: sha256 в манифесте заглавными буквами → хеши совпали, установка выполнена" {
  PKG_SHA_UPPER=1 make_pkg
  run grep -q '"sha256": "[0-9A-F]\{64\}"' "$PKG/common/manifest.json"
  assert_status 0
  oc_run --no-launch
  assert_status 0
  [ -x "$(bin_dir_path)/opencode" ]
  [ -f "$(config_dir_path)/tander-ca-bundle.pem" ]
}

@test "AC-22: синтаксически некорректный PEM с верным sha256 устанавливается без замечаний" {
  printf 'это не сертификат\n' >"$PKG/certs/tander-ca-bundle.pem"
  write_manifest "$PKG"
  oc_run --no-launch
  assert_status 0
  run cat "$(config_dir_path)/tander-ca-bundle.pem"
  assert_output_contains "это не сертификат"
}

@test "AC-33: пакет для другой ОС → код 3 с обеими парами ос-архитектура" {
  PKG_OS=$(other_os) make_pkg
  oc_run --no-launch
  assert_status 3
  assert_output_contains "Пакет собран для $(other_os)-$(host_arch)"
  assert_output_contains "система — $(host_os)-$(host_arch)"
  [ ! -e "$(bin_dir_path)/opencode" ]
  [ ! -d "$(config_dir_path)" ]
}

@test "AC-34: пакет для другой архитектуры → код 3, установка не выполняется" {
  PKG_ARCH=$(other_arch) make_pkg
  oc_run --no-launch
  assert_status 3
  assert_output_contains "Пакет собран для $(host_os)-$(other_arch)"
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-36: нормализация архитектуры: aarch64, x86_64, amd64 → arm64, x64, x64; armv7l → отказ" {
  source_installer
  [ "$(normalize_arch aarch64)" = "arm64" ]
  [ "$(normalize_arch arm64)" = "arm64" ]
  [ "$(normalize_arch x86_64)" = "x64" ]
  [ "$(normalize_arch amd64)" = "x64" ]
  run normalize_arch armv7l
  [ "$status" -ne 0 ]
  [ -z "$output" ]
}

@test "AC-36: нормализация ОС: Darwin и Linux распознаются, прочее — отказ" {
  source_installer
  [ "$(normalize_os Darwin)" = "darwin" ]
  [ "$(normalize_os Linux)" = "linux" ]
  run normalize_os SunOS
  [ "$status" -ne 0 ]
}

@test "AC-36: uname -m = armv7l → код 3 «неподдерживаемая архитектура», ничего не установлено" {
  local stub="$BATS_TEST_TMPDIR/unamestub"
  mkdir -p "$stub"
  cat >"$stub/uname" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  -m) printf 'armv7l\n' ;;
  -s) printf 'Linux\n' ;;
  *) printf 'Linux\n' ;;
esac
STUB
  chmod 0755 "$stub/uname"
  PKG_OS=linux PKG_ARCH=x64 PKG_PLATFORM=linux make_pkg
  local saved=$PATH
  PATH="$stub:$PATH"
  run bash "$PKG/install.sh" --prefix "$PREFIX_DIR" --no-launch
  PATH=$saved
  assert_status 3
  assert_output_contains "Неподдерживаемая архитектура: uname -m = armv7l"
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-19: полный прогон без сети завершается кодом 0, сетевые утилиты не вызывались" {
  # curl/wget/nc/git/npm/brew/apt/winget/choco подменены ловушками (setup_sandbox).
  run env http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
    bash "$PKG/install.sh" --prefix "$PREFIX_DIR" --no-launch
  assert_status 0
  assert_no_forbidden_calls
  [ -x "$(bin_dir_path)/opencode" ]
  [ -f "$(config_dir_path)/tander-ca-bundle.pem" ]
  refute_output_contains "сеть"
  refute_output_contains "Не удалось загрузить"
}

@test "AC-19: --check и --uninstall без сети не обращаются к сетевым утилитам" {
  oc_run --no-launch
  assert_status 0
  oc_run --check
  [ "$status" -eq 0 ] || [ "$status" -eq 7 ]
  oc_run --uninstall
  assert_status 0
  assert_no_forbidden_calls
}
