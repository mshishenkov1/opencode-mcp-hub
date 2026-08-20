#!/usr/bin/env bats
#
# Статические проверки исходников, документации и CI (N5-P1, N5-P6, N5-B5, N5-D1, N5-D2,
# N5-T3…N5-T6). Выполняются без запуска установщика.

setup() {
  load helpers
}

@test "AC-01: каталог installers/ содержит все обязательные пути, лончеры без логики установки" {
  local path
  for path in common/install-posix.sh common/manifest.schema.json common/manifest.example.json \
    macos/install.sh linux/install.sh windows/install.ps1 windows/install.bat release.sh \
    ci/installers.yml docs/install-user.md docs/install-admin.md docs/spec.md \
    docs/acceptance-criteria.yaml; do
    [ -f "$INSTALLERS_ROOT/$path" ] || { printf 'нет файла: %s\n' "$path" >&2; return 1; }
  done
  for path in tests/bats tests/pester tests/fixtures; do
    [ -d "$INSTALLERS_ROOT/$path" ] || { printf 'нет каталога: %s\n' "$path" >&2; return 1; }
  done
  local launcher
  for launcher in macos/install.sh linux/install.sh; do
    [ "$(wc -l <"$INSTALLERS_ROOT/$launcher" | tr -d ' ')" -lt 20 ]
    assert_file_contains "$INSTALLERS_ROOT/$launcher" "common/install-posix.sh"
    refute_file_contains "$INSTALLERS_ROOT/$launcher" "NODE_EXTRA_CA_CERTS"
    refute_file_contains "$INSTALLERS_ROOT/$launcher" "sha256"
  done
  assert_file_contains "$INSTALLERS_ROOT/macos/install.sh" "OPENCODE_INSTALLER_PLATFORM=macos"
  assert_file_contains "$INSTALLERS_ROOT/linux/install.sh" "OPENCODE_INSTALLER_PLATFORM=linux"
}

@test "AC-18: в исходниках установщиков нет вызовов сетевых утилит и пакетных менеджеров" {
  run grep -nE '(^|[^A-Za-z0-9_./-])(curl|wget|nc|Invoke-WebRequest|Invoke-RestMethod|WebClient|git|npm|brew|apt|apt-get|winget|choco)([^A-Za-z0-9_-]|$)' \
    "$INSTALLERS_ROOT/common/install-posix.sh" \
    "$INSTALLERS_ROOT/macos/install.sh" \
    "$INSTALLERS_ROOT/linux/install.sh" \
    "$INSTALLERS_ROOT/windows/install.ps1" \
    "$INSTALLERS_ROOT/windows/install.bat" \
    "$INSTALLERS_ROOT/release.sh"
  if [ "$status" -eq 0 ]; then
    printf 'Найдены сетевые вызовы:\n%s\n' "$output" >&2
    return 1
  fi
}

@test "AC-32: install.bat состоит только из ASCII и запускает install.ps1 в обход политики" {
  local f="$INSTALLERS_ROOT/windows/install.bat"
  # Все байты < 0x80: после удаления диапазона 0x00-0x7F не остаётся ни одного байта.
  run bash -c "LC_ALL=C tr -d '\\000-\\177' <'$f' | wc -c | tr -d ' '"
  assert_status 0
  [ "$output" = "0" ]
  assert_file_contains "$f" 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*'
  assert_file_contains "$f" 'exit /b %ERRORLEVEL%'
  # Собственной логики в лончере нет: ни проверок, ни установки.
  refute_file_contains "$f" "sha256"
  refute_file_contains "$f" "NODE_EXTRA_CA_CERTS"
}

@test "AC-120: release.sh не собирает форк, не подписывает сборки и не ходит в сеть" {
  run grep -nE '(^|[^A-Za-z0-9_./-])(bun|codesign|signtool|productsign|notarytool)([^A-Za-z0-9_-]|$)|corp/build\.ts' \
    "$INSTALLERS_ROOT/release.sh"
  [ "$status" -ne 0 ]
  run bash "$INSTALLERS_ROOT/release.sh" --help
  assert_status 0
  assert_output_contains "не собирает форк"
  assert_output_contains "не подписывает сборки"
  assert_output_contains "ничего не загружает"
}

@test "AC-121: install-user.md содержит обязательные разделы и таблицу кодов выхода" {
  local f="$INSTALLERS_ROOT/docs/install-user.md"
  assert_file_contains "$f" "Что делает установщик"
  assert_file_contains "$f" "Чего установщик не делает"
  assert_file_contains "$f" "macOS"
  assert_file_contains "$f" "Linux"
  assert_file_contains "$f" "Windows"
  assert_file_contains "$f" "install.bat"
  assert_file_contains "$f" "Gatekeeper"
  assert_file_contains "$f" "SmartScreen"
  assert_file_contains "$f" "SSO"
  assert_file_contains "$f" "/connectors"
  assert_file_contains "$f" "--check"
  assert_file_contains "$f" "corp status"
  assert_file_contains "$f" "--uninstall"
  assert_file_contains "$f" "--purge"
  local code
  for code in 0 1 2 3 4 5 6 7; do
    run grep -E "\| *\`?$code\`? *\|" "$f"
    assert_status 0
  done
  # Документ на русском: кириллица присутствует.
  run grep -q 'установщик' "$f"
  assert_status 0
}

@test "AC-122: install-admin.md содержит разделы сборки, манифеста, записей в систему и ограничений" {
  local f="$INSTALLERS_ROOT/docs/install-admin.md"
  assert_file_contains "$f" "release.sh"
  assert_file_contains "$f" "manifest.json"
  assert_file_contains "$f" "purge"
  assert_file_contains "$f" "--uninstall"
  assert_file_contains "$f" ".bak"
  assert_file_contains "$f" "R-14"
  assert_file_contains "$f" "D-7"
  assert_file_contains "$f" "S-B9"
  # Выкладка: черновик релиза по тегу v<версия> и ручная карточка загрузок портала (D-8).
  assert_file_contains "$f" "Выкладка"
  assert_file_contains "$f" "--publish"
  assert_file_contains "$f" "релиз"
  assert_file_contains "$f" "портал"
  assert_file_contains "$f" "SHA256SUMS"
  assert_file_contains "$f" "Известные ограничения"
  assert_file_contains "$f" "GUI"
  assert_file_contains "$f" "Desktop"
}

@test "AC-127: каждому обязательному сценарию N5-T3 соответствует хотя бы один тест" {
  local scenario ids id found
  # «сценарий:AC-…,AC-…» — перечень обязательных сценариев N5-T3.
  for scenario in \
    "чистая система:AC-37,AC-51,AC-74" \
    "повторный запуск:AC-42,AC-79,AC-94" \
    "обновление с .bak:AC-80,AC-81" \
    "совпадение хеша:AC-38,AC-83" \
    "конфликт конфига:AC-70,AC-71" \
    "dry-run:AC-96,AC-97" \
    "check:AC-100,AC-101,AC-107" \
    "uninstall:AC-85,AC-93" \
    "повторный uninstall:AC-94" \
    "purge:AC-89,AC-92" \
    "неверный хеш:AC-13,AC-14,AC-98" \
    "повреждённый манифест:AC-06,AC-07,AC-08,AC-09,AC-10,AC-11" \
    "чужая ОС или архитектура:AC-33,AC-34" \
    "нет утилиты хеширования:AC-15" \
    "нет прав:AC-53,AC-54" \
    "purge без uninstall:AC-95" \
    "неизвестный аргумент:AC-25" \
    "отсутствие сети:AC-19"; do
    ids=${scenario#*:}
    found=0
    for id in $(printf '%s' "$ids" | tr ',' ' '); do
      if grep -h '^@test ' "$INSTALLERS_ROOT"/tests/bats/*.bats | grep -q "$id"; then
        found=1
      fi
    done
    if [ "$found" -ne 1 ]; then
      printf 'нет теста для сценария: %s\n' "${scenario%%:*}" >&2
      return 1
    fi
  done
  # Занятый бинарник на Windows — тип manual, покрывается Pester/стендом (D-9).
  run grep -h 'AC-58' "$INSTALLERS_ROOT/docs/acceptance-criteria.yaml"
  assert_status 0
}

@test "AC-127: трассировка AC → тесты сходится (installers/tests/trace-ac.sh)" {
  run bash "$INSTALLERS_ROOT/tests/trace-ac.sh"
  assert_status 0
  assert_output_contains "Все критерии типа unit/integration покрыты."
}

@test "AC-128: состав заданий ci/installers.yml — bats, pester-pwsh, pester-ps51, lint, release-dry-run, installers-required" {
  local f="$INSTALLERS_ROOT/ci/installers.yml" job
  # Ревизия 1.5 (N5-T4): единого задания pester больше нет — три комбинации Pester разведены
  # по двум заданиям, потому что оболочку в GitHub Actions нельзя задать выражением.
  for job in "  bats:" "  pester-pwsh:" "  pester-ps51:" "  lint:" "  release-dry-run:" \
    "  installers-required:"; do
    assert_file_contains "$f" "$job"
  done
  # Единого задания pester в файле быть не должно (строка целиком, а не префикс pester-*).
  if grep -q -E '^  pester:[[:space:]]*$' "$f"; then
    printf 'В %s осталось единое задание pester:\n' "$f" >&2
    return 1
  fi
  # Площадки: bats — ubuntu и macOS; pester-pwsh — ubuntu и windows; pester-ps51 — windows.
  assert_file_contains "$f" "ubuntu-latest, macos-latest"
  assert_file_contains "$f" "runs-on: windows-latest"
  assert_file_contains "$f" "shellcheck"
  assert_file_contains "$f" "Invoke-ScriptAnalyzer"
  # Агрегирующее задание ждёт все пять.
  assert_file_contains "$f" "needs: [bats, pester-pwsh, pester-ps51, lint, release-dry-run]"
  run grep -c 'required: true' "$f"
  [ "$output" -ge 4 ]
}

@test "AC-128: значение shell в каждом задании — литерал pwsh/powershell/bash" {
  local f="$INSTALLERS_ROOT/ci/installers.yml" bad
  bad=$(grep -n -E '^[[:space:]]*shell:' "$f" | grep -v -E 'shell: (pwsh|powershell|bash)$' || true)
  [ -z "$bad" ] || { printf 'Нелитеральная оболочка в %s:\n%s\n' "$f" "$bad" >&2; return 1; }
  # Три комбинации Pester: ubuntu/pwsh, windows/pwsh, windows/powershell 5.1.
  assert_file_contains "$f" "shell: powershell"
  assert_file_contains "$f" "shell: pwsh"
}

@test "AC-128: подстроки «shell: \${{» в installers.yml нет ни разу" {
  # `shell: ${{ matrix.shell }}` GitHub отвергает вместе со всем workflow: прогон падает на
  # старте и не создаёт ни одного задания. AC-128 требует ноль вхождений подстроки в файле —
  # без исключений для комментариев, чтобы отвергнутая форма не возвращалась копипастой.
  local f="$INSTALLERS_ROOT/ci/installers.yml" n
  n=$(grep -F -c 'shell: ${{' "$f" || true)
  if [ "$n" != "0" ]; then
    printf 'В %s найдена подстрока «shell: ${{» (%s раз):\n' "$f" "$n" >&2
    grep -n -F 'shell: ${{' "$f" >&2
    return 1
  fi
}

@test "AC-128, AC-152: оба Windows-задания отвергают прогон с пропусками (SkippedCount > 0)" {
  local f="$INSTALLERS_ROOT/ci/installers.yml" n
  # Ветка отказа обязана быть в pester-pwsh (windows-ветка матрицы) и в pester-ps51.
  n=$(grep -F -c 'SkippedCount -gt 0' "$f" || true)
  [ "$n" -ge 2 ] || { printf 'Проверок SkippedCount > 0 найдено %s, нужно не меньше двух\n' "$n" >&2; return 1; }
  # Пустой прогон тоже красный — во всех трёх комбинациях.
  n=$(grep -F -c 'PassedCount -lt 1' "$f" || true)
  [ "$n" -ge 2 ] || { printf 'Проверок PassedCount < 1 найдено %s\n' "$n" >&2; return 1; }
  assert_file_contains "$f" "ExpandedPath"
  assert_file_contains "$f" "На Windows пропусков быть не должно"
}

@test "AC-153: .gitlab-ci.yml — нет заданий Windows и macOS, Pester только под pwsh" {
  local f="$REPO_ROOT/.gitlab-ci.yml" job
  [ -f "$f" ] || { printf 'Нет файла %s\n' "$f" >&2; return 1; }
  for job in "^gates:" "^bats:" "^pester:" "^lint-shell:" "^lint-powershell:" "^release-dry-run:"; do
    grep -q -E "$job" "$f" || { printf 'В %s нет задания %s\n' "$f" "$job" >&2; return 1; }
  done
  # Ни одного задания на Windows- и macOS-раннерах: ни образа, ни тега, ни имени площадки.
  local bad
  bad=$(grep -n -i -E 'windows-latest|macos-latest|macos|windowsservercore|tags:.*(windows|macos)' "$f" \
    | grep -v -i -E '^[0-9]+:[[:space:]]*#' || true)
  [ -z "$bad" ] || { printf 'Найдены Windows/macOS-площадки в %s:\n%s\n' "$f" "$bad" >&2; return 1; }
  # Pester гоняется только под pwsh в Linux-образе, пропуски в нём допустимы.
  assert_file_contains "$f" "PWSH_IMAGE"
  assert_file_contains "$f" "pwsh -NoProfile -Command"
  refute_file_contains "$f" "SkippedCount -gt 0"
  # Спецификация прямо называет источник подтверждения Windows-веток (N5-T4).
  local spec="$INSTALLERS_ROOT/docs/spec.md"
  assert_file_contains "$spec" "Windows- и macOS-раннеров там нет"
}

@test "AC-154: в installers/tests нет sed-подстановок по литералу старого имени бандла" {
  # После переименования бандла подстановка sed по литералу '"app_name": "OpenCode.app"'
  # перестала бы срабатывать МОЛЧА (sed не меняет файл и не возвращает ошибку), и тест начал бы
  # проверять не то, что заявлено. Штатное имя задаётся ровно одним параметром фабрики фикстур
  # (helpers.bash: OM_APP_NAME + PKG_APP_NAME), см. N5-T1.
  # Строки самого этого теста и комментарии исключаются: иначе тест сработал бы на своём тексте.
  local hits
  hits=$(grep -rn -F '"app_name": "OpenCode.app"' "$INSTALLERS_ROOT/tests" |
    grep -v -E ':[[:space:]]*#|grep ' || true)
  [ -z "$hits" ] || { printf 'Найдены подстановки по литералу старого имени:\n%s\n' "$hits" >&2; return 1; }
  hits=$(grep -rn -E 'sed .*app_name' "$INSTALLERS_ROOT/tests" |
    grep -v -E ':[[:space:]]*#|grep ' || true)
  [ -z "$hits" ] || { printf 'Найдены sed-выражения по полю app_name:\n%s\n' "$hits" >&2; return 1; }
  # Единственная точка задания штатного имени в фикстурах bats.
  assert_file_contains "$INSTALLERS_ROOT/tests/bats/helpers.bash" "OM_APP_NAME='OpenCode Magnit.app'"
  local n
  n=$(grep -F -c "OM_APP_NAME='" "$INSTALLERS_ROOT/tests/bats/helpers.bash" || printf '0')
  [ "$n" = "1" ] || { printf 'Штатное имя задано в %s местах helpers.bash\n' "$n" >&2; return 1; }
}

@test "AC-130: shellcheck -s bash проходит на всех installers/**/*.sh без ошибок" {
  if ! command -v shellcheck >/dev/null 2>&1; then
    skip "shellcheck не установлен"
  fi
  run bash -c "find '$INSTALLERS_ROOT' -name '*.sh' -not -path '*/dist/*' -print0 | xargs -0 shellcheck -s bash"
  assert_status 0
  # Каждое подавление сопровождается комментарием-обоснованием в соседней строке.
  run bash -c "grep -rn -A1 'shellcheck disable=' '$INSTALLERS_ROOT' --include='*.sh' | grep -A0 'Обоснование' | wc -l"
  local justified=$output
  run bash -c "grep -rn 'shellcheck disable=' '$INSTALLERS_ROOT' --include='*.sh' | wc -l"
  [ "$(printf '%s' "$justified" | tr -d ' ')" = "$(printf '%s' "$output" | tr -d ' ')" ]
}

@test "AC-131: в *.sh нет конструкций bash 4+" {
  run bash -c "find '$INSTALLERS_ROOT' -name '*.sh' -not -path '*/dist/*' -print0 | xargs -0 grep -nE 'declare -A|mapfile|readarray|\\\$\\{[A-Za-z_][A-Za-z0-9_]*\\^\\^|\\\$\\{[A-Za-z_][A-Za-z0-9_]*,,|local -n|&>>|\\\$\\{![A-Za-z_]*@\\}'"
  [ "$status" -ne 0 ]
}

@test "AC-132: install.ps1 требует PowerShell 5.1 и не содержит конструкций 6+" {
  local f="$INSTALLERS_ROOT/windows/install.ps1"
  assert_file_contains "$f" "#Requires -Version 5.1"
  run grep -nE '\?\?|\?\.|-Parallel|Join-String|\$PSStyle' "$f"
  [ "$status" -ne 0 ]
  run grep -nE '\)[[:space:]]*\?[[:space:]]*[^ ]+[[:space:]]*:' "$f"
  [ "$status" -ne 0 ]
}

@test "AC-133: install.ps1 сохранён в UTF-8 с BOM" {
  local f="$INSTALLERS_ROOT/windows/install.ps1"
  run bash -c "head -c 3 '$f' | od -An -tx1 | tr -d ' \n'"
  assert_status 0
  [ "$output" = "efbbbf" ]
}

@test "AC-134: во всех *.sh нет BOM и CR, первая строка начинается с #!" {
  local file
  while IFS= read -r file; do
    local head3 first
    head3=$(head -c 3 "$file" | od -An -tx1 | tr -d ' \n')
    if [ "$head3" = "efbbbf" ]; then
      printf 'BOM в файле: %s\n' "$file" >&2
      return 1
    fi
    first=$(head -1 "$file")
    case $first in
      '#!'*) : ;;
      *) printf 'нет shebang: %s\n' "$file" >&2; return 1 ;;
    esac
    if LC_ALL=C grep -q "$(printf '\r')" "$file"; then
      printf 'символы CR в файле: %s\n' "$file" >&2
      return 1
    fi
  done <<EOF
$(find "$INSTALLERS_ROOT" -name '*.sh' -not -path '*/dist/*')
EOF
}
