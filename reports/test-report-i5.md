# Отчёт о тестировании I-5: установщики и пакет для портала

Итерация: I-5 (`installers/`), ветка `pipeline/i1-hub-login-catalog-20260818`.
Спецификация: `installers/docs/spec.md`; критерии: `installers/docs/acceptance-criteria.yaml`
(AC-01…AC-137, ревизия 1.1; раздел `hub_backlog_criteria` HAC-* к установщикам не относится и здесь не
рассматривается — он закрыт в worktree I-3).

Зона тестов: `installers/tests/`. Код установщиков, `release.sh`, `ci/` и документация не
изменялись.

## 1. Что написано

| Набор | Файлов | Тестов | Где выполняется |
|---|---|---|---|
| bats (`installers/tests/bats/*.bats`) | 11 | 153 | локально (macOS, bash 3.2) и в CI на `ubuntu-latest` + `macos-latest` |
| Pester (`installers/tests/pester/*.Tests.ps1`) | 3 | 56 | только CI: `pwsh` на `ubuntu-latest` (локально `pwsh` отсутствует) |
| Трассировка (`installers/tests/trace-ac.sh`) | 1 | — | локально и в CI (запускается тестом AC-127) |
| Фикстуры (`installers/tests/fixtures/`) | 3 | — | фейковый «бинарник» (обычный и без рабочего `--version`) и образец CA |

Раскладка bats-файлов:

| Файл | Тестов | Что покрывает |
|---|---|---|
| `10-manifest.bats` | 18 | манифест: схема, поля, sha256, безопасные пути, неизменность ФС (N5-P3, N5-P4) |
| `20-args.bats` | 13 | аргументы, режимы, справка, версия, взаимоисключения, отсутствие флага пропуска хеша (N5-I1) |
| `30-integrity.bats` | 13 | sha256, отсутствие утилит хеширования, ОС/архитектура, оффлайновость (N5-P5, N5-P6, N5-I3) |
| `35-manifest-paths.bats` | 13 | пути из манифеста в файловых операциях: `app_name`, `install_name` — регрессия на блокер review-i5-1 (N5-P3, N5-P4, N5-R1) |
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

Ниже — прогоны исходного набора (140 тестов). Прогоны после `review-i5-1`, с добавленными
регрессионными тестами (153 теста), — в §8.4.

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
| `integration` | 80 | 80 |
| `ci` | 27 | 25 |
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
| `bats` (`ubuntu-latest`, `macos-latest`) | `bats installers/tests/bats` | все 153 bats-теста; на ubuntu естественным образом проверяется linux-ветка `install-posix.sh`, на macos — macos-ветка и bash 3.2 |
| `pester` (`ubuntu-latest`, `pwsh`) | `Invoke-Pester installers/tests/pester` | 56 Pester-тестов: `Read-Manifest`, `Confirm-FileHash`, `Get-InstallPlan`, `Invoke-Check`, `Invoke-Main` (аргументы), `Expand-UserPathTemplate`/`Test-PurgePathSafe`, `Test-UserPathContains`, кодировки, ветки `Invoke-Uninstall` и `ConvertFrom-UninstallString` (§8.2) |
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
| AC-11 | unit | 14 | 10-manifest.bats 35-manifest-paths.bats Install.Tests.ps1 |
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
| AC-126 | ci | 2 | Install.Tests.ps1 Uninstall.Tests.ps1 |
| AC-127 | ci | 2 | 95-sources.bats |
| AC-128 | ci | 1 | 95-sources.bats |
| AC-129 | ci | 0 | — |
| AC-130 | ci | 1 | 95-sources.bats |
| AC-131 | ci | 1 | 95-sources.bats |
| AC-132 | ci | 2 | 95-sources.bats Encoding.Tests.ps1 |
| AC-133 | unit | 3 | 95-sources.bats Encoding.Tests.ps1 |
| AC-134 | unit | 2 | 95-sources.bats Encoding.Tests.ps1 |
| AC-135 | integration | 18 | 35-manifest-paths.bats Uninstall.Tests.ps1 |
| AC-136 | ci | 10 | Uninstall.Tests.ps1 |
| AC-137 | ci | 2 | Uninstall.Tests.ps1 |

## 8. После review-i5-1

Ревью `reports/review-i5-1.json` вынесло `request_changes`. Из трёх `must_fix` один адресован
тестам: пробел в наборе, из-за которого блокер (обход каталога через `artifacts[].app_name`) не
был пойман. Ниже — что добавлено в зоне `installers/tests/` после исправлений DEV.

### 8.1 Регрессия на блокер: пути из манифеста в файловых операциях

Новый файл `installers/tests/bats/35-manifest-paths.bats` — 13 тестов, маркеры `AC-11` и `AC-135`.

| Проверка | Значение поля | Путь запуска | Ожидание |
|---|---|---|---|
| `app_name` с `../..` | `../../victim` | без флагов | код 2, поле `artifacts.1.app_name`, каталог-жертва цел |
| `app_name` с `../..` | `../../victim` | `--uninstall` после успешной установки | код 2, нет строки «Desktop: удалён», бинарник и блок профиля на месте |
| `app_name` с `../..` | `../../victim` | `--dry-run --uninstall` | код 2, плана не печатается |
| `app_name` с `../..` | `../../victim` | `--uninstall --purge` | код 2, `auth.json` и каталог конфига целы |
| PoC из ревью | `../..<абс. путь>/victim` | `--uninstall` | код 2, жертва цела (тест пропускается вне macOS) |
| ведущий `/` | `/tmp/victim` | установка и `--uninstall` | код 2 в обоих прогонах |
| обратный слэш | `..\victim` | без флагов | код 2 |
| буква диска | `C:\victim` | `--uninstall` | код 2 |
| штатное значение | `OpenCode.app` | `--dry-run --uninstall` | код 0, в плане `/Applications/OpenCode.app` |
| `ca.install_name` | `../x` | без флагов | код 2, файла `~/.config/x` нет |
| `artifacts[].install_name` | `../y` | без флагов | код 2, файла `<prefix>/y` нет |
| `install_name` | `/tmp/opencode`, `C:\opencode` | без флагов | код 2 |
| порядок проверок | `../y` + заведомо неверный `sha256` | без флагов | код 2 (не 4): разбор манифеста завершается до сверки целостности |

Каждый из этих тестов, помимо кода выхода и текста сообщения, проверяет **отсутствие файловых
операций**: снимки трёх каталогов (`$HOME`, корень установки `--prefix`, каталог-жертва) до и
после запуска сравниваются побайтово, а ловушка `sudo` в `PATH` подтверждает, что привилегии не
запрашивались (`assert_no_forbidden_calls`).

Каталог-жертва создаётся вне корня установки, а обход строится через `$HOME/Applications`
(`../../victim`), поэтому тесты воспроизводят уязвимость на любой платформе, а не только там,
где существует `/Applications`.

**Проверка чувствительности (мутация).** Из `manifest_load` временно удалена ветка проверки
`app_name` (`is_safe_pkg_path`), прогон повторён, изменение откачено `git checkout --`:
**8 из 13 тестов упали** — тесты действительно ловят именно блокер, а не сопутствующее поведение.
Остальные 5 относятся к `install_name` и к штатному значению `app_name` и мутацией не затрагиваются.

### 8.2 Pester: штатный деинсталлятор Desktop на Windows

Новый файл `installers/tests/pester/Uninstall.Tests.ps1` — 18 тестов, все `Describe` помечены
тегом `ci` (набор идёт в задание `pester`, локально `pwsh` по-прежнему отсутствует).

| Группа | Тестов | Что проверяется |
|---|---|---|
| `ConvertFrom-UninstallString` | 5 | путь в кавычках с аргументами и без, `MsiExec.exe /X{GUID}`, команда без кавычек, обрамляющие пробелы |
| Ветки `Invoke-Uninstall` | 7 | `QuietUninstallString` (запускается как есть, `silent_args` не добавляются), `UninstallString` + `silent_args` из манифеста, отсутствие записи в реестре, пустые строки деинсталляции, ненулевой код, отсутствие desktop-артефакта |
| Безопасность `app_name` в `Read-Manifest` | 6 | `../../victim`, ведущий `/`, `..\victim`, `C:\victim` → код 2; имя поля `artifacts.1.app_name` в сообщении; штатное значение принимается; самопроверка «набор не трогает реестр» |

Граница среды подменена двумя моками: `Get-DesktopUninstallEntry` (что зарегистрировано в
системе) и `Invoke-UninstallProcess` (фактический запуск и код возврата). Проверяется не факт
вызова мока, а наблюдаемое поведение: какой именно путь и какие аргументы получил процесс
(`Should -Invoke … -ParameterFilter`), какие строки напечатаны и какой код выхода вернул
`Invoke-Uninstall`. Для ветки с кодом 3 дополнительно проверено, что удаление CLI, записи в PATH,
переменной `NODE_EXTRA_CA_CERTS` и CA уже выполнено и не откатывается, файлы физически удалены,
а итоговый код — 1.

Набор Pester вырос с 38 до 56 тестов (3 файла).

### 8.3 Новые критерии приёмки (предложение для spec-agent)

Зона `installers/docs/` тестам недоступна, поэтому формулировки предлагаются здесь; в именах
тестов уже проставлены маркеры `AC-135`, `AC-136`, `AC-137` (следующие свободные после AC-134).

- **AC-135** (`N5-P4`, `N5-P3`, `N5-R1`; тип `integration`).
  *Дано*: каталог-жертва вне зоны установки и пакет, в манифесте которого `artifacts[].app_name`
  равен `../../<путь>/victim` (варианты: ведущий `/`, обратный слэш, буква диска `C:\`).
  *Когда*: установщик запущен без флагов, с `--uninstall` и с `--dry-run --uninstall`.
  *Тогда*: во всех прогонах — сообщение с именем поля `artifacts.<i>.app_name` и код выхода 2;
  каталог-жертва и его содержимое на месте; снимок `$HOME` и корня установки до и после совпадает
  побайтово (ни одной файловой операции); строки «Desktop: удалён» в выводе нет. Тот же результат
  для небезопасных `ca.install_name` и `artifacts[].install_name`.
- **AC-136** (`N5-R1`; тип `ci`).
  *Дано*: манифест с desktop-артефактом и `silent_args`, моки записи деинсталляции и запуска
  процесса. *Когда*: разобраны три случая — непустая `QuietUninstallString`; пустая
  `QuietUninstallString` при непустой `UninstallString`; записи нет.
  *Тогда*: (а) выполняется команда из `QuietUninstallString` без добавления `silent_args`;
  (б) выполняется команда из `UninstallString` с добавленными `silent_args`; в обоих случаях при
  коде 0 печатается «Desktop: удалён (<путь>)»; (в) процесс не запускается, печатается
  «Desktop: удалите вручную через «Приложения и возможности»», код выхода 0.
- **AC-137** (`N5-R1`, `N5-I1`; тип `ci`).
  *Дано*: деинсталлятор Desktop возвращает ненулевой код (3). *Когда*: выполнено
  `install.ps1 -Uninstall`. *Тогда*: печатается «Desktop: ошибка (код 3)», строки «Desktop: удалён»
  нет; удаление CLI, записи PATH, переменной `NODE_EXTRA_CA_CERTS` и CA выполнено и не
  откатывается; итоговый код выхода 1.

К моменту сдачи spec-agent уже внёс AC-135…AC-137 в `installers/docs/acceptance-criteria.yaml`
(ревизия 1.1) — формулировки совпадают с приведёнными, номера закреплены за теми же критериями,
трассировка `trace-ac.sh` их видит.

### 8.4 Прогон и триаж

| Прогон | Команда | Результат | Время |
|---|---|---|---|
| 1 | `bats installers/tests/bats` | 153/153 ok | ~44 с |
| 2 | `bats installers/tests/bats` | 153/153 ok | ~44 с |
| 3 (контрольный) | `bats installers/tests/bats` | 153/153 ok | ~44 с |

`shellcheck -s bash` на собственных хелперах (`installers/tests/bats/helpers.bash`,
`installers/tests/trace-ac.sh`) чист; тест AC-130 (shellcheck по всем `installers/**/*.sh`)
зелёный. Трассировка: 137 критериев, все `unit`/`integration` покрыты.

Падений уровня `code_bug` нет: исправления DEV по блокеру и по Windows-деинсталлятору
подтверждены тестами. Новых файлов `bugs/BUG-I5-*.json` не создавалось. Флаки-тестов не
обнаружено (три прогона подряд без расхождений).

Не проверено локально: Pester-набор (`pwsh` в среде отсутствует) — как и прежде, первый прогон
задания `pester` в CI остаётся точкой контроля; при его падении тесты будут доработаны по
результату, а расхождение поведения `install.ps1` с AC-136/AC-137 оформлено багом.

---

## 9. После review-i5-2

Ревью `reports/review-i5-2.json` выдало два must_fix с `assignee: test` (находки 3 и 4) — четыре
поведения ревизии 1.1 без единого теста (выжившие мутации M5–M8) и отсутствие тестов на находки
1 и 2 самого цикла (dmg без `app_name`; `Get-DesktopUninstallEntry` замокан во всех тестах).
DEV закрыл находки кодом (`c2d5a28`, `02a3187`, `be7f9f0`, `a0377a0`), spec-agent — критериями
AC-138…AC-140 (`baac2e3`). Ниже — что добавлено в набор и чем это проверено.

### 9.1 Что добавлено

| Файл | Было | Стало | Что покрыто |
|---|---|---|---|
| `installers/tests/bats/36-desktop-safety.bats` (новый) | — | 22 | обязательность и строгость `app_name` для `installer_type="dmg"`; прямой вызов `install_desktop`, `find_installed_desktop`, `desktop_target_path` |
| `installers/tests/bats/35-manifest-paths.bats` | 13 | 17 | «схлопывающиеся» пути в `ca.file`/`ca.install_name`/`artifacts[].file`/`artifacts[].install_name`; таблица `is_safe_pkg_path`/`is_safe_app_name` |
| `installers/tests/bats/50-profile.bats` | 10 | 18 | схлопывание задвоенного блока с сохранением позиции; формы чужого присваивания и молчание на упоминание |
| `installers/tests/bats/90-release.bats` | 15 | 24 | валидация формата `--version`; экранирование строковых значений в манифесте |
| `installers/tests/pester/DesktopEntry.Tests.ps1` (новый) | — | 24 | `Get-DesktopUninstallEntry` на подменённом источнике записей реестра; `ConvertFrom-UninstallString`; `Read-Manifest` и `Test-AppName` по `app_name` |

Итого bats: **153 → 196** (+43). Pester: **56 → 80** (+24, 4 файла).

**36-desktop-safety.bats.** Шесть значений `app_name` (поле отсутствует, `""`, `"   "`, `"\t"`,
`"."`, `"./OpenCode.app"`, `"OpenCode.app/"`, дополнительно `".."` и `"sub/OpenCode.app"`) ×
шесть режимов (установка, `--check`, `--dry-run`, `--uninstall`, `--dry-run --uninstall`,
`--uninstall --purge`). В каждом сочетании: код 2, названо поле `artifacts.1.app_name`,
каталог-жертва и `$HOME/Applications` с посторонним файлом целы, побайтовые снимки `$HOME`,
корня `--prefix` и каталога-жертвы до и после совпадают, ловушка `sudo` пуста. Отдельно —
что отказ происходит до сверки целостности (заведомо неверный хеш CLI не меняет код 2 на 4)
и что штатное `OpenCode.app` по-прежнему проходит `--dry-run` (0) и `--check` (7,
«Манифест: корректен»).

Прямой вызов `install_desktop` при `OPENCODE_INSTALLER_SOURCE_ONLY=1` сделан в двух режимах,
чтобы утверждение «привилегированных операций нет» не сводилось к проверке собственного мока:

- `MODE=stub` — подменены `run_priv`, `hdiutil`, `ditto`, `desktop_dest_dir`; журнал операций
  при небезопасном `app_name` пуст, при штатном — содержит `ditto` с целью `<dest>/OpenCode.app`
  и ни одного `rm -rf` по самому `<dest>`;
- `MODE=priv` — `run_priv` настоящий (`use_sudo=1`), `sudo` перехвачен ловушкой из
  `install_trap_stubs`, поэтому «ни одного привилегированного вызова» видно в `forbidden.log`
  наблюдаемо; при штатном значении в журнале ровно `sudo ditto … <dest>/OpenCode.app`.

**DesktopEntry.Tests.ps1.** Подменяется ровно один источник — сами записи реестра
(`Test-IsWindowsHost`, `Test-Path`, `Get-ChildItem`, `Get-ItemProperty`); логика сопоставления
`DisplayName`, экранирование шаблона и выбор ветки исполняются как есть. В подменённой ветке
`Uninstall` лежат чужие записи (7-Zip, Notepad++, Microsoft 365) и, в части тестов, наша запись
«OpenCode Magnit». Значения `app_name` `"*"`, `"?"`, `"[A-z]*"`, `"*.app"`, `"Open*"` не дают
совпадения ни с одной записью; негативный контроль — `"OpenCode"` и `"OpenCode.app"` находят
именно нашу запись с её `UninstallString` и `DisplayTarget`.

### 9.2 Проверка тестов мутациями

Каждый новый блок тестов проверен обратной мутацией: старое поведение временно возвращалось в
`installers/common/install-posix.sh` / `installers/release.sh`, набор прогонялся, затем
`git checkout -- installers/common installers/release.sh`. После всех прогонов `git status` по
`installers/` чист, кроме файлов набора в `installers/tests/`.

| Мутация | Что откачено | Падений | Убита |
|---|---|---|---|
| M5a | `apply_profile_block`: правится только первый блок, остальные остаются | 2 | да |
| M5b | `apply_profile_block`: все блоки вырезаются, новый дописывается в конец (переезд) | 1 | да |
| M6a | `profile_foreign_env_line`: откат к «любое упоминание переменной» | 2 | да |
| M6b | `profile_foreign_env_line`: только `export NAME=` / `NAME=` в начале строки | 4 | да |
| M7 | `release.sh`: снята валидация формата `--version` | 4 | да |
| M8 | `release.sh`: `json_escape` сделана тождественной | 2 | да |
| MA1 | `manifest_load`: `app_name` перестал быть обязательным для `dmg` | 8 | да |
| MA2 | `is_safe_app_name` ослаблена до `is_safe_pkg_path` | 2 | да |
| MA3 | снята страховка `desktop_target_path` и проверка в `find_installed_desktop` | 5 | да |
| MA4 | `is_safe_pkg_path`: сняты ветки `.`, `./x`, `x/.`, завершающего `/` | 4 | да |

MA4 при первом заходе **выжила** (0 падений): для `app_name` её перекрывала более строгая
`is_safe_app_name`, а для `ca.install_name`, `artifacts[].install_name`, `ca.file` и
`artifacts[].file` этих форм не проверял никто. Пробел закрыт четырьмя тестами в
`35-manifest-paths.bats`; после этого мутация убивается.

M5a и M5b проверяют разные половины требования: первая — «ровно один блок», вторая — «блок
правится на месте». Тест на позицию сверяет номера строк `USER_ONE < блок < USER_TWO < USER_THREE`
и требует, чтобы последней строкой файла осталась пользовательская.

### 9.3 Маркеры AC

Spec-agent параллельно внёс AC-138…AC-140 (`baac2e3`, ревизия 1.2) и закрепил за ними Desktop и
Windows-ветку. Маркеры в именах тестов приведены к этой нумерации:

| AC | Что закреплено | Тесты |
|---|---|---|
| AC-138 | `installer_type="dmg"` без `app_name` (и с пустым/пробельным/«схлопывающимся») → код 2 на всех шести режимах, ни одной файловой операции | 24 (`36-desktop-safety.bats`, `DesktopEntry.Tests.ps1`) |
| AC-139 | `Get-DesktopUninstallEntry`: подстановочные символы в `app_name` не совпадают с чужими записями; `Read-Manifest` отвергает такие значения | 16 (`DesktopEntry.Tests.ps1`) |
| AC-140 | `ConvertFrom-UninstallString`: некавыченный путь с пробелами, аргумент в кавычках, неоднозначность → `$null` и отказ от запуска | 8 (`DesktopEntry.Tests.ps1`) |

Два поведения ревизии 1.1, названные в находке 3 ревью, критериями **не закрыты** — spec-agent
отвёл AC-138…140 под Desktop. Тесты написаны и работают, но опираются на ближайшие существующие
критерии; для них предлагаются два новых номера:

- **AC-141** (`N5-I5`, `N5-U1`; тип `integration`).
  *Дано*: файл профиля, где маркированный блок стоит в середине и задвоен, вокруг —
  пользовательские строки. *Когда*: выполнен `install.sh --no-launch`.
  *Тогда*: в файле ровно один блок; он остался на месте первого (порядок пользовательских строк
  не изменён, последняя строка файла — пользовательская); содержимое блока обновлено; повторный
  запуск файл не меняет. Дополнительно: предупреждение о чужом `NODE_EXTRA_CA_CERTS` выдаётся на
  формы `NAME=`, `export NAME=`, `declare -x`, `typeset -x`, `readonly`, `local`, `env NAME=… cmd`,
  `setenv NAME`, `set -gx NAME` и на присваивание внутри составной строки (`if …; then export …; fi`),
  и не выдаётся на комментарий, `echo "NAME=…"`, упоминание в тексте и на наше собственное значение.
  Сейчас тесты помечены `AC-42` и `AC-47` (ближайшие существующие) + `AC-141`.
- **AC-142** (`N5-B1`; тип `ci`).
  *Дано*: фикстурный каталог артефактов. *Когда*: `release.sh` вызван с `--version` в формах
  `1.17.9`, `v1.17.9-magnit.1`, `1.17.9-magnit`, `1.17-magnit.1`, `1.17.9-magnit.1a`,
  `1.17.9-MAGNIT.1`, с пробелом, слэшем и кавычкой. *Тогда*: код 2, сообщение
  «Недопустимый формат --version: <значение>», каталог `--out` не создан, каталог артефактов не
  изменён; валидная версия по-прежнему собирается. Дополнительно: `--hub-url` со значением,
  содержащим `"` и `\`, даёт манифест, который читается строгим парсером JSON (`python3 json.load`),
  значение возвращается без искажения и печатается установщиком в строке `Hub:` как есть.
  Сейчас тесты помечены `AC-111` (ближайший существующий) + `AC-142`.

### 9.4 Прогон и триаж

| Прогон | Команда | Результат |
|---|---|---|
| 1 | `bats installers/tests/bats` | 196 тестов: 195 ok, 1 not ok (BUG-I5-001) |
| 2 | `bats installers/tests/bats` | 196 тестов: 195 ok, 1 not ok (тот же) — результаты прогонов совпадают построчно |
| — | `shellcheck -s bash` по `installers/**/*.sh` и по собственным хелперам | чисто |
| — | `installers/tests/trace-ac.sh` | 140 критериев, все `unit`/`integration` покрыты, код 0 |

Собственные хелперы новых файлов проверены отдельно: тела `.bats` без блоков `@test` и текст
скрипта-драйвера `install_desktop` прогнаны через `shellcheck -s bash` — чисто (единственное
подавление — файловое `SC2034` в драйвере с обоснованием: глобальные переменные читает
загруженный через `source` `install-posix.sh`).

**Триаж единственного падения — `code_bug`, оформлен `bugs/BUG-I5-001.json`** (severity `medium`,
статус `open`). `json_escape` в `release.sh` экранирует только `\` и `"`, но не управляющие
символы: `--hub-url` со значением, содержащим перевод строки, даёт `common/manifest.json`, который
JSON-парсер отвергает («Invalid control character»), при этом `release.sh` завершается кодом 0.
Самопроверка N5-B2 поломку не ловит — она запускает `install.sh --dry-run`, а awk-парсер
`install-posix.sh` терпим к сырым управляющим символам внутри строки (проверено: `--check` на таком
пакете печатает «Манифест: корректен»). Контракт N5-B1 «строковые значения, попадающие в манифест,
экранируются в JSON» нарушен; на портал уходит пакет, не проходящий собственную
`common/manifest.schema.json`, а `install.ps1` разбирает манифест строгим `ConvertFrom-Json`.
Минимальный воспроизводящий тест — `90-release.bats::AC-142: --hub-url с переводом строки →
манифест остаётся валидным JSON`; он остаётся в наборе красным до фикса и после фикса становится
регрессионным. Кавычка и обратный слэш экранируются верно — соответствующие тесты зелёные.

Флаки не обнаружены: два прогона подряд дали побайтово совпадающий список результатов.

Не проверено локально: Pester-набор — `pwsh` в среде по-прежнему отсутствует, `DesktopEntry.Tests.ps1`
(24 теста) написан по чтению `install.ps1` и рассчитан на задание `pester` в CI. Подмена
`Test-IsWindowsHost`/`Test-Path`/`Get-ChildItem`/`Get-ItemProperty` — новая для набора техника;
если в CI она даст сбой, тесты будут доработаны по результату, а расхождение поведения
`install.ps1` с AC-138…AC-140 оформлено багом.
