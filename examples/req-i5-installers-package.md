# Требование I-5: установщики и пакет для портала; бэклог I-3 (minor review-i3-2)

Контекст — `docs/req-mvp.md` rev 0.2 §6.7 (R-13, R-14), §1.1; состояние: Hub I-1/I-3 готов (ветка
`pipeline/i3-hub-oauth-facade-proxy`), форк OpenCode с SSO/витриной (`opencode`, ветка `corp/i4-sso-connectors`,
сборка `corp/build.ts` → `opencode` CLI под darwin-arm64/x64, linux-x64, win32-x64, версия `1.17.9-magnit.N`;
Desktop (Electron) — `packages/desktop`). Портал сегодня раздаёт `install-opencode.sh/.bat` + `tander-ca-bundle.pem`
+ Desktop-установщики (см. `docs/req-mvp.md` §3, скрипт в `examples/` исследований нет — факты: CA через
`NODE_EXTRA_CA_CERTS`, `%USERPROFILE%\.config\opencode` на Windows, `~/.config/opencode` на macOS/Linux,
`opencode.json` пользователя может существовать). **[проверено]** — снято с портала 2026-08-18.

## 1. Цель
Один пакет на ОС: пользователь скачал → запустил установщик → сертификаты, корпоративная сборка OpenCode и умолчания
установлены → OpenCode при первом запуске предлагает вход по SSO. Ничего не спрашивает, ключей не просит. Повторный
запуск — обновление без потери пользовательского конфига.

## 2. Требования
- I5-01. Структура пакета (артефакты CI форка + этот репозиторий): `installers/macos/install.sh` (+ `.pkg` опционально),
  `installers/linux/install.sh`, `installers/windows/install.ps1` и тонкий `install.bat`-лончер (ExecutionPolicy
  Bypass), общий `installers/common/` (манифест версии `manifest.json`: версия сборки, URL/хеши артефактов, адрес
  Hub, CA); CA `tander-ca-bundle.pem` кладётся рядом.
- I5-02. Действия установщика: (1) проверка ОС/архитектуры; (2) установка CA: копия в каталог конфига OpenCode и
  `NODE_EXTRA_CA_CERTS` (macOS/Linux — в профиль shell идемпотентно; Windows — переменная пользователя через
  `[Environment]::SetEnvironmentVariable(...,"User")`), для Desktop — та же переменная пользователя; (3) установка
  бинарника CLI/TUI в PATH (`/usr/local/bin` или `~/.local/bin`; Windows — `%LOCALAPPDATA%\Programs\opencode` + PATH
  пользователя) с проверкой хеша из манифеста; (4) Desktop — запуск штатного установщика из пакета (macOS `.dmg` →
  `/Applications`, Windows `.exe` silent, если поддерживается electron-builder); (5) **не** пишет ключи и не трогает
  пользовательский `opencode.json` кроме резервной копии при конфликте; (6) печатает итог и запускает первый запуск
  (`opencode corp status`, затем предложение `opencode` / открыть Desktop).
- I5-03. Идемпотентность и обновление: повторный запуск обновляет бинарник/приложение/CA, сохраняет конфиг; откат —
  предыдущий бинарник сохраняется как `.bak`.
- I5-04. Удаление: `--uninstall` (бинарник, PATH, переменная CA, Desktop; конфиг — только с `--purge`).
- I5-05. Проверяемость без установки: `--dry-run` печатает план; `--check` проверяет среду (PATH, CA, версия).
- I5-06. Тесты: bash — `bats` (macOS/Linux, с фейковым `$HOME`/`$PREFIX`, без sudo), PowerShell — `Pester`
  (Windows-стенд или `pwsh` на macOS для логики без реестра), общие сценарии: чистая система, повторный запуск,
  конфликт конфига, `--dry-run`/`--check`/`--uninstall`, неверный хеш → отказ. CI: bats/pester на PR.
- I5-07. Пакет на портал: `installers/release.sh` собирает `opencode-magnit-<os>-<arch>-<ver>.zip|tar.gz` из
  артефактов CI форка + установщик + CA + README; публикация в GitHub Releases приватного репо (для портала — ручная
  выкладка AI Lab; формат карточки загрузок портала — как у существующего «OpenCode CLI»).
- I5-08. Документация для пользователя (кратко, RU): `docs/install-user.md` — что делает установщик, первый запуск,
  где витрина; для админов — `docs/install-admin.md` (подпись сборок, выкладка, переменные).
- I5-09. Бэклог Hub из review-i3-2 (minor, в ветке I-3 → ветка `pipeline/i5-...` от неё): TTL ключа пробы CB ≥
  максимальной длительности пробы или продление пробой; валидация `X-Forwarded-For` как IP при `HUB_TRUST_PROXY`;
  убрать лишний `delete` в `record_success`; упростить `record_failure`. AC на каждое.

## 3. Зависимости
Подпись сборок (Apple Developer ID / Authenticode) — ИБ; без подписи — документ с обходом предупреждений только для
пилота. Windows-стенд для Pester/реестра — D-9.
