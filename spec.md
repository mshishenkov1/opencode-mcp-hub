# Спецификация I-1: Hub — вход по SSO, постоянный ключ LiteLLM, каталог, well-known / remote-config

Источник: `examples/req-i1-hub-login-catalog.md` (на основе `docs/req-mvp.md` rev 0.2, §3, §4, §6.2, §6.3, §7, §8).
Реализация — `src/hub/` (Python 3.12, FastAPI, async). Тесты — только против локальных моков.

> **Ревизия 2.2 (2026-08-20).** Точечная правка по бэклогу Hub H5-1…H5-4
> (`installers/docs/acceptance-criteria.yaml`, раздел `hub_backlog_criteria`). Нумерация существующих
> AC не менялась, ни одно требование не ослаблено; добавлены AC-157…AC-168 (перенос HAC-01…HAC-12):
> 1. **R-P10** (§13): TTL ключа `cb:<alias>:probe` — не `HUB_CB_RESET`, а
>    `max(HUB_CB_RESET, HUB_UPSTREAM_TIMEOUT, HUB_UPSTREAM_SSE_IDLE_TIMEOUT) + HUB_CB_PROBE_GRACE`.
>    Пока проба в полёте (вплоть до таймаута SSE), право на неё второй раз не выдаётся и вторая
>    проба на лежащий upstream не уходит. `Retry-After` отказов в half-open считается от `until`
>    записи пробы и не меньше 1 с. AC-157, AC-158, AC-159.
> 2. **R-M4**: та же формула TTL в таблице ключей KV; TTL — страховка на случай аварии реплики,
>    штатно право снимается явным удалением по итогам пробы.
> 3. **R-T1**: добавлена настройка `HUB_CB_PROBE_GRACE` (дефолт `5.0`, дробное > 0 по R-T2);
>    её имя добавлено в перечень AC-145 (проверка `deploy/.env.example`).
> 4. **R-T5 / §9.2**: при `HUB_TRUST_PROXY=true` левый элемент `X-Forwarded-For` принимается только
>    если разбирается как IP-адрес (`ipaddress.ip_address`; снимаются `[…]` и суффикс `:<port>`;
>    элемент длиннее 45 символов не разбирается вовсе), иначе — откат на адрес соединения. Отказ
>    логируется WARNING без самого значения. AC-160…AC-164.
> 5. **R-P10**: в закрытом состоянии ключа пробы не существует, поэтому на успешно проксированный
>    запрос приходится одна запись состояния в KV и ноль удалений; провал пробы оставляет состояние
>    `{failures: HUB_CB_FAILURES, open_until: now + HUB_CB_RESET}`. AC-165…AC-168.

> **Ревизия 2.1 (2026-08-20).** Точечная правка по `reports/review-i3-1.json` и `reports/test-report-i3.md`
> (нумерация существующих AC не менялась, ни одно требование не ослаблено; добавлены AC-150…AC-156):
> 1. **R-P8** переписано по резолюции диспута `disputes/spec-dispute-R-P8-tool-filter.json`
>    (`uphold_test`, AC-122 решающий): доступность инструмента определяется тремя условиями **по имени
>    инструмента** (`deny` → `allow` → `group_deny` только при отсутствии `group_allow`), а не вычитанием
>    строк-масок; пересекающиеся маски включённой и выключенной групп — AC-150.
> 2. **R-P10** уточнён: после `HUB_CB_RESET` выключатель переходит в half-open и пропускает **ровно один**
>    пробный запрос (право на пробу берётся атомарно, ключ `cb:<alias>:probe`, поэтому проба одна на все
>    реплики); провал пробы немедленно открывает окно снова, успех — закрывает выключатель; параллельные
>    запросы во время пробы получают `upstream_unavailable`. AC-151, AC-152.
> 3. **R-M4 / R-N1**: ключ виртуальной сессии — `mcpsess:<alias>:<client_session_id>` (alias в ключе),
>    добавлен ключ `cb:<alias>:probe`, в кэше `conn:<user_id>:<alias>` зафиксировано поле
>    `access_token_enc` (шифртекст, наружу не отдаётся — R-B9); отдельного счётчика сессий
>    (`mcpsessn:*`) нет — gauge `hub_upstream_sessions_active` считается по живым записям (`count_prefix`).
> 4. **R-T1 / R-T5 / §9.2**: добавлена настройка `HUB_TRUST_PROXY` (дефолт `false`); при `true` IP для
>    rate-limit берётся из левого адреса `X-Forwarded-For`. Доверие `X-Forwarded-For` больше не «вне
>    объёма», а явно выключено по умолчанию. AC-153.
> 5. **R-W1**: допустимые алгоритмы подписи `id_token` — `RS256`, `ES256`; `none` и HS* отклоняются
>    **до** обращения к JWKS (защита от algorithm confusion). AC-154. Там же зафиксирована cookie
>    `hub_csrf` (double-submit, R-W6).
> 6. **R-O8**: `redirect_uri` при обмене кода **обязателен**, если код был выдан с ним (RFC 6749 §4.1.3);
>    его отсутствие → `invalid_grant`. AC-155.
> 7. Устранены 4 противоречия из `reports/test-report-i3.md` §6: AC-83/AC-148 приведены в соответствие
>    с R-O4.1 (расхождение только по порту loopback допустимо — чужой `redirect_uri` различается путём);
>    AC-98 — «изменённая подпись (любой значащий символ)» вместо «последняя буква»; AC-73 — в `given`
>    явно заданы переменные **каталога** (не настройки Hub); AC-74 — CSRF-токен не выводится на
>    страницах `/ui/*`, на экране прав он присутствует как скрытое поле формы.
> 8. **R-B7**: расширение прав на экране прав выполняется повторным OAuth целевой системы **в той же
>    транзакции** `/oauth/authorize`; отдельного шага `scope_upgrade` нет, экран прав после возврата
>    показывается по правилам `HUB_CONSENT` (R-O6.3).
> 9. **R-M1 / R-N5**: миграции при старте выполняются под advisory-блокировкой (PostgreSQL), пустой
>    `HUB_REDIS_URL` даёт WARNING при старте (реплики не делят состояние). AC-156.

> **Ревизия 2 (2026-08-19). Итерация I-3 — OAuth-фасад и MCP-proxy.** Документ описывает уже две итерации:
> разделы 1–8 — I-1 (правила `R-K*`, `R-C*`, `R-L*`, `R-A*`, `R-S*`, критерии AC-01…AC-69, **не изменены**);
> **часть II (разделы 9–18)** — I-3 по требованию `examples/req-i3-hub-oauth-facade-proxy.md` (P-01…P-19):
> новые правила `R-T*` (настройки), `R-O*` (Hub как authorization server), `R-B*` (брокер токенов целевых
> систем), `R-P*` (MCP-proxy), `R-W*` (веб-интерфейс), `R-M*` (модель данных и миграции), `R-N*`
> (наблюдаемость, эксплуатация, моки); критерии — AC-70…AC-149.
> Совместимость: ни одно правило I-1 не ослаблено, ни один AC I-1 не изменён; новых **обязательных**
> переменных окружения нет (R-T3). Раздел 5 «Вне объёма итерации» относится к I-1 — границы I-3 см. §9.2.

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

---

# Часть II. Итерация I-3: OAuth-фасад и MCP-proxy (ревизия 2)

Источник: `examples/req-i3-hub-oauth-facade-proxy.md` (P-01…P-19) на основе `docs/req-mvp.md` rev 0.2
(§4, §6.4 R-08…R-11, §6.6 R-12, §8 S-01…S-06). Разделы 1–8 (правила `R-K*`, `R-C*`, `R-L*`, `R-A*`,
`R-S*`, критерии AC-01…AC-69) остаются в силе без изменений; правила ниже их дополняют и нигде не ослабляют.
Факты требования с пометкой `[проверено]` перенесены как есть; `[проверить]` — параметризованы настройками
и/или каталогом и явно помечены ниже.

## 9. Назначение и границы I-3

### 9.1. Назначение

Для каждого сервера каталога с `mode: facade` Hub:

1. выглядит для любого MCP-клиента (OpenCode 1.17.9, Claude Desktop, Cursor, VS Code) как удалённый
   MCP-сервер со стандартной MCP-авторизацией: RFC 9728 (protected resource metadata),
   RFC 8414 (authorization server metadata), RFC 7591 (динамическая регистрация клиента),
   PKCE S256, `authorization_code` + `refresh_token` **[проверено]**;
2. является брокером доступа к целевой системе: получает по OAuth и хранит (в зашифрованном виде)
   токены пользователя к GitLab / GitLab Platform / Jira DC / Confluence DC, обновляет их фоном;
3. проксирует MCP-вызовы на неизменённые облачные MCP-серверы AI Lab (`upstream_url` каталога),
   подставляя персональные `credential_headers` и заголовок групп прав;
4. показывает пользователю страницы входа, экрана прав, «Мои подключения» и карточки сервера.

Границы правил: `R-T*` — настройки, `R-O*` — Hub как authorization server, `R-B*` — брокер токенов
целевых систем, `R-P*` — MCP-proxy, `R-W*` — веб-интерфейс, `R-M*` — модель данных и миграции,
`R-N*` — наблюдаемость, эксплуатация и моки.

### 9.2. Вне объёма I-3

Раздел 5 («Вне объёма итерации») относится к I-1; для I-3 вне объёма остаются:

- нативные серверы (`mode: native`, ТЭГ) — Hub их не проксирует и не авторизует (I-2);
- провайдеры-заглушки `pat` / `pat_via_password` (§6.4 R-11 req-mvp) — в этой итерации не реализуются;
- документация P-19 (`docs/*.md`: «как подключить facade-сервер», «как добавить сервер в каталог») —
  каталог `docs/` вне зоны записи dev-агента (`pipeline.config.yaml`), выполняется вне конвейера;
  в зоне конвейера обязателен только `deploy/.env.example` (R-N3);
- изменение `catalog.yaml` репозитория: спека расширяет **схему** каталога только необязательными полями
  (R-P8), существующий файл остаётся валидным и не требует правок;
- нагрузочный тест k6 на 30 000 VU (S-07), sticky-сессии, партиционирование аудита, CDN, HTTP/2-тюнинг;
- отзыв ключей LiteLLM и CORS — как и в I-1. Доверие `X-Forwarded-For` **не** вне объёма ревизии 2.1:
  оно управляется настройкой `HUB_TRUST_PROXY` (по умолчанию выключено) и влияет только на выбор IP
  для ключей rate-limit (R-T5); на аутентификацию и авторизацию заголовок не влияет никогда.
  Ревизия 2.2: даже при `HUB_TRUST_PROXY=true` значение заголовка не принимается «как есть» —
  оно обязано разбираться как IP-адрес (R-T5), поэтому произвольная строка клиента в ключи KV
  и в журналы не попадает ни в каком режиме.

### 9.3. Факты `[проверить]` и как они параметризованы

| Факт требования | Решение спеки |
|---|---|
| GitLab MCP принимает OAuth-токен в `Authorization: Bearer` | Значение и имя заголовка берутся **только** из `credential_headers` каталога (`Authorization: "Bearer {{access_token}}"`); смена на `Private-Token` — правка каталога, кода не требует (R-P2) |
| Atlassian MCP принимает OAuth-токен DC в `X-Atlassian-*-Personal-Token` | То же: имя и шаблон заголовка — из каталога (R-P2) |
| Ротация refresh-токенов Jira/Confluence DC | Реализация не полагается на ротацию: новый `refresh_token` в ответе сохраняется, его отсутствие — не ошибка, прежний остаётся действующим (R-B6) |
| Кэширование конфигурации клиентом при недоступности Hub (S-09) | Вне объёма I-3 |

## 10. Настройки I-3 (R-T*)

- **R-T1. Новые переменные.** Читаются тем же механизмом, что и в R-K1 (pydantic-settings). Переменные
  с префиксом `HUB_` — часть `Settings`; переменные `KEYCLOAK_*` читаются без префикса `HUB_`
  (явные псевдонимы имён окружения). Ни одна новая переменная не является обязательной безусловно —
  окружение I-1 запускает Hub и в ревизии 2 (R-T3).

  | Переменная | По умолчанию | Назначение |
  |---|---|---|
  | `HUB_WEB_AUTH` | `litellm` | Способ входа в веб-интерфейс: `keycloak` (OIDC) или `litellm` (CLI-SSO флоу I-1, временный) |
  | `HUB_CONSENT` | `always` | `always` — экран прав при каждом `/oauth/authorize`; `remember` — повторно не показывать при совпадении сохранённого согласия |
  | `HUB_OAUTH_ALLOWED_REDIRECTS` | `["http://127.0.0.1:*","http://localhost:*"]` | JSON-массив масок допустимых `redirect_uri` при DCR (`*` — любая последовательность символов) |
  | `HUB_ACCESS_TOKEN_TTL` | `3600` | Срок жизни access-токена Hub, с |
  | `HUB_REFRESH_TOKEN_TTL` | `2592000` | Срок жизни refresh-токена Hub, с (30 дней) |
  | `HUB_AUTH_CODE_TTL` | `60` | Срок жизни кода авторизации, с |
  | `HUB_OAUTH_TX_TTL` | `600` | Срок жизни транзакции `/oauth/authorize` в KV, с |
  | `HUB_WEB_SESSION_TTL` | `28800` | Срок жизни веб-сессии пользователя, с |
  | `HUB_RATE_LIMIT_REGISTER` | `10` | Регистраций клиента (`/oauth/register`) в 60 с на IP |
  | `HUB_RATE_LIMIT_TOKEN` | `60` | Обращений к `/oauth/token` в 60 с на пару (`client_id`, IP) |
  | `HUB_RATE_LIMIT_MCP` | `120` | Запросов к `/mcp/{alias}` в 60 с на пару (пользователь, alias) |
| `HUB_TRUST_PROXY` | `false` | Доверять ли `X-Forwarded-For` при вычислении IP для rate-limit (R-T5): `false` — адрес соединения, `true` — левый (первый) адрес заголовка |
  | `HUB_MAX_SSE_PER_USER` | `4` | Одновременных SSE-потоков на пользователя (по всем alias) |
  | `HUB_MAX_BODY_BYTES` | `1048576` | Максимальный размер тела запроса к `/mcp/{alias}`, байт |
  | `HUB_UPSTREAM_TIMEOUT` | `30.0` | Таймаут соединения и ожидания первого байта ответа upstream, с |
  | `HUB_UPSTREAM_SSE_IDLE_TIMEOUT` | `300.0` | Таймаут бездействия внутри установленного SSE-потока, с |
  | `HUB_UPSTREAM_IDLE_TTL` | `600` | Простой, после которого upstream-сессия считается закрытой (P-12), с |
  | `HUB_CLIENT_SESSION_TTL` | `86400` | Срок жизни клиентской (виртуальной) MCP-сессии, с |
  | `HUB_TOOLS_CACHE_TTL` | `300` | TTL кэша `tools/list`, с |
  | `HUB_TOKEN_REFRESH_LEAD` | `300` | За сколько секунд до `expires_at` обновлять upstream-токен |
  | `HUB_TOKEN_REFRESH_INTERVAL` | `60` | Период фоновой задачи обновления, с |
  | `HUB_TOKEN_REFRESH_ENABLED` | `true` | Включение фоновой задачи обновления (в тестах отключается) |
  | `HUB_CB_FAILURES` | `5` | Порог circuit-breaker: подряд идущих 5xx/таймаутов upstream |
  | `HUB_CB_RESET` | `30` | Время «открытого» состояния circuit-breaker, с |
  | `HUB_CB_PROBE_GRACE` | `5.0` | Запас к TTL права на пробу half-open поверх самого долгого запроса, с (дробное > 0, R-P10) |
  | `HUB_CONNECTION_CACHE_TTL` | `60` | TTL кэша подключения (права, срок токена) в KV, с |
  | `HUB_DB_AUTO_MIGRATE` | `true` | Применять миграции при старте (R-M1) |
  | `KEYCLOAK_ISSUER` | пусто | Issuer OIDC (обязателен при `HUB_WEB_AUTH=keycloak`) |
  | `KEYCLOAK_CLIENT_ID` | `opencode-mcp-hub` | OIDC-клиент Hub |
  | `KEYCLOAK_CLIENT_SECRET` | пусто | Секрет OIDC-клиента (обязателен при `HUB_WEB_AUTH=keycloak`) |
  | `KEYCLOAK_SCOPES` | `openid profile email` | Запрашиваемые scope OIDC |
  | `KEYCLOAK_JWKS_TTL` | `3600` | TTL кэша JWKS issuer'а, с |

- **R-T2. Валидация.** Правила R-K2 распространяются на новые переменные:
  - все `*_TTL`, `*_LIMIT`, `*_BYTES`, `HUB_CB_*`, `HUB_MAX_SSE_PER_USER`, `HUB_TOKEN_REFRESH_*`
    (кроме `_ENABLED`) — целые/дробные > 0; иначе — ошибка старта с именем переменной;
  - `HUB_WEB_AUTH ∈ {keycloak, litellm}`, `HUB_CONSENT ∈ {always, remember}` — иное значение — ошибка
    старта с именем переменной и перечнем допустимых;
  - `HUB_TRUST_PROXY`, `HUB_TOKEN_REFRESH_ENABLED`, `HUB_DB_AUTO_MIGRATE` — булевы (`true`/`false`,
    разбор pydantic-settings); дефолт `HUB_TRUST_PROXY` — `false`;
  - `HUB_OAUTH_ALLOWED_REDIRECTS` — строка JSON, разбираемая в непустой массив непустых строк; иначе —
    ошибка старта с именем переменной;
  - при `HUB_WEB_AUTH=keycloak` обязательны `KEYCLOAK_ISSUER` и `KEYCLOAK_CLIENT_SECRET`: отсутствие
    любой — ошибка старта, текст содержит имя переменной; при `HUB_WEB_AUTH=litellm` они не требуются;
  - `HUB_SECRET_KEY` (подпись JWT Hub, HS256) и `HUB_ENCRYPTION_KEY` (Fernet, шифрование токенов систем)
    в ревизии 2 действительно используются; требования к их формату — прежние (R-K1, R-K2).
- **R-T3. Обратная совместимость.** Приложение, запущенное с окружением I-1 (четыре обязательные
  переменные и валидный каталог, без единой новой переменной), стартует и обслуживает как эндпоинты I-1,
  так и эндпоинты I-3 с дефолтами из таблицы R-T1. Дефолт `HUB_WEB_AUTH=litellm` выбран именно поэтому:
  Keycloak-клиент — внешняя зависимость D-5, и её отсутствие не должно ломать старт.
- **R-T4. Секреты.** Правило R-K3 расширяется: `KEYCLOAK_CLIENT_SECRET`, `client_secret` провайдеров,
  токены целевых систем (access/refresh, в т.ч. в зашифрованном виде), refresh-токены Hub, коды
  авторизации, идентификаторы веб-сессий и CSRF-токены, `code_verifier` — не попадают в логи,
  `audit_log.details`, метрики, HTML-страницы, ответы `/api/*`, `/oauth/*` (кроме собственно выдачи
  токена клиенту в теле `/oauth/token`), `/.well-known/*`, `/remote-config`.
- **R-T5. Доверенный прокси (`HUB_TRUST_PROXY`).** IP клиента для ключей rate-limit
  (`rl:register:<ip>`, `rl:token:<client_id>:<ip>`) вычисляется так:
  - `HUB_TRUST_PROXY=false` (по умолчанию) — адрес TCP-соединения (`request.client.host`); заголовки
    `X-Forwarded-*` игнорируются (клиент подделывает их сам). За ingress/service mesh это означает
    один общий ключ на весь Hub, поэтому `HUB_RATE_LIMIT_REGISTER` фактически становится глобальным
    лимитом — предупреждение об этом обязательно в `deploy/.env.example` (R-N3);
  - `HUB_TRUST_PROXY=true` — **левый (первый)** элемент заголовка `X-Forwarded-For` (список
    `client, proxy1, proxy2`, значения обрезаются по пробелам); при пустом или отсутствующем
    заголовке — адрес соединения. Включать разрешено только когда `X-Forwarded-For` проставляет
    доверенный прокси.

  Ревизия 2.2: левый элемент принимается **только если он разбирается как IP-адрес**. Типовой
  ingress (`proxy_add_x_forwarded_for`) заголовок не перезаписывает, а дополняет, поэтому левый
  элемент задаёт клиент; без проверки в ключи `rl:register:<ip>` и `rl:token:<client_id>:<ip>`
  попадали бы произвольные строки — это и обход лимита регистрации, и неограниченные
  кардинальность и длина ключей KV. Правила разбора:
  - элемент длиннее 45 символов (максимум текстового представления IPv6) отклоняется **без**
    попытки разбора (AC-161);
  - у формы `[<IPv6>]:<port>` снимаются скобки и порт; у формы `<IPv4>:<port>` (ровно одно
    двоеточие) снимается порт; остальное разбирается как есть;
  - результат проверяется `ipaddress.ip_address` и подставляется в ключ в **нормализованном**
    виде (AC-162);
  - если элемент не разобрался — используется адрес соединения (`request.client.host`), а при его
    отсутствии — строка `unknown`; ключ лимита при этом такой же, как при `HUB_TRUST_PROXY=false`
    (AC-160, AC-161, AC-164);
  - отказ фиксируется записью журнала уровня WARNING, в которой есть имя заголовка и длина
    отклонённого элемента, но **нет самого значения**; в `audit_log` отклонённое значение тоже не
    попадает (R-T4, R-K3, AC-163).

  Заголовок не влияет ни на аутентификацию, ни на авторизацию, ни на выбор alias — только на выбор
  ключа лимита; в логи и `audit_log` попадает то же значение IP (`created_ip` в `oauth_clients`).

## 11. Hub как authorization server для facade-серверов (R-O*)

Во всех правилах `<HUB>` — нормализованный `HUB_PUBLIC_URL`; `{alias}` — alias сервера каталога с
`mode: facade`, который не `unconfigured`. Для неизвестного alias, `mode: native` и `unconfigured`
серверов эндпоинты `/mcp/{alias}` и `/.well-known/oauth-protected-resource/mcp/{alias}` отвечают
404 `{error:"not_found"}`.

- **R-O1. Метаданные authorization server (P-01).** `GET /.well-known/oauth-authorization-server` и
  `GET /.well-known/oauth-authorization-server/mcp/{alias}` (вариант с суффиксом пути ресурса) —
  без аутентификации, `Cache-Control: public, max-age=300`, 200:

  ```json
  {"issuer": "<HUB>",
   "authorization_endpoint": "<HUB>/oauth/authorize",
   "token_endpoint": "<HUB>/oauth/token",
   "registration_endpoint": "<HUB>/oauth/register",
   "revocation_endpoint": "<HUB>/oauth/revoke",
   "response_types_supported": ["code"],
   "grant_types_supported": ["authorization_code", "refresh_token"],
   "code_challenge_methods_supported": ["S256"],
   "token_endpoint_auth_methods_supported": ["none"],
   "revocation_endpoint_auth_methods_supported": ["none"],
   "scopes_supported": ["<alias>:readonly", "<alias>:readwrite", …]}
  ```

  `scopes_supported` — объединение по всем видимым facade-серверам каталога в порядке файла:
  для каждого — `<alias>:readonly`, затем `<alias>:readwrite`. Вариант с суффиксом отдаёт то же тело
  (issuer общий), но 404, если alias неизвестен/не facade. Ответ не зависит от аутентификации и не
  содержит секретов.
- **R-O2. Метаданные защищённого ресурса (P-02).** `GET /.well-known/oauth-protected-resource/mcp/{alias}`
  → 200 `{"resource":"<HUB>/mcp/{alias}", "authorization_servers":["<HUB>"],
  "scopes_supported":["{alias}:readonly","{alias}:readwrite"], "bearer_methods_supported":["header"],
  "resource_name":"<title сервера>", "resource_documentation":"<docs_url или null>"}`,
  `Cache-Control: public, max-age=300`.
  Любой запрос к `/mcp/{alias}` без заголовка `Authorization: Bearer` → 401
  `{error:"unauthorized", message:"…", hint:"…"}` с заголовком
  `WWW-Authenticate: Bearer resource_metadata="<HUB>/.well-known/oauth-protected-resource/mcp/{alias}"`.
- **R-O3. Динамическая регистрация клиента (P-03).** `POST /oauth/register` (RFC 7591, публичный клиент),
  без аутентификации, `Content-Type: application/json`:
  - принимаются поля `redirect_uris` (обяз., непустой массив строк), `client_name` (опц., ≤ 128),
    `grant_types` (опц., подмножество `["authorization_code","refresh_token"]`),
    `response_types` (опц., только `["code"]`), `token_endpoint_auth_method` (опц., только `"none"`),
    `scope` (опц.); прочие поля RFC 7591 принимаются и игнорируются;
  - каждый `redirect_uri` проверяется: абсолютный URI без фрагмента; допустимы схемы `http` **только**
    для loopback-хостов `127.0.0.1`/`localhost`/`[::1]` (любой порт) и `https`; кроме того, URI должен
    совпасть хотя бы с одной маской `HUB_OAUTH_ALLOWED_REDIRECTS` (сравнение по маске с `*`, регистр
    схемы и хоста не важен). Нарушение → 400 `{"error":"invalid_redirect_uri","error_description":"…"}`;
  - иные нарушения метаданных (пустой `redirect_uris`, `token_endpoint_auth_method != none`,
    `response_types != ["code"]`, `grant_types` вне списка) → 400
    `{"error":"invalid_client_metadata","error_description":"…"}`;
  - успех → 201 `{"client_id":"<uuid4-hex>", "client_id_issued_at":<unix>, "redirect_uris":[…],
    "grant_types":["authorization_code","refresh_token"], "response_types":["code"],
    "token_endpoint_auth_method":"none", "client_name":…}`; `client_secret` не выдаётся никогда;
    запись в `oauth_clients` (R-M2), аудит `oauth_client_registered`;
  - rate-limit `HUB_RATE_LIMIT_REGISTER` в 60 с на IP клиента (определяется по R-T5) → 429
    `{error:"rate_limited"}` с `Retry-After` (как R-L8).
- **R-O4. `GET /oauth/authorize` — валидация (P-04).** Параметры: `response_type=code`, `client_id`,
  `redirect_uri`, `code_challenge`, `code_challenge_method=S256`, `state` (опц., но возвращается как есть),
  `scope` (опц.), `resource` (опц.). Порядок проверок:
  1. `client_id` неизвестен, `redirect_uri` отсутствует или не принадлежит этому клиенту → **редирект не
     выполняется**: 400 HTML-страница с русским текстом ошибки и кодом `invalid_client`/`invalid_redirect_uri`
     (тело содержит `error=` в машиночитаемом виде в `<meta name="hub-error">`). Совпадение `redirect_uri`
     со строкой регистрации — точное, **с единственным исключением**: для loopback-хостов (`127.0.0.1`,
     `localhost`, `[::1]`) допускается отличающийся **порт** при совпадении схемы, хоста и пути
     (RFC 8252) — такой `redirect_uri` принимается, даже если тот же порт зарегистрирован другим
     клиентом. Любое расхождение пути (в т.ч. обход `…/cb/../evil`), схемы или хоста, а также
     расхождение порта для не-loopback хостов — ошибка `invalid_redirect_uri` без редиректа;
  2. далее ошибки возвращаются редиректом на `redirect_uri` с `error`, `error_description`, `state`:
     `unsupported_response_type` (`response_type != code`), `invalid_request` (нет `code_challenge`,
     `code_challenge_method != S256`), `invalid_target` (`resource` не соответствует ни одному
     facade-alias), `invalid_scope` (scope не из `scopes_supported` или относится к другому alias,
     чем `resource`).
- **R-O5. `GET /oauth/authorize` — alias, scope и веб-сессия (P-04).**
  - alias определяется по `resource` (`<HUB>/mcp/{alias}`, сравнение после нормализации без завершающего
    `/`); если `resource` не задан — по префиксу `scope` (`<alias>:…`); если не задано ни то, ни другое,
    либо они указывают на разные alias — `invalid_request` (редирект по R-O4.2);
  - scope: `<alias>:readonly` или `<alias>:readwrite`; не задан → `<alias>:readonly`;
  - веб-сессия (R-W1): нет валидной cookie-сессии → 302 на `/auth/login?next=<исходный URL authorize>`;
    после успешного входа пользователь возвращается на тот же URL и флоу продолжается;
  - создаётся транзакция `oauthtx:<tx_id>` в KV с TTL `HUB_OAUTH_TX_TTL` (все параметры запроса,
    `user_id`, alias, scope, шаг флоу). Истёкшая транзакция при возврате из целевой системы или с экрана
    прав → 400 HTML «Сессия авторизации истекла, начните заново».
- **R-O6. `GET /oauth/authorize` — подключение и экран прав (P-04).**
  1. если у пользователя нет подключения к целевой системе для alias со статусом `connected` (или токен
     непригоден и не обновляется — R-B5) → запускается OAuth целевой системы (R-B2) с возвратом в ту же
     транзакцию; после успеха — шаг 2;
  2. экран прав (R-W3): пресет `readonly` отмечен по умолчанию, галочки групп `permission_model`,
     кнопки «Разрешить»/«Отмена». Выбор сохраняется в `connections` (`preset`, `groups`) и в `consents`;
  3. `HUB_CONSENT=remember`: если для тройки (`user_id`, `client_id`, `alias`) есть сохранённое согласие
     с тем же `scope`, экран не показывается и код выдаётся сразу; `HUB_CONSENT=always` — экран
     показывается всегда;
  4. «Отмена» → редирект на `redirect_uri` с `error=access_denied` и `state`.
- **R-O7. Выдача кода (P-04).** Код — ≥ 32 байта случайности (urlsafe), хранится как sha256 в
  `oauth_codes`, TTL `HUB_AUTH_CODE_TTL`, привязан к `client_id`, `redirect_uri`, `code_challenge`,
  `scope`, `resource`, `user_id`, `connection_id`. Редирект 302 на `redirect_uri` с `code` и `state`
  (`state` возвращается байт-в-байт; если не был передан — параметр отсутствует). Транзакция удаляется.
  Аудит `oauth_code_issued`.
- **R-O8. `POST /oauth/token` — `grant_type=authorization_code` (P-05).** Тело
  `application/x-www-form-urlencoded`: `grant_type`, `code`, `code_verifier`, `redirect_uri`, `client_id`.
  Аутентификация клиента не требуется и не принимается (`token_endpoint_auth_method: none`);
  переданный `client_secret` игнорируется. Проверки и ошибки (все — 400 JSON, кроме `invalid_client` — 401):
  - неизвестный `client_id` → `invalid_client`;
  - код неизвестен, истёк или уже использован → `invalid_grant`; **повторное предъявление уже
    использованного кода дополнительно отзывает всю цепочку токенов, выданных по этому коду** (R-O10);
  - `client_id`/`redirect_uri` не совпадают с сохранёнными при выдаче кода → `invalid_grant`;
  - `redirect_uri` **обязателен**, если код был выдан с ним (на `/oauth/authorize` он обязателен
    всегда — R-O4.1): его отсутствие в теле запроса → `invalid_grant` (RFC 6749 §4.1.3);
  - `code_verifier` отсутствует или `BASE64URL(SHA256(code_verifier)) != code_challenge` → `invalid_grant`;
  - успех → 200 `{"access_token":"<JWT>", "token_type":"Bearer", "expires_in":<HUB_ACCESS_TOKEN_TTL>,
    "refresh_token":"<opaque>", "scope":"<alias>:<preset>"}`, заголовки `Cache-Control: no-store`,
    `Pragma: no-cache`; код помечается использованным; аудит `oauth_token_issued`
    (details: `client_id`, `alias`, `grant`, без значений токенов).
- **R-O9. Формат токенов Hub (P-05).** Access-токен — JWT, `alg: HS256`, ключ `HUB_SECRET_KEY`,
  claims: `iss` = `<HUB>`, `sub` = `user_id`, `aud` = `<HUB>/mcp/{alias}`, `scope`, `cid` = id подключения,
  `client_id`, `jti` (uuid4-hex), `iat`, `exp` = `iat + HUB_ACCESS_TOKEN_TTL`. Refresh-токен — непрозрачная
  строка ≥ 32 байт случайности (urlsafe), в БД хранится только sha256; срок — `HUB_REFRESH_TOKEN_TTL`.
  Ни один токен не хранится в БД в открытом виде.
- **R-O10. `grant_type=refresh_token`, ротация и отзыв цепочки (P-05).**
  - Тело: `grant_type=refresh_token`, `refresh_token`, `client_id`, опц. `scope` (только сужение до
    `<alias>:readonly`; расширение → `invalid_scope`).
  - Успех → новая пара (новый access + новый refresh); предъявленный refresh переводится в состояние
    `rotated` и больше не принимается; новый принадлежит той же цепочке (`chain_id`), срок цепочки
    отсчитывается от первой выдачи и не продлевается сверх `HUB_REFRESH_TOKEN_TTL`.
  - Предъявление refresh в состоянии `rotated`/`revoked` (повторное использование) → 400 `invalid_grant`
    **и отзыв всей цепочки**: все refresh цепочки → `revoked`, все выданные по ней access-токены с ещё
    не истёкшим `exp` заносятся в denylist KV (`jtiden:<jti>` с TTL до `exp`); аудит
    `oauth_refresh_reuse_detected`. Последующие запросы `/mcp/{alias}` с этими access-токенами → 401.
  - Истёкший refresh → `invalid_grant`; refresh, выданный другому `client_id`, → `invalid_grant`.
  - Rate-limit `HUB_RATE_LIMIT_TOKEN` на пару (`client_id`, IP — определяется по R-T5) → 429
    `{error:"rate_limited"}` + `Retry-After`.
- **R-O11. `POST /oauth/revoke` (P-06).** Тело: `token`, опц. `token_type_hint`, опц. `client_id`.
  Ответ всегда 200 с пустым JSON-объектом `{}` (в т.ч. для неизвестного токена — RFC 7009);
  отсутствие параметра `token` → 400 `invalid_request`. Отзыв refresh-токена отзывает всю его цепочку
  и заносит все связанные `jti` в denylist; отзыв access-токена заносит его `jti` в denylist до `exp`
  и отзывает цепочку refresh, выданную вместе с ним. Аудит `oauth_token_revoked`.
- **R-O12. Проверка access-токена на горячем пути без БД (P-07).** Для `/mcp/{alias}`:
  подпись HS256 → `exp` (с допуском 0 с) → `aud` == `<HUB>/mcp/{alias}` → `jti` отсутствует в denylist KV.
  Ошибки: невалидная подпись/формат, истёкший, отозванный (`jti` в denylist) → 401
  `{error:"unauthorized"}` + `WWW-Authenticate: Bearer resource_metadata="…", error="invalid_token"`;
  валидный токен с чужим `aud` → 403 `{error:"forbidden"}`. Данные подключения (статус, права,
  `expires_at` upstream-токена) читаются из KV-кэша `conn:<user_id>:<alias>` (TTL
  `HUB_CONNECTION_CACHE_TTL`), который наполняется из БД при промахе и инвалидируется при любом изменении
  подключения (смена прав, обновление токена, `needs_reauth`, отключение). Обращений к БД на успешном
  горячем пути при попадании в кэш нет. Подключение с `cid` отсутствует или принадлежит другому
  пользователю → 401.
- **R-O13. Формат ошибок OAuth и общие правила.** Все ответы `/oauth/token`, `/oauth/register`,
  `/oauth/revoke` — JSON RFC 6749/7591 `{"error":"…","error_description":"<русский текст>"}` (поле
  `error_description` обязательно для 4xx), `Cache-Control: no-store`. Формат `{error, message}` из R-A7
  здесь не применяется; на остальных маршрутах Hub он сохраняется. Заголовок `X-Content-Type-Options:
  nosniff` и `X-Request-ID` (R-S4) присутствуют и здесь.

## 12. Брокер токенов целевых систем (R-B*)

- **R-B1. Провайдер из каталога (P-08).** Параметры OAuth целевой системы берутся только из
  `catalog.yaml` (`auth`): `authorize_url`, `token_url`, `revoke_url` (опц.), `client_id`, `client_secret`
  (`Secret`/`EnvRef`, читается лениво), `pkce`, `scopes.readonly|readwrite`. Реализация — общий клиент
  `authorization_code` (confidential, `client_secret_post`), с PKCE S256 при `pkce: true`.
  Запрашиваемые scope = `scopes[<пресет>]`, соединённые пробелом (GitLab: `read_api read_user
  read_repository` / `api read_user`; Jira/Confluence DC: `READ` / `WRITE`).
  Отсутствие переменной окружения для `client_secret`/`client_id` в момент использования → ошибка
  подключения: пользователю — страница/JSON `{error:"server_unconfigured"}`, в лог — имя переменной
  без значения.
- **R-B2. OAuth целевой системы и `/oauth/callback/{alias}` (P-04, P-08).** Callback фиксирован:
  `<HUB>/oauth/callback/{alias}` (совпадает с D-2/D-3). Hub генерирует `state` (≥ 32 байта случайности),
  связывает его с транзакцией (`oauthstate:<state>` → `tx_id`, TTL транзакции), при `pkce: true`
  генерирует `code_verifier`/`code_challenge` и хранит verifier в транзакции. Проверки в callback:
  - `state` отсутствует/неизвестен/истёк/уже использован → 400 HTML + машинный код `invalid_state`;
  - параметр `error` от системы → страница с русским пояснением и кнопкой «Повторить», транзакция
    завершается; если authorize-транзакция принадлежала клиенту MCP — редирект на его `redirect_uri`
    с `error=access_denied`;
  - `state` принадлежит другому пользователю (веб-сессия сменилась) → 400 `invalid_state`;
  - успех → обмен `code` на токены на `token_url` (с `client_secret`, `redirect_uri`, `code_verifier`);
    ошибка обмена (4xx/5xx/сеть/невалидное тело) → 502 HTML/JSON `upstream_auth_failed`, токены не
    сохраняются, транзакция остаётся живой до TTL.
- **R-B3. Сохранение токенов (P-08).** Успешный обмен → upsert `connections` (`status: connected`,
  `preset`, `groups`, `revision += 1`) и `upstream_tokens`: `access_token_enc`, `refresh_token_enc`
  (если выдан), `expires_at` (= now + `expires_in`, если задан; иначе `NULL`), `scopes`, `token_type`,
  `obtained_at`. Шифрование — Fernet (AES-128-CBC + HMAC-SHA256) ключом `HUB_ENCRYPTION_KEY`; в БД
  открытых значений токенов нет. Кэш `conn:<user_id>:<alias>` инвалидируется. Аудит `connection_connected`.
- **R-B4. Обновление токенов (P-09).** Токен считается пригодным, если `expires_at` пуст или
  `expires_at − now > 0`.
  - Фоновая задача (при `HUB_TOKEN_REFRESH_ENABLED=true`, период `HUB_TOKEN_REFRESH_INTERVAL`) обновляет
    подключения, у которых `expires_at − now ≤ HUB_TOKEN_REFRESH_LEAD` и есть `refresh_token`.
  - «По требованию»: 401 от upstream MCP или истёкший токен перед вызовом → синхронное обновление, после
    успеха исходный запрос к upstream повторяется **ровно один раз** с новым токеном.
  - Гонки: обновление выполняется под блокировкой `refreshlock:<connection_id>` в KV (атомарный
    set-if-absent, TTL 30 с). Не захвативший блокировку ждёт до 5 с, перечитывает подключение и использует
    обновлённый токен; если за это время токен не обновился — отвечает как при провале (R-B5).
  - Успех → новые значения зашифрованы, `last_refresh_at`, инвалидация кэша, аудит `connection_refreshed`.
- **R-B5. Провал обновления → `needs_reauth` (P-09).** Провал (ошибка `invalid_grant`/4xx от системы,
  отсутствие `refresh_token` при истёкшем access, исчерпание попыток при 5xx/сети) переводит подключение
  в `status: needs_reauth` с `needs_reauth_reason`, инвалидирует кэш, пишет аудит
  `connection_needs_reauth`. При этом:
  - access/refresh-токены Hub, выданные клиенту, **остаются валидными** и не отзываются;
  - вызовы `/mcp/{alias}` отвечают JSON-RPC ошибкой `-32002` с русским `message` и
    `data.hint_url = <HUB>/ui/servers/{alias}`;
  - повторная авторизация (пользователь проходит `/oauth/authorize` тем же клиентом или нажимает
    «Переподключить» на странице) восстанавливает подключение **без новой регистрации клиента (DCR)**;
    после восстановления прежние access-токены клиента снова работают (тот же `cid`).
- **R-B6. Ротация refresh целевой системы (факт `[проверить]`).** Если ответ `token_url` содержит новый
  `refresh_token` — сохраняется он; если не содержит — сохраняется прежний, и это не ошибка. Если система
  ответила `invalid_grant` на использование refresh — см. R-B5.
- **R-B7. Смена прав (P-10).** `PUT /api/me/connections/{alias}/permissions` с телом
  `{preset: "readonly"|"readwrite", groups: [id, …]}`:
  - неизвестный alias/не facade → 404; чужое подключение недоступно (доступ только к своим);
  - неизвестный `id` группы или группа с `preset: none` в списке → 400 `invalid_request`;
  - при `preset: readonly` группы с `preset: readwrite` в набор не включаются, даже если переданы;
  - успех → 200 `{alias, status, preset, groups}`, `revision += 1`, инвалидация кэша подключения и кэша
    `tools/list`; **следующий** MCP-вызов уходит на upstream с новым заголовком групп, переподключение
    не требуется; аудит `connection_permissions_changed`;
  - если новый пресет требует scope целевой системы шире выданных (`readonly → readwrite`), подключение
    переводится в `needs_reauth` с пояснением «нужно заново разрешить доступ в <система>», 200 с
    `status: "needs_reauth"`; фактические права применяются после повторного OAuth системы;
  - то же расширение, выбранное **на экране прав** (`POST /oauth/consent` с `preset: readwrite`),
    выполняется повторным OAuth целевой системы (R-B2) **в той же транзакции** `/oauth/authorize`:
    отдельного шага/состояния `scope_upgrade` в флоу нет. После возврата из целевой системы флоу
    продолжается обычным порядком R-O6: при `HUB_CONSENT=always` экран прав показывается ещё раз,
    при `remember` с уже сохранённым тем же scope код выдаётся сразу; новой регистрации клиента (DCR)
    и нового подключения не требуется.
- **R-B8. Отключение и переподключение (P-06, P-16).** `DELETE /api/me/connections/{alias}`:
  best-effort `POST revoke_url` (если задан в каталоге; ошибка отзыва не блокирует), удаление
  `upstream_tokens`, `connections.status = not_connected` (или удаление строки — эквивалентно для
  `/api/catalog`), отзыв **всех** клиентских токенов Hub этого подключения (цепочки refresh → `revoked`,
  их access `jti` → denylist), инвалидация кэшей, аудит `connection_disconnected`. После этого
  `/mcp/{alias}` с прежним токеном → 401, `/remote-config` не содержит alias.
- **R-B9. Токены систем не выходят наружу (P-08).** Значения upstream-токенов не появляются в ответах
  `/api/*`, `/oauth/*`, HTML-страницах, `/metrics`, логах и `audit_log.details`; наружу отдаются только
  производные признаки (`status`, `preset`, `groups`, `updated_at`, при необходимости `expires_at`).

## 13. MCP-proxy (R-P*)

- **R-P1. Маршруты и доступ (P-11).** `POST /mcp/{alias}`, `GET /mcp/{alias}`, `DELETE /mcp/{alias}`.
  Аутентификация — только access-токен Hub (R-O12); ключ LiteLLM здесь не принимается. Порядок проверок:
  alias существует, `mode: facade`, не `unconfigured` (иначе 404) → токен (401/403 по R-O12) → лимит
  тела (413) → rate-limit (429) → статус подключения (R-P11) → circuit-breaker (R-P10) → проксирование.
- **R-P2. Заголовки запроса к upstream (P-11).** К `upstream_url` сервера передаются:
  - тело запроса без изменений (байт-в-байт), метод — тот же;
  - проброшенные заголовки клиента: `Accept`, `Content-Type`, `MCP-Protocol-Version`, `Last-Event-ID`,
    `Accept-Encoding`;
  - `Mcp-Session-Id` — **upstream-идентификатор** из мэппинга (R-P4), а не клиентский;
  - `credential_headers` каталога: в значении шаблон `{{access_token}}` заменяется на расшифрованный
    access-токен целевой системы; значения-ссылки `env:VAR` — на значение переменной окружения; иные
    вхождения `{{…}}` — ошибка каталога (R-C1);
  - `static_headers` каталога (после подстановки `env:VAR`);
  - заголовок групп: имя — `permission_model.header` (`Enabled-Groups`), значение — идентификаторы через
    запятую без пробелов: сначала все `always` в порядке каталога, затем выбранные пользователем группы
    в порядке каталога, без дублей. Группы с `preset: none` не включаются никогда; при `preset: readonly`
    подключения группы с `preset: readwrite` не включаются. Для `permission_model.kind != header_groups`
    заголовок не добавляется;
  - удаляются заголовки клиента: `Authorization`, `Cookie`, `X-Forwarded-*`, `Host`, а также любые
    заголовки, имена которых совпадают с `credential_headers`/`static_headers`/заголовком групп.
- **R-P3. Потоковость и таймауты (P-11).** Ответ upstream передаётся клиенту потоково, без буферизации
  тела: `Content-Type` (в т.ч. `text/event-stream`), `Cache-Control`, `Last-Event-ID`-совместимые данные
  и код статуса сохраняются; события SSE доходят до клиента по мере поступления (Hub не ждёт конца
  потока). Таймаут соединения и первого байта — `HUB_UPSTREAM_TIMEOUT`; для установленного SSE-потока
  действует таймаут бездействия `HUB_UPSTREAM_SSE_IDLE_TIMEOUT`; превышение → поток закрывается,
  в лог — `upstream_timeout`, клиенту (если ответ ещё не начат) — 502 с JSON-RPC `-32004`.
  Заголовок upstream `Mcp-Session-Id` клиенту не пересылается (см. R-P4).
- **R-P4. Виртуализация сессий (P-12).** Идентификатор сессии, который видит клиент, выдаёт Hub:
  - при успешном ответе upstream на `initialize` Hub создаёт `client_session_id` (uuid4-hex), запись
    `mcpsess:<alias>:<client_session_id>` в KV (`user_id`, `alias`, `connection_id`, `upstream_session_id`,
    `protocol_version`, `client_info`, `upstream_last_used_at`) с TTL `HUB_CLIENT_SESSION_TTL`,
    и возвращает клиенту заголовок `Mcp-Session-Id: <client_session_id>`;
  - при последующих запросах клиентский `Mcp-Session-Id` заменяется на `upstream_session_id`;
    TTL записи и `upstream_last_used_at` продлеваются;
  - запись хранится только в KV (реплики Hub без sticky-сессий); запись принадлежит пользователю —
    `Mcp-Session-Id` чужого пользователя или другого alias трактуется как неизвестный;
  - неизвестный/истёкший `client_session_id` → 404 с JSON-RPC ошибкой `-32000` и текстом «Сессия не
    найдена, выполните initialize» (клиент по протоколу MCP переинициализируется).
- **R-P5. Пересоздание upstream-сессии (P-12).** Upstream-сессия считается закрытой, если
  `now − upstream_last_used_at > HUB_UPSTREAM_IDLE_TTL`, либо upstream ответил 404 / ошибкой
  «неизвестная сессия» на запрос с `Mcp-Session-Id`. В этом случае Hub прозрачно:
  1. (best-effort) шлёт `DELETE` на upstream со старым `Mcp-Session-Id`;
  2. повторяет `initialize` с сохранёнными `protocolVersion` и `clientInfo` и уведомление
     `notifications/initialized`, получает новый `upstream_session_id`;
  3. повторяет исходный запрос — **ровно один раз**; клиент получает обычный ответ и **тот же**
     `Mcp-Session-Id`, что и раньше.
  Повторная ошибка после пересоздания → 502 + JSON-RPC `-32004`. Аудит/лог `upstream_session_recreated`.
- **R-P6. `DELETE /mcp/{alias}` (P-11).** Закрывает upstream-сессию (DELETE на upstream с
  `upstream_session_id`) и удаляет запись `mcpsess:*`; клиенту — статус upstream (или 204, если upstream
  ответил ошибкой закрытия). Повторный запрос с тем же клиентским `Mcp-Session-Id` → 404 (R-P4).
- **R-P7. Кэш `tools/list` (P-13).** Ответ на JSON-RPC метод `tools/list` **без** параметра `cursor`
  кэшируется в KV под ключом `toolscache:<alias>:<catalog_version>:<sha256(preset|groups)>` на
  `HUB_TOOLS_CACHE_TTL`. Кэшируется результат **до** фильтрации (фильтр применяется при каждой отдаче).
  Попадание в кэш → upstream не вызывается, клиенту отдаётся `application/json` c `id` из запроса.
  Кэш инвалидируется при смене прав (R-B7), перезагрузке каталога (R-C4) и по TTL; запросы с `cursor`
  и вызовы в рамках SSE-ответа не кэшируются.
- **R-P8. Фильтр инструментов (P-13).** Схема каталога расширяется **необязательными** полями
  (существующий `catalog.yaml` остаётся валидным; отсутствие полей = фильтр не применяется):
  - `permission_model` вида `header_groups`: `tool_filter: {allow: [маска, …], deny: [маска, …]}` (опц.)
    и у каждой группы — `tools: [маска, …]` (опц.);
  - `permission_model` вида `tool_filter`: как в I-1, `presets.<пресет>.tools` — allow-маски.

  Маска — шаблон `fnmatch` (`*`, `?`, `[…]`), регистр важен. Итоговые наборы для пользователя:
  - `allow` = `tool_filter.allow` ∪ `tools` включённых групп (`always` + выбранные); если оба источника
    пусты — `allow = ["*"]`;
  - `deny` = `tool_filter.deny`;
  - `group_allow` = `tools` включённых групп; `group_deny` = `tools` групп, не включённых пользователю
    (в т.ч. отсечённых пресетом `readonly` и групп с `preset: none`).

  Решение принимается **по имени инструмента**, а не сравнением строк-масок. Инструмент доступен,
  если одновременно выполнены три условия:
  1. имя не совпало ни с одной маской `deny` (deny приоритетнее всего);
  2. имя совпало хотя бы с одной маской `allow`;
  3. если имя совпало хотя бы с одной маской `group_deny` — оно совпало **также** хотя бы с одной
     маской `group_allow`.

  Иначе говоря: инструмент, который «приносит» только не включённая пользователю группа, недоступен,
  даже если общий `allow` — `*` (AC-122); инструмент, явно выданный включённой группой или общим
  `tool_filter.allow`, остаётся доступен и при пересекающихся масках — включённая группа с
  `tools: ["create_issue"]` при выключенной группе с `tools: ["create_*"]` и `allow: ["*"]` даёт
  доступ к `create_issue`, но не к остальным `create_*` (AC-150). Правило fail-closed: расширить права
  сверх включённых групп оно не может. При отсутствии `tool_filter` и `tools` у всех групп фильтр не
  применяется (`allow = ["*"]`, `deny` и `group_deny` пусты) — существующий `catalog.yaml` остаётся
  валидным (решение 51).

  - В ответе `tools/list` недоступные инструменты удаляются (в т.ч. в SSE-ответе upstream);
  - `tools/call` недоступного (или отсутствующего в отфильтрованном списке) инструмента → запрос на
    upstream **не отправляется**, ответ 200 с JSON-RPC ошибкой
    `{"code":-32001,"message":"Инструмент <name> недоступен с текущими правами",
    "data":{"hint_url":"<HUB>/ui/servers/{alias}","tool":"<name>"}}`;
  - если тело — batch (JSON-массив), проверяются все элементы; при наличии хотя бы одного запрещённого
    `tools/call` весь запрос отклоняется одним JSON-RPC error `-32001` с `id` первого запрещённого
    элемента, upstream не вызывается.
- **R-P9. Лимиты (P-14).** На пару (пользователь, alias) — `HUB_RATE_LIMIT_MCP` запросов в 60 с
  (скользящее окно, KV): превышение → 429 `Retry-After` и тело JSON-RPC error `-32003` («Слишком много
  запросов»). Одновременных SSE-потоков на пользователя (по всем alias) — не более
  `HUB_MAX_SSE_PER_USER`: превышение → 429 с JSON-RPC `-32003` и `data.reason:"too_many_streams"`;
  счётчик уменьшается при завершении/разрыве потока (в т.ч. при ошибке). Тело запроса больше
  `HUB_MAX_BODY_BYTES` (по `Content-Length` или фактически прочитанным байтам) → 413
  `{error:"payload_too_large"}`, upstream не вызывается.
- **R-P10. Circuit-breaker (P-14).** На alias: `HUB_CB_FAILURES` подряд идущих ошибок upstream
  (5xx, таймаут, сетевая ошибка) переводят выключатель в открытое состояние на `HUB_CB_RESET` секунд
  (состояние в KV `cb:<alias>` = `{failures, open_until}`, общее для реплик). В открытом состоянии
  запросы к upstream не идут: ответ 503 + JSON-RPC `-32004` `upstream_unavailable` с `data.hint_url`
  и `Retry-After`. По истечении `HUB_CB_RESET` выключатель переходит в состояние half-open:
  - на upstream пропускается **ровно один** пробный запрос. Право на пробу выдаётся атомарно
    (`set_if_absent` по ключу KV `cb:<alias>:probe`), поэтому проба одна на все реплики Hub,
    а не одна на реплику. TTL ключа пробы —
    `max(HUB_CB_RESET, HUB_UPSTREAM_TIMEOUT, HUB_UPSTREAM_SSE_IDLE_TIMEOUT) + HUB_CB_PROBE_GRACE`
    (ревизия 2.2): право должно жить дольше самого долгого возможного запроса, иначе, пока проба
    ещё в полёте (обычный ответ — до `HUB_UPSTREAM_TIMEOUT`, SSE-поток — до
    `HUB_UPSTREAM_SSE_IDLE_TIMEOUT`), истёкший ключ был бы захвачен вторым запросом и на лежащий
    upstream ушла бы вторая проба (AC-157, AC-158). Восстановление длинный TTL не задерживает:
    право снимается сразу по завершении пробы, а не по TTL;
  - все прочие запросы, пришедшие, пока проба не завершилась (в т.ч. параллельные и на других
    репликах), получают тот же ответ, что и в открытом состоянии — 503 + `-32004`
    `upstream_unavailable`, upstream не вызывается (AC-152); `Retry-After` таких отказов
    вычисляется по значению `until` записи `cb:<alias>:probe` и не меньше 1 секунды;
  - **успех** пробы → счётчик ошибок обнуляется, `open_until` сбрасывается, право на пробу удаляется,
    выключатель закрыт, обычное обслуживание возобновляется (AC-159);
  - **ошибка** пробы (5xx, таймаут, сетевая ошибка) → окно открывается снова на `HUB_CB_RESET` секунд
    немедленно, без повторного накопления `HUB_CB_FAILURES` ошибок; состояние `cb:<alias>` при этом
    равно `{failures: HUB_CB_FAILURES, open_until: now + HUB_CB_RESET}`, право на пробу удаляется и
    выдаётся заново только по истечении нового окна (AC-151, AC-167, AC-168).

  В закрытом состоянии успешный ответ upstream обнуляет счётчик ошибок. Ключ `cb:<alias>:probe`
  в закрытом состоянии не существует никогда, поэтому на обычный успешно проксированный запрос
  приходится одна операция записи состояния `cb:<alias>` в KV и **ноль** операций удаления —
  снятие права выполняется только там, где оно выдавалось, то есть при выходе из открытого
  состояния или half-open (ревизия 2.2, AC-165, AC-166).
- **R-P11. Ошибки, понятные клиенту (P-15).** Все ошибки Hub на `/mcp/{alias}` для запросов с JSON-RPC
  телом отдаются как JSON-RPC error (`jsonrpc: "2.0"`, `id` из запроса или `null`) с русским `message` и
  `data.hint_url`:

  | Ситуация | HTTP | JSON-RPC `code` | `data` |
  |---|---|---|---|
  | Нет токена / невалидный / истёкший / отозванный | 401 (+`WWW-Authenticate`) | — (тело `{error:"unauthorized"}`) | — |
  | Токен для другого alias (`aud`) | 403 | — (`{error:"forbidden"}`) | — |
  | Подключение `not_connected` | 200 | `-32002` | `hint_url`, `reason:"not_connected"` |
  | Подключение `needs_reauth` | 200 | `-32002` | `hint_url`, `reason:"needs_reauth"` |
  | Нет прав / скрытый инструмент | 200 | `-32001` | `hint_url`, `tool` |
  | Превышен лимит | 429 (+`Retry-After`) | `-32003` | `retry_after` |
  | Upstream недоступен / таймаут / circuit-breaker | 502 (503 при открытом выключателе) | `-32004` | `hint_url` |
  | Неизвестная сессия | 404 | `-32000` | — |
  | Тело больше лимита | 413 | — (`{error:"payload_too_large"}`) | — |

  Для `GET`/`DELETE` (без JSON-RPC тела) те же ситуации отдаются JSON-объектом `{error, message, hint_url}`
  c тем же HTTP-статусом. Ответы upstream (в т.ч. его собственные JSON-RPC ошибки) не переписываются.

## 14. Веб-интерфейс Hub (R-W*)

- **R-W1. Веб-сессия и вход через OIDC (P-16).** `GET /auth/login?next=<путь>`:
  - при `HUB_WEB_AUTH=keycloak` — 302 на `KEYCLOAK_ISSUER` `authorization_endpoint` (берётся из
    `/.well-known/openid-configuration` issuer'а, кэш JWKS/метаданных `KEYCLOAK_JWKS_TTL`) с
    `client_id`, `redirect_uri=<HUB>/auth/callback`, `response_type=code`, `scope=KEYCLOAK_SCOPES`,
    `state`, `nonce`, PKCE S256;
  - `GET /auth/callback`: неизвестный/повторный/истёкший `state` → 400; ошибка провайдера → 400 с русским
    текстом; успех → обмен кода на токены, проверка `id_token`: сначала **алгоритм подписи** —
    допустимы только асимметричные `RS256` и `ES256`; `none`, HS* и любой другой `alg` отклоняются
    **до** обращения к JWKS и независимо от его содержимого (защита от algorithm confusion);
    затем подпись по JWKS issuer'а, `iss`, `aud`, `exp`, `nonce` — нарушение любой проверки → 400,
    сессия не создаётся;
  - `user_id` = первый непустой из claims `preferred_username`, `email`, `sub`; `email` = claim `email`;
    выполняется upsert `users` (как R-L5, без создания `api_keys`);
  - создаётся веб-сессия: строка ≥ 32 байта случайности, в БД (`sessions`) хранится sha256, cookie
    `hub_session` — `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` (если `HUB_PUBLIC_URL` начинается с
    `https`), срок `HUB_WEB_SESSION_TTL`; вместе с ней выставляется cookie `hub_csrf` — механизм
    доставки CSRF-токена (double-submit, R-W6): те же `SameSite=Lax`, `Path=/`, `Secure` и срок, но
    `HttpOnly=false` (значение читает скрипт страницы и отправляет в заголовке `X-CSRF-Token`).
    Значение `hub_csrf` — производная (HMAC) от идентификатора сессии, самим по себе доступом оно не
    является; сверка идёт с полем формы/заголовком, а не с cookie. Далее — редирект на `next`
    (только относительный путь внутри Hub; внешний/абсолютный `next` заменяется на `/ui/connections`);
  - `POST /auth/logout` (CSRF-токен обязателен) удаляет сессию и cookie.
- **R-W2. Временный режим `HUB_WEB_AUTH=litellm` (P-16).** Страница `/auth/login` показывает тот же экран
  входа; внутри выполняется CLI-SSO флоу I-1: Hub создаёт сессию входа (R-L1), показывает ссылку/кнопку
  на `browser_url` и код `user_code`, страница опрашивает Hub (HTMX) до `ready` (R-L2, R-L3, включая
  экран выбора команды при двух и более командах). По `ready` Hub создаёт веб-сессию (R-W1) для того же
  `user_id`, сохраняет ключ (R-L5) и перенаправляет на `next`. Отличий в поведении последующих страниц нет.
- **R-W3. Экран прав (P-04, P-16).** `GET /oauth/authorize` при необходимости согласия отдаёт HTML:
  название и описание сервера, имя клиента (`client_name` или `client_id`), запрошенный scope,
  переключатель пресета (`Только чтение` отмечен по умолчанию), галочки групп `permission_model` с
  русскими `title` из каталога (группы с `preset: none` не показываются; группы `always` показаны
  включёнными и неизменяемыми), кнопки «Разрешить» и «Отмена». Отправка — `POST /oauth/consent`
  (`tx`, CSRF-токен, `preset`, `groups[]`, `action`): неизвестная/истёкшая транзакция → 400;
  чужая транзакция (другая веб-сессия) → 403; `action=deny` → редирект с `error=access_denied`;
  `action=allow` → сохранение прав в `connections`/`consents` и выдача кода (R-O7).
- **R-W4. «Мои подключения» (P-16).** `GET /ui/connections` (веб-сессия обязательна, иначе редирект на
  вход): список видимых пользователю серверов каталога со статусом (`Не подключён`, `Подключён`,
  `Нужна повторная авторизация`), текущим пресетом и группами, кнопками «Отключить»
  (`DELETE /api/me/connections/{alias}`) и «Переподключить» (переход на флоу подключения). Отображаются
  только подключения текущего пользователя.
- **R-W5. Карточка сервера (P-16).** `GET /ui/servers/{alias}` (веб-сессия обязательна): `title`,
  `description`, `owner`, `contact`, `docs_url`, `status`, режим, адрес для клиента
  (`<HUB>/mcp/{alias}` для facade), статус подключения и полный список групп прав с возможностью
  изменить их (`PUT /api/me/connections/{alias}/permissions`). Это страница, на которую указывает
  `data.hint_url` в JSON-RPC ошибках (R-P11). Неизвестный, `unconfigured` или невидимый пользователю
  alias → 404 (HTML).
- **R-W6. Общие правила страниц (P-16).** Шаблоны Jinja2, интерактивность — HTMX, весь текст на русском,
  `Content-Type: text/html; charset=utf-8`, `Cache-Control: private, no-store`. Для всех небезопасных
  методов при аутентификации по cookie обязателен CSRF-токен (скрытое поле формы или заголовок
  `X-CSRF-Token`), привязанный к веб-сессии: отсутствие/несовпадение → 403 `{error:"forbidden"}`.
  Токен доставляется браузеру cookie `hub_csrf` (R-W1) и подставляется в запрос скриптом страницы
  (заголовок `X-CSRF-Token`) либо берётся из скрытого поля формы. В HTML-страницах его значение
  выводится **только** в скрытом поле формы экрана прав (`POST /oauth/consent`) и формы
  `POST /auth/logout`; страницы `/ui/*` (`/ui/connections`, `/ui/servers/{alias}`) значение
  CSRF-токена в разметке не содержат (AC-74).
  Эндпоинты `/api/me/*` принимают **либо** Bearer-ключ LiteLLM (R-L6), **либо** веб-сессию с CSRF;
  прочие правила R-A7 сохраняются. HTML не содержит токенов, секретов и внутренних URL (`upstream_url`).

## 15. Модель данных и миграции (R-M*)

- **R-M1. Миграции: Alembic (решение).** `create_all` заменяется на Alembic. Каталог версий —
  внутри пакета (`src/hub/migrations/`, `script_location` задаётся программно через `alembic.config.Config`,
  файл `alembic.ini` в корне не требуется — корень вне зоны записи dev-агента). Базовая ревизия
  повторяет схему I-1 (`users`, `api_keys`, `connections`, `audit_log`), следующая добавляет объекты I-3.
  При `HUB_DB_AUTO_MIGRATE=true` (дефолт) миграции применяются при старте (lifespan) до обслуживания
  запросов — поведение AC-65 сохраняется. На PostgreSQL миграция выполняется под advisory-блокировкой
  (`pg_advisory_xact_lock` на время транзакции): при одновременном старте нескольких реплик вторая
  ждёт первую и затем видит схему уже на `head` (`upgrade` — no-op) вместо падения с ошибкой миграции;
  для SQLite блокировка не берётся (запись сериализует сам файл). См. также R-N5. CLI: `mcp-hub db upgrade [--revision head]`,
  `mcp-hub db current` (печатает текущую ревизию, код 0). Ошибка миграции → приложение не поднимается,
  сообщение содержит имя ревизии.
- **R-M2. Новые таблицы.**

  | Таблица | Поля |
  |---|---|
  | `oauth_clients` | `id` PK; `client_id` TEXT UNIQUE; `client_name` TEXT NULL; `redirect_uris` JSON; `grant_types` JSON; `response_types` JSON; `token_endpoint_auth_method` TEXT (`none`); `scope` TEXT NULL; `created_at`; `created_ip` TEXT NULL; `last_used_at` DATETIME NULL |
  | `oauth_codes` | `id` PK; `code_sha256` TEXT UNIQUE (индекс); `client_id` TEXT (индекс); `user_id` FK→users; `alias` TEXT; `connection_id` FK→connections NULL; `redirect_uri` TEXT; `code_challenge` TEXT; `code_challenge_method` TEXT (`S256`); `scope` TEXT; `resource` TEXT; `created_at`; `expires_at`; `used_at` DATETIME NULL |
  | `refresh_tokens` | `id` PK; `token_sha256` TEXT UNIQUE (индекс); `chain_id` TEXT (индекс); `parent_id` INTEGER NULL; `client_id` TEXT; `user_id` FK→users; `connection_id` FK→connections; `alias` TEXT; `scope` TEXT; `access_jti` TEXT NULL; `access_exp` DATETIME NULL; `status` TEXT (`active`\|`rotated`\|`revoked`); `created_at`; `expires_at`; `used_at` DATETIME NULL |
  | `upstream_tokens` | `id` PK; `connection_id` FK→connections UNIQUE; `access_token_enc` TEXT; `refresh_token_enc` TEXT NULL; `token_type` TEXT; `scopes` JSON; `expires_at` DATETIME NULL; `obtained_at`; `updated_at`; `refresh_failed_at` DATETIME NULL; `last_error` TEXT NULL (код ошибки, без секретов) |
  | `sessions` | `id` PK; `session_sha256` TEXT UNIQUE (индекс); `csrf_sha256` TEXT; `user_id` FK→users (индекс); `auth_method` TEXT (`keycloak`\|`litellm`); `created_at`; `expires_at` |
  | `consents` | `id` PK; `user_id` FK→users; `client_id` TEXT; `alias` TEXT; `scope` TEXT; `preset` TEXT; `groups` JSON; `created_at`; `updated_at`; UNIQUE(`user_id`,`client_id`,`alias`) |

- **R-M3. Изменения существующих таблиц.** `connections` дополняется: `needs_reauth_reason` TEXT NULL;
  `last_refresh_at` DATETIME NULL; `revision` INTEGER NOT NULL DEFAULT 0 (инкремент при любом изменении
  прав/статуса — используется как часть ключей кэша); `provider_account` TEXT NULL (идентификатор
  пользователя в целевой системе, если система его вернула). Существующие поля и уникальность
  (`user_id`, `alias`) сохраняются; допустимые значения `status` — `not_connected` | `connected` |
  `needs_reauth` (как в I-1). Таблицы `users`, `api_keys`, `audit_log` не меняются.
- **R-M4. KeyValueStore.** Протокол `KeyValueStore` (R-S2) расширяется атомарными операциями:
  `set_if_absent(key, value, ttl) -> bool` (блокировки), `incr(key, delta, ttl) -> int` /
  `decr(key, delta) -> int` (счётчик SSE-потоков); обе реализации (in-memory и Redis — через `SET NX PX`
  и Lua/`INCRBY`) их поддерживают. Ключи ревизии 2:

  | Ключ | Значение | TTL |
  |---|---|---|
  | `oauthtx:<tx_id>` | параметры `/oauth/authorize`, `user_id`, alias, scope, шаг флоу, `provider_state`, `provider_verifier` | `HUB_OAUTH_TX_TTL` |
  | `oauthstate:<state>` | `tx_id` (для `/oauth/callback/{alias}`) | `HUB_OAUTH_TX_TTL` |
  | `jtiden:<jti>` | `1` (denylist отозванных access-токенов) | до `exp` токена |
  | `conn:<user_id>:<alias>` | `{connection_id, status, preset, groups, revision, token_expires_at, access_token_enc}` | `HUB_CONNECTION_CACHE_TTL` |
  | `mcpsess:<alias>:<client_session_id>` | `{user_id, alias, connection_id, upstream_session_id, protocol_version, client_info, upstream_last_used_at}` | `HUB_CLIENT_SESSION_TTL` |
  | `toolscache:<alias>:<catalog_version>:<hash прав>` | результат `tools/list` до фильтрации | `HUB_TOOLS_CACHE_TTL` |
  | `refreshlock:<connection_id>` | владелец блокировки | 30 с |
  | `cb:<alias>` | `{failures, open_until}` | `HUB_CB_RESET` × 2 |
  | `cb:<alias>:probe` | право на единственный пробный запрос в half-open (R-P10) | `max(HUB_CB_RESET, HUB_UPSTREAM_TIMEOUT, HUB_UPSTREAM_SSE_IDLE_TIMEOUT)` + `HUB_CB_PROBE_GRACE` |
  | `sse:<user_id>` | счётчик активных SSE-потоков | 1 ч (обновляется) |
  | `rl:mcp:<user_id>:<alias>`, `rl:register:<ip>`, `rl:token:<client_id>:<ip>` | окна rate-limit | 60 с |
  | `oidc:jwks:<issuer>`, `oidc:meta:<issuer>` | JWKS и метаданные OIDC | `KEYCLOAK_JWKS_TTL` |

  Пояснения к таблице:
  - alias входит в **ключ** записи сессии (`mcpsess:<alias>:<client_session_id>`), чтобы живые сессии
    сервера считались по фактическим записям (`count_prefix`, R-N1). Отдельного счётчика сессий
    (ключа вида `mcpsessn:<alias>`) нет: он завышал бы значение на сессии, истёкшие по
    `HUB_CLIENT_SESSION_TTL` без явного `DELETE /mcp/{alias}`;
  - `access_token_enc` в кэше подключения — **шифртекст** Fernet (тот же, что в БД), нужен для
    успешного горячего пути без обращения к БД (AC-99). Наружу он не отдаётся никогда (R-B9);
    при смене `HUB_ENCRYPTION_KEY` расшифровка не удаётся и запрос уходит по обычному пути
    обновления токена (при неуспехе — `needs_reauth`, R-B5);
  - `cb:<alias>:probe` берётся только через `set_if_absent` (атомарно) и удаляется по итогам пробы
    — как при успехе, так и при провале (R-P10). Его TTL заведомо больше самого долгого запроса
    к upstream (отсюда слагаемые `HUB_UPSTREAM_TIMEOUT` / `HUB_UPSTREAM_SSE_IDLE_TIMEOUT` и запас
    `HUB_CB_PROBE_GRACE`): TTL — только страховка от потери права при аварии реплики, штатно право
    снимается явным удалением (ревизия 2.2).

- **R-M5. Совместимость с БД I-1.** Если в БД уже есть таблицы I-1, созданные `create_all`, и нет таблицы
  `alembic_version` — при старте (или `mcp-hub db upgrade`) Hub помечает БД базовой ревизией (`stamp`)
  и применяет последующие миграции; данные `users`, `api_keys`, `connections`, `audit_log` сохраняются.
  На пустой БД применяются все ревизии подряд. Повторный запуск — идемпотентен.

## 16. Наблюдаемость, эксплуатация, тесты (R-N*)

- **R-N1. Метрики (P-14).** К метрикам R-S4 добавляются:
  `hub_mcp_requests_total{alias,method,status}` (counter), `hub_mcp_request_duration_seconds{alias}`
  (histogram, даёт p50/p95), `hub_upstream_sessions_active{alias}` (gauge — число **живых** записей `mcpsess:<alias>:*` в KV,
  считается на момент сбора метрик через `count_prefix`; отдельного инкрементального счётчика нет,
  поэтому сессии, истёкшие по `HUB_CLIENT_SESSION_TTL` без явного `DELETE /mcp/{alias}`, gauge не
  завышают),
  `hub_upstream_errors_total{alias,kind}` (`kind ∈ {timeout, http_5xx, network, circuit_open}`),
  `hub_oauth_tokens_issued_total{grant}` (`grant ∈ {authorization_code, refresh_token}`),
  `hub_token_refresh_total{alias,result}` (`result ∈ {ok, failed}`). Значения токенов, `user_id` и другие
  персональные данные в лейблы не попадают.
- **R-N2. Аудит (P-06, P-09).** Новые действия в `audit_log`: `oauth_client_registered`,
  `oauth_code_issued`, `oauth_token_issued`, `oauth_token_revoked`, `oauth_refresh_reuse_detected`,
  `connection_connected`, `connection_refreshed`, `connection_needs_reauth`,
  `connection_permissions_changed`, `connection_disconnected`, `web_login`. `details` содержит только
  несекретные поля (`client_id`, `alias`, `grant`, `preset`, `groups`, `reason`, код ошибки); значения
  токенов, кодов, `code_verifier`, секретов — никогда (R-T4). Отдельные MCP-вызовы в аудит не пишутся
  (только метрики и логи).
- **R-N3. `deploy/.env.example` (P-19).** Файл содержит все переменные таблицы R-T1 (закомментированные
  или с дефолтными значениями) с русскими пояснениями; секреты — с пустым значением/`change-me`.
- **R-N4. Тесты и моки (P-18).** Тесты — только против локальных моков, без сети. Обязательный состав:

  | Мок | Ветки |
  |---|---|
  | AS целевой системы (`authorize_url`, `token_url`, `revoke_url` из тестового каталога, respx) | обмен кода → `{access_token, refresh_token, expires_in, token_type, scope}`; проверка `client_id`/`client_secret`/`redirect_uri`/`code_verifier`; refresh с новым refresh; refresh без нового refresh; `400 invalid_grant`; `500`; сетевая ошибка; `revoke` → 200 |
  | Upstream MCP (streamable-http, `upstream_url`, respx) | `POST /mcp`: `initialize` (выдаёт `Mcp-Session-Id`), `tools/list` (`application/json`), `tools/call`, ответ `text/event-stream` из нескольких событий; `401` (истёк токен системы); `404` (неизвестная сессия); `405` на `GET` без сессии; `500`; таймаут; `DELETE /mcp` → 204; фиксация полученных заголовков для проверок R-P2 |
  | OIDC (Keycloak) | `/.well-known/openid-configuration`, `/authorize` (редирект с `code`), `/token` (`id_token` с корректной подписью, `nonce`, `iss`, `aud`), JWKS; ветки: неверная подпись, неверный `nonce`, ошибка `access_denied` |
  | LiteLLM (моки I-1) | для `HUB_WEB_AUTH=litellm` и обратной совместимости — без изменений |

  Обязателен сквозной сценарий: DCR → `/oauth/authorize` (вход в веб) → OAuth целевой системы → экран
  прав → код → `/oauth/token` → `initialize`/`tools/list`/`tools/call` через proxy → refresh → revoke.
  Нагрузочный smoke (100 параллельных SSE-потоков через мок) — в отдельном маркере pytest
  (`@pytest.mark.load`), в обычном прогоне не выполняется. Часы подменяемые (TTL кодов, токенов, idle
  сессий, кэшей, окон rate-limit), KV — in-memory, БД — SQLite (после миграций).

- **R-N5. Предупреждения и блокировки при старте (эксплуатация).**
  - **Пустой `HUB_REDIS_URL`.** KeyValueStore по умолчанию — in-memory, то есть состояние живёт в
    памяти процесса. При старте без `HUB_REDIS_URL` Hub пишет в лог запись уровня **WARNING**,
    содержащую имя переменной `HUB_REDIS_URL` и пояснение: реплики не делят denylist отозванных
    токенов (`jtiden:*`), виртуальные MCP-сессии (`mcpsess:*`), окна rate-limit, состояние
    circuit-breaker (`cb:*`) и кэш подключений (`conn:*`), поэтому запуск в нескольких репликах
    требует общего Redis. Старт при этом **не** прерывается (однорепличный и тестовый запуск
    остаются штатными, R-T3); при заданном `HUB_REDIS_URL` предупреждение отсутствует. То же
    пояснение обязательно в `deploy/.env.example` (R-N3).
  - **Одновременные миграции.** См. R-M1: при `HUB_DB_AUTO_MIGRATE=true` на PostgreSQL миграции
    выполняются под advisory-блокировкой, поэтому одновременный старт реплик не роняет «проигравшую».
    Альтернатива для эксплуатации — `HUB_DB_AUTO_MIGRATE=false` и отдельный job `mcp-hub db upgrade`
    (обе схемы поддерживаются).

## 17. Контракты эндпоинтов I-3

| Метод и путь | Auth | Запрос | Ответы |
|---|---|---|---|
| `GET /.well-known/oauth-authorization-server` | нет | — | 200 метаданные AS (R-O1) |
| `GET /.well-known/oauth-authorization-server/mcp/{alias}` | нет | — | 200 те же метаданные; 404 `not_found` |
| `GET /.well-known/oauth-protected-resource/mcp/{alias}` | нет | — | 200 PRM (R-O2); 404 `not_found` |
| `POST /oauth/register` | нет, rate-limit | JSON RFC 7591 | 201 `{client_id, …}`; 400 `invalid_redirect_uri`/`invalid_client_metadata`; 429 `rate_limited` |
| `GET /oauth/authorize` | веб-сессия | query RFC 6749 + PKCE + `resource` | 302 на `redirect_uri` c `code`/`error`; 302 на `/auth/login`; 200 HTML (экран прав); 400 HTML |
| `POST /oauth/consent` | веб-сессия + CSRF | форма `{tx, preset, groups[], action}` | 302 на `redirect_uri`; 400; 403 |
| `GET /oauth/callback/{alias}` | веб-сессия | `code`/`error`, `state` | 302 (продолжение флоу); 400 `invalid_state`; 502 `upstream_auth_failed` |
| `POST /oauth/token` | нет (public client), rate-limit | form: `authorization_code` \| `refresh_token` | 200 `{access_token, token_type, expires_in, refresh_token, scope}`; 400 `invalid_grant`/`invalid_request`/`invalid_scope`; 401 `invalid_client`; 429 |
| `POST /oauth/revoke` | нет | form `{token, token_type_hint?}` | 200 `{}`; 400 `invalid_request` |
| `POST /mcp/{alias}` | Bearer JWT Hub | JSON-RPC (объект или batch) | 200 (JSON или SSE, потоково); 401/403; 404; 413; 429; 502/503 (см. R-P11) |
| `GET /mcp/{alias}` | Bearer JWT Hub | `Mcp-Session-Id`, `Last-Event-ID` | 200 SSE (потоково); 401/403/404/429; 502/503 |
| `DELETE /mcp/{alias}` | Bearer JWT Hub | `Mcp-Session-Id` | 200/204; 401/403/404 |
| `PUT /api/me/connections/{alias}/permissions` | Bearer ключ LiteLLM или веб-сессия + CSRF | `{preset, groups[]}` | 200 `{alias, status, preset, groups}`; 400 `invalid_request`; 401; 404 |
| `DELETE /api/me/connections/{alias}` | Bearer ключ LiteLLM или веб-сессия + CSRF | — | 200 `{alias, status:"not_connected"}`; 401; 404 |
| `GET /auth/login` | нет | `?next=` | 302 на OIDC / 200 HTML (режим `litellm`) |
| `GET /auth/callback` | нет | `code`, `state` | 302 на `next`; 400 |
| `POST /auth/logout` | веб-сессия + CSRF | — | 302 на `/auth/login`; 403 |
| `GET /ui/connections` | веб-сессия | — | 200 HTML; 302 на вход |
| `GET /ui/servers/{alias}` | веб-сессия | — | 200 HTML; 302 на вход; 404 HTML |

Примеры ошибок ревизии 2:

```json
{"error": "invalid_grant", "error_description": "Код авторизации недействителен или уже использован"}
{"jsonrpc": "2.0", "id": 3, "error": {"code": -32002,
  "message": "Подключение к GitLab требует повторной авторизации",
  "data": {"reason": "needs_reauth", "hint_url": "https://hub.example/ui/servers/gitlab"}}}
{"jsonrpc": "2.0", "id": 7, "error": {"code": -32001,
  "message": "Инструмент create_merge_request недоступен с текущими правами",
  "data": {"tool": "create_merge_request", "hint_url": "https://hub.example/ui/servers/gitlab"}}}
```

## 18. Принятые решения ревизии 2

Даны человеком (считаются решёнными, не переспрашиваются):

30. Hub для facade-серверов — стандартный MCP authorization server: RFC 9728 PRM на
    `/.well-known/oauth-protected-resource/mcp/{alias}`, RFC 8414 на `/.well-known/oauth-authorization-server`,
    RFC 7591 DCR публичного клиента (`token_endpoint_auth_method=none`), PKCE S256 обязателен,
    гранты `authorization_code` + `refresh_token` с ротацией и отзывом цепочки, `/oauth/revoke`.
31. Access-токен Hub — JWT HS256 по `HUB_SECRET_KEY`, `exp` 1 ч, claims `aud`/`scope`/`sub`/`cid`/`jti`;
    refresh — opaque, 30 дней; проверка на горячем пути без БД (подпись + denylist `jti` в KV).
32. Веб-сессия для `/oauth/authorize` — OIDC Keycloak (`KEYCLOAK_*`) **и** временный режим
    `HUB_WEB_AUTH=litellm` (CLI-SSO флоу I-1 внутри браузерной страницы); описаны оба.
33. Брокер токенов целевых систем — общий клиент `authorization_code` с параметрами из `catalog.yaml`
    (GitLab с PKCE; Jira/Confluence DC `/rest/oauth2/latest/*`, `READ`/`WRITE`); токены шифруются ключом
    `HUB_ENCRYPTION_KEY`; фоновое обновление с блокировкой в KV; при провале — `needs_reauth`.
34. Экран прав при `authorize`: пресет `readonly` по умолчанию, галочки групп `permission_model`;
    `HUB_CONSENT=always|remember`.
35. MCP-proxy `POST|GET|DELETE /mcp/{alias}`: потоковый streamable-http, удаление клиентского
    `Authorization`, подстановка `credential_headers` (`{{access_token}}`) и `static_headers`, заголовок
    групп (`always` + выбранные), виртуализация сессий (`Mcp-Session-Id` выдаёт Hub, upstream-сессия
    лениво, idle TTL 600 с, прозрачное пересоздание с повтором `initialize`), кэш `tools/list` 300 с,
    фильтр инструментов (скрытый `tools/call` → `-32001 forbidden`), rate-limit, лимит SSE,
    circuit-breaker, JSON-RPC ошибки с `data.hint_url`.
36. `remote-config` / `api/me/connections` отражают `connected|needs_reauth`; `mcp.<alias>` в well-known —
    с `oauth: {}`. Совместимость: `/remote-config` по-прежнему включает **только** `connected`
    (`{enabled:true}`, AC-61/AC-62 не меняются); `needs_reauth` виден в `/api/me/connections`,
    `/api/catalog` и на страницах Hub.
37. Страницы Hub: вход, экран прав, «Мои подключения», карточка сервера (Jinja2 + HTMX, русский язык).
38. Миграции БД — **Alembic** (R-M1); явные `ALTER` при старте отвергнуты как неотслеживаемые.
39. Тесты — только против локальных моков: мок AS целевых систем, мок upstream MCP (SSE, сессии,
    401/404/5xx), мок OIDC.
40. Все новые настройки `HUB_*`/`KEYCLOAK_*` — с дефолтами в таблице R-T1.

Приняты spec-агентом как рабочие допущения (детализация решений выше, требований не меняют):

41. Шифрование токенов систем — Fernet (ключ `HUB_ENCRYPTION_KEY` уже валидируется как Fernet в R-K2);
    вариант «AES-GCM» из требования покрывается тем же полем настройки.
42. `HUB_WEB_AUTH` по умолчанию — `litellm`: иначе окружение I-1 перестало бы стартовать (D-5 не выдан).
43. `HUB_CONSENT` по умолчанию — `always` (более строгий вариант).
44. Ошибки `authorize` с неизвестным `client_id`/чужим `redirect_uri` не редиректятся (RFC 6749 §4.1.2.1);
    остальные — редиректом с `error`/`state`.
45. Повторное предъявление кода отзывает выданную по нему цепочку токенов (RFC 6819).
46. `redirect_uri` сравнивается точно; для loopback допускается другой порт (RFC 8252).
47. Ключи denylist — `jtiden:<jti>` с TTL до `exp`; отзыв access влечёт отзыв связанной цепочки refresh.
48. Alias определяется из `resource`, при его отсутствии — из префикса `scope`; конфликт → `invalid_request`.
49. `scope` по умолчанию — `<alias>:readonly`; `readonly`-scope не включает группы с `preset: readwrite`.
50. Заголовок групп — идентификаторы через запятую, `always` сначала, порядок каталога, без дублей.
51. Фильтр инструментов — необязательные поля каталога (`tool_filter`, `groups[].tools`); их отсутствие
    означает «все инструменты доступны», поэтому текущий `catalog.yaml` менять не требуется.
52. Кэш `tools/list` хранит нефильтрованный ответ; ключ включает `catalog_version` и хеш прав;
    запросы с `cursor` не кэшируются.
53. Клиентская MCP-сессия живёт `HUB_CLIENT_SESSION_TTL` (24 ч), upstream-сессия — `HUB_UPSTREAM_IDLE_TTL`
    (10 мин) с прозрачным пересозданием; неизвестная клиентская сессия → 404 (клиент переинициализируется).
54. Повтор запроса после пересоздания сессии или обновления токена — ровно один раз.
55. HTTP-статусы MCP-ошибок: 401/403 (токен), 200 + JSON-RPC `-32001`/`-32002` (права/подключение),
    429 (лимиты), 413 (тело), 502/503 (upstream/выключатель), 404 (сессия/alias).
56. Веб-сессия — cookie `hub_session` (HttpOnly, SameSite=Lax, Secure при https), хранение sha256 в БД;
    CSRF-токен обязателен для небезопасных методов при cookie-аутентификации.
57. `user_id` из OIDC — `preferred_username` → `email` → `sub` (первый непустой).
58. Дополнительная таблица `consents` (для `HUB_CONSENT=remember`) — сверх перечня решения 30.
59. `KEYCLOAK_*` читаются без префикса `HUB_`; обязательны только при `HUB_WEB_AUTH=keycloak`.
60. Документация P-19 (`docs/`) — вне зоны записи конвейера; в объёме итерации только `deploy/.env.example`.

## 19. Покрытие правил ревизии 2 критериями приёмки

| Правило | Критерии |
|---|---|
| R-T1 | AC-70, AC-73 |
| R-T2 | AC-71, AC-72 |
| R-T3 | AC-73 |
| R-T4 | AC-74 |
| R-T5 | AC-153, AC-160, AC-161, AC-162, AC-163, AC-164 |
| R-O1 | AC-75, AC-76 |
| R-O2 | AC-77, AC-78, AC-79 |
| R-O3 | AC-80, AC-81, AC-82, AC-148 |
| R-O4 | AC-83, AC-84, AC-148 |
| R-O5 | AC-85, AC-86 |
| R-O6 | AC-87, AC-88, AC-89 |
| R-O7 | AC-90 |
| R-O8 | AC-91, AC-92, AC-93, AC-155 |
| R-O9 | AC-94 |
| R-O10 | AC-95, AC-96 |
| R-O11 | AC-97 |
| R-O12 | AC-98, AC-99, AC-130 |
| R-O13 | AC-100, AC-101 |
| R-B1 | AC-102 |
| R-B2 | AC-103 |
| R-B3 | AC-104 |
| R-B4 | AC-105, AC-106 |
| R-B5 | AC-107, AC-108, AC-149 |
| R-B6 | AC-109 |
| R-B7 | AC-110, AC-111 |
| R-B8 | AC-112 |
| R-B9 | AC-113 |
| R-P1 | AC-114 |
| R-P2 | AC-115 |
| R-P3 | AC-116 |
| R-P4 | AC-117 |
| R-P5 | AC-118, AC-119 |
| R-P6 | AC-120 |
| R-P7 | AC-121 |
| R-P8 | AC-122, AC-123, AC-124, AC-150 |
| R-P9 | AC-125, AC-126, AC-127 |
| R-P10 | AC-128, AC-151, AC-152, AC-157, AC-158, AC-159, AC-165, AC-166, AC-167, AC-168 |
| R-P11 | AC-129, AC-130 |
| R-W1 | AC-131, AC-132, AC-154 |
| R-W2 | AC-133 |
| R-W3 | AC-134 |
| R-W4 | AC-135 |
| R-W5 | AC-136 |
| R-W6 | AC-137 |
| R-M1 | AC-138 |
| R-M2 | AC-139 |
| R-M3 | AC-140 |
| R-M4 | AC-141 |
| R-M5 | AC-142 |
| R-N1 | AC-143 |
| R-N2 | AC-144 |
| R-N3 | AC-145 |
| R-N4 | AC-146, AC-147 |
| R-N5 | AC-156 |

Сквозной сценарий P-18 — AC-147; обязательные негативные сценарии требования: чужой redirect — AC-81,
AC-83, AC-148; неверный PKCE — AC-92; повтор кода — AC-90; повтор refresh с отзывом цепочки — AC-96;
истёкший upstream-токен без refresh — AC-108; `needs_reauth` — AC-107, AC-129, AC-149; лимиты — AC-82,
AC-101, AC-125, AC-126, AC-127; idle-сессия и пересоздание — AC-118, AC-119; кэш `tools/list` — AC-121;
фильтр инструментов — AC-122, AC-123, AC-124, AC-150; провал пробы circuit-breaker и параллельные
запросы во время неё — AC-151, AC-152; `id_token` с недопустимым алгоритмом — AC-154; обмен кода без
`redirect_uri` — AC-155; старт без общего KV — AC-156.

Критерии ревизии 2.1 (AC-150…AC-156) добавлены к существующим; ни один AC ревизии 2 не удалён,
изменены только формулировки AC-73, AC-74, AC-83, AC-98, AC-141, AC-148 (см. блок «Ревизия 2.1»
в начале документа) — их ID и проверяемое поведение сохранены.

Критерии ревизии 2.2 (AC-157…AC-168, перенос HAC-01…HAC-12 бэклога Hub) добавлены к существующим;
ни один прежний AC не удалён, изменена только формулировка AC-145 — в перечень переменных таблицы
R-T1 добавлено имя `HUB_CB_PROBE_GRACE`. Обязательные негативные сценарии ревизии 2.2: вторая проба
при незавершённой первой — AC-157; невалидный, слишком длинный и списочный `X-Forwarded-For` —
AC-160, AC-161, AC-164; отсутствие отклонённого значения в журнале — AC-163; провал пробы — AC-167.
