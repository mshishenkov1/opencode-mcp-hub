#!/usr/bin/env bats
#
# Пути из манифеста, попадающие в файловые операции: app_name, install_name (N5-P3, N5-P4).
#
# Регрессия на уязвимость обхода каталога: значение artifacts[].app_name подставляется в
# rm -rf ("/Applications/$app_name", "$HOME/Applications/$app_name", "$dest/$app_name"), а при
# --system — через sudo. Любое значение с "..", ведущим "/", обратным слэшем или буквой диска
# обязано отвергаться на разборе манифеста (код 2) до единой файловой операции — на всех путях:
# обычная установка, --uninstall, --dry-run --uninstall, --uninstall --purge.
# Совместимость: bash 3.2 (N5-T5).

setup() {
  load helpers
  setup_sandbox
  PKG_DESKTOP=1 make_pkg
  # Каталог-жертва вне корня установки: обход "../../victim" из $HOME/Applications ведёт сюда.
  VICTIM="$SANDBOX/victim"
  export VICTIM
  mkdir -p "$VICTIM" "$HOME/Applications"
  printf 'МАРКЕР ЖЕРТВЫ\n' >"$VICTIM/keep.txt"
}

# Подмена app_name: пакет пересобирается фабрикой с параметром PKG_APP_NAME (N5-T1).
# sed-подстановки по литералу штатного имени здесь нет намеренно — после переименования
# бандла она перестала бы срабатывать молча.
set_app_name() {
  PKG_DESKTOP=1 PKG_APP_NAME=$1 make_pkg
}

assert_victim_intact() {
  if [ ! -d "$VICTIM" ] || [ ! -f "$VICTIM/keep.txt" ]; then
    printf 'Каталог-жертва удалён: %s\n' "$VICTIM" >&2
    return 1
  fi
  assert_file_contains "$VICTIM/keep.txt" "МАРКЕР ЖЕРТВЫ"
}

# Снимки каталогов, которые установщик мог бы изменить.
snapshot_state() {
  local tag=$1
  snapshot_dir "$HOME" "$BATS_TEST_TMPDIR/$tag.home"
  snapshot_dir "$PREFIX_DIR" "$BATS_TEST_TMPDIR/$tag.prefix"
  snapshot_dir "$VICTIM" "$BATS_TEST_TMPDIR/$tag.victim"
}

# Ни одной файловой операции: три каталога совпадают со снимком "before" побайтово.
assert_no_file_changes() {
  local tag
  snapshot_state after
  for tag in home prefix victim; do
    if ! diff "$BATS_TEST_TMPDIR/before.$tag" "$BATS_TEST_TMPDIR/after.$tag" >&2; then
      printf 'Установщик изменил каталог (%s), хотя манифест невалиден\n' "$tag" >&2
      return 1
    fi
  done
  return 0
}

# ------------------------------------------------------------------ app_name: обход каталога

@test "AC-11, AC-135: app_name с ../.. при установке → код 2, каталог-жертва цел, ничего не создано" {
  set_app_name "../../victim"
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_output_contains "недопустимый путь: ../../victim"
  assert_victim_intact
  [ ! -e "$(bin_dir_path)/opencode" ]
  [ ! -e "$(config_dir_path)/tander-ca-bundle.pem" ]
  assert_no_file_changes
  assert_no_forbidden_calls
}

@test "AC-11, AC-135: --uninstall с app_name=../../victim → код 2, жертва цела, установленное не тронуто" {
  oc_run --no-launch --no-desktop
  assert_status 0
  [ -x "$(bin_dir_path)/opencode" ]
  set_app_name "../../victim"
  snapshot_state before
  oc_run --uninstall
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_output_contains "недопустимый путь: ../../victim"
  refute_output_contains "Desktop: удалён"
  refute_output_contains "Готово: OpenCode удалён."
  assert_victim_intact
  [ -x "$(bin_dir_path)/opencode" ]
  assert_file_contains "$HOME/.zshrc" "# >>> opencode-magnit >>>"
  assert_no_file_changes
  assert_no_forbidden_calls
}

@test "AC-11, AC-135: --dry-run --uninstall с app_name=../../victim → код 2, плана нет, жертва цела" {
  set_app_name "../../victim"
  snapshot_state before
  oc_run --dry-run --uninstall
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  refute_output_contains "План удаления OpenCode"
  assert_victim_intact
  assert_no_file_changes
  assert_no_forbidden_calls
}

@test "AC-11, AC-135: --uninstall --purge с app_name=../../victim → код 2, пользовательские данные целы" {
  oc_run --no-launch --no-desktop
  assert_status 0
  mkdir -p "$HOME/.local/share/opencode"
  printf '{"key":"MARKER"}\n' >"$HOME/.local/share/opencode/auth.json"
  set_app_name "../../victim"
  snapshot_state before
  oc_run --uninstall --purge
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  refute_output_contains "Удаляются пользовательские данные:"
  assert_victim_intact
  [ -f "$HOME/.local/share/opencode/auth.json" ]
  [ -d "$(config_dir_path)" ]
  assert_no_file_changes
  assert_no_forbidden_calls
}

@test "AC-135: app_name с обходом из /Applications по абсолютному пути → код 2, жертва цела" {
  if [ ! -d /Applications ]; then
    skip "нет каталога /Applications (не macOS)"
  fi
  # Форма из отчёта о ревью: /Applications/../..<абсолютный путь> ведёт за пределы /Applications.
  set_app_name "../..$VICTIM"
  snapshot_state before
  oc_run --uninstall
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_output_contains "недопустимый путь: ../..$VICTIM"
  assert_victim_intact
  assert_no_file_changes
  assert_no_forbidden_calls
}

# ------------------------------------------------------------------ app_name: прочие формы пути

@test "AC-135: app_name с ведущим слэшем → код 2 и при установке, и при удалении" {
  set_app_name "/tmp/victim"
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: /tmp/victim"
  oc_run --uninstall
  assert_status 2
  assert_output_contains "недопустимый путь: /tmp/victim"
  assert_no_file_changes
}

@test "AC-135: app_name с обратным слэшем → код 2" {
  set_app_name '..\victim'
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_output_contains "недопустимый путь"
  assert_no_file_changes
}

@test "AC-135: app_name с буквой диска C:\\ → код 2" {
  set_app_name 'C:\victim'
  snapshot_state before
  oc_run --uninstall
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  assert_output_contains "недопустимый путь"
  assert_no_file_changes
}

@test "AC-135, AC-154: штатное app_name=\"OpenCode Magnit.app\" по-прежнему принимается (--dry-run --uninstall)" {
  snapshot_state before
  oc_run --dry-run --uninstall
  assert_status 0
  assert_output_contains "План удаления OpenCode 1.17.9-magnit.1"
  assert_output_contains "/Applications/$OM_APP_NAME"
  assert_no_file_changes
}

# ------------------------------------------------------------------ install_name

@test "AC-11, AC-135: ca.install_name=../x → код 2, CA вне каталога конфига не появляется" {
  manifest_edit 's|"install_name": "tander-ca-bundle.pem"|"install_name": "../x"|'
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле ca.install_name"
  assert_output_contains "недопустимый путь: ../x"
  [ ! -e "${XDG_CONFIG_HOME:-$HOME/.config}/x" ]
  assert_no_file_changes
  assert_no_forbidden_calls
}

@test "AC-11, AC-135: artifacts[].install_name=../y → код 2, бинарник вне bin_dir не появляется" {
  manifest_edit 's|"install_name": "opencode"|"install_name": "../y"|'
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.0.install_name"
  assert_output_contains "недопустимый путь: ../y"
  [ ! -e "$PREFIX_DIR/y" ]
  assert_no_file_changes
  assert_no_forbidden_calls
}

@test "AC-11, AC-135: install_name с ведущим слэшем и с буквой диска → код 2" {
  manifest_edit 's|"install_name": "opencode"|"install_name": "/tmp/opencode"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: /tmp/opencode"
  PKG_DESKTOP=1 make_pkg
  manifest_edit 's|"install_name": "opencode"|"install_name": "C:\\\\opencode"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь"
  [ ! -e "$PREFIX_DIR/bin/opencode" ]
}

@test "AC-11, AC-135, AC-155: многосегментный ca.install_name — промежуточные каталоги создаёт установщик, повторный запуск идемпотентен" {
  # N5-P3 разрешает разделитель "/" внутри ca.install_name, и белый список такое значение
  # принимает. Значит недостающие каталоги внутри каталога конфига обязан создать установщик:
  # иначе cp обрывает установку кодом 1 и СИСТЕМНОЙ АНГЛИЙСКОЙ диагностикой
  # "No such file or directory" — прямое нарушение N5-I1 (всё общение по-русски).
  # Пакет пересобирается без артефакта desktop: фикстурный dmg — не образ, и шаг Desktop дал бы
  # код не по теме теста.
  PKG_CA_INSTALL_NAME='certs/corp/tander-ca-bundle.pem' make_pkg
  export SHELL=/bin/zsh
  local config_dir
  config_dir=$(config_dir_path)
  # Ни одного звена цепочки до запуска нет — иначе тест не отличал бы «создал установщик»
  # от «каталог уже был».
  if [ -e "$config_dir/certs" ]; then
    printf 'Предусловие нарушено: %s существует до установки\n' "$config_dir/certs" >&2
    return 1
  fi
  oc_run --no-launch
  assert_status 0
  refute_output_contains "No such file"
  refute_output_contains "cp:"
  refute_output_contains "поле ca.install_name"
  local ca="$config_dir/certs/corp/tander-ca-bundle.pem"
  [ -f "$ca" ] || { printf 'CA не установлен по многосегментному имени: %s\n' "$ca" >&2; return 1; }
  # Каждое промежуточное звено создано самим установщиком, с правами 0755 (AC-155).
  local link mode
  for link in "$config_dir/certs" "$config_dir/certs/corp"; do
    [ -d "$link" ] || { printf 'Промежуточный каталог не создан: %s\n' "$link" >&2; return 1; }
    mode=$(mode_of "$link")
    [ "$mode" = "755" ] || { printf 'Права %s: ожидалось 755, получено %s\n' "$link" "$mode" >&2; return 1; }
  done
  # Содержимое доехало целым: sha256 установленного CA равен ca.sha256 манифеста.
  local ca_sha_manifest ca_sha_real
  ca_sha_manifest=$(awk '
    index($0, "\"ca\":") > 0 { in_ca = 1 }
    in_ca == 1 && index($0, "\"sha256\"") > 0 {
      line = $0
      sub(/^[^:]*: "/, "", line)
      sub(/".*$/, "", line)
      print line
      exit
    }
  ' "$PKG/common/manifest.json")
  [ -n "$ca_sha_manifest" ] || { printf 'Не удалось прочитать ca.sha256 манифеста\n' >&2; return 1; }
  ca_sha_real=$(sha256_file "$ca")
  [ "$ca_sha_real" = "$ca_sha_manifest" ] || {
    printf 'sha256 установленного CA %s не равен ca.sha256 манифеста %s\n' "$ca_sha_real" "$ca_sha_manifest" >&2
    return 1
  }
  # Цель осталась внутри каталога конфига, а не рядом с ним.
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS='$ca'"
  assert_output_contains "$ca"
  # Повторный запуск на уже созданной цепочке каталогов проходит так же и НЕ МЕНЯЕТ состояния
  # (N5-U1): снимок дома до и после совпадает побайтово.
  snapshot_home "$BATS_TEST_TMPDIR/multiseg.after1"
  oc_run --no-launch
  assert_status 0
  refute_output_contains "No such file"
  refute_output_contains "cp:"
  snapshot_home "$BATS_TEST_TMPDIR/multiseg.after2"
  run diff "$BATS_TEST_TMPDIR/multiseg.after1" "$BATS_TEST_TMPDIR/multiseg.after2"
  assert_status 0
  oc_run --check
  assert_status 0
}

@test "AC-11, AC-135: install_name и app_name проверяются до чтения файлов пакета" {
  # Хеш CLI-артефакта заведомо неверный: если бы проверка путей шла после сверки целостности,
  # код был бы 4. Ожидается 2 — разбор манифеста завершается раньше любых файловых операций.
  manifest_edit 's|"install_name": "opencode"|"install_name": "../y"|'
  manifest_edit 's|^      "sha256": "................................................................"|      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: ../y"
}

# ------------------------------------------------------------------ «схлопывающиеся» пути в пакете
#
# Значения, при которых "<каталог>/<значение>" схлопывается в сам каталог, а не в объект внутри
# него: ".", "./x", "x/." и завершающий "/". Для install_name это дало бы цели вида
# "<bin_dir>/." и "<config_dir>/opencode/" — не то намерение, которое описывает манифест (N5-P3).

@test "AC-11, AC-135: ca.install_name=\".\" и \"./x\" → код 2, каталог конфига не тронут" {
  manifest_edit 's|"install_name": "tander-ca-bundle.pem"|"install_name": "."|'
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле ca.install_name"
  assert_output_contains "недопустимый путь: ."
  assert_no_file_changes
  PKG_DESKTOP=1 make_pkg
  manifest_edit 's|"install_name": "tander-ca-bundle.pem"|"install_name": "./ca.pem"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: ./ca.pem"
  [ ! -e "$(config_dir_path)/ca.pem" ]
}

@test "AC-11, AC-135: artifacts[].install_name с завершающим слэшем и \"x/.\" → код 2" {
  manifest_edit 's|"install_name": "opencode"|"install_name": "opencode/"|'
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.0.install_name"
  assert_output_contains "недопустимый путь: opencode/"
  assert_no_file_changes
  PKG_DESKTOP=1 make_pkg
  manifest_edit 's|"install_name": "opencode"|"install_name": "sub/."|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: sub/."
  [ ! -e "$(bin_dir_path)/opencode" ]
}

@test "AC-11, AC-135: ca.file и artifacts[].file со «схлопывающимся» путём → код 2" {
  manifest_edit 's|"file": "certs/tander-ca-bundle.pem"|"file": "certs/"|'
  snapshot_state before
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле ca.file"
  assert_output_contains "недопустимый путь: certs/"
  assert_no_file_changes
  PKG_DESKTOP=1 make_pkg
  manifest_edit 's|"file": "bin/opencode"|"file": "bin/."|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле artifacts.0.file"
  assert_output_contains "недопустимый путь: bin/."
}

@test "AC-135: is_safe_pkg_path и is_safe_app_name — таблица допустимых и запрещённых значений" {
  source_installer
  local v
  # Внутри пакета допустимо: относительный путь с разделителем "/".
  for v in "opencode" "bin/opencode" "certs/tander-ca-bundle.pem" "a/b/c.pem" "OpenCode.app"; do
    is_safe_pkg_path "$v" || { printf 'Отвергнут допустимый путь: %s\n' "$v" >&2; return 1; }
  done
  # Запрещено для любого пути внутри пакета.
  for v in "" "." ".." "./x" "x/." "x/" "/etc/passwd" "../x" "a/../b" "a/.." "C:/x" 'a\b'; do
    if is_safe_pkg_path "$v"; then
      printf 'Принят недопустимый путь пакета: [%s]\n' "$v" >&2
      return 1
    fi
  done
  # Имя приложения строже: разделителей пути быть не может вовсе.
  is_safe_app_name "OpenCode.app" || return 1
  is_safe_app_name "$OM_APP_NAME" || { printf 'Отвергнуто штатное имя: %s\n' "$OM_APP_NAME" >&2; return 1; }
  for v in "" "." ".." "./OpenCode.app" "OpenCode.app/" "sub/OpenCode.app" "../victim" "/Applications"; do
    if is_safe_app_name "$v"; then
      printf 'Принято недопустимое имя приложения: [%s]\n' "$v" >&2
      return 1
    fi
  done
}
