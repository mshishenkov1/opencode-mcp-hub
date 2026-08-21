#!/usr/bin/env bats
#
# Экранирование значений в генерируемом коде оболочки (N5-I13, N5-I5, N5-P3, N5-P4).
#
# Модель угрозы. Блок в файле профиля — это ГЕНЕРИРУЕМЫЙ КОД: оболочка пользователя исполнит его
# при следующем входе. Значение, попавшее туда без кавычек, исполняется: '$(…)' и обратная
# кавычка запускают команду, '"' закрывает строку, ';' и '&' начинают новую. Отсюда две линии
# защиты ревизии 1.5:
#   1) путевые поля манифеста (ca.install_name, artifacts[].install_name, artifacts[].app_name)
#      ограничены белым списком символов — метасимвол в них означает код 2 (AC-145);
#   2) всё, что в код профиля всё-таки попадает (пути каталогов из --prefix и $HOME), печатается
#      в ОДИНАРНЫХ кавычках с экранированием — отдельно для POSIX-оболочек и для fish
#      (AC-146, AC-147).
# Совместимость: bash 3.2 (N5-T5).

setup() {
  load helpers
  setup_sandbox
  make_pkg
  # Каталог исполнения: сюда попали бы файлы-канарейки, если бы подстановка сработала.
  CANARY_DIR="$SANDBOX/canary"
  export CANARY_DIR
  mkdir -p "$CANARY_DIR"
  cd "$CANARY_DIR" || return 1
}

# Ни одной канарейки — ни в каталоге исполнения, ни в $HOME, ни в каталоге пакета.
assert_no_canaries() {
  local name found
  for name in PWNED BREAKOUT CANARY1 CANARY2 CANARY3; do
    found=$(find "$SANDBOX" -name "$name" 2>/dev/null | head -5)
    if [ -n "$found" ]; then
      printf 'Сработала подстановка: создан файл-канарейка\n%s\n' "$found" >&2
      return 1
    fi
  done
  return 0
}

# ------------------------------------------------------------------ AC-145: метасимволы в манифесте
#
# Перечень значений разведён по полям, потому что правила N5-P3/N5-P4 для них разные:
# в app_name пробел U+0020 ДОПУСТИМ, если он не первый и не последний символ (на этом держатся
# AC-62, AC-64, AC-138 и AC-154 со штатным именем «OpenCode Magnit.app»), а в путях внутри
# пакета пробел запрещён. Поэтому для app_name вместо 'a b.pem' проверяются краевые пробелы.

OM_META_PATH_VALUES() {
  # printf, а не heredoc: краевые пробелы и табуляция обязаны дойти до значения побайтово.
  printf 'pwn$(touch PWNED).pem\n'
  printf 'pwn`touch PWNED`.pem\n'
  printf 'a.pem"; touch BREAKOUT; x="\n'
  printf 'a;touch BREAKOUT.pem\n'
  printf "a'b.pem\n"
  printf 'a b.pem\n'
  printf 'a\tb.pem\n'
}

# Для app_name перечень отличается ровно одним пунктом: пробел ВНУТРИ имени правилами N5-P3/N5-P4
# разрешён (на нём держится штатное «OpenCode Magnit.app», AC-62, AC-64, AC-138, AC-154), поэтому
# вместо 'a b.pem' проверяются краевые пробелы — ведущий и завершающий.
OM_META_APP_VALUES() {
  printf 'pwn$(touch PWNED).app\n'
  printf 'pwn`touch PWNED`.app\n'
  printf 'a.app"; touch BREAKOUT; x="\n'
  printf 'a;touch BREAKOUT.app\n'
  printf "a'b.app\n"
  printf ' a.app\n'
  printf 'a.app \n'
}

# Управляющие символы вынесены отдельно: по N5-P3/N5-P4 они отвергаются наравне с метасимволами,
# но проходят по другому пути — сначала через разбор JSON-экранирования, а уже потом через
# белый список. См. bugs/BUG-I5-003.
OM_CONTROL_VALUES() {
  printf 'a\tb.app\n'
}

# ------------------------------------------------------------------ AC-145: посимвольный перечень
#
# Перечни выше проверяют ФОРМЫ атаки, и почти каждое значение в них несёт сразу несколько
# запрещённых символов: 'pwn$(touch PWNED).pem' — это '$', '(', ')' и пробел, 'a.pem"; touch
# BREAKOUT; x="' — двойная кавычка, ';' и пробел. Такой набор не отличает «белый список отверг
# значение» от «белый список отверг ПОПУТНЫЙ символ»: если из набора is_safe_pkg_path выпадут
# '$' и ';', все значения выше по-прежнему получат код 2 — по скобкам, кавычкам и пробелу, — и
# ослабление ПЕРВОЙ линии защиты N5-I13 останется незамеченным (вторая линия, экранирование при
# печати в профиль, при этом молча начнёт держать нагрузку, на которую не рассчитана).
#
# Поэтому ниже — ровно ОДИН запрещённый символ на значение. Каждое отличается от заведомо
# принимаемого 'ab.pem' («AC-145: базовое значение посимвольного перечня…» ниже) единственным
# символом, значит его отказ может объясняться только этим символом. Перечень покрывает
# запрещённые символы spec.md N5-P3/N5-P4 поимённо, включая управляющие, 0x7f и байт >= 0x80.
OM_SINGLE_CHAR_PATH_VALUES() {
  local c
  # printf с '%s', а не с готовым форматом: значение символа не должно попасть в строку формата
  # (иначе '%' и '\' были бы истолкованы самим printf).
  for c in '$' '`' '"' "'" ';' '&' '|' '<' '>' '(' ')' '{' '}' '[' ']' \
           '*' '?' '!' '#' '~' '%' ':' '=' ',' '^' '@' '+' '\' ' '; do
    printf 'a%sb.pem\n' "$c"
  done
  # Управляющие: табуляция (проходит через JSON-экранирование \t), вертикальная табуляция и
  # 0x7f (доходят до белого списка сырым байтом), плюс кодовая точка >= 0x80.
  printf 'a\tb.pem\n'
  printf 'a\013b.pem\n'
  printf 'a\177b.pem\n'
  printf 'aйb.pem\n'
}

# Для app_name перечень отличается ровно двумя пунктами: пробел ВНУТРИ имени разрешён (N5-P3,
# N5-P4 — «OpenCode Magnit.app»), поэтому из перечня исключён, а разделитель '/' в имени
# приложения запрещён (это ровно одно имя каталога), поэтому в перечень добавлен.
OM_SINGLE_CHAR_APP_VALUES() {
  local c
  for c in '$' '`' '"' "'" ';' '&' '|' '<' '>' '(' ')' '{' '}' '[' ']' \
           '*' '?' '!' '#' '~' '%' ':' '=' ',' '^' '@' '+' '\' '/'; do
    printf 'a%sb.app\n' "$c"
  done
  printf 'a\tb.app\n'
  printf 'a\013b.app\n'
  printf 'a\177b.app\n'
  printf 'aйb.app\n'
}

# Шесть режимов запуска из AC-145.
OM_MODES="--no-launch|--check|--dry-run|--uninstall|--dry-run --uninstall|--uninstall --purge"

# Один прогон: код 2, названо поле и путь к манифесту, профиль не создан, канареек нет.
# Аргументы: <ожидаемое имя поля> <режим…>
assert_field_rejected() {
  local field=$1
  shift
  oc_run "$@"
  assert_status 2 || return 1
  assert_output_contains "поле $field" || return 1
  assert_output_contains "$PKG/common/manifest.json" || return 1
  refute_output_contains "Манифест: корректен" || return 1
  [ ! -e "$HOME/.zshrc" ] || { printf 'Создан файл профиля при невалидном манифесте\n' >&2; return 1; }
  [ ! -e "$HOME/.config/fish/conf.d/opencode-magnit.fish" ] || return 1
  assert_no_canaries || return 1
  return 0
}

# Перебор значений × шести режимов для одного поля манифеста.
# Аргументы: <поле фабрики: ca|cli|app> <генератор значений> <ожидаемое имя поля в сообщении>
run_meta_matrix() {
  local field_kind=$1 values=$2 field=$3
  local value mode before after
  before="$BATS_TEST_TMPDIR/home.before"
  after="$BATS_TEST_TMPDIR/home.after"
  while IFS= read -r value; do
    [ -n "$value" ] || continue
    # Пакет пересобирается фабрикой: значение поля передаётся параметром и экранируется в JSON.
    case $field_kind in
      ca) ( PKG_CA_INSTALL_NAME=$value; export PKG_CA_INSTALL_NAME; make_pkg ) ;;
      cli) ( PKG_CLI_INSTALL_NAME=$value; export PKG_CLI_INSTALL_NAME; make_pkg ) ;;
      app) ( PKG_DESKTOP=1; PKG_APP_NAME=$value; export PKG_DESKTOP PKG_APP_NAME; make_pkg ) ;;
      *) printf 'неизвестное поле фабрики: %s\n' "$field_kind" >&2; return 1 ;;
    esac
    snapshot_home "$before"
    local IFS='|'
    for mode in $OM_MODES; do
      unset IFS
      # shellcheck disable=SC2086
      # Обоснование: $mode — заведомо безопасный список флагов, расщепление по пробелу здесь
      # и есть способ передать «--dry-run --uninstall» двумя аргументами.
      if ! assert_field_rejected "$field" $mode; then
        printf 'Значение [%s], режим [%s]\n' "$value" "$mode" >&2
        return 1
      fi
      local IFS='|'
    done
    unset IFS
    snapshot_home "$after"
    if ! diff "$before" "$after" >&2; then
      printf 'Снимок $HOME изменился на значении [%s]\n' "$value" >&2
      return 1
    fi
  done <<VALUES
$($values)
VALUES
  return 0
}

# Прогон посимвольного перечня. Режим один — установка без флагов: именно в нём пропущенное
# значение дошло бы до файла профиля, поэтому «код 0 вместо 2» здесь наблюдаем в самой сильной
# форме (assert_field_rejected заодно требует, чтобы профиль не появился). Независимость проверки
# от режима — отдельное измерение AC-145, и её держат перечни форм атаки выше, где каждое
# значение проходит все шесть режимов; перемножать оба измерения смысла нет: белый список
# применяется один раз при разборе манифеста, до ветвления по режиму.
run_char_matrix() {
  local field_kind=$1 values=$2 field=$3
  local value before after
  before="$BATS_TEST_TMPDIR/char.before"
  after="$BATS_TEST_TMPDIR/char.after"
  while IFS= read -r value; do
    [ -n "$value" ] || continue
    case $field_kind in
      ca) ( PKG_CA_INSTALL_NAME=$value; export PKG_CA_INSTALL_NAME; make_pkg ) ;;
      cli) ( PKG_CLI_INSTALL_NAME=$value; export PKG_CLI_INSTALL_NAME; make_pkg ) ;;
      app) ( PKG_DESKTOP=1; PKG_APP_NAME=$value; export PKG_DESKTOP PKG_APP_NAME; make_pkg ) ;;
      *) printf 'неизвестное поле фабрики: %s\n' "$field_kind" >&2; return 1 ;;
    esac
    snapshot_home "$before"
    if ! assert_field_rejected "$field" --no-launch; then
      # Шестнадцатеричный дамп: часть значений отличается непечатным байтом, и без дампа
      # сообщение о падении было бы неотличимо от сообщения о соседнем значении.
      printf 'Значение [%s], байты: %s\n' "$value" "$(printf '%s' "$value" | od -An -tx1 | tr -d '\n')" >&2
      return 1
    fi
    snapshot_home "$after"
    if ! diff "$before" "$after" >&2; then
      printf 'Снимок $HOME изменился на значении [%s]\n' "$value" >&2
      return 1
    fi
  done <<VALUES
$($values)
VALUES
  return 0
}

@test "AC-145: базовое значение посимвольного перечня без запрещённых символов принимается" {
  # Якорь перечня OM_SINGLE_CHAR_*_VALUES: 'ab.pem', 'ab' и 'ab.app' состоят только из
  # разрешённых символов и принимаются. Значит каждое значение перечня отличается от принятого
  # ровно одним символом, и его код 2 объясняется этим символом, а не формой имени.
  PKG_DESKTOP=1 PKG_CA_INSTALL_NAME='ab.pem' PKG_CLI_INSTALL_NAME='ab' PKG_APP_NAME='ab.app' make_pkg
  export SHELL=/bin/zsh
  oc_run --no-launch --no-desktop
  assert_status 0
  refute_output_contains "недопустимый путь"
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS='$(config_dir_path)/ab.pem'"
  [ -x "$(bin_dir_path)/ab" ]
  assert_no_canaries
}

@test "AC-145: каждый запрещённый символ ПО ОТДЕЛЬНОСТИ в ca.install_name → код 2" {
  run_char_matrix ca OM_SINGLE_CHAR_PATH_VALUES "ca.install_name"
}

@test "AC-145: каждый запрещённый символ ПО ОТДЕЛЬНОСТИ в artifacts[].install_name → код 2" {
  run_char_matrix cli OM_SINGLE_CHAR_PATH_VALUES "artifacts.0.install_name"
}

@test "AC-145: каждый запрещённый символ ПО ОТДЕЛЬНОСТИ в artifacts[].app_name → код 2" {
  run_char_matrix app OM_SINGLE_CHAR_APP_VALUES "artifacts.1.app_name"
}

@test "AC-145: метасимвол в ca.install_name → код 2 во всех шести режимах, канареек нет" {
  run_meta_matrix ca OM_META_PATH_VALUES "ca.install_name"
}

@test "AC-145: метасимвол в artifacts[].install_name → код 2 во всех шести режимах, канареек нет" {
  run_meta_matrix cli OM_META_PATH_VALUES "artifacts.0.install_name"
}

@test "AC-145: метасимвол и краевой пробел в artifacts[].app_name → код 2 во всех шести режимах" {
  run_meta_matrix app OM_META_APP_VALUES "artifacts.1.app_name"
}

@test "AC-145: контрольные значения принимаются — установка доходит до кода 0/7" {
  # Без контроля весь перебор выше был бы ложно-зелёным: любой сломанный пакет давал бы код 2.
  # --no-desktop: фикстурный dmg — не образ, и шаг Desktop на macOS дал бы код 1 не по теме теста.
  PKG_DESKTOP=1 make_pkg
  export SHELL=/bin/zsh
  oc_run --no-launch --no-desktop
  assert_status 0
  refute_output_contains "поле artifacts.1.app_name"
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS='$(config_dir_path)/tander-ca-bundle.pem'"
  [ -x "$(bin_dir_path)/opencode" ]
  oc_run --check
  # Desktop в пакете есть, но не устанавливался (--no-desktop) — это расхождение, код 7.
  assert_status 7
  assert_output_contains "Манифест: корректен"
  assert_output_contains "Бинарник: установлен"
  assert_no_canaries
}

@test "AC-145: управляющий символ (табуляция) в app_name → код 2, а не молчаливая подмена пробелом" {
  # Минимальное воспроизведение BUG-I5-003. N5-P3/N5-P4 относят управляющие символы к
  # запрещённым наравне с метасимволами, AC-145 требует кода 2 на значении 'a\tb.pem'.
  PKG_DESKTOP=1 PKG_APP_NAME="$(printf 'a\tb.app')" make_pkg
  assert_file_contains "$PKG/common/manifest.json" '"app_name": "a\tb.app"'
  oc_run --dry-run
  assert_status 2
  assert_output_contains "поле artifacts.1.app_name"
  refute_output_contains "/Applications/a b.app"
}

@test "AC-145: управляющий символ в ca.install_name и artifacts[].install_name → код 2" {
  PKG_CA_INSTALL_NAME="$(printf 'a\tb.pem')" make_pkg
  oc_run --dry-run
  assert_status 2
  assert_output_contains "поле ca.install_name"
  PKG_CLI_INSTALL_NAME="$(printf 'a\tb')" make_pkg
  oc_run --dry-run
  assert_status 2
  assert_output_contains "поле artifacts.0.install_name"
}

# ------------------------------------------------------------------ AC-145: JSON-экранирование \uXXXX
#
# Набор ПРИНЯТЫХ манифестов обязан совпадать на обеих платформах (N5-P4): install.ps1 разбирает
# манифест через ConvertFrom-Json, который \uXXXX декодирует по кодовой точке. Значит и здесь
# запись "tander-…" — это строка "tander-…", а не отдельное значение. Проверяется и обратное:
# декодирование не отмывает запрещённый символ в разрешённый — вердикт по-прежнему выносит
# белый список, и сообщение называет поле.

@test "AC-145: \\uXXXX печатного ASCII декодируется — тот же манифест принимают обе платформы" {
  # "tander-ca-bundle.pem" — корректная по RFC 8259 запись строки "tander-ca-bundle.pem".
  manifest_edit 's|"install_name": "tander-ca-bundle.pem"|"install_name": "\\u0074ander-ca-bundle.pem"|'
  # В самом манифесте лежит именно escape-запись, а не готовая строка.
  assert_file_contains "$PKG/common/manifest.json" '"install_name": "\u0074ander-ca-bundle.pem"'
  export SHELL=/bin/zsh
  oc_run --no-launch
  assert_status 0
  refute_output_contains "поле ca.install_name"
  [ -f "$(config_dir_path)/tander-ca-bundle.pem" ] || {
    printf 'CA не установлен по декодированному имени\n' >&2
    return 1
  }
  assert_file_contains "$HOME/.zshrc" "export NODE_EXTRA_CA_CERTS='$(config_dir_path)/tander-ca-bundle.pem'"
  assert_no_canaries
}

@test "AC-145: \\u0020 в app_name — пробел ВНУТРИ имени, а не запрещённый символ" {
  # Паритет с ConvertFrom-Json на разрешённом символе: "OpenCode Magnit.app" — та же строка,
  # что и штатное имя бандла, и она обязана приниматься.
  #
  # Пакет собирается с PKG_PLATFORM=macos на ОБЕИХ площадках (как в AC-154 в 36-desktop-safety):
  # план установки бандла существует только в macOS-ветке (на Linux каталога /Applications нет и
  # шага с бандлом в плане нет вовсе), а --dry-run до hdiutil/ditto не доходит. Так проверка
  # декодирования \\u0020 в имени бандла выполняется и на Linux, без молчаливого пропуска.
  PKG_PLATFORM=macos PKG_DESKTOP=1 make_pkg
  # Имя берётся из $OM_APP_NAME, а не из литерала: подстановка sed по литералу имени после
  # переименования бандла перестала бы срабатывать МОЛЧА (N5-T1, AC-154).
  case $OM_APP_NAME in
    *' '*) ;;
    *) skip "в штатном имени бандла нет пробела — проверять декодирование \\u0020 не на чем" ;;
  esac
  # Четыре косые в исходнике — это две в значении переменной и одна в манифесте: подстановку
  # обрабатывает сначала bash, потом sed (у sed "\\u" в замене на GNU означало бы «сделать
  # следующий символ заглавным», поэтому косая обязана дойти до него экранированной).
  local escaped_name=${OM_APP_NAME// /\\\\u0020}
  manifest_edit "s|\"app_name\": \"$OM_APP_NAME\"|\"app_name\": \"$escaped_name\"|"
  refute_file_contains "$PKG/common/manifest.json" '"app_name": "'"$OM_APP_NAME"'"'
  oc_run --dry-run
  assert_status 0
  refute_output_contains "поле artifacts.1.app_name"
  assert_output_contains "/Applications/$OM_APP_NAME"
}

@test "AC-145: \\u0024 не отмывает \$ — код 2 с именем поля и декодированным значением" {
  manifest_edit 's|"install_name": "tander-ca-bundle.pem"|"install_name": "\\u0024pwn.pem"|'
  snapshot_home "$BATS_TEST_TMPDIR/home.before"
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле ca.install_name"
  assert_output_contains 'недопустимый путь: $pwn.pem'
  snapshot_home "$BATS_TEST_TMPDIR/home.after"
  diff "$BATS_TEST_TMPDIR/home.before" "$BATS_TEST_TMPDIR/home.after" >&2
  assert_no_canaries
}

@test "AC-145: \\uXXXX вне 0x20-0x7e и некорректная запись escape → код 2" {
  # Управляющая кодовая точка, 0x7f, не-ASCII и запись без четырёх шестнадцатеричных цифр
  # остаются маркером ctl — байтом, запрещённым во всех путевых полях.
  local esc
  for esc in '\\u0009' '\\u0000' '\\u007f' '\\u043a' '\\u00e9' '\\uZZZZ'; do
    make_pkg
    manifest_edit "s|\"install_name\": \"tander-ca-bundle.pem\"|\"install_name\": \"a${esc}b.pem\"|"
    # В манифесте обязана лежать ОДНА обратная косая: '\\u0009' — это уже не escape-запись,
    # а буквальная косая с текстом, и код 2 объяснялся бы ею, а не разбором \uXXXX.
    refute_file_contains "$PKG/common/manifest.json" '\\u'
    oc_run --no-launch
    assert_status 2 || { printf 'Значение [a%sb.pem]\n' "$esc" >&2; return 1; }
    assert_output_contains "поле ca.install_name" || { printf 'Значение [a%sb.pem]\n' "$esc" >&2; return 1; }
  done
  assert_no_canaries
}

@test "AC-145: пробел ВНУТРИ app_name допустим — «OpenCode Magnit.app» принимается (N5-P3, N5-P4)" {
  # Ровно то различие полей, из-за которого перечни значений AC-145 разведены: для путей внутри
  # пакета пробел запрещён, для имени приложения — разрешён, кроме краевого.
  PKG_DESKTOP=1 PKG_APP_NAME="$OM_APP_NAME" make_pkg
  oc_run --dry-run
  assert_status 0
  refute_output_contains "поле artifacts.1.app_name"
  PKG_DESKTOP=1 PKG_CA_INSTALL_NAME="tander ca.pem" make_pkg
  oc_run --dry-run
  assert_status 2
  assert_output_contains "поле ca.install_name"
}

# ------------------------------------------------------------------ AC-146: POSIX-профиль
#
# Каталоги установки метасимволы содержать МОГУТ: белый список N5-P3 распространяется на поля
# манифеста, а не на $HOME и --prefix пользователя. Значит, эти пути обязаны быть экранированы
# при печати в профиль, и профиль обязан оставаться исполнимым.

OM_DIR_NAMES() {
  cat <<'NAMES'
pre$(touch CANARY1)fix
pre`touch CANARY2`fix
pre";touch CANARY3;x="fix
pre'quote
pre fix
pre&fix
NAMES
}

# Исполняет файл профиля указанной оболочкой в пустом рабочем каталоге и печатает две строки:
# NECC=<значение NODE_EXTRA_CA_CERTS> и PATHIS=<значение PATH>.
# Внешних команд блок профиля не требует: echo и «.» — встроенные во всех POSIX-оболочках.
source_profile_with() {
  local sh=$1 prof=$2 work=$3 prev=$4
  # Путь профиля передаётся ПЕРЕМЕННОЙ ОКРУЖЕНИЯ, а не подставляется в текст -c: иначе метасимволы
  # имени каталога раскрыла бы сама оболочка теста, и проверять было бы нечего.
  ( cd "$work" && env -i HOME="$HOME" PATH="$prev" OM_PROF="$prof" "$sh" -c \
    '. "$OM_PROF"; echo "NECC=$NODE_EXTRA_CA_CERTS"; echo "PATHIS=$PATH"' )
}

@test "AC-146: метасимволы в путях $HOME и --prefix — установка кодом 0, профиль исполняется без побочных эффектов" {
  local name shell_path prof_name profile home_dir prefix work prev out necc pathis ca_actual
  prev="/om/prev/bin"
  for shell_path in /bin/zsh /bin/bash /bin/sh; do
    [ -x "$shell_path" ] || continue
    case $shell_path in
      */zsh) prof_name=.zshrc ;;
      */bash) if [ "$(host_platform)" = "macos" ]; then prof_name=.bash_profile; else prof_name=.bashrc; fi ;;
      *) prof_name=.profile ;;
    esac
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      home_dir="$SANDBOX/h/$name"
      prefix="$SANDBOX/p/$name/opt"
      work="$SANDBOX/w/$name-$(basename "$shell_path")"
      mkdir -p "$home_dir" "$work"
      HOME=$home_dir
      export HOME
      export SHELL=$shell_path
      run bash "$PKG/install.sh" --prefix "$prefix" --no-launch
      assert_status 0 || { printf 'Каталог [%s], оболочка [%s]\n' "$name" "$shell_path" >&2; return 1; }
      profile="$home_dir/$prof_name"
      [ -f "$profile" ] || { printf 'Не создан профиль %s\n' "$profile" >&2; return 1; }
      # Значение записано в ОДИНАРНЫХ кавычках; двойные остались только вокруг "$PATH".
      ca_actual="$home_dir/.config/opencode/tander-ca-bundle.pem"
      [ -f "$ca_actual" ] || { printf 'CA не установлен: %s\n' "$ca_actual" >&2; return 1; }
      # Значение открыто одинарной кавычкой и двойными не обёрнуто; точная форма экранирования
      # апострофа ('\'') проверяется отдельным тестом ниже — здесь достаточно того, что профиль
      # исполняется и возвращает путь побайтово (проверка ниже).
      grep -q "^export NODE_EXTRA_CA_CERTS='" "$profile" || {
        printf 'Строка NODE_EXTRA_CA_CERTS не в одинарных кавычках:\n%s\n' "$(cat "$profile")" >&2
        return 1
      }
      refute_file_contains "$profile" 'NODE_EXTRA_CA_CERTS="' || return 1
      assert_file_contains "$profile" ':"$PATH"'
      # Исполнение профиля: канареек нет, значения вернулись побайтово.
      out=$(source_profile_with "$shell_path" "$profile" "$work" "$prev")
      necc=$(printf '%s\n' "$out" | sed -n 's/^NECC=//p')
      pathis=$(printf '%s\n' "$out" | sed -n 's/^PATHIS=//p')
      [ "$necc" = "$ca_actual" ] || {
        printf 'NODE_EXTRA_CA_CERTS отличается от фактического пути CA:\n[%s]\n[%s]\n' "$necc" "$ca_actual" >&2
        return 1
      }
      [ "$pathis" = "$prefix/bin:$prev" ] || {
        printf 'PATH после профиля: [%s], ожидался [%s]\n' "$pathis" "$prefix/bin:$prev" >&2
        return 1
      }
      [ -z "$(ls -A "$work")" ] || {
        printf 'Каталог исполнения не пуст — сработала подстановка:\n%s\n' "$(ls -A "$work")" >&2
        return 1
      }
      assert_no_canaries || { printf 'Каталог [%s]\n' "$name" >&2; return 1; }
      # Повторный запуск файл не меняет.
      local before_sum
      before_sum=$(sha256_file "$profile")
      run bash "$PKG/install.sh" --prefix "$prefix" --no-launch
      assert_status 0
      [ "$(sha256_file "$profile")" = "$before_sum" ] || {
        printf 'Повторный запуск изменил профиль %s\n' "$profile" >&2
        return 1
      }
      [ "$(count_lines_with "$profile" "# >>> opencode-magnit >>>")" = "1" ]
    done <<NAMES
$(OM_DIR_NAMES)
NAMES
  done
}

@test "AC-146: апостроф в пути закрыт по правилу POSIX ('\\'') — профиль синтаксически корректен" {
  local home_dir="$SANDBOX/h/it's here" prefix="$SANDBOX/p/it's here/opt"
  mkdir -p "$home_dir"
  HOME=$home_dir
  export HOME SHELL=/bin/sh
  run bash "$PKG/install.sh" --prefix "$prefix" --no-launch
  assert_status 0
  local profile="$home_dir/.profile" ca_actual="$home_dir/.config/opencode/tander-ca-bundle.pem"
  # Ровно та форма, которую предписывает N5-I13: закрыть кавычку, экранированный апостроф, открыть.
  assert_file_contains "$profile" "it'\\''s here"
  refute_file_contains "$profile" "export NODE_EXTRA_CA_CERTS=\""
  run sh -n "$profile"
  assert_status 0
  local work="$SANDBOX/w/apostrophe"
  mkdir -p "$work"
  local out necc
  out=$(source_profile_with /bin/sh "$profile" "$work" /om/prev/bin)
  necc=$(printf '%s\n' "$out" | sed -n 's/^NECC=//p')
  [ "$necc" = "$ca_actual" ] || { printf 'Значение [%s] != [%s]\n' "$necc" "$ca_actual" >&2; return 1; }
  [ -z "$(ls -A "$work")" ]
}

# ------------------------------------------------------------------ AC-147: профиль fish
#
# В одинарных кавычках fish обрабатывает ровно два символа — обратный слэш и апостроф;
# $, обратная кавычка, ';' и скобки специального смысла там не имеют и экранироваться не должны.

# Таблица N5-I13 для fish: имя каталога → его форма ВНУТРИ одинарных кавычек профиля.
# Правая колонка выписана ВРУЧНУЮ по правилу fish (внутри '…' обрабатываются ровно два символа:
# обратный слэш и апостроф; '$', обратная кавычка, ';', '&', '"' и скобки специального смысла
# там не имеют). Вычислять её выражением sed нельзя: такое выражение — вторая реализация того же
# алгоритма, что и fish_quote_value, и ошибка, внесённая в алгоритм, воспроизвелась бы в
# ожидании, оставив тест зелёным. Разделитель колонок — '|', ни в одном значении его нет.
OM_FISH_TABLE() {
  printf '%s\n' 'pre$(touch CANARY1)fix|pre$(touch CANARY1)fix'
  printf '%s\n' 'pre`touch CANARY2`fix|pre`touch CANARY2`fix'
  printf '%s\n' 'pre";touch CANARY3;x="fix|pre";touch CANARY3;x="fix'
  printf '%s\n' 'pre fix|pre fix'
  printf '%s\n' 'pre&fix|pre&fix'
  # Апостроф: закрыть кавычку fish нельзя, поэтому перед ним ставится обратный слэш.
  printf '%s\n' "pre'quote|pre\\'quote"
  # Один обратный слэш → два; два → четыре.
  printf '%s\n' 'pre\back|pre\\back'
  printf '%s\n' 'pre\\two|pre\\\\two'
  # Порядок замен: апостроф экранируется ПОСЛЕ удвоения слэшей, иначе добавленный слэш был бы
  # удвоен второй заменой и значение вернулось бы из профиля другим.
  printf '%s\n' "mix'\\end|mix\\'\\\\end"
}

@test "AC-147: fish-профиль — экранированы ровно \\\\ и ', прочие метасимволы оставлены как есть" {
  local row name want home_dir prefix f
  # Ожидание — константа, склеенная с путём песочницы; если бы сам путь песочницы содержал
  # обратный слэш или апостроф, его тоже пришлось бы экранировать, и константа перестала бы
  # быть константой. Такой машины в проекте нет, но предусловие проверяется явно.
  case $SANDBOX in
    *\\*|*\'*) skip "путь песочницы содержит \\ или ' — ожидание нельзя выписать константой" ;;
  esac
  export SHELL=/usr/local/bin/fish
  while IFS= read -r row; do
    [ -n "$row" ] || continue
    name=${row%%|*}
    want=${row#*|}
    home_dir="$SANDBOX/fh/$name"
    prefix="$SANDBOX/fp/$name/opt"
    mkdir -p "$home_dir"
    HOME=$home_dir
    export HOME
    run bash "$PKG/install.sh" --prefix "$prefix" --no-launch
    assert_status 0 || { printf 'Каталог [%s]\n' "$name" >&2; return 1; }
    f="$home_dir/.config/fish/conf.d/opencode-magnit.fish"
    [ -f "$f" ] || { printf 'Не создан %s\n' "$f" >&2; return 1; }
    # CA лежит по НЕэкранированному пути — иначе сверять было бы не с чем.
    [ -f "$home_dir/.config/opencode/tander-ca-bundle.pem" ] || {
      printf 'CA не установлен в [%s]\n' "$home_dir" >&2
      return 1
    }
    assert_file_contains "$f" \
      "set -gx NODE_EXTRA_CA_CERTS '$SANDBOX/fh/$want/.config/opencode/tander-ca-bundle.pem'" || {
      printf 'Каталог [%s], ожидалась форма [%s]\n%s\n' "$name" "$want" "$(cat "$f")" >&2
      return 1
    }
    assert_file_contains "$f" "fish_add_path '$SANDBOX/fp/$want/opt/bin'" || {
      printf 'Каталог [%s], ожидалась форма [%s]\n%s\n' "$name" "$want" "$(cat "$f")" >&2
      return 1
    }
    # Двойных кавычек ВОКРУГ значения нет (сам путь двойную кавычку содержать может);
    # $ и обратная кавычка внутри одинарных кавычек fish НЕ экранируются.
    refute_file_contains "$f" 'NODE_EXTRA_CA_CERTS "'
    refute_file_contains "$f" 'fish_add_path "'
    refute_file_contains "$f" '\$'
    refute_file_contains "$f" '\`'
    [ ! -e "$home_dir/.zshrc" ]
    assert_no_canaries
  done <<TABLE
$(OM_FISH_TABLE)
TABLE
}

@test "AC-147: fish исполняет сгенерированный профиль — значение и bin_dir не расщеплены" {
  if ! command -v fish >/dev/null 2>&1; then
    skip "fish в системе отсутствует: побайтовая проверка строк выполнена отдельным тестом AC-147, исполнение профиля пропущено"
  fi
  local home_dir="$SANDBOX/fexec/pre fix" prefix="$SANDBOX/fexecp/pre fix/opt"
  mkdir -p "$home_dir"
  HOME=$home_dir
  export HOME SHELL=$(command -v fish)
  run bash "$PKG/install.sh" --prefix "$prefix" --no-launch
  assert_status 0
  local f="$home_dir/.config/fish/conf.d/opencode-magnit.fish"
  local work="$SANDBOX/fwork"
  mkdir -p "$work"
  run bash -c "cd '$work' && env -i HOME='$home_dir' PATH=/om/prev/bin '$SHELL' -c 'source \"$f\"; echo NECC=\$NODE_EXTRA_CA_CERTS; echo COUNT=(count \$PATH)'"
  assert_status 0
  assert_output_contains "NECC=$home_dir/.config/opencode/tander-ca-bundle.pem"
  [ -z "$(ls -A "$work")" ]
  assert_no_canaries
}
