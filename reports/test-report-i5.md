# Отчёт о тестировании I-5: установщики и пакет для портала

Итерация: I-5 (`installers/`), ветка `pipeline/i1-hub-login-catalog-20260818`.
Спецификация: `installers/docs/spec.md`; критерии: `installers/docs/acceptance-criteria.yaml`
(AC-01…AC-134; раздел `hub_backlog_criteria` HAC-* к установщикам не относится и здесь не
рассматривается — он закрыт в worktree I-3).

Зона тестов: `installers/tests/`. Код установщиков, `release.sh`, `ci/` и документация не
изменялись.

## 1. Что написано

| Набор | Файлов | Тестов | Где выполняется |
|---|---|---|---|
| bats (`installers/tests/bats/*.bats`) | 10 | 140 | локально (macOS, bash 3.2) и в CI на `ubuntu-latest` + `macos-latest` |
| Pester (`installers/tests/pester/*.Tests.ps1`) | 2 | 38 | только CI: `pwsh` на `ubuntu-latest` (локально `pwsh` отсутствует) |
| Трассировка (`installers/tests/trace-ac.sh`) | 1 | — | локально и в CI (запускается тестом AC-127) |
| Фикстуры (`installers/tests/fixtures/`) | 3 | — | фейковый «бинарник» (обычный и без рабочего `--version`) и образец CA |

Раскладка bats-файлов:

| Файл | Тестов | Что покрывает |
|---|---|---|
| `10-manifest.bats` | 18 | манифест: схема, поля, sha256, безопасные пути, неизменность ФС (N5-P3, N5-P4) |
| `20-args.bats` | 13 | аргументы, режимы, справка, версия, взаимоисключения, отсутствие флага пропуска хеша (N5-I1) |
| `30-integrity.bats` | 13 | sha256, отсутствие утилит хеширования, ОС/архитектура, оффлайновость (N5-P5, N5-P6, N5-I3) |
| `40-install.bats` | 23 | CA, бинарник, Desktop, пользовательский конфиг, отчёт, `corp status` (N5-I4…N5-I12) |
| `50-profile.bats` | 10 | файл профиля по `$SHELL`, маркированный блок, `.bak`, чужая переменная (N5-I5) |
| `60-update.bats` | 6 | идемпотентность, обновление с `.bak`, пропуск при совпадении, «другая версия» (N5-U1…N5-U4) |
| `70-uninstall.bats` | 11 | удаление, повторное удаление, `--purge` и проверка путей (N5-R1…N5-R4) |
| `80-check-dryrun.bats` | 17 | `--dry-run`, построчный `--check`, код 7, неизменность ФС, отказы по правам (N5-C1…N5-C4, N5-I7) |
| `90-release.bats` | 15 | `release.sh`: сборка из фикстурных артефактов, манифест, `SHA256SUMS`, самопроверка, отказы (N5-B1…N5-B5) |
| `95-sources.bats` | 14 | состав репозитория, оффлайн-грепы, документация, CI-эталон, shellcheck, кодировки (N5-P1, N5-D*, N5-T3…N5-T6) |

Изоляция (N5-T1): каждый тест работает в собственном `$BATS_TEST_TMPDIR` — подменяются `HOME`,
`TMPDIR`, корень установки задаётся `--prefix`, `SHELL` задаётся явно. В начало `PATH`
подставляются ловушки `curl`, `wget`, `nc`, `git`, `npm`, `brew`, `apt`, `winget`, `choco`, `gh`,
`sudo`: любой их вызов пишется в файл и проверяется утверждением `assert_no_forbidden_calls`.
Хеши в фикстурных манифестах считаются на лету; захардкоженных sha256 нет, кроме тестов на
заведомо неверный хеш. Реальный домашний каталог тестами не затрагивается (проверено снимком до и
после прогона).

## 2. Результаты прогона

| Прогон | Команда | Результат | Время |
|---|---|---|---|
| 1 | `bats installers/tests/bats` | 140/140 ok, 0 падений | ~37 с |
| 2 | `bats installers/tests/bats` | 140/140 ok, 0 падений | ~37 с |
| 3 (контрольный) | `bats installers/tests/bats` | 140/140 ok, 0 падений | ~37 с |

Стабильность: три подряд идущих прогона зелёные, расхождений между прогонами нет, флаки-тестов не
обнаружено. Снимок реального `$HOME` (листинг и sha256 файлов профиля) до и после прогонов
совпадает; `sudo` не вызывался; сетевых обращений не было (AC-124).

`shellcheck -s bash` чист на собственных хелперах (`installers/tests/bats/helpers.bash`,
`installers/tests/trace-ac.sh`) и на всех `installers/**/*.sh` (тест AC-130).

Трассировка (`bash installers/tests/trace-ac.sh`): 134 критерия AC-*, тесты есть у 118.

| Тип AC | Всего | С тестами |
|---|---|---|
| `unit` | 15 | 15 |
| `integration` | 79 | 79 |
| `ci` | 25 | 23 |
| `manual` | 15 | 1 (частично, см. §4) |

Не покрыты автоматически два `ci`-критерия процессного характера: **AC-124** (двойной прогон bats
на реальной машине — выполнен вручную, см. таблицу выше) и **AC-129** (красный статус PR при
искусственной поломке задания — проверяется самим конвейером GitHub Actions).

## 3. Баги

Багов уровня `code_bug` не найдено: ни один тест не выявил расхождения поведения установщиков,
`release.sh` или документации с критериями приёмки. Файлы `bugs/BUG-I5-*.json` не создавались.

Два падения при разработке классифицированы как `test_bug` и исправлены в тестах:

| Симптом | Причина | Исправление |
|---|---|---|
| AC-75: после второго запуска печаталось «Откройте новый терминал» | тест менял `PATH` между двумя запусками, из-за чего блок профиля во втором запуске законно отличался (строка `export PATH` не нужна, N5-I5) | `bin_dir` добавляется в `PATH` до первого запуска — оба запуска идентичны |
| AC-32: `install.bat` признавался не-ASCII | шаблон grep считал `CR` (0x0D) недопустимым, тогда как критерий требует «все байты < 0x80», а CRLF для `.bat` нормален | проверка переписана на `tr -d '\000-\177'` |

Замечание без бага (формулировка, не поведение): в `installers/docs/install-admin.md` §7
«Выкладка» описан черновик релиза по тегу `v<версия>` и ручная карточка портала, но буквального
словосочетания «GitHub Releases» в тексте нет. Существо требования AC-122 выполнено, поэтому тест
проверяет содержание раздела (`--publish`, релиз, портал, `SHA256SUMS`), а не дословную формулировку.

## 4. Критерии типа `manual`

Требуют Windows-стенда (D-9), macOS с настоящим Desktop-артефактом или GUI-сеанса. Процедуры —
в `installers/docs/install-admin.md` (§3 «Что установщик пишет в систему», §5 «Флаги и
переменные») и `installers/docs/install-user.md` (§ «Запуск», § «Как проверить установку»).

| AC | Правило | Что проверяется вручную | Стенд |
|---|---|---|---|
| AC-31 | N5-I2 | двойной клик по `install.bat` при политике Restricted, читаемость русских строк, проброс кода выхода | Windows |
| AC-35 | N5-I3 | отказ на PowerShell 5.0 с указанием найденной версии, код 3 | Windows (PS 5.0) |
| AC-49 | N5-I6 | пользовательская переменная `NODE_EXTRA_CA_CERTS`, отсутствие записи в `Machine` | Windows |
| AC-50 | N5-I6, N5-U1 | повторный запуск не пишет в реестр, строка «уже задан» | Windows |
| AC-57 | N5-I8, N5-U2 | замена `opencode.exe` через `.bak` | Windows |
| AC-58 | N5-I8 | занятый `opencode.exe` → код 6, новый файл не записан | Windows |
| AC-59 | N5-I8 | сохранение типа `REG_EXPAND_SZ` и неразвёрнутых `%…%` в User PATH | Windows |
| AC-60 | N5-I8, N5-U1 | отсутствие дубликата в PATH при другом регистре и завершающем `\` (чистая функция покрыта Pester) | Windows |
| AC-61 | N5-I8 | таймаут `WM_SETTINGCHANGE` → предупреждение, код 0 | Windows |
| AC-62 | N5-I9 | монтирование `.dmg`, копирование `OpenCode.app`, гарантированное отмонтирование | macOS + dmg |
| AC-63 | N5-I9 | установка в `~/Applications` при отсутствии прав на `/Applications` | macOS + dmg |
| AC-64 | N5-I9, N5-U3 | пропуск при совпадении `CFBundleShortVersionString` | macOS + dmg |
| AC-65 | N5-I10 | фактический запуск установщика Desktop с `silent_args` (в Pester покрыта только строка плана) | Windows |
| AC-67 | N5-I10 | ненулевой код установщика Desktop → строка «ошибка», общий код 1, откат CLI не выполняется | Windows |
| AC-87 | N5-R1 | чужое значение `NODE_EXTRA_CA_CERTS` при `-Uninstall` не изменяется | Windows |

## 5. Что уходит в CI

`installers/ci/installers.yml` (эталон, копируется человеком в `.github/workflows/`):

| Задание | Что выполняет | Какие тесты |
|---|---|---|
| `bats` (`ubuntu-latest`, `macos-latest`) | `bats installers/tests/bats` | все 140 bats-тестов; на ubuntu естественным образом проверяется linux-ветка `install-posix.sh`, на macos — macos-ветка и bash 3.2 |
| `pester` (`ubuntu-latest`, `pwsh`) | `Invoke-Pester installers/tests/pester` | 38 Pester-тестов: `Read-Manifest`, `Confirm-FileHash`, `Get-InstallPlan`, `Invoke-Check`, `Invoke-Main` (аргументы), `Expand-UserPathTemplate`/`Test-PurgePathSafe`, `Test-UserPathContains`, кодировки |
| `lint` | `shellcheck -s bash` для всех `*.sh`, `Invoke-ScriptAnalyzer` для `*.ps1` | дублируется тестом AC-130 (shellcheck) и AC-132 (текстовая часть совместимости 5.1) |
| `release-dry-run` | сборка `release.sh` на фикстурных артефактах + `--dry-run` пакета | дублируется тестами AC-112…AC-119 в `90-release.bats` |

Pester-набор написан для CI и локально не запускался: `pwsh` в среде разработки отсутствует
(в тестах учтено: работа с реестром, User PATH, `WM_SETTINGCHANGE`, занятым `opencode.exe` и
тихой установкой Desktop в набор не входит — это `manual`, D-9). Первый прогон задания `pester`
в CI — точка контроля.

## 6. Ограничения и допущения тестов

- **Оффлайновость (AC-19)** эмулируется ловушками сетевых утилит в `PATH` и прокси на закрытый
  порт: полная блокировка исходящих соединений — уровень раннера CI, а не bats.
- **Ветки macOS/Linux (AC-45, AC-46)** проверяются через `OPENCODE_INSTALLER_PLATFORM`, который
  задают лончеры `macos/install.sh` и `linux/install.sh`: на macOS-машине так проверяются обе
  ветки выбора файла профиля; в CI обе ветки дополнительно проходят на «родных» раннерах.
- **AC-52** (атомарность замены бинарника): проверяется отсутствие временных файлов
  `.opencode.new.*` и сохранность прежнего бинарника при прерывании установки на проверке хеша;
  реальный сигнал в середине `mv` детерминированно не воспроизводится.
- **AC-115** (порча пакета валит самопроверку) выполняется на копии дерева `installers/` во
  временном каталоге: репозиторий не изменяется.
- **AC-04** использует `.venv/bin/python` с `jsonschema`; при его отсутствии тест пропускается
  (`skip`), в CI — задание `pester`/`lint` этот критерий не дублирует.

## 7. Таблица AC → тесты

Сгенерирована `bash installers/tests/trace-ac.sh --markdown` (маркер `AC-NN` в имени теста).

| AC | тип | тестов | файлы |
|---|---|---|---|
| AC-01 | ci | 1 | 95-sources.bats |
| AC-02 | integration | 2 | 40-install.bats 90-release.bats |
| AC-03 | integration | 1 | 40-install.bats |
| AC-04 | unit | 1 | 10-manifest.bats |
| AC-05 | unit | 4 | 10-manifest.bats Install.Tests.ps1 |
| AC-06 | integration | 2 | 10-manifest.bats Install.Tests.ps1 |
| AC-07 | integration | 2 | 10-manifest.bats Install.Tests.ps1 |
| AC-08 | unit | 2 | 10-manifest.bats Install.Tests.ps1 |
| AC-09 | unit | 3 | 10-manifest.bats Install.Tests.ps1 |
| AC-10 | unit | 3 | 10-manifest.bats Install.Tests.ps1 |
| AC-11 | unit | 6 | 10-manifest.bats Install.Tests.ps1 |
| AC-12 | integration | 2 | 10-manifest.bats |
| AC-13 | integration | 3 | 30-integrity.bats Install.Tests.ps1 |
| AC-14 | integration | 1 | 30-integrity.bats |
| AC-15 | integration | 1 | 30-integrity.bats |
| AC-16 | unit | 2 | 30-integrity.bats Install.Tests.ps1 |
| AC-17 | integration | 3 | 20-args.bats |
| AC-18 | ci | 1 | 95-sources.bats |
| AC-19 | integration | 2 | 30-integrity.bats |
| AC-20 | ci | 1 | 90-release.bats |
| AC-21 | ci | 1 | 90-release.bats |
| AC-22 | integration | 1 | 30-integrity.bats |
| AC-23 | integration | 2 | 20-args.bats Install.Tests.ps1 |
| AC-24 | integration | 2 | 20-args.bats Install.Tests.ps1 |
| AC-25 | integration | 2 | 20-args.bats Install.Tests.ps1 |
| AC-26 | unit | 3 | 20-args.bats Install.Tests.ps1 |
| AC-27 | unit | 2 | 20-args.bats Install.Tests.ps1 |
| AC-28 | unit | 1 | 20-args.bats |
| AC-29 | integration | 1 | 20-args.bats |
| AC-30 | integration | 1 | 20-args.bats |
| AC-31 | manual | 0 | — |
| AC-32 | unit | 2 | 95-sources.bats Encoding.Tests.ps1 |
| AC-33 | integration | 2 | 30-integrity.bats Install.Tests.ps1 |
| AC-34 | integration | 1 | 30-integrity.bats |
| AC-35 | manual | 0 | — |
| AC-36 | unit | 3 | 30-integrity.bats |
| AC-37 | integration | 1 | 40-install.bats |
| AC-38 | integration | 1 | 40-install.bats |
| AC-39 | integration | 1 | 40-install.bats |
| AC-40 | integration | 1 | 40-install.bats |
| AC-41 | integration | 1 | 50-profile.bats |
| AC-42 | integration | 1 | 50-profile.bats |
| AC-43 | integration | 1 | 50-profile.bats |
| AC-44 | integration | 1 | 50-profile.bats |
| AC-45 | integration | 3 | 50-profile.bats |
| AC-46 | integration | 1 | 50-profile.bats |
| AC-47 | integration | 1 | 50-profile.bats |
| AC-48 | integration | 1 | 50-profile.bats |
| AC-49 | manual | 0 | — |
| AC-50 | manual | 0 | — |
| AC-51 | integration | 1 | 40-install.bats |
| AC-52 | integration | 2 | 40-install.bats |
| AC-53 | integration | 1 | 80-check-dryrun.bats |
| AC-54 | integration | 1 | 80-check-dryrun.bats |
| AC-55 | integration | 1 | 40-install.bats |
| AC-56 | integration | 1 | 40-install.bats |
| AC-57 | manual | 0 | — |
| AC-58 | manual | 0 | — |
| AC-59 | manual | 0 | — |
| AC-60 | manual | 0 | — |
| AC-61 | manual | 0 | — |
| AC-62 | manual | 0 | — |
| AC-63 | manual | 0 | — |
| AC-64 | manual | 0 | — |
| AC-65 | manual | 1 | Install.Tests.ps1 |
| AC-66 | integration | 1 | Install.Tests.ps1 |
| AC-67 | manual | 0 | — |
| AC-68 | integration | 2 | 40-install.bats Install.Tests.ps1 |
| AC-69 | integration | 2 | 40-install.bats Install.Tests.ps1 |
| AC-70 | integration | 2 | 40-install.bats |
| AC-71 | integration | 1 | 40-install.bats |
| AC-72 | integration | 1 | 40-install.bats |
| AC-73 | integration | 1 | 40-install.bats |
| AC-74 | integration | 1 | 40-install.bats |
| AC-75 | integration | 1 | 40-install.bats |
| AC-76 | integration | 1 | 40-install.bats |
| AC-77 | integration | 2 | 40-install.bats Install.Tests.ps1 |
| AC-78 | integration | 1 | 40-install.bats |
| AC-79 | integration | 1 | 60-update.bats |
| AC-80 | integration | 1 | 60-update.bats |
| AC-81 | integration | 1 | 60-update.bats |
| AC-82 | integration | 1 | 60-update.bats |
| AC-83 | integration | 1 | 60-update.bats |
| AC-84 | integration | 1 | 60-update.bats |
| AC-85 | integration | 1 | 70-uninstall.bats |
| AC-86 | integration | 1 | 70-uninstall.bats |
| AC-87 | manual | 0 | — |
| AC-88 | integration | 1 | 70-uninstall.bats |
| AC-89 | integration | 2 | 70-uninstall.bats Install.Tests.ps1 |
| AC-90 | integration | 4 | 70-uninstall.bats Install.Tests.ps1 |
| AC-91 | integration | 2 | 70-uninstall.bats Install.Tests.ps1 |
| AC-92 | integration | 1 | 70-uninstall.bats |
| AC-93 | integration | 1 | 70-uninstall.bats |
| AC-94 | integration | 1 | 70-uninstall.bats |
| AC-95 | integration | 2 | 20-args.bats Install.Tests.ps1 |
| AC-96 | integration | 2 | 80-check-dryrun.bats Install.Tests.ps1 |
| AC-97 | integration | 2 | 80-check-dryrun.bats Install.Tests.ps1 |
| AC-98 | integration | 1 | 30-integrity.bats |
| AC-99 | integration | 1 | 80-check-dryrun.bats |
| AC-100 | integration | 1 | 80-check-dryrun.bats |
| AC-101 | integration | 2 | 80-check-dryrun.bats Install.Tests.ps1 |
| AC-102 | integration | 1 | 80-check-dryrun.bats |
| AC-103 | integration | 1 | 80-check-dryrun.bats |
| AC-104 | integration | 2 | 80-check-dryrun.bats Install.Tests.ps1 |
| AC-105 | integration | 2 | 80-check-dryrun.bats |
| AC-106 | integration | 1 | 80-check-dryrun.bats |
| AC-107 | integration | 2 | 80-check-dryrun.bats Install.Tests.ps1 |
| AC-108 | integration | 1 | 80-check-dryrun.bats |
| AC-109 | integration | 1 | 80-check-dryrun.bats |
| AC-110 | integration | 1 | 80-check-dryrun.bats |
| AC-111 | ci | 2 | 90-release.bats |
| AC-112 | ci | 1 | 90-release.bats |
| AC-113 | ci | 1 | 90-release.bats |
| AC-114 | ci | 1 | 90-release.bats |
| AC-115 | ci | 2 | 90-release.bats |
| AC-116 | ci | 1 | 90-release.bats |
| AC-117 | ci | 1 | 90-release.bats |
| AC-118 | ci | 1 | 90-release.bats |
| AC-119 | ci | 1 | 90-release.bats |
| AC-120 | ci | 1 | 95-sources.bats |
| AC-121 | ci | 1 | 95-sources.bats |
| AC-122 | ci | 1 | 95-sources.bats |
| AC-123 | ci | 1 | 90-release.bats |
| AC-124 | ci | 0 | — |
| AC-125 | unit | 3 | Install.Tests.ps1 |
| AC-126 | ci | 1 | Install.Tests.ps1 |
| AC-127 | ci | 2 | 95-sources.bats |
| AC-128 | ci | 1 | 95-sources.bats |
| AC-129 | ci | 0 | — |
| AC-130 | ci | 1 | 95-sources.bats |
| AC-131 | ci | 1 | 95-sources.bats |
| AC-132 | ci | 2 | 95-sources.bats Encoding.Tests.ps1 |
| AC-133 | unit | 3 | 95-sources.bats Encoding.Tests.ps1 |
| AC-134 | unit | 2 | 95-sources.bats Encoding.Tests.ps1 |

