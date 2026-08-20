# Снимок состояния проекта на 2026-08-20 (для продолжения в новой сессии)

## Что это за проект
Корпоративный OpenCode для Magnit/Тандер: форк OpenCode с витриной MCP-коннекторов и входом по корпоративному
SSO + серверный Hub (каталог, OAuth, proxy) + собственные MCP-серверы. Требования — `docs/req-mvp.md` (rev 0.2).
Цель MVP (формулировка заказчика): установка на macOS/Windows/Linux; лёгкое добавление новых MCP; понятный
процесс переноса каталога при обновлении OpenCode; Atlassian + GitLab + ТЭГ подключаются «в 2 клика».

## Репозитории и ветки
| Репозиторий | Локально | Ветка | Состояние |
|---|---|---|---|
| `mshishenkov1/opencode-mcp-hub` | `~/Documents/opencode-mcp-hub` | `pipeline/i1-hub-login-catalog-20260818` | Hub I-1 + установщики I-5 |
| то же (worktree) | `~/Documents/opencode-mcp-hub-i3` | `pipeline/i3-hub-oauth-facade-proxy` | Hub I-3 + H5 + деплой I-6 |
| `mshishenkov1/opencode` (форк upstream MIT) | `~/Documents/opencode` | `corp/i4-sso-connectors` | форк I-4 |
| `mshishenkov1/magnit-tag-mcp` | `~/Documents/magnit-tag-mcp` | `feature/oauth-mode` | ТЭГ-MCP I-2 |
Ветки запушены в GitHub; `main` нигде не тронут — merge остаётся решением человека.

## Закрытые итерации (review approve + гейты)
- **I-1 Hub**: вход через LiteLLM CLI-SSO, постоянный ключ, каталог, well-known/remote-config. 408 тестов, coverage 95 %, mutation 89 %.
- **I-2 ТЭГ-MCP**: нативная MCP-авторизация (FastMCP OAuthProxy) + per-user режим. 889 тестов, покрытие 100 %.
- **I-3 Hub**: OAuth-фасад (DCR/PKCE/refresh/revoke), брокер токенов, MCP-proxy с виртуализацией сессий, веб, миграции. 649 тестов, coverage 93 %, mutation 74 %.
- **I-4 форк OpenCode**: вход по SSO при первом запуске, витрина `/connectors`, умолчания, сборки, CI. 214 corp-тестов.
- **I-6 деплой**: Helm (pilot/prod), Windows-стенд, smoke, backup, runbook, k6. Нагрузка: добавка proxy p50 3,3 мс / p95 5,0 мс при бюджете 50 мс, ~480 rps на реплику.

## В работе на момент переезда
- **I-5 установщики**: код+спека (144 AC)+227 bats готовы; идёт цикл ревью (три блокера уже найдены и закрыты: обход каталога через `app_name`, `rm -rf /Applications` при пустом `app_name`, `rm -rf $HOME` через `purge_paths`); осталось финальное ревью + гейты.
- **BUG-I1-001 (critical, открыт)**: живой вход не завершается — `api_keys` вставляется без записи в `users` (нарушение FK в PostgreSQL); тесты не поймали, т.к. идут на SQLite с выключенной проверкой FK. Фикс в работе (dev-агент), вместе с включением `PRAGMA foreign_keys=ON`.
- **I-7 ТЭГ (не начата)**: карта кастомного API готова — `reports/tag-custom-api-map.md` (43 кастомных пути / 55 операций), состав доработки — 3 правки без новых инструментов + 15 инструментов + маленький оверлей OpenAPI-спека.

## Открытые баги
- `bugs/BUG-I1-001.json` — critical, вход/FK (в работе).
- `bugs/BUG-I4-002.json`, `BUG-I4-003.json` — исправлены (Desktop падал на старте; `corp/build.ts --desktop` не собирал Electron).
- `bugs/BUG-I5-001.json` — исправлен (экранирование управляющих символов в манифесте).

## Известные проблемы, требующие решения
1. **Автообновление Desktop тянет ванильный OpenCode** (`1.18.19` с GitHub) — блокер раздачи; чинится правкой `packages/desktop/electron-builder.config.ts`, который заморожен правилом S-B9/AC-112 → нужно изменение спеки.
2. **Одинаковый appId с установленным OpenCode** — сборки не запускаются одновременно; корп-каналу нужен свой идентификатор.
3. **Сертификат**: Desktop не видит CA из `NODE_EXTRA_CA_CERTS` (Electron ходит через стек Chromium) — на проде опираемся на системное хранилище, которое наполняет установщик; на локальном стенде поднят HTTP-режим.
4. **Локализация витрины**: интерфейс показывается по-английски, ожидался русский по умолчанию — не проверено.
5. **Автослой MCP** строится из ванильного спека Mattermost — кастом ТЭГ им не покрывается в принципе.

## Локальный стенд (Mac)
```
colima start                                            # если Docker не запущен
cd ~/Documents/opencode-mcp-hub-i3/deploy
docker compose -p hubi3 -f docker-compose.yml -f docker-compose.local-http.yml up -d
curl -s http://localhost:8080/health                    # HTTP-режим для Desktop
curl -sk https://localhost:8443/health                  # TLS-режим (самоподписанный)
```
Приложение: `~/Documents/opencode/packages/desktop/dist/mac-arm64/OpenCode.app` (собрано с `CORP_HUB_URL=http://localhost:8080`).
Пересборка: `cd ~/Documents/opencode && CORP_HUB_URL=http://localhost:8080 bun run corp/build.ts --desktop --skip-cli`.
CLI-сборка: `packages/opencode/dist/opencode-darwin-arm64/bin/opencode`.

## Внешние зависимости (блокируют MVP, не разработку)
1. OAuth-приложения: GitLab ×2, Jira, Confluence (не запрашивались), ТЭГ (на встрече отказали, склоняются к PAT).
2. Внутренняя Linux-ВМ с DNS-именем и сертификатом (арендованный внешний сервер не подходит: Hub обязан ходить внутрь периметра).
3. Согласование ИБ: хранение токенов, распространение корпоративной сборки, подпись бинарников.
4. AI Lab: право CLI-JWT на `/key/generate`, лимит `maxSessions=1000` у их GitLab MCP.

## Как ведётся разработка
Агентный конвейер: spec → dev → test → review → гейты (G1–G6), зоны записи по ролям, отчёты в `reports/`.
Субагенты запускаются с моделью `opus`; оркестрация и постановка задач — на основной сессии.
Особенности: `mutmut` на macOS требует `no_proxy='*'`; при обрыве субагента его работа в дереве сохраняется —
перезапускать с «продолжи с текущего дерева».

## Дополнение на момент передачи сессии (2026-08-20, вечер)
- **BUG-I1-001 исправлен** (ветка `pipeline/i3-hub-oauth-facade-proxy`, коммиты `2abc688`, `d37216e`): причина — SQLAlchemy
  упорядочивает вставки по мапперам, а не по внешним ключам, из-за чего `api_keys` уходил в базу раньше `users`;
  добавлен `flush()` после upsert пользователя внутри той же транзакции. Дополнительно включён `PRAGMA foreign_keys=ON`
  для SQLite, чтобы тестовое окружение вело себя как боевое. Подтверждено на живом PostgreSQL (users+api_keys+audit_log
  создаются, `/api/me` по выданному ключу отвечает 200). **Статус: fixed_pending_verification** — реальный вход
  пользователя через корпоративный SSO после фикса ещё не повторяли; это первое, что нужно сделать в новой сессии
  (заодно выяснится, выдаёт LiteLLM постоянный ключ или суточный JWT — вопрос всё ещё открыт).
- Диспут `disputes/test-dispute-BUG-I1-001-sqlite-fk` разрешён как `uphold_dispute`: тест `test_missing_connection_returns_jsonrpc_error`
  опирался на состояние, недостижимое на PostgreSQL; переоформить его — задача test-агента.
- Прогон после фикса: 648 passed / 1 failed (тот самый тест из диспута), ruff и mypy чисто.
- Локальный стенд: из-за фиксированного порта 8080 в `docker-compose.local-http.yml` поднимается одна реплика Hub
  (`hubi3-hub-2`), вторая остаётся в статусе `Created` — свойство локального override, на работу не влияет.
