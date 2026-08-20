#!/usr/bin/env bats
#
# Шаги установки: CA (N5-I4), бинарник (N5-I7), Desktop (N5-I10), конфиг (N5-I11),
# итоговый отчёт и первый запуск (N5-I12).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

@test "AC-03: запуск из произвольного каталога находит common/manifest.json рядом со скриптом" {
  cd "$HOME"
  run bash "$PKG/install.sh" --prefix "$PREFIX_DIR" --check
  [ "$status" -eq 0 ] || [ "$status" -eq 7 ]
  assert_output_contains "Пакет: opencode-magnit 1.17.9-magnit.1"
  assert_output_contains "Манифест: корректен"
}

@test "AC-02: распакованный POSIX-пакет содержит установщик, common, bin, certs и не содержит windows-файлов" {
  [ -x "$PKG/install.sh" ]
  [ -f "$PKG/common/install-posix.sh" ]
  [ -f "$PKG/common/manifest.json" ]
  [ -x "$PKG/bin/opencode" ]
  [ -f "$PKG/certs/tander-ca-bundle.pem" ]
  [ -f "$PKG/README.md" ]
  [ ! -e "$PKG/install.ps1" ]
  [ ! -e "$PKG/install.bat" ]
}

@test "AC-37: чистый домашний каталог → создан каталог конфига 0755 и CA-файл 0644 из пакета" {
  oc_run --no-launch
  assert_status 0
  [ -d "$(config_dir_path)" ]
  [ "$(mode_of "$(config_dir_path)")" = "755" ]
  [ "$(mode_of "$(config_dir_path)/tander-ca-bundle.pem")" = "644" ]
  [ "$(sha256_file "$(config_dir_path)/tander-ca-bundle.pem")" = "$(sha256_file "$PKG/certs/tander-ca-bundle.pem")" ]
}

@test "AC-38: CA уже установлен и совпадает → строка «CA: уже установлен», mtime не изменился" {
  oc_run --no-launch
  assert_status 0
  local ca before
  ca="$(config_dir_path)/tander-ca-bundle.pem"
  before=$(mtime_of "$ca")
  sleep 1
  oc_run --no-launch
  assert_status 0
  assert_output_contains "CA: уже установлен"
  [ "$(mtime_of "$ca")" = "$before" ]
}

@test "AC-39: устаревший CA перезаписывается содержимым пакета, резервная копия CA не создаётся" {
  mkdir -p "$(config_dir_path)"
  printf 'старый CA\n' >"$(config_dir_path)/tander-ca-bundle.pem"
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$(config_dir_path)/tander-ca-bundle.pem")" = "$(sha256_file "$PKG/certs/tander-ca-bundle.pem")" ]
  run ls "$(config_dir_path)"
  refute_output_contains "tander-ca-bundle.pem.bak"
  refute_output_contains "tander-ca-bundle.pem.opencode-magnit.bak"
}

@test "AC-40: XDG_CONFIG_HOME задан → CA ставится туда, каталог .config не создаётся" {
  export XDG_CONFIG_HOME="$HOME/xdg"
  oc_run --no-launch
  assert_status 0
  [ -f "$HOME/xdg/opencode/tander-ca-bundle.pem" ]
  [ ! -d "$HOME/.config" ]
}

@test "AC-51: чистый домашний каталог → создан ~/.local/bin с opencode 0755 из пакета" {
  oc_run_default --no-launch
  assert_status 0
  [ -d "$HOME/.local/bin" ]
  [ "$(mode_of "$HOME/.local/bin/opencode")" = "755" ]
  [ "$(sha256_file "$HOME/.local/bin/opencode")" = "$(sha256_file "$PKG/bin/opencode")" ]
}

@test "AC-52: после установки в bin_dir нет временных файлов .opencode.new.*" {
  oc_run --no-launch
  assert_status 0
  run find "$(bin_dir_path)" -name '.opencode.new.*'
  assert_status 0
  [ -z "$output" ]
}

@test "AC-52: прерывание установки на проверке хеша оставляет прежний бинарник нетронутым" {
  oc_run --no-launch
  assert_status 0
  local target before
  target="$(bin_dir_path)/opencode"
  before=$(sha256_file "$target")
  printf '\n# подмена пакета\n' >>"$PKG/bin/opencode"
  oc_run --no-launch
  assert_status 4
  [ "$(sha256_file "$target")" = "$before" ]
  run find "$(bin_dir_path)" -name '.opencode.new.*'
  [ -z "$output" ]
}

@test "AC-55: --prefix ставит бинарник в <prefix>/bin, каталог ~/.local/bin не создаётся" {
  oc_run --no-launch
  assert_status 0
  [ -x "$PREFIX_DIR/bin/opencode" ]
  [ ! -d "$HOME/.local/bin" ]
}

@test "AC-56: bin_dir не в PATH → строка export PATH в блоке профиля и пункт «Откройте новый терминал»" {
  oc_run --no-launch
  assert_status 0
  assert_file_contains "$HOME/.zshrc" "export PATH='$(bin_dir_path)':\"\$PATH\""
  assert_output_contains "1. Откройте новый терминал"
  assert_output_contains "PATH: добавлен $(bin_dir_path)"
}

@test "AC-68: манифест без артефакта desktop → строка «Desktop: не входит в пакет», код 0" {
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Desktop: не входит в пакет"
  assert_output_contains "  Desktop: не входит в пакет"
}

@test "AC-69: --no-desktop → строка «Desktop: пропущен (--no-desktop)», установщик Desktop не запускался" {
  PKG_DESKTOP=1 make_pkg
  local stub="$BATS_TEST_TMPDIR/desktopstub"
  mkdir -p "$stub"
  cat >"$stub/hdiutil" <<STUB
#!/usr/bin/env bash
printf 'hdiutil %s\n' "\$*" >>"$BATS_TEST_TMPDIR/desktop.log"
exit 1
STUB
  chmod 0755 "$stub/hdiutil"
  local saved=$PATH
  PATH="$stub:$PATH"
  run bash "$PKG/install.sh" --prefix "$PREFIX_DIR" --no-desktop --no-launch
  PATH=$saved
  assert_status 0
  assert_output_contains "Desktop: пропущен (--no-desktop)"
  [ ! -f "$BATS_TEST_TMPDIR/desktop.log" ]
}

@test "AC-70: существующий opencode.json не изменён, создан ровно один .bak, строка отчёта о конфиге" {
  mkdir -p "$(config_dir_path)"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  local before
  before=$(sha256_file "$(config_dir_path)/opencode.json")
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$(config_dir_path)/opencode.json")" = "$before" ]
  [ "$(sha256_file "$(config_dir_path)/opencode.json.bak")" = "$before" ]
  run find "$(config_dir_path)" -name 'opencode.json.bak*'
  [ "$(printf '%s\n' "$output" | wc -l | tr -d ' ')" = "1" ]
}

@test "AC-70: отчёт содержит строку «Конфиг: <путь> (не изменялся)»" {
  mkdir -p "$(config_dir_path)"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Конфиг: $(config_dir_path)/opencode.json (не изменялся)"
}

@test "AC-71: существующий opencode.json.bak не перезаписывается при повторном запуске" {
  mkdir -p "$(config_dir_path)"
  printf '{"theme":"user"}\n' >"$(config_dir_path)/opencode.json"
  printf '{"theme":"старая копия"}\n' >"$(config_dir_path)/opencode.json.bak"
  local bak_before
  bak_before=$(sha256_file "$(config_dir_path)/opencode.json.bak")
  oc_run --no-launch
  assert_status 0
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$(config_dir_path)/opencode.json.bak")" = "$bak_before" ]
  assert_output_contains "Конфиг: резервная копия уже существует"
}

@test "AC-72: opencode.json отсутствует → ни он, ни .bak не создаются" {
  oc_run --no-launch
  assert_status 0
  [ ! -e "$(config_dir_path)/opencode.json" ]
  [ ! -e "$(config_dir_path)/opencode.json.bak" ]
  run ls "$(config_dir_path)"
  [ "$output" = "tander-ca-bundle.pem" ]
}

@test "AC-73: auth.json не изменён и не прочитан, в выводе нет ключей и токенов" {
  mkdir -p "$HOME/.local/share/opencode"
  printf '{"litellm":{"key":"MARKER-SECRET-VALUE"}}\n' >"$HOME/.local/share/opencode/auth.json"
  local before
  before=$(sha256_file "$HOME/.local/share/opencode/auth.json")
  oc_run --no-launch
  assert_status 0
  [ "$(sha256_file "$HOME/.local/share/opencode/auth.json")" = "$before" ]
  refute_output_contains "MARKER-SECRET-VALUE"
  refute_output_contains "auth.json"
}

@test "AC-74: отчёт печатается фиксированным набором строк в заданном порядке" {
  oc_run --no-launch
  assert_status 0
  local report="$BATS_TEST_TMPDIR/report.txt"
  printf '%s\n' "$output" >"$report"
  line_of() {
    local n
    n=$(grep -n -F -m1 -e "$1" "$report" | head -1 | cut -d: -f1)
    [ -n "$n" ] || { printf 'нет строки: %s\n' "$1" >&2; return 1; }
    printf '%s' "$n"
  }
  local l1 l2 l3 l4 l5 l6 l7
  l1=$(line_of "Готово: OpenCode 1.17.9-magnit.1 установлен.")
  l2=$(line_of "  Бинарник: $(bin_dir_path)/opencode")
  l3=$(line_of "  CA: $(config_dir_path)/tander-ca-bundle.pem")
  l4=$(line_of "  Desktop: ")
  l5=$(line_of "  Конфиг: $(config_dir_path)/opencode.json (не изменялся)")
  l6=$(line_of "  Hub: https://hub.test")
  l7=$(line_of "Дальше:")
  [ "$l1" -lt "$l2" ] && [ "$l2" -lt "$l3" ] && [ "$l3" -lt "$l4" ] &&
    [ "$l4" -lt "$l5" ] && [ "$l5" -lt "$l6" ] && [ "$l6" -lt "$l7" ]
}

@test "AC-75: повторный запуск с актуальным профилем и bin_dir в PATH не печатает «Откройте новый терминал»" {
  # bin_dir в PATH с самого начала: тогда блок профиля обоих запусков одинаков (N5-I5).
  PATH="$(bin_dir_path):$PATH"
  oc_run --no-launch
  assert_status 0
  oc_run --no-launch
  assert_status 0
  assert_output_contains "Профиль: изменений не требуется"
  refute_output_contains "Откройте новый терминал"
  assert_output_contains "2. Запустите: opencode"
}

@test "AC-76: corp status печатается, его ненулевой код игнорируется — установщик завершается кодом 0" {
  oc_run
  assert_status 0
  assert_output_contains "Ключ: не найден"
  assert_output_contains "Готово: OpenCode 1.17.9-magnit.1 установлен."
}

@test "AC-77: --no-launch → corp status не запускался (файл-маркер не создан)" {
  export OPENCODE_TEST_MARKER="$BATS_TEST_TMPDIR/calls.log"
  oc_run --no-launch
  assert_status 0
  [ ! -e "$OPENCODE_TEST_MARKER" ]
}

@test "AC-78: без --no-launch бинарник вызывается ровно один раз и ровно с «corp status»" {
  export OPENCODE_TEST_MARKER="$BATS_TEST_TMPDIR/calls.log"
  oc_run
  assert_status 0
  [ -f "$OPENCODE_TEST_MARKER" ]
  [ "$(wc -l <"$OPENCODE_TEST_MARKER" | tr -d ' ')" = "1" ]
  [ "$(cat "$OPENCODE_TEST_MARKER")" = "corp status" ]
}
