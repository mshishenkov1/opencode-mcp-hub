# Требование I-4: корпоративный форк OpenCode — вход по SSO, витрина коннекторов, умолчания, сборки

Репозиторий реализации: `opencode` (зеркало anomalyco/opencode, локально
`/Users/miroslavshishenkov/Documents/opencode`, ветка `main` = upstream `v1.17.9`, ветка
`upstream-main` — для rebase). Контекст продукта — `docs/req-mvp.md` rev 0.2 (§1.1, §6.1
F-01…F-07). Карта исходников — `docs/opencode-source-map.md` **[проверено]**.

## 1. Цель

Пользователь ставит корпоративную сборку OpenCode; при первом запуске входит через
корпоративный SSO (один экран, браузер); модель `MagnitCopilot` подключается сама; в
приложении есть витрина корпоративных MCP-коннекторов (`/connectors`), из которой сервер
подключается «в 2 клика» (OAuth в браузере) и получает права; всё работает в TUI, Desktop
и `opencode web`. Наши изменения — минимальный, отделимый набор коммитов поверх upstream.

## 2. Факты о коде [проверено]

- TUI: `packages/tui` (SolidJS + opentui), команды/палитра — `packages/tui/src/app.tsx`
  (`appCommands`, поле `slashName`: `models`, `agents`, `mcps`, `connect`, …), MCP-диалог —
  `packages/tui/src/component/dialog-mcp.tsx` (список, статус, тумблер через
  `local.mcp.toggle` → `sdk.client.mcp.connect/disconnect`), first-run «нет провайдера» —
  `app.tsx` (открывает `DialogProviderList`, `dialog-provider.tsx`, OAuth провайдера через
  `sdk.client.provider.oauth.authorize/callback`). В TUI нет запуска MCP-OAuth и нет i18n.
- Desktop/web: `packages/app` (SolidJS; Desktop — Electron через `packages/desktop`,
  electron-vite + electron-builder), MCP-диалог `dialog-select-mcp.tsx`, тумблер
  `global-sync/mcp.ts` (`needs_auth → sdk.mcp.auth.authenticate`), команды
  `context/command.tsx`, `use-session-commands.tsx`, i18n `src/i18n/*.ts` (русский есть, неполный).
- Сервер (внутри бинарника): HTTP API `packages/opencode/src/server/routes/instance/httpapi/groups/`
  — `mcp.ts` (`GET /mcp`, `POST /mcp`, `POST /mcp/:name/auth`, `…/auth/authenticate`,
  `…/auth/callback`, `DELETE /mcp/:name/auth`, `…/connect`, `…/disconnect`), `config.ts`
  (`GET/PATCH /config`), `provider.ts` (`/provider/:id/oauth/authorize|callback`).
  MCP: `packages/opencode/src/mcp/index.ts` (статусы `connected|disabled|failed|needs_auth|
  needs_client_registration`, `authenticate` открывает браузер), OAuth callback
  `http://127.0.0.1:19876/mcp/oauth/callback`, токены `mcp-auth.json`.
  Тумблер `enabled` **не персистится** (только runtime); персист — `PATCH /config` /
  `opencode mcp add` (`cli/cmd/mcp.ts` `addMcpToConfig`).
- Конфиг: `.well-known/opencode` (`auth.command`, `config`, `remote_config`) в
  `packages/opencode/src/config/config.ts`; флаги `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`;
  build-константы через `define` в `packages/opencode/script/build.ts`.
- Сборка: bun 1.3.14 workspaces + turbo; CLI — `bun run script/build.ts`
  (`Bun.build({compile})`, цели darwin/linux/win32; Web UI встраивается); Desktop —
  `packages/desktop` `package:mac|win|linux`; версия — `Script.version`/package.json.
- Plugin API: хуки `auth` (методы `oauth`/`api` для провайдера с `authorize()/callback()`),
  `config`, `tool`; плагины — npm/локальный файл.

## 3. Требования

### 3.1. Корпоративные умолчания и минимальный diff
- F-01. Каталог `corp/` в корне монорепо: `corp/config/opencode.corp.json` (провайдер
  `magnit_prod`, модель, лимиты, `autoupdate:false`, `enabled_providers`, `share:"disabled"`,
  адрес Hub), `corp/README.md`, `corp/patches.md` (перечень наших изменений upstream с
  причинами — для rebase). Умолчания попадают в бинарник build-константами (`define`
  `OPENCODE_CORP_HUB_URL`, `OPENCODE_CORP_CONFIG`) и применяются как слой конфигурации с
  приоритетом ниже `.well-known` (пользовательский конфиг всегда выше). При отсутствии
  констант поведение = upstream (форк можно собрать «ванильным»).
- F-02. Все изменения upstream-кода — отдельные коммиты с префиксом `corp:`; тесты — рядом с
  изменяемыми модулями (bun test / vitest как в пакете); `bun run typecheck` чисто.

### 3.2. Вход по SSO при первом запуске
- F-03. Если нет ключа провайдера `magnit_prod` (auth store) — TUI и Desktop показывают
  экран «Войти через корпоративный SSO» вместо upstream-списка провайдеров (upstream-список
  доступен по кнопке «Другой провайдер»). Логика: сервер (`packages/opencode/src/…`) вызывает
  Hub `POST /cli/start` → отдаёт UI URL для браузера и код; UI открывает браузер
  (`open`), показывает код и ждёт; сервер poll'ит `GET /cli/poll/{login_id}`; при
  `requires_team_selection` UI показывает выбор команды (`POST …/team`); по `ready` ключ
  сохраняется в auth store провайдера `magnit_prod` (тип `api`), провайдер активируется без
  перезапуска. Тот же метод виден в `/connect` → «LiteLLM Copilot prod» → «Корпоративный
  SSO». Реализация — предпочтительно **plugin-hook `auth`** (встроенный корп-плагин в
  `corp/plugin/`), чтобы не трогать провайдерный код upstream; если хука недостаточно для
  first-run экрана — минимальная правка `app.tsx`/`layout.tsx`.
- F-04. CLI: `opencode corp login [--hub URL]` (тот же флоу без TUI), `opencode corp status`
  (Hub, пользователь, ключ есть/нет, версия каталога).

### 3.3. Витрина коннекторов
- F-05. Команда `/connectors` (TUI: `slashName: "connectors"` в `app.tsx`; Desktop/web:
  команда + пункт меню; палитра/кейбинд) открывает витрину: карточки из Hub `GET /api/catalog`
  (+ `GET /api/me/connections`, Bearer = ключ `magnit_prod`): название, описание, владелец,
  статус (`не подключён | подключён | требуется авторизация | недоступен (unconfigured/
  deprecated)`), выбранный пресет прав; поиск; группировка по владельцу; пустое состояние
  и состояние «Hub недоступен» (кэш последнего ответа на диск, TTL 24 ч).
- F-06. «Подключить»: если сервера нет в конфиге — добавить `mcp.<alias>` (`type: remote`,
  `url` из каталога, `oauth: { scope: <scopes пресета> }`, `enabled: true`) в **глобальный**
  конфиг пользователя через существующий `Config.updateGlobal` (не только runtime), затем
  запустить стандартный MCP-OAuth флоу сервера (`POST /mcp/:name/auth/authenticate` — браузер,
  callback 19876); статус в витрине обновляется по `GET /mcp`. Для TUI это добавляет
  недостающий запуск OAuth (в upstream его в TUI нет).
- F-07. «Отключить»: `DELETE /mcp/:name/auth` + `POST /mcp/:name/disconnect` + `enabled:false`
  в глобальном конфиге; «Права»: смена пресета = обновление `oauth.scope` и повторная
  авторизация (для facade-серверов Hub права меняются на странице Hub — витрина показывает
  ссылку). «Открыть в Hub» — ссылка на карточку.
- F-08. `/mcps` продолжает работать; в его списке серверы каталога показывают
  человеческие названия (из каталога) рядом с alias.

### 3.4. Локализация
- F-09. Витрина, экран SSO и наши сообщения — русский по умолчанию, английский как fallback:
  в `packages/app` — через существующий i18n (`ru.ts`/`en.ts`, тест паритета ключей проходит);
  в TUI (без i18n) — минимальный словарь `corp/tui-i18n.ts` только для наших экранов.

### 3.5. Сборки и обновление от upstream
- F-10. Скрипт `corp/build.sh|ts`: CLI для darwin-arm64/x64, linux-x64, win32-x64 (артефакты
  `dist/opencode-<os>-<arch>` + zip), Desktop для mac-arm64 и win-x64 (electron-builder),
  версия = upstream + `-magnit.N`; воспроизводимо на этом Mac (Windows-сборка Desktop —
  на Windows-стенде или CI). Публикация — GitHub Releases приватного репо (для портала).
- F-11. Скрипт `corp/upstream-sync.sh`: `git fetch upstream` → rebase `main` на выбранный
  тег → прогон `bun run typecheck` и наших тестов → отчёт о конфликтах. Документ
  `corp/patches.md` поддерживается актуальным.
- F-12. CI (GitHub Actions в форке): typecheck + наши тесты + сборка CLI для трёх ОС на PR
  в `main`; Desktop — по тегу.

### 3.6. Тесты
- F-13. Модульные тесты: клиент Hub (catalog/connections/login с моками HTTP), логика витрины
  (маппинг статусов, кэш), персист конфига при подключении/отключении, SSO-флоу (мок Hub);
  smoke: сборка CLI запускается с `--version` и `opencode corp status --hub <mock>`.

## 4. Открытые вопросы [проверить]
1. Достаточно ли plugin-hook `auth` для экрана first-run (или нужна правка `app.tsx`).
2. Точный формат имён MCP-инструментов для `permission` (`alias_tool` vs `alias-tool`) —
   влияет на Hub `remote-config`.
3. Подпись сборок (Apple Developer ID / Authenticode) — вне репо, решение ИБ.
