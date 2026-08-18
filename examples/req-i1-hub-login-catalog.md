# Требование I-1: Hub — вход по SSO, постоянный ключ LiteLLM, каталог, well-known / remote-config

Источник: `docs/req-mvp.md` (rev 0.2) — §3 (факты), §4, §6.2 (R-01, R-02), §6.3 (R-03…R-07),
§7, §8. Это первая итерация Hub. Всё ниже реализуется в `src/hub/` (Python 3.12, FastAPI,
async), тесты — только против локальных моков (без обращений к боевым системам).

## 1. Цель итерации

Hub умеет: (1) провести пользователя через SSO-вход LiteLLM из любого клиента (форк
OpenCode или CLI-хелпер) и выдать ему **постоянный** ключ LiteLLM; (2) идентифицировать
пользователя по этому ключу; (3) отдавать каталог MCP-серверов из `catalog.yaml` для витрины и
для стандартного OpenCode (`/.well-known/opencode` + персональный `/remote-config`).
Подключения серверов (OAuth-фасад, proxy) — следующая итерация; здесь только модель данных и
пустые ответы для них.

## 2. Конфигурация (env, pydantic-settings, префикс `HUB_`)

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `HUB_PUBLIC_URL` | публичный адрес Hub (в ссылках, well-known) | обязательна |
| `HUB_LITELLM_BASE_URL` | база LiteLLM | обязательна |
| `HUB_LITELLM_MODEL` | имя модели для провайдера | `MagnitCopilot` |
| `HUB_LITELLM_PROVIDER_ID` / `HUB_LITELLM_PROVIDER_NAME` | id/название провайдера в конфиге OpenCode | `magnit_prod` / `LiteLLM Copilot prod` |
| `HUB_LITELLM_CONTEXT_LIMIT` / `HUB_LITELLM_OUTPUT_LIMIT` | лимиты модели | 250000 / 8192 |
| `HUB_CATALOG_PATH` | путь к `catalog.yaml` | `./catalog.yaml` |
| `HUB_DATABASE_URL` | SQLAlchemy async URL | `sqlite+aiosqlite:///./hub.db` |
| `HUB_REDIS_URL` | Redis; пусто → in-memory кэш (dev/тесты) | пусто |
| `HUB_SECRET_KEY` | подпись/секреты (poll-секреты, будущие JWT) | обязательна (в тестах задаётся) |
| `HUB_ENCRYPTION_KEY` | шифрование токенов систем (следующая итерация; здесь валидируется формат) | обязательна |
| `HUB_WELLKNOWN_AUTH_COMMAND` | JSON-массив команды для `auth.command` в well-known | `["opencode","corp","login","--hub","<HUB_PUBLIC_URL>"]` |
| `HUB_WELLKNOWN_ENV_NAME` | имя env-переменной токена в well-known | `MAGNIT_COPILOT_KEY` |
| `HUB_LOGIN_SESSION_TTL` | TTL сессии входа, с | 600 |
| `HUB_KEY_ALIAS_PREFIX` | префикс alias создаваемых ключей | `opencode` |
| `HUB_LOG_LEVEL` | уровень логов | `INFO` |

Секреты (`*_KEY`, client_secret) никогда не попадают в логи и ответы.

## 3. Каталог (`catalog.yaml`)

Формат — как в `catalog.yaml` репозитория (см. файл). Требования к загрузчику:

- R-C1. Загрузка и валидация схемы (pydantic): обязательные поля `version`, `servers[]`, у
  сервера — `alias` (уникален, `^[a-z][a-z0-9-]{1,31}$`), `title`, `description`, `owner`,
  `status ∈ {beta, ga, deprecated}`, `audience` (список групп; `all` — всем), `mode ∈ {native,
  facade}`; для `native` обязателен `mcp_url`; для `facade` — `upstream_url`, `auth`
  (`type: oauth2`, `authorize_url`, `token_url`, `client_id`, `client_secret`, `pkce`,
  `scopes{readonly, readwrite}`), `credential_headers`; `permission_model.kind ∈ {header_groups,
  consent, tool_filter}` с полями по виду. Невалидный каталог — ошибка старта с понятным
  сообщением (путь к полю).
- R-C2. Подстановка `${VAR}` из окружения в строковых значениях (отсутствие переменной —
  ошибка загрузки с именем переменной, кроме полей, помеченных как опциональные, например
  `client_id` у сервера со статусом `beta`, тогда сервер помечается `unconfigured` и не
  показывается пользователям, но валиден). Значения вида `env:VAR` (секреты) хранятся как
  ссылки: читаются лениво, никогда не сериализуются наружу.
- R-C3. Поддержка `{ $ref: "#/servers/<alias>/<поле>" }` внутри файла (одна ступень).
- R-C4. Перечитывание без рестарта: `POST /admin/catalog/reload` (защищено заголовком
  `X-Admin-Token` = `HUB_ADMIN_TOKEN`; отсутствие переменной → эндпоинт отключён) — атомарная
  замена; при ошибке валидации остаётся прежний каталог, ответ 400 с ошибкой.
- R-C5. `mcp-hub catalog validate [--path]` — CLI-проверка каталога, код выхода 0/1.
- R-C6. Публичное представление сервера (для API/витрины) не содержит секретов, `client_secret`,
  `credential_headers`, `upstream_url`; содержит `alias, title, description, owner, contact,
  docs_url, status, mode, mcp_url` (для facade — `<HUB_PUBLIC_URL>/mcp/<alias>`; для native —
  `mcp_url`), `permission_model` (виды/группы/пресеты) и `auth_kind` (`oauth2`).

## 4. Вход и постоянный ключ LiteLLM

Факты (проверено 2026-08-18): LiteLLM `POST /sso/cli/start` → `{login_id, poll_secret,
user_code, expires_in}`; браузер `GET {LITELLM}/sso/key/generate?source=litellm-cli&key=<login_id>`;
`GET /sso/cli/poll/<login_id>` с заголовком `x-litellm-cli-poll-secret` → `{status:"pending"}` |
`{status:"ready", requires_team_selection:true, teams:[…], team_details:[{team_id, team_alias}]}` |
`{status:"ready", key:<JWT>, user_id, team_id, teams}`; повторный poll с `?team_id=` после выбора;
`POST /key/generate` с `Authorization: Bearer <JWT>` создаёт ключ (`{"key": "sk-…", …}`).

- R-L1. `POST /cli/start` (без аутентификации; rate-limit по IP: 30/мин) → `{login_id,
  poll_secret, browser_url, user_code, expires_in}`. Hub создаёт **свою** сессию входа
  (`login_id` Hub ≠ login_id LiteLLM), вызывает LiteLLM `/sso/cli/start`, сохраняет связку в
  хранилище сессий (Redis или память) с TTL; `browser_url` = URL LiteLLM для браузера;
  `user_code` — от LiteLLM (клиент показывает его пользователю). Ошибка LiteLLM → 502 с
  `{error:"litellm_unavailable"}`.
- R-L2. `GET /cli/poll/{login_id}` с заголовком `X-Hub-Poll-Secret` (несовпадение → 403;
  неизвестный/истёкший id → 404 `{error:"login_expired"}`) → `{status:"pending"}` |
  `{status:"team_selection_required", teams:[{team_id, team_alias}]}` |
  `{status:"ready", key, key_kind:"persistent"|"jwt", user:{user_id, email}, team_id}`.
  Hub опрашивает LiteLLM не чаще 1 раза в 2 с на сессию (кэш последнего ответа).
- R-L3. `POST /cli/poll/{login_id}/team` `{team_id}` (тот же секрет) — выбор команды
  пользователем; `team_id` должен быть из списка, иначе 400. После выбора poll продолжает.
  Правило: если команда одна — выбирается автоматически; если несколько — **всегда**
  спрашивать пользователя (никакого «первая по умолчанию»).
- R-L4. Получив JWT, Hub вызывает LiteLLM `POST /key/generate` (`key_alias`
  `"<prefix>-<user_id>-<yyyymmdd-hhmm>"`, `metadata: {source: "opencode-mcp-hub", client:
  <client из /cli/start, если передан>}`, `team_id` при наличии) и возвращает клиенту
  постоянный ключ (`key_kind: persistent`). Если `/key/generate` вернул 4xx (нет права) —
  Hub возвращает JWT (`key_kind: jwt`, `expires_in` из JWT `exp`), логирует предупреждение
  один раз на сессию, и это не ошибка. Секрет и ключ в логи не попадают.
- R-L5. Hub сохраняет `sha256(key)`, `user_id`, `email` (из JWT/`user_id` LiteLLM),
  `key_kind`, `key_alias`, `created_at`, `client` в таблице `api_keys`; пользователь —
  в `users` (upsert по `user_id`). Ключ в открытом виде не хранится. Повторный вход того же
  пользователя добавляет новую запись; прежние остаются валидными (отзыв — вне итерации).
- R-L6. Аутентификация запросов: `Authorization: Bearer <key>` → `sha256` → `api_keys` →
  пользователь (кэш в Redis/памяти на 60 с). Нет/неверный → 401
  `{error:"unauthorized", hint:"выполните вход: opencode corp login"}`.
  Также принимается `x-litellm-api-key` (совместимость).
- R-L7. Сессия входа одноразовая: после `ready` запись удаляется; `login_id` истекает по TTL.

## 5. API для витрины и стандартного OpenCode

- R-A1. `GET /health` (без аутентификации) → `{status:"ok", version, catalog_version,
  time}`; `GET /ready` — 200 только если БД доступна и каталог загружен.
- R-A2. `GET /api/me` (Bearer) → `{user_id, email, key_kind, created_at}`.
- R-A3. `GET /api/catalog` (Bearer) → `{version, servers:[публичное представление +
  connection:{status:"not_connected"|"connected"|"needs_reauth", preset, updated_at}]}`;
  фильтр по `audience` (группы пользователя; в этой итерации у пользователя группы `["all"]`,
  расширяется позже) и `status != deprecated` для `?include_deprecated=false` (по умолчанию
  deprecated показываются с признаком). `unconfigured` серверы скрыты.
- R-A4. `GET /api/me/connections` (Bearer) → список подключений пользователя (в этой
  итерации пустой список; таблица `connections(user_id, alias, status, preset, groups,
  created_at, updated_at)` создаётся миграцией/`create_all`).
- R-A5. `GET /.well-known/opencode` (без аутентификации, `Cache-Control: public, max-age=300`,
  `ETag`) → `{auth:{command, env}, config:{...}, remote_config:{url:"<HUB>/remote-config",
  headers:{Authorization:"Bearer {env:<ENV_NAME>}"}}}`, где `config` = `$schema`,
  `autoupdate:false`, `enabled_providers:[<provider_id>]`, `provider.<id>` (npm
  `@ai-sdk/openai-compatible`, name, options.baseURL=`<LITELLM>/v1`, `options.apiKey =
  "{env:<ENV_NAME>}"`, models.<model>.limit), `mcp.<alias>` для каждого видимого сервера
  каталога: `{type:"remote", url:<mcp_url публичный>, enabled:false, oauth:{}}` (для facade —
  `oauth:{}`; для native — `oauth:{}`) — без заголовков с секретами.
- R-A6. `GET /remote-config` (Bearer) → `{config:{mcp:{…}, permission:{…}, tools:{…}}}`:
  для каждого **подключённого** сервера `mcp.<alias>.enabled=true` (в этой итерации
  подключений нет → пустые объекты, ответ 200). `Cache-Control: private, no-store`.
- R-A7. Все ответы JSON, ошибки — `{error, message?, hint?}`; CORS не включён (не нужен);
  заголовки безопасности `X-Content-Type-Options: nosniff`.

## 6. Хранилище, кэш, наблюдаемость

- R-S1. SQLAlchemy 2.x async; SQLite (`aiosqlite`) по умолчанию, Postgres через тот же код;
  создание схемы при старте (`create_all`; Alembic — позже). Таблицы: `users`, `api_keys`,
  `connections`, `audit_log(ts, user_id?, action, alias?, details JSON без секретов)`.
- R-S2. Сессии входа и кэш ключей — интерфейс `KeyValueStore` с двумя реализациями:
  in-memory (TTL) и Redis; выбор по `HUB_REDIS_URL`.
- R-S3. Аудит: `login_started`, `login_completed(key_kind)`, `catalog_reloaded`.
- R-S4. JSON-логи (structlog не обязателен — стандартный logging с JSON-форматтером),
  `request_id` на каждый запрос (заголовок `X-Request-ID`, генерируется при отсутствии),
  без секретов. `/metrics` в формате Prometheus: счётчики запросов по эндпоинту/статусу,
  гистограмма латентности, число активных сессий входа.
- R-S5. Приложение создаётся фабрикой `hub.app:create_app(settings=None)`; HTTP-клиент к
  LiteLLM инжектируется (для тестов — `respx`); CLI `mcp-hub serve` (uvicorn) и
  `mcp-hub catalog validate`.

## 7. Нефункциональные и тестовые ограничения

- Никаких сетевых вызовов в тестах: LiteLLM мокается (`respx`), Redis — in-memory реализация,
  БД — SQLite в памяти/tmp.
- Покрытие новых строк ≥ 90 %, mutation ≥ 70 % (пороги конвейера), ruff/mypy чисто.
- Ответы `/cli/*` не должны содержать `poll_secret` LiteLLM; поллинг LiteLLM — только через Hub.
- Все пользовательские строки/подсказки — на русском.

## 8. Вне этой итерации (не реализовывать, но не мешать)

OAuth-фасад (`/mcp/<alias>`, `/oauth/*`), proxy, страницы UI/Keycloak, права/группы в
`remote-config`, отзыв ключей, Alembic-миграции.
