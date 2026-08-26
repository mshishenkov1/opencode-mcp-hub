#!/usr/bin/env bats
#
# Снятие карантина Gatekeeper с неподписанной сборки Desktop (S-B17) и признак подписи в
# манифесте (поле "signed").
#
# Зачем это вообще: бандл, скопированный из dmg, наследует расширенный атрибут
# com.apple.quarantine. Для подписанного приложения это одно предупреждение при первом запуске,
# для НЕподписанного на macOS 15+ — отказ запуска с сообщением «повреждено». Без снятия атрибута
# установка формально успешна, а приложение не работает — то есть отчёт установщика врёт.
#
# Как проверяется. Установка Desktop прогоняется через драйвер (install-posix.sh загружается как
# библиотека, OPENCODE_INSTALLER_SOURCE_ONLY=1) с подменённым desktop_dest_dir — ровно как в
# 36-desktop-safety.bats. Причина та же: `desktop_dest_dir` в настоящем запуске выбирает
# /Applications, если каталог доступен на запись, и полный прогон установки писал бы в РЕАЛЬНЫЙ
# каталог приложений машины, где идут тесты. Границы среды — hdiutil, ditto и xattr — подменены
# журналирующими заглушками в PATH (run_priv настоящий, use_sudo=0), поэтому «xattr вызван с
# нужными аргументами» проверяется наблюдаемо, а не по внутреннему флагу установщика.
# Ветка macOS проверяется и на Linux: платформа задаётся переменной драйвера.
# Совместимость: bash 3.2 (N5-T5).

setup() {
  load helpers
  setup_sandbox
  PKG_PLATFORM=macos PKG_DESKTOP=1 make_pkg
  DESKTOP_LOG="$BATS_TEST_TMPDIR/ops.log"
  export DESKTOP_LOG
}

# Заглушки границ среды. hdiutil «монтирует» образ, создавая бандл в точке монтирования, которую
# выбрал сам install_desktop; ditto копирует; xattr журналирует аргументы и завершается кодом
# OM_XATTR_EXIT. Каждый аргумент отдельной строкой: так видно, что путь с пробелом пришёл ОДНИМ
# аргументом и не был расщеплён оболочкой.
desktop_stubs() {
  local dir="$BATS_TEST_TMPDIR/desktopstub"
  mkdir -p "$dir"

  cat >"$dir/hdiutil" <<STUB
#!/usr/bin/env bash
printf 'hdiutil %s\n' "\$*" >>"$DESKTOP_LOG"
prev=""
mp=""
for a in "\$@"; do
  [ "\$prev" != "-mountpoint" ] || mp=\$a
  prev=\$a
done
if [ "\${1:-}" = "attach" ] && [ -n "\$mp" ]; then
  mkdir -p "\$mp/$OM_APP_NAME" 2>/dev/null || true
fi
exit 0
STUB

  cat >"$dir/ditto" <<STUB
#!/usr/bin/env bash
printf 'ditto %s\n' "\$*" >>"$DESKTOP_LOG"
dest=""
for a in "\$@"; do
  printf 'ARG %s\n' "\$a" >>"$DESKTOP_LOG"
  dest=\$a
done
# Последний аргумент — каталог назначения: создаём его, чтобы дальнейшие шаги видели бандл.
[ -z "\$dest" ] || mkdir -p "\$dest" 2>/dev/null || true
exit 0
STUB

  cat >"$dir/xattr" <<STUB
#!/usr/bin/env bash
printf 'xattr %s\n' "\$*" >>"$DESKTOP_LOG"
for a in "\$@"; do printf 'ARG %s\n' "\$a" >>"$DESKTOP_LOG"; done
exit \${OM_XATTR_EXIT:-0}
STUB

  chmod 0755 "$dir/hdiutil" "$dir/ditto" "$dir/xattr"
  : >"$DESKTOP_LOG"
  PATH="$dir:$PATH"
  export PATH
}

# Драйвер: устанавливает Desktop в каталог песочницы и печатает отчёт установки. Присваивания
# заменяют то, что в обычном запуске делают parse_args и manifest_load.
write_quarantine_driver() {
  cat >"$BATS_TEST_TMPDIR/drive.sh" <<'DRV'
#!/usr/bin/env bash
# shellcheck disable=SC2034
# Обоснование: глобальные переменные ниже читает загруженный через source install-posix.sh.
OPENCODE_INSTALLER_SOURCE_ONLY=1
export OPENCODE_INSTALLER_SOURCE_ONLY
# shellcheck source=/dev/null
. "$INSTALLERS_ROOT/common/install-posix.sh"

# Единственная подменённая функция установщика — граница «куда ставим»: настоящая выбрала бы
# /Applications машины, где идут тесты.
desktop_dest_dir() { printf '%s' "$DEST_DIR"; }

use_sudo=0
opt_quiet=0
opt_no_desktop=${NO_DESKTOP:-0}
opt_system=0
platform=$PLATFORM
pkg_root=$PKG
manifest_path="$PKG/common/manifest.json"
MF_version=1.17.9-magnit.1
MF_os=darwin
MF_arch=arm64
MF_hub_url=https://hub.test
MF_catalog_url=""
MF_signed=$SIGNED
MF_desktop_file=${DESKTOP_FILE-desktop/OpenCode.dmg}
MF_desktop_type=dmg
MF_desktop_app=$OM_APP_NAME
desktop_summary=""
desktop_failed=0
desktop_unsigned=0

bin_target="$SANDBOX/opt/bin/opencode"
ca_target="$HOME/.config/opencode/tander-ca-bundle.pem"
config_dir="$HOME/.config/opencode"
profile_file="$HOME/.zshrc"

install_desktop
print_report 0
DRV
}

# Запуск драйвера. $1 — значение MF_signed, дальше — переопределения через окружение.
drive_install_desktop() {
  write_quarantine_driver
  desktop_stubs
  export SIGNED=$1
  export PLATFORM=${PLATFORM:-macos}
  export DEST_DIR="$SANDBOX/dest"
  mkdir -p "$DEST_DIR"
  printf 'ЧУЖОЕ ПРИЛОЖЕНИЕ\n' >"$DEST_DIR/Neighbour.marker"
  run bash "$BATS_TEST_TMPDIR/drive.sh"
}

desktop_log() {
  cat "$DESKTOP_LOG" 2>/dev/null || true
}

assert_log_contains() {
  if printf '%s\n' "$(desktop_log)" | grep -F -q -e "$1"; then
    return 0
  fi
  printf 'В журнале операций нет строки: %s\nЖурнал:\n%s\n' "$1" "$(desktop_log)" >&2
  return 1
}

refute_log_contains() {
  if printf '%s\n' "$(desktop_log)" | grep -F -q -e "$1"; then
    printf 'В журнале операций найдена нежелательная строка: %s\nЖурнал:\n%s\n' "$1" "$(desktop_log)" >&2
    return 1
  fi
  return 0
}

# ------------------------------------------------------------------ снятие карантина

@test "S-B17: signed=false → xattr -dr com.apple.quarantine вызван для установленного бандла" {
  drive_install_desktop false
  assert_status 0
  assert_output_contains "Desktop: карантин снят (сборка не подписана)"
  assert_log_contains "xattr -dr com.apple.quarantine"
  # Путь бандла пришёл ОДНИМ аргументом: пробел в «OpenCode Magnit.app» не расщепил его.
  # Сравнение построчное (grep -x): подстрочное совпадало бы с началом целого пути.
  printf '%s\n' "$(desktop_log)" | grep -F -x -q "ARG $SANDBOX/dest/$OM_APP_NAME" || {
    printf 'Путь бандла не пришёл одним аргументом:\n%s\n' "$(desktop_log)" >&2
    return 1
  }
  if printf '%s\n' "$(desktop_log)" | grep -F -x -q "ARG $SANDBOX/dest/OpenCode"; then
    printf 'Путь расщеплён по пробелу: аргумент <dest>/OpenCode\n%s\n' "$(desktop_log)" >&2
    return 1
  fi
  [ -f "$SANDBOX/dest/Neighbour.marker" ]
}

@test "S-B17: отчёт установки называет неподписанную сборку отдельной строкой" {
  drive_install_desktop false
  assert_status 0
  assert_output_contains "  Сборка не подписана: карантин снят"
}

@test "S-B17: карантин снимается ПОСЛЕ копирования, а не до — снимать не с чего было бы" {
  drive_install_desktop false
  assert_status 0
  local log ditto_line xattr_line
  log=$(desktop_log)
  ditto_line=$(printf '%s\n' "$log" | grep -n '^ditto ' | head -1 | cut -d: -f1)
  xattr_line=$(printf '%s\n' "$log" | grep -n '^xattr ' | head -1 | cut -d: -f1)
  [ -n "$ditto_line" ]
  [ -n "$xattr_line" ]
  [ "$ditto_line" -lt "$xattr_line" ]
}

# ------------------------------------------------------------------ условность по signed

@test "S-B17: signed=true → карантин не снимается, xattr не вызывается" {
  drive_install_desktop true
  assert_status 0
  refute_log_contains "xattr"
  refute_output_contains "карантин снят"
  refute_output_contains "Сборка не подписана"
  # Сам Desktop при этом установлен — условным стал только шаг карантина.
  assert_output_contains "Desktop: установлен"
}

@test "S-B17: любое иное значение signed трактуется как «не подписана»" {
  # Умолчание «подписана» дало бы приложение, которое не запускается, при успешном отчёте
  # установщика. Пакеты, собранные до появления поля, поля не содержат — и они неподписаны.
  drive_install_desktop ""
  assert_status 0
  assert_log_contains "xattr -dr com.apple.quarantine"
  assert_output_contains "карантин снят"
}

@test "S-B17: на Linux карантина нет — xattr не вызывается даже при signed=false" {
  # Ветка Desktop на Linux до копирования не доходит (dmg ставится вручную, N5-I10), но
  # условие снятия карантина обязано быть привязано и к площадке: com.apple.quarantine
  # существует только на macOS.
  PLATFORM=linux drive_install_desktop false
  assert_status 0
  refute_log_contains "xattr"
  refute_output_contains "карантин"
}

@test "S-B17: --no-desktop → карантин не снимается (снимать не с чего)" {
  NO_DESKTOP=1 drive_install_desktop false
  assert_status 0
  assert_output_contains "Desktop: пропущен (--no-desktop)"
  refute_log_contains "xattr"
  refute_output_contains "Сборка не подписана"
}

@test "S-B17: без Desktop в пакете карантина нет ни в выводе, ни в вызовах" {
  DESKTOP_FILE="" drive_install_desktop false
  assert_status 0
  assert_output_contains "Desktop: не входит в пакет"
  refute_log_contains "xattr"
  refute_output_contains "Сборка не подписана"
}

# ------------------------------------------------------------------ best-effort

@test "S-B17: неудача xattr — предупреждение с ручной командой, установка успешна (код 0)" {
  # Приложение к этому моменту уже скопировано и работоспособно: ронять установку из-за
  # расширенного атрибута нельзя, молчать о нём — тем более.
  export OM_XATTR_EXIT=1
  drive_install_desktop false
  assert_status 0
  assert_output_contains "не удалось снять карантин"
  assert_output_contains "xattr -dr com.apple.quarantine"
  assert_output_contains "Desktop: установлен"
  # Ложной строки отчёта «карантин снят» при неудаче быть не должно.
  refute_output_contains "Сборка не подписана"
}

@test "S-B17: xattr отсутствует в системе — та же best-effort ветка, код 0" {
  write_quarantine_driver
  desktop_stubs
  rm -f "$BATS_TEST_TMPDIR/desktopstub/xattr"
  export SIGNED=false PLATFORM=macos DEST_DIR="$SANDBOX/dest"
  mkdir -p "$DEST_DIR"
  # Настоящего xattr в PATH тоже быть не должно, иначе тест проверил бы не то, что заявлено.
  PATH="$BATS_TEST_TMPDIR/desktopstub:$(min_path_dir xattr)"
  export PATH
  run bash "$BATS_TEST_TMPDIR/drive.sh"
  assert_status 0
  assert_output_contains "не удалось снять карантин"
  assert_output_contains "Desktop: установлен"
}

# ------------------------------------------------------------------ план (--dry-run)

# Каталог назначения в настоящем запуске выбирает desktop_dest_dir: /Applications, если он
# доступен на запись, иначе <дом>/Applications. План ничего не пишет, поэтому здесь безопасно
# гонять полный запуск, но ожидание строится по тому же правилу, а не по литералу.
plan_dest() {
  if [ -w "/Applications" ]; then
    printf '/Applications'
  else
    printf '%s/Applications' "$HOME"
  fi
}

@test "S-B17: --dry-run печатает шаг снятия карантина при signed=false" {
  PKG_PLATFORM=macos PKG_DESKTOP=1 PKG_SIGNED=false make_pkg
  oc_run --dry-run
  assert_status 0
  assert_output_contains "Снять карантин Gatekeeper: $(plan_dest)/$OM_APP_NAME"
}

@test "S-B17: --dry-run при signed=true шага карантина не печатает — план совпадает с установкой" {
  PKG_PLATFORM=macos PKG_DESKTOP=1 PKG_SIGNED=true make_pkg
  oc_run --dry-run
  assert_status 0
  assert_output_contains "Установить Desktop"
  refute_output_contains "Снять карантин"
}

@test "S-B17: --dry-run ничего не выполняет — xattr не вызывается" {
  PKG_PLATFORM=macos PKG_DESKTOP=1 PKG_SIGNED=false make_pkg
  desktop_stubs
  oc_run --dry-run
  assert_status 0
  refute_log_contains "xattr"
  assert_no_forbidden_calls
}

# ------------------------------------------------------------------ поле signed в манифесте

@test "S-B17: signed — не обязательное поле: манифест без него остаётся корректным" {
  PKG_PLATFORM=macos PKG_DESKTOP=1 make_pkg
  refute_file_contains "$PKG/common/manifest.json" '"signed"'
  oc_run --check
  # Ничего не установлено → код 7 (расхождение), но манифест признан корректным.
  assert_status 7
  assert_output_contains "Манифест: корректен"
}

@test "S-B17: signed=true в манифесте не ломает разбор" {
  PKG_PLATFORM=macos PKG_DESKTOP=1 PKG_SIGNED=true make_pkg
  assert_file_contains "$PKG/common/manifest.json" '"signed": true'
  oc_run --dry-run
  assert_status 0
  assert_output_contains "План установки OpenCode"
}
