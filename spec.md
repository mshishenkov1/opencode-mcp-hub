# Спецификация I-1: Hub — вход по SSO, постоянный ключ LiteLLM, каталог, well-known / remote-config

Источник: `examples/req-i1-hub-login-catalog.md` (на основе `docs/req-mvp.md` rev 0.2, §3, §4, §6.2, §6.3, §7, §8).
Реализация — `src/hub/` (Python 3.12, FastAPI, async). Тесты — только против локальных моков.

> **Ревизия 1.1 (2026-08-19).** Точечная правка по `reports/test-report-i1.md` §5 (противоречия внутри спеки;
> нумерация AC не менялась, требования не ослаблены):
> 1. **AC-59 ↔ R-A5/AC-58.** Плейсхолдеры OpenCode `{env:<HUB_WELLKNOWN_ENV_NAME>}` в `/.well-known/opencode`
>    (`provider.*.options.apiKey`, `remote_config.headers`) — штатный синтаксис и **разрешены**; запрет касается
>    ссылок каталога вида `env:VAR` (например `client_secret`) и значений секретов. Уточнены R-K3, R-C2, R-A5 и AC-59.
> 2. **R-C1 ↔ AC-54/AC-55.** Регэксп alias приведён к `^[a-z][a-z0-9-]{0,31}$` (1–32 символа): односимвольные
>    alias (`a`) допустимы; невалидные (`Bad_Alias`, `1abc`, `-x`, `ABC`, пробелы, 33 символа) отклоняются как прежде.
> 3. **R-L9 ↔ R-L1.** `login_id` LiteLLM допустим только внутри `browser_url` ответа `/cli/start` (значение `key=`,
>    предназначенное браузеру); `poll_secret` LiteLLM и прочие внутренние идентификаторы не раскрываются нигде.

## 1. Назначение

Hub первой итерации умеет:

1. провести пользователя любого клиента (форк OpenCode, CLI-хелпер) через CLI-SSO LiteLLM и выдать ему
   **постоянный** ключ LiteLLM (или JWT как запасной вариант);
2. аутентифицировать запросы к своему API по этому ключу;
3. отдавать каталог MCP-серверов из `catalog.yaml` — для витрины (`/api/catalog`) и для стандартного
   OpenCode (`/.well-known/opencode` + персональный `/remote-config`).

Подключения серверов (OAuth-фасад, proxy) — следующая итерация: здесь только модель данных
(`connections`) и ответы, построенные на её содержимом (в этой итерации — пустом).

## 2. Входы и выходы

**Входы:** переменные окружения `HUB_*`; файл `catalog.yaml`; HTTP-запросы клиентов; ответы LiteLLM
(`/sso/cli/start`, `/sso/cli/poll/{id}`, `/key/generate`).

**Выходы:** HTTP JSON-ответы; записи в БД (`users`, `api_keys`, `connections`, `audit_log`);
записи в KeyValueStore (сессии входа, кэш ключей, счётчики rate-limit); JSON-логи; метрики Prometheus;
код выхода CLI.

## 3. Правила поведения

### 3.1. Конфигурация (R-K*)

- **R-K1. Загрузка настроек.** Настройки читаются через pydantic-settings из окружения с префиксом `HUB_`.
  Обязательные переменные: `HUB_PUBLIC_URL`, `HUB_LITELLM_BASE_URL`, `HUB_SECRET_KEY`, `HUB_ENCRYPTION_KEY`.
  Отсутствие любой из них → `create_app()` завершается ошибкой, текст которой содержит имя переменной
  (`HUB_…`). Значения по умолчанию — по таблице:

  | Переменная | По умолчанию |
  |---|---|
  | `HUB_LITELLM_MODEL` | `MagnitCopilot` |
  | `HUB_LITELLM_PROVIDER_ID` / `HUB_LITELLM_PROVIDER_NAME` | `magnit_prod` / `LiteLLM Copilot prod` |
  | `HUB_LITELLM_CONTEXT_LIMIT` / `HUB_LITELLM_OUTPUT_LIMIT` | `250000` / `8192` |
  | `HUB_CATALOG_PATH` | `./catalog.yaml` |
  | `HUB_DATABASE_URL` | `sqlite+aiosqlite:///./hub.db` |
  | `HUB_REDIS_URL` | пусто → in-memory KeyValueStore |
  | `HUB_ADMIN_TOKEN` | пусто → `/admin/*` отключён |
  | `HUB_WELLKNOWN_AUTH_COMMAND` | `["opencode","corp","login","--hub","<HUB_PUBLIC_URL>"]` |
  | `HUB_WELLKNOWN_ENV_NAME` | `MAGNIT_COPILOT_KEY` |
  | `HUB_LOGIN_SESSION_TTL` | `600` (секунд, целое > 0) |
  | `HUB_KEY_ALIAS_PREFIX` | `opencode` |
  | `HUB_LOG_LEVEL` | `INFO` |

- **R-K2. Валидация форматов.**
  - `HUB_ENCRYPTION_KEY` — ключ Fernet: 44 символа urlsafe-base64, декодирующиеся в 32 байта. Иной формат →
    ошибка старта с именем переменной (сам ключ в этой итерации не используется).
  - `HUB_WELLKNOWN_AUTH_COMMAND` — строка JSON, разбираемая в непустой массив строк; иначе — ошибка старта
    с именем переменной. Подстрока `<HUB_PUBLIC_URL>` в элементах заменяется на нормализованный `HUB_PUBLIC_URL`.
  - `HUB_PUBLIC_URL`, `HUB_LITELLM_BASE_URL` — нормализуются: завершающий `/` удаляется.
  - `HUB_LOGIN_SESSION_TTL`, лимиты модели — целые > 0; иначе — ошибка старта.
- **R-K3. Секреты не утекают.** `HUB_SECRET_KEY`, `HUB_ENCRYPTION_KEY`, `HUB_ADMIN_TOKEN`, ключи LiteLLM
  (`sk-…`, JWT), poll-секреты (Hub и LiteLLM), `client_secret`, значения `env:VAR` (и сами ссылки каталога
  `env:VAR`) никогда не попадают в логи, `audit_log.details`, ответы `/health`, `/api/*`, `/.well-known/opencode`,
  `/remote-config`, `/metrics`. Плейсхолдер OpenCode `{env:<HUB_WELLKNOWN_ENV_NAME>}` в well-known (R-A5) секретом
  не является: он ссылается на переменную окружения клиента и не связан со ссылками `env:VAR` каталога (ревизия 1.1).
- **R-K4. Фабрика приложения.** `hub.app:create_app(settings=None)`: при `settings=None` настройки
  читаются из окружения; при переданном объекте `Settings` окружение не требуется. HTTP-клиент к LiteLLM
  (`httpx.AsyncClient`) — инжектируемый (`create_app(settings, litellm_client=...)` либо через атрибут
  состояния приложения), чтобы тесты подменяли его `respx`.

### 3.2. Каталог (R-C*)

- **R-C1. Схема.** Каталог — YAML c обязательными полями `version` (целое ≥ 1) и `servers` (список, может
  быть пустым); необязательное `defaults` (произвольный объект, отдаётся как есть внутри Hub, наружу не
  требуется). Сервер:

  | Поле | Обязательность | Ограничение |
  |---|---|---|
  | `alias` | обяз. | `^[a-z][a-z0-9-]{0,31}$` (1–32 символа: строчная латинская буква, далее строчные буквы/цифры/`-`; ревизия 1.1 — прежнее `{1,31}` требовало ≥ 2 символов и противоречило AC-54/AC-55), уникален в файле |
  | `title`, `description`, `owner` | обяз. | непустые строки |
  | `contact`, `docs_url`, `icon` | опц. | строки |
  | `status` | обяз. | `beta` \| `ga` \| `deprecated` |
  | `audience` | обяз. | непустой список строк; `all` — всем |
  | `mode` | обяз. | `native` \| `facade` |
  | `mcp_url` | обяз. для `native` | строка (URL) |
  | `upstream_url` | обяз. для `facade` | строка (URL) |
  | `auth` | обяз. для `facade` | `type: oauth2`, `authorize_url`, `token_url`, `client_id`, `client_secret`, `pkce` (bool), `scopes{readonly:[…], readwrite:[…]}`; опц. `revoke_url` |
  | `credential_headers` | обяз. для `facade` | непустой объект `имя → шаблон` |
  | `static_headers` | опц. | объект `имя → строка` |
  | `permission_model` | обяз. | `kind ∈ {header_groups, consent, tool_filter}` (см. ниже) |

  Поля `permission_model` по виду:
  - `header_groups`: `header` (строка, обяз.), `always` (список строк, опц., по умолчанию `[]`),
    `groups` (непустой список `{id, title, preset ∈ {readonly, readwrite, none}}`, `id` уникальны);
  - `consent`: `presets` (объект `имя_пресета → объект`, обяз., непустой);
  - `tool_filter`: `presets` (объект `имя_пресета → {tools: [строки]}`, обяз., непустой).

  Лишние неизвестные поля у сервера/auth/permission_model — ошибка (строгая схема). Любая ошибка схемы
  → ошибка старта (`create_app` не создаётся) с сообщением на русском, содержащим путь к полю в форме
  `servers[<i>].<поле>[.<подполе>]` (например `servers[2].auth.client_id`), а для дубликата alias — сам alias.
  Для `mcp-hub catalog validate` — то же сообщение и код выхода 1.

- **R-C2. Подстановка `${VAR}` и ссылки `env:VAR`.**
  - В любом строковом значении (включая вложенные списки/объекты) вхождения `${VAR}` заменяются значением
    переменной окружения `VAR`; в одной строке может быть несколько вхождений.
  - Отсутствующая переменная: если сервер, в котором она встречается, имеет `status: beta` — сервер
    помечается `unconfigured` (каталог валиден, сервер не показывается пользователям и не попадает в
    well-known); иначе (`ga`, `deprecated`, вне `servers`) — ошибка загрузки с именем переменной и путём к полю.
  - Значение, целиком имеющее вид `env:VAR`, — ссылка на секрет: допустима только в
    `auth.client_secret`, значениях `credential_headers` и `static_headers`; в других полях — ошибка схемы.
    Ссылка хранится как объект-ссылка, значение читается лениво при использовании; отсутствие переменной
    окружения при загрузке — не ошибка. Наружу (API, well-known, логи, `repr`) ни имя переменной с
    префиксом `env:`, ни значение не сериализуются. Это правило относится именно к ссылкам каталога `env:VAR`;
    оно не запрещает плейсхолдеры OpenCode `{env:<HUB_WELLKNOWN_ENV_NAME>}`, которые Hub сам формирует в
    well-known по R-A5 (ревизия 1.1).
- **R-C3. `$ref` внутри файла.** Значение поля сервера (на любой глубине) вида `{ $ref: "#/servers/<alias>/<поле>" }`
  заменяется копией одноимённого поля верхнего уровня сервера `<alias>` (одна ступень: целевое значение
  само не может быть `$ref` и не может содержать `$ref`). Порядок обработки: разбор YAML → `$ref` →
  `${VAR}` → валидация схемы. Неверный формат ссылки, неизвестный alias/поле, вложенный `$ref` → ошибка
  загрузки с путём к полю и текстом ссылки.
- **R-C4. Перечитывание.** `POST /admin/catalog/reload`. Если `HUB_ADMIN_TOKEN` пуст — эндпоинт отключён:
  404 `{error:"not_found"}`. Заголовок `X-Admin-Token` отсутствует или не равен `HUB_ADMIN_TOKEN` → 403
  `{error:"forbidden"}`. Успех → файл `HUB_CATALOG_PATH` перечитан по правилам R-C1–R-C3, каталог заменён
  атомарно (все последующие запросы видят новый), 200 `{status:"ok", catalog_version, servers:<число>}`,
  запись аудита `catalog_reloaded`. Ошибка загрузки (нет файла, YAML, схема, `${VAR}`, `$ref`) → 400
  `{error:"catalog_invalid", message:"<текст с путём>"}`, прежний каталог остаётся действующим.
- **R-C5. CLI.** `mcp-hub catalog validate [--path <файл>]` (по умолчанию `HUB_CATALOG_PATH`, если задан,
  иначе `./catalog.yaml`); не требует остальных `HUB_*`. Валидный каталог → печатает `OK`, версию и число
  серверов (с пометкой `unconfigured`), код 0. Невалидный → печатает сообщение ошибки (с путём к полю), код 1.
  Отсутствие файла → код 1.
- **R-C6. Публичное представление сервера.** Объект `{alias, title, description, owner, contact, docs_url,
  status, mode, mcp_url, permission_model, auth_kind}`:
  - `mcp_url`: для `native` — значение из каталога; для `facade` — `<HUB_PUBLIC_URL>/mcp/<alias>`;
  - `auth_kind`: `"oauth2"` для обоих режимов;
  - `permission_model`: `{kind}` + для `header_groups`: `groups:[{id,title,preset}]`, `always:[…]`;
    для `consent` и `tool_filter`: `presets` (как в каталоге). Поле `header` не отдаётся;
  - отсутствуют: `upstream_url`, `auth` (включая `client_id`, `client_secret`), `credential_headers`,
    `static_headers`, любые значения `env:VAR`; `contact`/`docs_url` могут быть `null`.
  - `unconfigured` серверы никогда не попадают в публичные списки.

### 3.3. Вход и постоянный ключ LiteLLM (R-L*)

- **R-L1. `POST /cli/start`.** Без аутентификации. Тело — необязательный JSON `{client?: string ≤ 128}`
  (пустое тело допустимо; иное тело → 400 `invalid_request`). Hub:
  1. генерирует собственные `login_id` (uuid4) и `poll_secret` (≥ 32 байт случайности, urlsafe);
  2. вызывает LiteLLM `POST {LITELLM}/sso/cli/start`; ответ `{login_id, poll_secret, user_code, expires_in}`;
  3. сохраняет сессию в KeyValueStore под ключом `login:<login_id>` с TTL =
     `min(HUB_LOGIN_SESSION_TTL, expires_in LiteLLM)` (если LiteLLM не вернул `expires_in` — `HUB_LOGIN_SESSION_TTL`);
  4. пишет аудит `login_started` (details: `client`, без секретов);
  5. отвечает 200 `{login_id, poll_secret, browser_url, user_code, expires_in}`, где
     `browser_url = <LITELLM>/sso/key/generate?source=litellm-cli&key=<login_id LiteLLM>`, `expires_in` = TTL из п. 3.
  `login_id`/`poll_secret` Hub ≠ значениям LiteLLM; значения LiteLLM в ответ не попадают.
  Сетевая ошибка/тайм-аут/5xx/невалидный ответ LiteLLM → 502 `{status:"error", error:"litellm_unavailable", message}`; сессия не создаётся.
- **R-L2. `GET /cli/poll/{login_id}`.** Заголовок `X-Hub-Poll-Secret`. Порядок проверок: сессия неизвестна
  или истекла → 404 `{status:"error", error:"login_expired", message}`; секрет отсутствует/не совпадает →
  403 `{status:"error", error:"forbidden"}`. Далее Hub опрашивает LiteLLM
  `GET {LITELLM}/sso/cli/poll/<login_id LiteLLM>` с заголовком `x-litellm-cli-poll-secret` (и `?team_id=` после
  выбора команды) и отвечает одним из:
  - `{status:"pending"}`;
  - `{status:"team_selection_required", teams:[{team_id, team_alias}]}`;
  - `{status:"ready", key, key_kind:"persistent"|"jwt", user:{user_id, email}, team_id, expires_in?}`
    (`expires_in` — только для `jwt`; `team_id` может быть `null`).
  Ошибки LiteLLM: сеть/тайм-аут/5xx → 502 `{status:"error", error:"litellm_unavailable"}`, сессия жива;
  4xx → сессия удаляется, 404 `login_expired`; неразбираемое/неожиданное тело (нет `status`, `ready` без
  `key` и без списка команд, `requires_team_selection:true` с пустым списком команд) → 502
  `{status:"error", error:"litellm_invalid_response"}`, сессия жива.
- **R-L3. Выбор команды.** При `requires_team_selection:true` список команд берётся из `team_details`
  (`[{team_id, team_alias}]`; при отсутствии — из `teams` как `team_alias = team_id`):
  - ровно одна команда — выбирается автоматически: Hub в том же запросе повторно опрашивает LiteLLM с
    `?team_id=` (без ожидания 2 с) и обрабатывает результат; ответ `team_selection_required` клиенту не отдаётся;
  - две и более — Hub сохраняет список в сессии и отдаёт `team_selection_required`; последующие poll без
    выбора возвращают тот же ответ (LiteLLM не опрашивается до выбора);
  - ноль команд — см. R-L2 (`litellm_invalid_response`). Ответ `ready` без `team_id` (у пользователя нет
    команд) — нормален: ключ создаётся без `team_id`.
  `POST /cli/poll/{login_id}/team` с телом `{team_id}` и тем же `X-Hub-Poll-Secret`: проверки как в R-L2
  (404/403); если сессия не в состоянии выбора команды → 409 `{status:"error", error:"team_selection_not_required"}`;
  тело невалидно → 400 `invalid_request`; `team_id` не из сохранённого списка → 400
  `{status:"error", error:"invalid_team", message}`; успех → команда сохранена, кэш poll сброшен,
  200 `{status:"pending"}`; следующий `GET /cli/poll` опрашивает LiteLLM с `?team_id=<выбранный>`.
  Правило «первая по умолчанию» запрещено.
- **R-L4. Постоянный ключ.** Получив от LiteLLM `{status:"ready", key:<JWT>, user_id, team_id?}`, Hub
  определяет `user_id` (`user_id` из ответа, иначе claim `user_id`/`sub` JWT; если не определён —
  `litellm_invalid_response`) и `email` (claim `email` JWT; иначе `user_id`, если содержит `@`; иначе `null`),
  затем вызывает `POST {LITELLM}/key/generate` с `Authorization: Bearer <JWT>` и телом
  `{key_alias:"<HUB_KEY_ALIAS_PREFIX>-<user_id>-<yyyymmdd-hhmm UTC>", metadata:{source:"opencode-mcp-hub", client?:<client из /cli/start>}, team_id?}`
  (`client` и `team_id` — только если есть):
  - 2xx с полем `key` → `key_kind:"persistent"`, клиенту отдаётся `key` из ответа;
  - 4xx (любой) → `key_kind:"jwt"`, клиенту отдаётся JWT, `expires_in = max(0, exp − now)` из claim `exp`
    (нет `exp`/не декодируется → `expires_in: null`); предупреждение в лог один раз на сессию; это не ошибка;
  - 5xx/сеть/тайм-аут/2xx без `key` → 502 `{status:"error", error:"litellm_unavailable"}`; сессия остаётся
    живой (JWT сохранён в ней), повторный poll (не чаще R-L10) повторяет только `/key/generate`, не SSO-poll.
  JWT декодируется без проверки подписи (только чтение claims).
- **R-L5. Сохранение.** Перед ответом `ready` Hub в одной транзакции: upsert `users` по `user_id`
  (`email`, `groups=["all"]`, `updated_at`); вставка `api_keys` (`key_sha256` = sha256(ключ) hex в нижнем
  регистре, `user_id`, `key_kind`, `key_alias`, `client`, `created_at`, `expires_at` для jwt); аудит
  `login_completed` с details `{key_kind, key_alias, team_id, client}`. Ключ в открытом виде не хранится.
  Повторный вход того же пользователя добавляет новую запись `api_keys`; прежние ключи остаются валидными.
- **R-L6. Аутентификация Bearer.** Для `/api/*` и `/remote-config`: ключ берётся из `Authorization: Bearer <key>`,
  иначе из `x-litellm-api-key` (если есть оба — `Authorization`). Ключ → sha256 → `api_keys` → пользователь;
  положительный результат кэшируется в KeyValueStore на 60 с (`keyauth:<sha256>`), отрицательный — не кэшируется.
  Нет заголовка / не `Bearer` / неизвестный ключ → 401 `{error:"unauthorized", message, hint:"выполните вход: opencode corp login"}`,
  заголовок `WWW-Authenticate: Bearer`.
- **R-L7. Одноразовость сессии.** После ответа `ready` запись сессии удаляется: повторный poll/team →
  404 `login_expired`. Сессия истекает по TTL независимо от активности клиента.
- **R-L8. Rate-limit `/cli/start`.** 30 запросов в 60 с на IP клиента (скользящее окно, счётчик в
  KeyValueStore под `rl:cli_start:<ip>`; IP = адрес соединения, `X-Forwarded-For` не учитывается в этой
  итерации). 31-й запрос в окне → 429 `{error:"rate_limited", message}` с заголовком `Retry-After` (секунды
  до освобождения окна, ≥ 1); LiteLLM не вызывается. По истечении окна запросы снова принимаются.
- **R-L9. Скрытие внутренних данных.** Ответы `/cli/*` никогда не содержат `poll_secret` LiteLLM и иные
  внутренние идентификаторы/секреты LiteLLM (JWT до `ready`, `litellm_poll_secret` и т.п.). Единственное
  исключение — `login_id` LiteLLM в составе `browser_url` ответа `POST /cli/start` (параметр `key=`, R-L1): этот
  URL предназначен для открытия в браузере и без него вход невозможен. Вне `browser_url` (в том числе в поле
  `login_id`, в ответах `/cli/poll/*`) `login_id` LiteLLM не появляется. Поллинг LiteLLM выполняется только
  Hub'ом. (Ревизия 1.1: прежняя формулировка «не содержат `login_id` LiteLLM» противоречила R-L1.)
- **R-L10. Дросселирование опроса LiteLLM.** На сессию — не чаще одного исходящего обращения к LiteLLM
  (poll или `/key/generate`) в 2 с: Hub сохраняет последний ответ клиенту (код и тело) с меткой времени;
  poll клиента раньше чем через 2 с возвращает сохранённый ответ без обращения к LiteLLM. Кэш сбрасывается
  при выборе команды (`POST …/team`) и не применяется к первому poll сессии. Пороги: `< 2.0 с` — кэш,
  `≥ 2.0 с` — новый запрос.

### 3.4. API для витрины и стандартного OpenCode (R-A*)

- **R-A1. Здоровье.** `GET /health` (без аутентификации) → 200 `{status:"ok", version:<версия пакета>,
  catalog_version:<version каталога>, time:<ISO-8601 UTC>}`. `GET /ready` → 200 `{status:"ready"}` только если
  БД отвечает на `SELECT 1` и каталог загружен; иначе 503 `{status:"not_ready", error:"not_ready", message}`.
- **R-A2. `GET /api/me`** (Bearer) → 200 `{user_id, email, key_kind, created_at}` — `key_kind`/`created_at`
  относятся к ключу, которым выполнен запрос.
- **R-A3. `GET /api/catalog`** (Bearer) → 200 `{version, servers:[…]}`; элемент = публичное представление
  (R-C6) + `connection:{status:"not_connected"|"connected"|"needs_reauth", preset, updated_at}` из таблицы
  `connections` для пользователя (нет строки → `{status:"not_connected", preset:null, updated_at:null}`).
  Фильтры: сервер виден, если `all ∈ audience` или пересечение `audience` с группами пользователя (в этой
  итерации `["all"]`) непусто; `unconfigured` скрыты; `?include_deprecated=false` (также `0`, `no`,
  регистр не важен) скрывает `status: deprecated`, по умолчанию deprecated показываются с `status:"deprecated"`.
  Порядок серверов — как в файле.
- **R-A4. `GET /api/me/connections`** (Bearer) → 200 список `{alias, status, preset, groups, created_at,
  updated_at}` из `connections` пользователя (в этой итерации — `[]`, если строк нет).
- **R-A5. `GET /.well-known/opencode`** (без аутентификации; заголовки `Cache-Control: public, max-age=300`,
  `ETag`) → 200:
  ```json
  {
    "auth": {"command": ["opencode","corp","login","--hub","https://hub.example"], "env": "MAGNIT_COPILOT_KEY"},
    "config": {
      "$schema": "https://opencode.ai/config.json",
      "autoupdate": false,
      "enabled_providers": ["magnit_prod"],
      "provider": {"magnit_prod": {"npm": "@ai-sdk/openai-compatible", "name": "LiteLLM Copilot prod",
        "options": {"baseURL": "https://litellm.example/v1", "apiKey": "{env:MAGNIT_COPILOT_KEY}"},
        "models": {"MagnitCopilot": {"name": "MagnitCopilot", "limit": {"context": 250000, "output": 8192}}}}},
      "mcp": {"tag": {"type": "remote", "url": "https://tag-mcp.example/mcp", "enabled": false, "oauth": {}},
              "gitlab": {"type": "remote", "url": "https://hub.example/mcp/gitlab", "enabled": false, "oauth": {}}}
    },
    "remote_config": {"url": "https://hub.example/remote-config",
                      "headers": {"Authorization": "Bearer {env:MAGNIT_COPILOT_KEY}"}}
  }
  ```
  `mcp` содержит по записи на каждый сервер каталога, у которого `all ∈ audience` и который не
  `unconfigured` (deprecated включаются); у записи нет `headers` и секретов. Пустой каталог → `mcp: {}`.
  Единственные допустимые вхождения подстроки `env:` в теле — плейсхолдеры `{env:<HUB_WELLKNOWN_ENV_NAME>}`
  в `provider.*.options.apiKey` и `remote_config.headers.Authorization`; ссылки каталога `env:VAR`, имена таких
  переменных, `client_secret`, `upstream_url`, `credential_headers`, `static_headers` и значения секретов
  в теле отсутствуют (ревизия 1.1).
- **R-A6. `GET /remote-config`** (Bearer; `Cache-Control: private, no-store`) → 200
  `{config:{mcp:{…}, permission:{}, tools:{}}}`: для каждой строки `connections` пользователя со `status:"connected"`
  — `mcp.<alias>: {enabled:true}`; при отсутствии подключений `mcp: {}`.
- **R-A7. Общий формат.** Все ответы — JSON; ошибки — `{error:"<snake_case>", message?:"<русский текст>", hint?}`
  (для `/cli/*` дополнительно `status:"error"`); ошибки валидации запроса → 400 `invalid_request`;
  неизвестный маршрут → 404 `not_found`; каждый ответ содержит `X-Content-Type-Options: nosniff`; CORS не включён.
- **R-A8. ETag well-known.** `ETag: "<первые 16 hex sha256 тела>"`; запрос с `If-None-Match`, совпадающим
  с текущим ETag → 304 без тела (с теми же `ETag`, `Cache-Control`). После изменения каталога/настроек ETag меняется.

### 3.5. Хранилище, кэш, наблюдаемость (R-S*)

- **R-S1. БД.** SQLAlchemy 2.x async; SQLite (`aiosqlite`) по умолчанию, Postgres тем же кодом. При старте
  `create_all`. Таблицы — §6.
- **R-S2. KeyValueStore.** Интерфейс `get / set(key, value, ttl) / delete / incr-подобная операция для
  окон rate-limit`; реализации in-memory (TTL, истечение по монотонному времени) и Redis; выбор по
  `HUB_REDIS_URL` (пусто → in-memory). Значение после истечения TTL недоступно (`None`).
- **R-S3. Аудит.** События `login_started`, `login_completed` (details содержит `key_kind`), `catalog_reloaded`
  в `audit_log`; `details` без секретов.
- **R-S4. Логи и метрики.** JSON-логи (стандартный `logging` + JSON-форматтер), в каждой записи о запросе —
  `request_id`; заголовок `X-Request-ID` принимается от клиента (≤ 128 символов), иначе генерируется (uuid4);
  всегда возвращается в ответе. `GET /metrics` (без аутентификации, `text/plain; version=0.0.4`):
  `hub_http_requests_total{method,path,status}` (path — шаблон маршрута), гистограмма
  `hub_http_request_duration_seconds`, gauge `hub_login_sessions_active` (число живых сессий входа: +1 при
  `/cli/start`, −1 при завершении/истечении).
- **R-S5. Точки входа.** `create_app(settings=None)` (R-K4); CLI `mcp-hub serve` (uvicorn, параметры
  `--host`, `--port`) и `mcp-hub catalog validate` (R-C5).

## 4. Принятые решения по неоднозначностям

Даны человеком (считаются решёнными):

1. Несколько команд LiteLLM — всегда спрашивать пользователя; одна — автоматически; ноль — без `team_id`.
2. `/key/generate` 4xx → JWT (`key_kind: jwt`), не ошибка; 5xx/сеть → 502 `{status:"error", error:"litellm_unavailable"}`, сессия жива до TTL.
3. Rate-limit `/cli/start`: 30/мин на IP, скользящее окно, 429 `{error:"rate_limited"}`.
4. `sha256` ключа — hex нижним регистром; сравнение через поиск по индексу.
5. Группы пользователя — `["all"]`; видимость: пересечение с `audience` непусто или `all ∈ audience`.
6. Пустой каталог валиден: `/api/catalog` → `servers: []`, well-known → `mcp: {}`.
7. Кэш poll: не чаще 1 раза в 2 с на сессию, иначе — последний известный ответ.
8. `ETag` = sha256 тела, усечённый до 16 hex; `If-None-Match` совпал → 304.
9. `env:VAR` без переменной окружения при загрузке — не ошибка (лениво); `${VAR}` — ошибка (кроме beta → `unconfigured`).
10. Ошибки: `{error:"<snake_case>", message:"<русский>", hint?}`.

Приняты spec-агентом как рабочие допущения (не меняют требований, фиксируют детали):

11. Формат `HUB_ENCRYPTION_KEY` — ключ Fernet (32 байта urlsafe-base64).
12. `unconfigured` — любой отсутствующий `${VAR}` внутри сервера со `status: beta`.
13. `env:VAR` допустим только в `client_secret`, `credential_headers`, `static_headers`.
14. `$ref` только вида `#/servers/<alias>/<поле>`, целевое значение без `$ref`.
15. LiteLLM 4xx на SSO-poll → сессия удаляется, 404 `login_expired`; неожиданное тело → 502 `litellm_invalid_response`.
16. Автовыбор единственной команды выполняется в том же запросе poll (повторный опрос LiteLLM без ожидания 2 с).
17. `POST …/team` при неподходящем состоянии → 409 `team_selection_not_required`; успех → 200 `{status:"pending"}`.
18. Временная метка alias ключа — UTC; `expires_in` для jwt из claim `exp`, без проверки подписи.
19. `email` = claim `email` JWT, иначе `user_id` с `@`, иначе `null`.
20. IP для rate-limit — адрес соединения (без `X-Forwarded-For`); `Retry-After` в 429.
21. Кэш аутентификации 60 с — только положительные результаты; `WWW-Authenticate: Bearer` в 401.
22. Well-known: серверы с `all ∈ audience`, не `unconfigured`, deprecated включаются; публичное `permission_model` без `header`.
23. `HUB_ADMIN_TOKEN` пуст → `/admin/catalog/reload` отвечает 404; неверный/отсутствующий токен → 403.
24. Ошибки валидации запросов → 400 `invalid_request` (а не 422); неизвестный маршрут → 404 `not_found`.
25. `/ready` при недоступной БД → 503 `not_ready`.
26. `remote-config` и `/api/me/connections` строятся из таблицы `connections` (строки в этой итерации создаются только тестами напрямую).

Ревизия 1.1 (2026-08-19, по `reports/test-report-i1.md` §5):

27. Плейсхолдеры `{env:<HUB_WELLKNOWN_ENV_NAME>}` в well-known разрешены (R-A5); запрет R-K3/R-C2/AC-59 распространяется
    только на ссылки каталога `env:VAR` и значения секретов.
28. Alias сервера — 1–32 символа, `^[a-z][a-z0-9-]{0,31}$` (R-C1); AC-54/AC-55 с alias `a` валидны, проверка
    невалидных alias (`Bad_Alias`, `-x`, 33 символа) сохранена в AC-09.
29. `login_id` LiteLLM допустим только в `browser_url` ответа `/cli/start` (R-L1, R-L9); `poll_secret` LiteLLM —
    нигде (AC-47 без изменений).

## 5. Вне объёма итерации

OAuth-фасад (`/mcp/<alias>`, `/oauth/*`), proxy, страницы UI/Keycloak, права/группы в `remote-config`,
отзыв ключей, Alembic-миграции, доверие `X-Forwarded-For`, шифрование токенов систем (только валидация
формата ключа), CORS, дедупликация ключей.

## 6. Модель данных

| Таблица | Поля |
|---|---|
| `users` | `user_id` TEXT PK; `email` TEXT NULL; `groups` JSON (по умолчанию `["all"]`); `created_at` DATETIME; `updated_at` DATETIME |
| `api_keys` | `id` INTEGER PK; `key_sha256` TEXT UNIQUE (64 hex, индекс); `user_id` FK→users; `key_kind` TEXT (`persistent`\|`jwt`); `key_alias` TEXT; `client` TEXT NULL; `created_at` DATETIME; `expires_at` DATETIME NULL |
| `connections` | `id` INTEGER PK; `user_id` FK→users; `alias` TEXT; `status` TEXT (`not_connected`\|`connected`\|`needs_reauth`); `preset` TEXT NULL; `groups` JSON; `created_at`; `updated_at`; UNIQUE(`user_id`,`alias`) |
| `audit_log` | `id` INTEGER PK; `ts` DATETIME; `user_id` TEXT NULL; `action` TEXT; `alias` TEXT NULL; `details` JSON (без секретов) |

KeyValueStore (не БД):

| Ключ | Значение | TTL |
|---|---|---|
| `login:<login_id>` | `{poll_secret, litellm_login_id, litellm_poll_secret, client, state ∈ {pending, team_selection, key_pending}, teams[], team_id, jwt, user_id, email, last_call_at, last_response{code, body}, jwt_warned}` | TTL сессии (R-L1) |
| `keyauth:<sha256>` | `{user_id, email, key_kind, created_at}` | 60 с |
| `rl:cli_start:<ip>` | метки времени запросов в окне | 60 с |

## 7. Контракты эндпоинтов

| Метод и путь | Auth | Запрос | Ответы |
|---|---|---|---|
| `POST /cli/start` | нет, rate-limit | `{client?}` | 200 `{login_id, poll_secret, browser_url, user_code, expires_in}`; 400 `invalid_request`; 429 `rate_limited` (+`Retry-After`); 502 `litellm_unavailable` |
| `GET /cli/poll/{login_id}` | `X-Hub-Poll-Secret` | — | 200 `pending` / `team_selection_required{teams}` / `ready{key,key_kind,user{user_id,email},team_id,expires_in?}`; 403 `forbidden`; 404 `login_expired`; 502 `litellm_unavailable` / `litellm_invalid_response` |
| `POST /cli/poll/{login_id}/team` | `X-Hub-Poll-Secret` | `{team_id}` | 200 `{status:"pending"}`; 400 `invalid_request` / `invalid_team`; 403; 404; 409 `team_selection_not_required` |
| `GET /health` | нет | — | 200 `{status:"ok", version, catalog_version, time}` |
| `GET /ready` | нет | — | 200 `{status:"ready"}`; 503 `not_ready` |
| `GET /metrics` | нет | — | 200 text/plain Prometheus |
| `GET /api/me` | Bearer | — | 200 `{user_id, email, key_kind, created_at}`; 401 |
| `GET /api/catalog` | Bearer | `?include_deprecated=` | 200 `{version, servers[]}`; 401 |
| `GET /api/me/connections` | Bearer | — | 200 `[…]`; 401 |
| `GET /.well-known/opencode` | нет | `If-None-Match?` | 200 (ETag, Cache-Control public, max-age=300); 304 |
| `GET /remote-config` | Bearer | — | 200 `{config:{mcp,permission,tools}}` (Cache-Control private, no-store); 401 |
| `POST /admin/catalog/reload` | `X-Admin-Token` | — | 200 `{status:"ok", catalog_version, servers}`; 400 `catalog_invalid`; 403 `forbidden`; 404 если отключён |

Примеры ошибок:

```json
{"error": "unauthorized", "message": "Требуется ключ доступа", "hint": "выполните вход: opencode corp login"}
{"status": "error", "error": "login_expired", "message": "Сессия входа не найдена или истекла"}
{"error": "catalog_invalid", "message": "servers[1].auth.client_id: не задана переменная окружения GITLAB_OAUTH_CLIENT_ID"}
```

## 8. Моки для тестов (respx, без сети)

LiteLLM (`HUB_LITELLM_BASE_URL`, например `https://litellm.test`):

| Маршрут | Ветки |
|---|---|
| `POST /sso/cli/start` | 200 `{login_id:"ll-…", poll_secret:"ll-secret", user_code:"ABCD-1234", expires_in:600}`; 500; сетевая ошибка (`httpx.ConnectError`); 200 с невалидным телом |
| `GET /sso/cli/poll/{id}` (проверять заголовок `x-litellm-cli-poll-secret`, параметр `team_id`) | `{status:"pending"}`; `{status:"ready", requires_team_selection:true, teams:["t1","t2"], team_details:[{team_id:"t1", team_alias:"A"},{team_id:"t2", team_alias:"B"}]}`; то же с одной командой; то же с пустым списком; `{status:"ready", key:"<JWT c claims sub/user_id/email/exp>", user_id:"u1", team_id:"t1", teams:["t1"]}`; `ready` без `team_id`; 404 (истекло на стороне LiteLLM); 500; сетевая ошибка |
| `POST /key/generate` (проверять `Authorization: Bearer <JWT>`, `key_alias`, `metadata`, `team_id`) | 200 `{key:"sk-test-…"}`; 401/403 (4xx → jwt); 500; сетевая ошибка; 200 без `key` |

Прочее: KeyValueStore — in-memory; БД — `sqlite+aiosqlite:///:memory:` или файл во временном каталоге;
время — подменяемые часы (для TTL сессий, окна rate-limit, кэша 60 с/2 с); каталоги — временные YAML-файлы
(валидный полный, пустой `servers: []`, с ошибками схемы, с `${VAR}`/`env:VAR`/`$ref`).
