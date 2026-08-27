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

@test "AC-111: вызов без --artifacts, без --ca и без единого адреса → код 2 в каждом случае" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --version "$VER" --ca "$ART/tander-ca-bundle.pem" --hub-url https://hub.test --out "$OUT"
  assert_status 2
  assert_output_contains "--artifacts"
  release_run --artifacts "$ART" --version "$VER" --hub-url https://hub.test --out "$OUT"
  assert_status 2
  assert_output_contains "--ca"
  # Ревизия 1.11 (S-C10): --hub-url перестал быть обязательным сам по себе, но пакет без единой
  # точки входа собирать незачем — отказ остаётся, меняется его формулировка.
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" --out "$OUT"
  assert_status 2
  assert_output_contains "--hub-url"
  assert_output_contains "--catalog-url"
  [ ! -d "$OUT" ]
}

# ------------------------------------------------------------------ адреса точки входа (S-C10)

@test "S-C10: --catalog-url без --hub-url собирает пакет; в манифесте только catalog_url" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --catalog-url https://updates.test/catalog/v1/catalog.json --out "$OUT" --targets linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  local mf="$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/common/manifest.json"
  [ "$(manifest_field "$mf" catalog_url)" = "https://updates.test/catalog/v1/catalog.json" ]
  # Пустого hub_url в манифесте быть не должно: схема требует непустую строку, а установщик
  # напечатал бы «Hub: » вместо честного «адреса Hub нет».
  refute_file_contains "$mf" '"hub_url"'
}

@test "S-C10: пакет без hub_url устанавливается, отчёт печатает каталог вместо Hub" {
  make_artifacts "$ART" "$VER" "$HOST_TARGET"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --catalog-url https://updates.test/catalog/v1/catalog.json --out "$OUT" --targets "$HOST_TARGET"
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-$HOST_TARGET-$VER.tar.gz" -C "$SANDBOX/unpack"
  run bash "$SANDBOX/unpack/opencode-magnit-$HOST_TARGET-$VER/install.sh" --prefix "$PREFIX_DIR" --no-launch
  assert_status 0
  assert_output_contains "Каталог: https://updates.test/catalog/v1/catalog.json"
  refute_output_contains "  Hub: "
}

@test "S-C10: оба адреса вместе попадают в манифест и в отчёт" {
  make_artifacts "$ART" "$VER" "$HOST_TARGET"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --catalog-url https://updates.test/catalog.json \
    --out "$OUT" --targets "$HOST_TARGET"
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-$HOST_TARGET-$VER.tar.gz" -C "$SANDBOX/unpack"
  local mf="$SANDBOX/unpack/opencode-magnit-$HOST_TARGET-$VER/common/manifest.json"
  [ "$(manifest_field "$mf" hub_url)" = "https://hub.test" ]
  [ "$(manifest_field "$mf" catalog_url)" = "https://updates.test/catalog.json" ]
  run bash "$SANDBOX/unpack/opencode-magnit-$HOST_TARGET-$VER/install.sh" --prefix "$PREFIX_DIR" --check
  assert_output_contains "Hub: https://hub.test"
  assert_output_contains "Каталог: https://updates.test/catalog.json"
}

# ------------------------------------------------------------------ подпись сборки (S-B17)

@test "S-B17: без --signed в манифест пишется \"signed\": false" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  assert_file_contains "$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/common/manifest.json" '"signed": false'
}

@test "S-B17: --signed пишет \"signed\": true — установщик перестаёт снимать карантин" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --signed --out "$OUT" --targets linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  assert_file_contains "$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/common/manifest.json" '"signed": true'
}

# ------------------------------------------------------------------ Desktop для обеих архитектур

@test "S-B16: dmg каждой архитектуры macOS попадает в свой пакет, а не только в arm64" {
  # Прежняя редакция find_desktop_artifact искала dmg только для darwin-arm64: пакет darwin-x64
  # собирался БЕЗ Desktop и молча — установщик печатал «Desktop: не входит в пакет».
  make_artifacts "$ART" "$VER" darwin-arm64 darwin-x64 linux-x64
  printf 'FIXTURE dmg arm64\n' >"$ART/opencode-magnit-desktop-mac-arm64.dmg"
  printf 'FIXTURE dmg x64\n' >"$ART/opencode-magnit-desktop-mac-x64.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64,darwin-x64,linux-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  local t
  for t in darwin-arm64 darwin-x64 linux-x64; do
    tar -xzf "$OUT/opencode-magnit-$t-$VER.tar.gz" -C "$SANDBOX/unpack"
  done
  [ -f "$SANDBOX/unpack/opencode-magnit-darwin-arm64-$VER/desktop/opencode-magnit-desktop-mac-arm64.dmg" ]
  [ -f "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/desktop/opencode-magnit-desktop-mac-x64.dmg" ]
  # Архитектуры не перепутаны: в x64-пакете нет arm64-бандла и наоборот.
  [ ! -e "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/desktop/opencode-magnit-desktop-mac-arm64.dmg" ]
  [ ! -e "$SANDBOX/unpack/opencode-magnit-darwin-arm64-$VER/desktop/opencode-magnit-desktop-mac-x64.dmg" ]
  [ ! -e "$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/desktop" ]
}

@test "S-B17: есть только arm64-dmg → x64-пакет собирается БЕЗ Desktop, а не с чужим бандлом" {
  # Положить arm64-бандл в x64-пакет хуже, чем не положить ничего: установка «успешна», а
  # приложение не запускается.
  make_artifacts "$ART" "$VER" darwin-arm64 darwin-x64
  printf 'FIXTURE dmg arm64\n' >"$ART/opencode-magnit-desktop-mac-arm64.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64,darwin-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-darwin-arm64-$VER.tar.gz" -C "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-darwin-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  [ -d "$SANDBOX/unpack/opencode-magnit-darwin-arm64-$VER/desktop" ]
  [ ! -e "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/desktop" ]
  refute_file_contains "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/common/manifest.json" '"kind": "desktop"'
}

@test "S-B17: отсутствие нужной архитектуры названо в отчёте СБОРКИ как дефект" {
  # Иначе единственным следом остаётся штатная строка установщика «Desktop: не входит в пакет» —
  # уже у пользователя и неотличимая от пакета, для которого Desktop и не собирали.
  make_artifacts "$ART" "$VER" darwin-x64
  printf 'FIXTURE dmg arm64\n' >"$ART/opencode-magnit-desktop-mac-arm64.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-x64
  assert_status 0
  assert_output_contains "Предупреждение: dmg для darwin-x64 не найден"
  # Строка обязана называть, ЧТО в каталоге есть: без этого непонятно, чинить сборку или каталог.
  assert_output_contains "opencode-magnit-desktop-mac-arm64.dmg"
  assert_output_contains "Desktop в пакет не вошёл"
}

@test "S-B17: строка дефекта перечисляет все чужие архитектуры, а не первую" {
  make_artifacts "$ART" "$VER" darwin-arm64
  printf 'FIXTURE dmg x64\n' >"$ART/opencode-magnit-desktop-mac-x64.dmg"
  printf 'FIXTURE dmg universal\n' >"$ART/opencode-magnit-desktop-mac-x64-legacy.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64
  assert_status 0
  assert_output_contains "opencode-magnit-desktop-mac-x64.dmg"
  assert_output_contains "opencode-magnit-desktop-mac-x64-legacy.dmg"
}

@test "S-B17: Desktop не собирали вовсе — предупреждения нет, это штатная сборка" {
  # Отрицательный контроль: пакет без Desktop — нормальный случай, и дефектом он не называется.
  make_artifacts "$ART" "$VER" darwin-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-x64
  assert_status 0
  refute_output_contains "Предупреждение: dmg для"
  refute_output_contains "Desktop в пакет не вошёл"
}

@test "S-B17: нужная архитектура найдена — предупреждения нет" {
  make_artifacts "$ART" "$VER" darwin-arm64 darwin-x64
  printf 'FIXTURE dmg arm64\n' >"$ART/opencode-magnit-desktop-mac-arm64.dmg"
  printf 'FIXTURE dmg x64\n' >"$ART/opencode-magnit-desktop-mac-x64.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64,darwin-x64
  assert_status 0
  refute_output_contains "Предупреждение: dmg для"
}

@test "S-B17: единственный dmg без архитектуры в имени — не дефект, предупреждения нет" {
  make_artifacts "$ART" "$VER" darwin-x64
  printf 'FIXTURE dmg\n' >"$ART/OpenCode.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-x64
  assert_status 0
  refute_output_contains "Предупреждение: dmg для"
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-darwin-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  [ -f "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/desktop/OpenCode.dmg" ]
}

@test "S-B16: единственный dmg без архитектуры в имени — ручная сборка, идёт в оба пакета" {
  make_artifacts "$ART" "$VER" darwin-arm64 darwin-x64
  printf 'FIXTURE dmg\n' >"$ART/OpenCode.dmg"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets darwin-arm64,darwin-x64
  assert_status 0
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-darwin-arm64-$VER.tar.gz" -C "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-darwin-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  [ -f "$SANDBOX/unpack/opencode-magnit-darwin-arm64-$VER/desktop/OpenCode.dmg" ]
  [ -f "$SANDBOX/unpack/opencode-magnit-darwin-x64-$VER/desktop/OpenCode.dmg" ]
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

# ------------------------------------------------------------------ формат --version (N5-B1)
#
# Версия попадает в манифест, в имена архивов и в каталог пакета, поэтому обязана проходить ту же
# схему, что и manifest.json: ^[0-9]+\.[0-9]+\.[0-9]+-magnit\.[0-9]+$. Иначе собрался бы пакет,
# не проходящий собственную схему. Отказ — до создания каталога --out.

# Одна невалидная версия: код 2, названа причина, каталог --out не создан, артефакты не тронуты.
assert_version_rejected() {
  local bad=$1 before after
  rm -rf "$OUT"
  before=$(cd "$ART" && find . -print | LC_ALL=C sort)
  release_run --artifacts "$ART" --version "$bad" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 2
  assert_output_contains "Недопустимый формат --version: $bad"
  if [ -d "$OUT" ]; then
    printf 'Каталог --out создан, хотя версия отвергнута: %s\n' "$OUT" >&2
    return 1
  fi
  after=$(cd "$ART" && find . -print | LC_ALL=C sort)
  [ "$before" = "$after" ] || { printf 'Каталог артефактов изменён\n' >&2; return 1; }
  return 0
}

@test "AC-111, AC-142: --version без суффикса -magnit.N → код 2 и каталог --out не создан" {
  make_artifacts "$ART" "$VER" linux-x64
  assert_version_rejected "1.17.9"
}

@test "AC-111, AC-142: --version с ведущим v и без номера ревизии → код 2 в обоих случаях" {
  make_artifacts "$ART" "$VER" linux-x64
  assert_version_rejected "v1.17.9-magnit.1"
  assert_version_rejected "1.17.9-magnit"
}

@test "AC-111, AC-142: прочие формы --version (неполная, буква в ревизии, пустой upstream) → код 2" {
  make_artifacts "$ART" "$VER" linux-x64
  assert_version_rejected "1.17-magnit.1"
  assert_version_rejected "1.17.9-magnit.1a"
  assert_version_rejected "-magnit.1"
  assert_version_rejected "1.17.9-MAGNIT.1"
}

@test "AC-111, AC-142: --version с посторонними символами (пробел, слэш, кавычка) → код 2, ничего не собрано" {
  make_artifacts "$ART" "$VER" linux-x64
  assert_version_rejected "1.17.9-magnit.1 extra"
  assert_version_rejected "../1.17.9-magnit.1"
  assert_version_rejected '1.17.9-magnit.1"'
}

@test "AC-111, AC-142: валидная --version по-прежнему принимается (контроль на пропуск проверки)" {
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url https://hub.test --out "$OUT" --targets linux-x64
  assert_status 0
  [ -f "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" ]
}

# ------------------------------------------------------------------ экранирование JSON (N5-B1)
#
# Строковые значения, попадающие в манифест из аргументов сборки, экранируются: иначе кавычка или
# обратный слэш в --hub-url дали бы синтаксически битый manifest.json. Проверка — строгим
# парсером (python3 json.load), а не глазами: самодельный awk-парсер установщика лоялен к
# нарушениям синтаксиса и такую поломку скрыл бы.

# Распаковывает собранный пакет цели linux-x64 и печатает путь к его manifest.json.
unpack_manifest() {
  rm -rf "$SANDBOX/unpack"
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" -C "$SANDBOX/unpack"
  printf '%s' "$SANDBOX/unpack/opencode-magnit-linux-x64-$VER/common/manifest.json"
}

# Значение ключа верхнего уровня строгим парсером JSON.
json_value() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

require_python3() {
  command -v python3 >/dev/null 2>&1 || skip "нет python3 для строгой проверки JSON"
}

@test "AC-142: --hub-url с кавычкой и обратным слэшем → манифест — валидный JSON, значение не искажено" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  local hub='https://hub.test/a"b\c/d'
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url "$hub" --out "$OUT" --targets linux-x64
  assert_status 0
  local mf
  mf=$(unpack_manifest)
  run python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$mf"
  assert_status 0
  [ "$(json_value "$mf" hub_url)" = "$hub" ]
  # В файле кавычка и слэш записаны экранированными, а не как есть.
  assert_file_contains "$mf" '\"'
  assert_file_contains "$mf" '\\'
}

@test "AC-142: экранированный hub_url читается установщиком без искажения" {
  require_python3
  make_artifacts "$ART" "$VER" "$HOST_TARGET"
  local hub='https://hub.test/a"b\c/d'
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url "$hub" --out "$OUT" --targets "$HOST_TARGET"
  assert_status 0
  rm -rf "$SANDBOX/unpack"
  mkdir -p "$SANDBOX/unpack"
  tar -xzf "$OUT/opencode-magnit-$HOST_TARGET-$VER.tar.gz" -C "$SANDBOX/unpack"
  local root="$SANDBOX/unpack/opencode-magnit-$HOST_TARGET-$VER"
  run bash "$root/install.sh" --prefix "$PREFIX_DIR" --check
  # Ничего не установлено → код 7, но манифест разобран и Hub напечатан как есть.
  assert_status 7
  assert_output_contains "Манифест: корректен"
  assert_output_contains "Hub: $hub"
}

# Строгая проверка ОБОИХ требований AC-142 по одному значению --hub-url: манифест валиден по
# RFC 8259 и значение hub_url после json.load совпадает с исходным без искажения. Сравнение
# выполняется внутри python3, а исходная строка передаётся файлом, а не подстановкой команд:
# $( ) срезает завершающие переводы строк и скрыл бы ровно ту потерю, которую тест ищет
# (мутация «json_escape без обработки \n»: манифест остаётся валидным за счёт awk-ветки, но
# перевод строки из значения исчезает — см. reports/review-i5-3.json, находка 3).
write_hub_checker() {
  cat >"$SANDBOX/check-hub.py" <<'PY'
import io, json, sys

# newline='' обязателен: в текстовом режиме python3 переводит '\r' в '\n' при чтении и
# сравнение перестало бы различать возврат каретки и перевод строки.
manifest = json.load(io.open(sys.argv[1], encoding='utf-8', newline=''))
got = manifest['hub_url']
want = io.open(sys.argv[2], encoding='utf-8', newline='').read()
if got != want:
    sys.stderr.write('hub_url после разбора: %r\n' % (got,))
    sys.stderr.write('исходное значение:    %r\n' % (want,))
    sys.exit(1)
PY
}

# Сборка с заданным --hub-url и строгая проверка результата. AC-142 допускает ровно два исхода:
# либо запуск отвергнут кодом 2 до сборки (каталог --out не создан), либо манифест валиден и
# значение не искажено. Исход «код 0 и искажённое/невалидное значение» валит тест.
assert_hub_roundtrip() {
  local hub=$1 label=$2 mf want="$SANDBOX/want-hub.bin"
  write_hub_checker
  rm -rf "$OUT" "$SANDBOX/unpack"
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url "$hub" --out "$OUT" --targets linux-x64
  if [ "$status" -eq 2 ]; then
    [ ! -d "$OUT" ] || { printf 'Значение %s отвергнуто, но каталог --out создан\n' "$label" >&2; return 1; }
    return 0
  fi
  if [ "$status" -ne 0 ]; then
    printf 'Сборка на значении %s завершилась кодом %s (допустимы 0 и 2):\n%s\n' "$label" "$status" "$output" >&2
    return 1
  fi
  mf=$(unpack_manifest)
  run python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$mf"
  if [ "$status" -ne 0 ]; then
    printf 'manifest.json на значении %s не является валидным JSON:\n%s\n' "$label" "$output" >&2
    return 1
  fi
  printf '%s' "$hub" >"$want"
  run python3 "$SANDBOX/check-hub.py" "$mf" "$want"
  if [ "$status" -ne 0 ]; then
    printf 'Значение %s искажено при сборке манифеста:\n%s\n' "$label" "$output" >&2
    return 1
  fi
  return 0
}

@test "AC-142, AC-144: --hub-url с переводом строки → манифест валиден И значение не искажено" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  assert_hub_roundtrip "$(printf 'https://hub.test/a\nb')" 'перевод строки'
}

@test "AC-142, AC-144: --hub-url с табуляцией → манифест валиден И значение не искажено" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  assert_hub_roundtrip "$(printf 'https://hub.test/a\tb')" 'табуляция'
}

@test "AC-142, AC-144: --hub-url с возвратом каретки и 0x01 → манифест валиден И значение не искажено" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  assert_hub_roundtrip "$(printf 'https://hub.test/a\rb')" 'возврат каретки'
  assert_hub_roundtrip "$(printf 'https://hub.test/a\001b')" '0x01'
  assert_hub_roundtrip "$(printf 'https://hub.test/a\bb\fc')" 'backspace и formfeed'
}

@test "AC-142, AC-144: --hub-url с кавычкой, слэшем и кириллицей → значение не искажено" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  assert_hub_roundtrip 'https://hub.test/a"b\c/d' 'кавычка и обратный слэш'
  assert_hub_roundtrip 'https://hub.test/каталог/приёмка' 'кириллица'
  assert_hub_roundtrip "$(printf 'https://hub.test/\"a\\b\nв\tг\001д/е')" 'все классы разом'
}

# ------------------------------------------------------------------ провал самопроверки (N5-B2)
#
# Единственный способ наблюдать поведение при провале самопроверки, не трогая продуктовый код, —
# собрать пакет заведомо сломанной КОПИЕЙ release.sh. Копия отличается ровно одной функцией
# (json_escape сделан тождественным), лежит в собственном корне, где каталоги с исходниками
# установщиков подставлены симлинками; остальной скрипт — тот же файл.

write_identity_release() {
  local root="$SANDBOX/fake-installers" sub
  mkdir -p "$root"
  for sub in common linux macos windows; do
    rm -f "$root/$sub"
    ln -s "$INSTALLERS_ROOT/$sub" "$root/$sub"
  done
  awk '
    /^json_escape\(\) \{$/ { print "json_escape() {"; print "  printf %s \"$1\""; skip = 1; next }
    skip == 1 && /^\}$/ { print "}"; skip = 0; next }
    skip == 1 { next }
    { print }
  ' "$INSTALLERS_ROOT/release.sh" >"$root/release.sh"
  chmod 0755 "$root/release.sh"
  printf '%s' "$root/release.sh"
}

@test "AC-144: самопроверка ловит невалидный манифест — код 1, битый архив не остаётся в --out" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  local broken
  broken=$(write_identity_release)
  run bash "$broken" --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url "$(printf 'https://hub.test/a\nb')" --out "$OUT" --targets linux-x64
  assert_status 1
  assert_output_contains "common/manifest.json в архиве не является валидным JSON"
  assert_output_contains "Архив не прошёл самопроверку и удалён из каталога сборки:"
  # Битый архив удалён: перепутать его с годным нельзя.
  [ ! -f "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" ]
  [ ! -f "$OUT/SHA256SUMS" ]
}

@test "AC-144: контроль — та же копия release.sh с исправным json_escape собирает пакет" {
  # Копия отличается от оригинала ровно одной функцией: если бы код 1 выше давала сама копия
  # (симлинки, корень, аргументы), а не поломка экранирования, этот прогон тоже упал бы.
  make_artifacts "$ART" "$VER" linux-x64
  local root="$SANDBOX/fake-installers" sub
  mkdir -p "$root"
  for sub in common linux macos windows; do
    rm -f "$root/$sub"
    ln -s "$INSTALLERS_ROOT/$sub" "$root/$sub"
  done
  cp "$INSTALLERS_ROOT/release.sh" "$root/release.sh"
  run bash "$root/release.sh" --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url "$(printf 'https://hub.test/a\nb')" --out "$OUT" --targets linux-x64
  assert_status 0
  [ -f "$OUT/opencode-magnit-linux-x64-$VER.tar.gz" ]
  [ -f "$OUT/SHA256SUMS" ]
}

@test "AC-142: собранный манифест по умолчанию — валидный JSON (контроль на тождественный json_escape)" {
  require_python3
  make_artifacts "$ART" "$VER" linux-x64
  release_run --artifacts "$ART" --version "$VER" --ca "$ART/tander-ca-bundle.pem" \
    --hub-url 'https://hub.test' --out "$OUT" --targets linux-x64
  assert_status 0
  local mf
  mf=$(unpack_manifest)
  run python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$mf"
  assert_status 0
  [ "$(json_value "$mf" hub_url)" = "https://hub.test" ]
  [ "$(json_value "$mf" version)" = "$VER" ]
}
