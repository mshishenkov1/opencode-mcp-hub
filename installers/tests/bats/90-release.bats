#!/usr/bin/env bats
#
# Сборка пакетов для портала: installers/release.sh (N5-B1…N5-B5, N5-P7, N5-D3).

setup() {
  load helpers
  setup_sandbox
  export ART="$SANDBOX/artifacts"
  export OUT="$SANDBOX/dist"
  export VER=1.17.9-magnit.1
  export HOST_TARGET="$(host_os)-$(host_arch)"
}

@test "AC-111: вызов без --version → справка в stderr, код 2, каталог --out не создан" {
  make_artifacts "$ART" "$VER" linux-x64
  run bash -c "bash '$INSTALLERS_ROOT/release.sh' --artifacts '$ART' --ca '$ART/tander-ca-bundle.pem' --hub-url https://hub.test --out '$OUT' 2>&1 1>/dev/null"
  assert_status 2
  assert_output_contains "Не задан обязательный аргумент: --version"
  assert_output_contains "Использование:"
  [ ! -d "$OUT" ]
}

@test "AC-111: вызов без --artifacts, без --ca и без --hub-url → код 2 в каждом случае" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --version "$VER" --ca "$ART/tander-ca-bundle.pem" --hub-url https://hub.test --out "$OUT"
  assert_status 2
  assert_output_contains "--artifacts"
  release_run --artifacts "$ART" --version "$VER" --hub-url https://hub.test --out "$OUT"
  assert_status 2
  assert_output_contains "--ca"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" --out "$OUT"
  assert_status 2
  assert_output_contains "--hub-url"
  [ ! -d "$OUT" ]
}

@test "AC-112: сборка linux-x64 из фикстурных артефактов даёт дерево N5-P2 и корректный манифест" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 0
  local archive="$OUT/opencode-magnit-linux-x64-$VER.tar.gz"
  [ -f "$archive" ]
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$archive" -C "$SANDBOX/unpack"
  local root="$SANDBOX/unpack/opencode-magnit-linux-x64-$VER"
  [ -f "$root/install.sh" ]
  [ -f "$root/common/install-posix.sh" ]
  [ -f "$root/common/manifest.json" ]
  [ -f "$root/bin/opencode" ]
  [ -f "$root/certs/tander-ca-bundle.pem" ]
  [ -f "$root/README.md" ]
  local mf="$root/common/manifest.json"
  [ "$(manifest_field "$mf" hub_url)" = "https://hub.test" ]
  [ "$(manifest_field "$mf" source_release)" = "v$VER" ]
  [ "$(manifest_field "$mf" version)" = "$VER" ]
  [ "$(manifest_field "$mf" os)" = "linux" ]
  [ "$(manifest_field "$mf" arch)" = "x64" ]
  [ "$(manifest_artifact_sha "$mf" cli)" = "$(sha256_file "$root/bin/opencode")" ]
  assert_file_contains "$mf" "$(sha256_file "$root/certs/tander-ca-bundle.pem")"
  run grep -c 'opencode' "$mf"
  assert_file_contains "$mf" '"purge_paths"'
  run grep -A3 '"purge_paths"' "$mf"
  assert_output_contains "opencode"
}

@test "AC-02: собранный POSIX-пакет распакован — состав по N5-P2, windows-установщиков нет" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  local root="$SANDBOX/unpack/opencode-magnit-linux-x64-$VER"
  [ -x "$root/install.sh" ]
  [ ! -e "$root/install.ps1" ]
  [ ! -e "$root/install.bat" ]
  [ ! -e "$root/windows" ]
}

@test "AC-113: POSIX-архив сохраняет бит +x, windows-архив — zip с ps1/bat и без install.sh" {
  make_artifacts "$ART" "$VER" linux-x64 windows-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64,windows-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  local root="$SANDBOX/unpack/opencode-magnit-linux-x64-$VER"
  [ -x "$root/install.sh" ]
  [ -x "$root/bin/opencode" ]
  [ -f "$OUT/opencode-magnit-windows-x64-$VER.zip" ]
  run unzip -Z1 "$OUT/opencode-magnit-windows-x64-$VER.zip"
  assert_status 0
  assert_output_contains "opencode-magnit-windows-x64-$VER/install.ps1"
  assert_output_contains "opencode-magnit-windows-x64-$VER/install.bat"
  assert_output_contains "opencode-magnit-windows-x64-$VER/bin/opencode.exe"
  refute_output_contains "install.sh"
}

@test "AC-114: версия артефакта не совпадает с --version → сообщение с обеими версиями, код 2" {
  make_artifacts "$ART" "1.17.9-magnit.0" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 2
  assert_output_contains "opencode-linux-x64-1.17.9-magnit.0.zip"
  assert_output_contains "1.17.9-magnit.1"
  [ ! -f "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" ]
}

@test "AC-115: самопроверка пакета выполняется — при исправном пакете план установки строится" {
  make_artifacts "$ART" "$VER" "$HOST_TARGET"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets "$HOST_TARGET"
  assert_status 0
  assert_output_contains "Самопроверка: opencode-magnit-$HOST_TARGET-$VER — план установки построен"
}

@test "AC-115: испорченный установщик в пакете валит сборку ненулевым кодом" {
  make_artifacts "$ART" "$VER" "$HOST_TARGET"
  # Копия дерева installers/: репозиторий не изменяется, портится только копия.
  local broken="$SANDBOX/broken-installers"
  mkdir -p "$broken"
  cp -R "$INSTALLERS_ROOT/common" "$INSTALLERS_ROOT/macos" "$INSTALLERS_ROOT/linux" \
    "$INSTALLERS_ROOT/windows" "$broken/"
  cp "$INSTALLERS_ROOT/release.sh" "$broken/release.sh"
  printf '#!/usr/bin/env bash\nexit 9\n' >"$broken/common/install-posix.sh"
  chmod 0755 "$broken/common/install-posix.sh"
  run bash "$broken/release.sh" --artifacts "$ART" --version "$VER" \
    --ca "$ART/tander-ca-bundle.pem" --hub-url https://hub.test --out "$OUT" --targets "$HOST_TARGET"
  [ "$status" -ne 0 ]
  assert_output_contains "Самопроверка пакета не пройдена"
}

@test "AC-116: dmg попадает только в пакет darwin-arm64" {
  make_artifacts "$ART" "$VER" darwin-arm64 darwin-x64 linux-x64
  printf 'FIXTURE dmg\n' >"$ART/OpenCode-$VER-arm64.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64,darwin-x64,linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  local t
  for t in darwin-arm64 darwin-x64 linux-x64; do
    tar -xzf "$OUT/opencode-magnit-$t-$VER.tar.gz" -C "$SANDBOX/unpack"
  done
  [ -d "$SANDBOX/unpack/opencode-magnit-darwin-arm64-$VER/desktop" ]
  assert_file_contains "$SANDBOX/unpack/opencode-magnit-darwin-arm64-$VER/common/manifest.json" '"kind": "desktop"'
  [ ! -e "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/desktop" ]
  [ ! -e "$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/desktop" ]
  refute_file_contains "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/common/manifest.json" '"kind": "desktop"'
  refute_file_contains "$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/common/manifest.json" '"kind": "desktop"'
}

@test "AC-117: SHA256SUMS содержит по строке на архив и проходит проверку sha256sum -c" {
  make_artifacts "$ART" "$VER" linux-x64 darwin-x64 windows-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64,darwin-x64,windows-x64
  assert_status 0
  [ -f "$OUT/SHA256SUMS" ]
  [ "$(wc -l <"$OUT/SHA256SUMS" | tr -d ' ')" = "3" ]
  run grep -c '^[0-9a-f]\{64\}  opencode-magnit-.*' "$OUT/SHA256SUMS"
  [ "$output" = "3" ]
  [ "$(sha256_file "$OUT/opencode-magnit-linux-x64-$VER.tar.gz")" = "$(awk '/linux-x64/ { print $1 }' "$OUT/SHA256SUMS")" ]
  run sums_check "$OUT"
  assert_status 0
}

@test "AC-118: без --publish печатается «публикация пропущена», gh не вызывался" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 0
  assert_output_contains "публикация пропущена: добавьте --publish"
  assert_no_forbidden_calls
}

@test "AC-119: нет CLI-артефакта для цели → ненулевой код и список ожидаемых имён файлов" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets windows-x64
  [ "$status" -ne 0 ]
  assert_output_contains "opencode-windows-x64-$VER.zip"
  assert_output_contains "opencode-windows-x64/bin/opencode.exe"
  [ ! -f "$OUT/opencode-magnit-windows-x64-$VER.zip" ]
}

@test "AC-20: имя архива совпадает с полями манифеста, установленный бинарник печатает ту же версию" {
  make_artifacts "$ART" "$VER" "$HOST_TARGET"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets "$HOST_TARGET"
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-$HOST_TARGET-$VER.tar.gz" -C "$SANDBOX/unpack"
  local root="$SANDBOX/unpack/opencode-magnit-$HOST_TARGET-$VER"
  local mf="$root/common/manifest.json"
  [ "opencode-magnit-$(manifest_field "$mf" os)-$(manifest_field "$mf" arch)-$(manifest_field "$mf" version)" = "opencode-magnit-$HOST_TARGET-$VER" ]
  run bash "$root/install.sh" --prefix "$PREFIX_DIR" --no-launch
  assert_status 0
  run "$PREFIX_DIR/bin/opencode" --version
  [ "$output" = "$VER" ]
}

@test "AC-21: darwin и linux упакованы в .tar.gz, windows — в .zip" {
  make_artifacts "$ART" "$VER" darwin-arm64 linux-x64 windows-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64,linux-x64,windows-x64
  assert_status 0
  [ -f "$OUT/opencode-magnit-darwin-arm64-$VER.tar.gz" ]
  [ -f "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" ]
  [ -f "$OUT/opencode-magnit-windows-x64-$VER.zip" ]
  [ ! -f "$OUT/opencode-magnit-windows-x64-$VER.tar.gz" ]
  [ ! -f "$OUT/opencode-magnit-darwin-arm64-$VER.zip" ]
}

@test "AC-123: README.md пакета короче 60 строк и содержит версию, команду запуска и SHA256SUMS" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  local readme="$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/README.md"
  [ -f "$readme" ]
  [ "$(wc -l <"$readme" | tr -d ' ')" -le 60 ]
  assert_file_contains "$readme" "$VER"
  assert_file_contains "$readme" "linux-x64"
  assert_file_contains "$readme" "bash install.sh"
  assert_file_contains "$readme" "install-user.md"
  assert_file_contains "$readme" "SHA256SUMS"
  [ "$(count_lines_with "$readme" "bash install.sh")" = "1" ]
}
