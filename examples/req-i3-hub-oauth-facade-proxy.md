# Требование I-3: Hub — стандартная MCP-авторизация (OAuth-фасад) и MCP-proxy для существующих серверов

Продолжение I-1 (`spec.md` ревизия 1, AC-01…AC-69 сохраняются; новая ревизия добавляет
правила и AC-70+). Контекст — `docs/req-mvp.md` rev 0.2 (§4, §6.4, §6.6, §8), каталог —
`catalog.yaml` (серверы `mode: facade`: `gitlab`, `gitlab-platform`, `jira`, `confluence`).
**[проверено]** — снято с живых систем/кода; **[проверить]** — гипотеза.

## 1. Цель

Для каждого facade-сервера каталога Hub выглядит для любого MCP-клиента как удалённый MCP-сервер
со стандартной авторизацией (MCP Authorization: RFC 9728 + RFC 8414 + RFC 7591 + PKCE), а
внутри — брокер: хранит и обновляет токены целевых систем пользователя, подставляет их в
заголовки существующих облачных MCP AI Lab (которые не меняются) и принудительно применяет
права. Первый сервер — GitLab (coderepo), затем GitLab Platform, Jira, Confluence.

## 2. Факты [проверено]

- Клиент OpenCode 1.17.9: `mcp.<alias>` `type: remote`, `oauth: { clientId?, clientSecret?,
  scope? }` или auto-discovery; DCR (RFC 7591), PKCE S256; callback
  `http://127.0.0.1:19876/mcp/oauth/callback` (переопределяется `oauth.redirectUri` /
  `oauth.callbackPort`); статусы `needs_auth`, `needs_client_registration`; токены в
  `mcp-auth.json`. Claude Desktop/Cursor/VS Code — тот же протокол.
- Пример метаданных AS у LiteLLM 1.94: `/.well-known/oauth-authorization-server` →
  `{issuer, authorization_endpoint, token_endpoint, response_types_supported:["code"],
  grant_types_supported:["authorization_code"], code_challenge_methods_supported:["S256"]}`;
  `/.well-known/oauth-protected-resource` → `{resource, authorization_servers:[…]}`.
- Upstream MCP AI Lab: streamable-http, `POST /mcp` (JSON-RPC, ответ `application/json` или
  `text/event-stream`), заголовок сессии `Mcp-Session-Id`, `GET /mcp` — SSE-поток (GitLab MCP
  отвечает 405 на GET без сессии), `DELETE /mcp` — закрыть сессию; GitLab MCP: заголовки
  `Private-Token` **или** `Authorization: Bearer` **[проверить для OAuth-токена]**,
  `Enabled-Groups`; сессии в памяти, `maxSessions=1000`. Atlassian MCP: `X-Atlassian-*-Personal-Token`,
  `X-Atlassian-*-Url`, `Enabled-Groups`; OAuth-токен DC в этом заголовке **[проверить]**.
- OAuth целевых систем: GitLab (`/oauth/authorize`, `/oauth/token`, `/oauth/revoke`, PKCE,
  access ~2 ч + refresh), Jira 9.12 / Confluence 7.19 (`/rest/oauth2/latest/authorize|token`,
  scopes `READ`/`WRITE`, refresh; ротация refresh — **[проверить]**).
- Веб-вход в Hub — Keycloak OIDC (клиент `opencode-mcp-hub`, зависимость D-5); до его выдачи —
  мок OIDC в тестах и режим `HUB_WEB_AUTH=litellm` (вход через тот же CLI-SSO флоу LiteLLM,
  что и в I-1) как временный вариант.

## 3. Требования

### 3.1. Hub как OAuth authorization server (для facade-серверов)
- P-01. `GET /.well-known/oauth-authorization-server` (и вариант с суффиксом пути ресурса)
  → issuer = `HUB_PUBLIC_URL`, `authorization_endpoint=/oauth/authorize`,
  `token_endpoint=/oauth/token`, `registration_endpoint=/oauth/register`,
  `revocation_endpoint=/oauth/revoke`, `response_types_supported:["code"]`,
  `grant_types_supported:["authorization_code","refresh_token"]`,
  `code_challenge_methods_supported:["S256"]`, `token_endpoint_auth_methods_supported:["none"]`,
  `scopes_supported` — объединение scopes каталога (`<alias>:readonly`, `<alias>:readwrite`).
- P-02. `GET /.well-known/oauth-protected-resource/mcp/{alias}` (и `GET /mcp/{alias}` без токена →
  `401` + `WWW-Authenticate: Bearer resource_metadata="<url>"`) → `{resource: "<HUB>/mcp/{alias}",
  authorization_servers:["<HUB>"], scopes_supported:[…], bearer_methods_supported:["header"]}`.
- P-03. `POST /oauth/register` (RFC 7591, public client): принимает `redirect_uris`,
  `client_name`, `grant_types`, `token_endpoint_auth_method:"none"`; валидирует redirect
  (`http://127.0.0.1:*`, `http://localhost:*`, `https://*` из allow-list `HUB_OAUTH_ALLOWED_REDIRECTS`);
  возвращает `client_id` (+ `client_id_issued_at`); хранит клиента; rate-limit по IP.
- P-04. `GET /oauth/authorize` (`response_type=code`, `client_id`, `redirect_uri` ∈ зарегистрированных,
  `code_challenge`+`S256`, `state`, `scope`, `resource=<HUB>/mcp/{alias}`): (1) требует
  веб-сессии пользователя Hub (нет — редирект на вход, затем возврат); (2) определяет alias
  по `resource`/scope; (3) если у пользователя нет валидного подключения к целевой системе для
  этого alias — запускает OAuth целевой системы (`/oauth/callback/{alias}` — фиксированный
  callback, state с привязкой к транзакции, PKCE если поддерживается) и после успеха сохраняет
  токены (зашифрованы) в `connections`; (4) показывает экран прав (пресет `readonly` по
  умолчанию, галочки групп из `permission_model`, «Разрешить/Отмена»; для повторного клиента —
  «запомнить» настройка `HUB_CONSENT=always|remember`); (5) выдаёт одноразовый код (TTL 60 с),
  редирект на `redirect_uri` с `code`+`state`. Транзакции — в Redis/БД с TTL 10 мин.
- P-05. `POST /oauth/token`: `authorization_code` (+`code_verifier`, `redirect_uri`,
  `client_id`) → `access_token` (JWT Hub, HS256/EdDSA по `HUB_SECRET_KEY`, `exp` 1 ч,
  claims: `sub`=user_id, `aud`=`<HUB>/mcp/{alias}`, `scope`, `cid`=connection id, `jti`),
  `refresh_token` (opaque, 30 дней, ротация: старый отзывается при использовании, повторное
  использование → отзыв всей цепочки), `token_type: Bearer`, `expires_in`, `scope`;
  `refresh_token` grant → новая пара; ошибки RFC 6749 (`invalid_grant`, `invalid_client`,
  `invalid_request`) в JSON.
- P-06. `POST /oauth/revoke` (`token`, `token_type_hint`) → 200 всегда; отзыв refresh отзывает и
  выданные по нему access (denylist `jti` до истечения). Отключение на странице Hub /
  `DELETE` через API отзывает все токены клиентов по подключению.
- P-07. Проверка access-токена на горячем пути без БД: подпись + `exp` + `aud` + denylist в
  Redis (кэш); данные подключения (upstream-токен, права) — Redis-кэш с инвалидацией при
  изменении.

### 3.2. Брокер токенов целевых систем
- P-08. Провайдеры OAuth целевых систем описываются в `catalog.yaml` (`auth`); реализация —
  общий `authorization_code` клиент (authlib) с параметрами провайдера: GitLab (PKCE, scopes
  по пресету), Jira/Confluence DC (`READ`/`WRITE`); токены `connections` (access, refresh,
  expires_at, scopes, preset, groups) — шифрование AES-GCM/Fernet ключом `HUB_ENCRYPTION_KEY`.
- P-09. Обновление upstream-токенов: фоновая задача за N минут до `expires_at` и «по требованию»
  при 401 от системы/upstream; блокировка на подключение (Redis lock) против гонок реплик;
  при провале refresh — статус подключения `needs_reauth`, MCP-вызовы отвечают JSON-RPC ошибкой
  с кодом и ссылкой на витрину/Hub, access-токены клиента остаются валидны (пере-авторизация
  восстанавливает подключение без нового DCR).
- P-10. Смена пресета/групп прав на странице Hub (`PUT /api/me/connections/{alias}/permissions`)
  применяется к следующим MCP-вызовам без переподключения (кэш инвалидируется); при расширении
  scopes целевой системы (readonly→readwrite) требуется повторный OAuth системы — Hub
  переводит подключение в `needs_reauth` с пояснением.

### 3.3. MCP-proxy
- P-11. `POST /mcp/{alias}`, `GET /mcp/{alias}`, `DELETE /mcp/{alias}` (Bearer access-токен Hub):
  прозрачное потоковое проксирование streamable-http на `upstream_url` (тело как есть,
  `Accept`, `Mcp-Session-Id`, `MCP-Protocol-Version`, `Last-Event-ID`; ответ — с тем же
  `Content-Type`, SSE без буферизации, таймауты чтения `HUB_UPSTREAM_TIMEOUT`); клиентский
  `Authorization` удаляется; подставляются `credential_headers` из каталога (шаблон
  `{{access_token}}` и статические), `static_headers`, заголовок групп (`Enabled-Groups`) из
  прав пользователя (`always` + выбранные).
- P-12. Виртуализация сессий: `Mcp-Session-Id`, который видит клиент, выдаёт Hub; сессия к
  upstream создаётся лениво при первом `initialize`/вызове и хранится в Redis
  (`client_session → upstream_session, alias, user`), закрывается после `HUB_UPSTREAM_IDLE_TTL`
  (по умолчанию 10 мин) и прозрачно пересоздаётся при следующем вызове (повтор `initialize`
  с теми же `clientInfo`/`protocolVersion`, затем исходный запрос); при `404`/невалидной сессии
  upstream — то же. Реплики Hub без sticky-сессий.
- P-13. Кэш `tools/list` по (`alias`, права, версия каталога) на `HUB_TOOLS_CACHE_TTL`
  (по умолчанию 5 мин) в Redis; фильтрация инструментов по правам (`permission_model
  tool_filter`, маски allow/deny) при отдаче `tools/list` и отказ `tools/call` для скрытого
  инструмента (JSON-RPC ошибка `-32001 forbidden` с подсказкой).
- P-14. Защита: rate-limit на пользователя/alias (`HUB_RATE_LIMIT_MCP`), лимит одновременных
  SSE-потоков на пользователя, ограничение размера тела, circuit-breaker на upstream
  (после N ошибок 5xx/таймаутов подряд — быстрые ошибки `upstream_unavailable` на T секунд),
  метрики: сессии upstream (активные), RPS, ошибки, p50/p95 по alias.
- P-15. Ошибки понятны клиенту: не подключён / нужна пере-авторизация / нет прав / upstream
  недоступен — JSON-RPC error с `code`, `message` (русский), `data.hint_url` (карточка в
  Hub); HTTP-статусы соответствуют MCP (401 с `WWW-Authenticate` при невалидном токене).

### 3.4. Веб-интерфейс Hub (минимум для флоу)
- P-16. Вход через OIDC (Keycloak; настройки `KEYCLOAK_*`; при `HUB_WEB_AUTH=litellm` — вход
  через LiteLLM CLI-SSO флоу с тем же экраном); страницы: вход, экран прав (P-04), «Мои
  подключения» (список, статус, права, «Отключить», «Переподключить»), карточка сервера
  (описание, владелец, статус, права). Jinja2 + HTMX, русский язык.
- P-17. `remote-config`/`api/me/connections` из I-1 отражают подключения этой ревизии
  (`connected|needs_reauth`), `mcp.<alias>` включаются с `oauth: {}` (клиент выполняет OAuth
  сам) и без секретов в заголовках.

### 3.5. Тесты и эксплуатация
- P-18. Тесты — только против локальных моков: мок AS целевых систем (authorize/token/revoke/
  refresh с ротацией), мок upstream MCP (streamable-http: initialize/tools/list/tools/call, SSE,
  сессии, 401/404/5xx), мок OIDC; сквозной сценарий стандартного клиента (DCR → authorize с
  веб-сессией → upstream OAuth → права → code → token → `initialize`/`tools/list`/`tools/call`
  через proxy → refresh → revoke); негативные: чужой redirect, неверный PKCE, повтор кода,
  повтор refresh (отзыв цепочки), истёкший upstream без refresh, `needs_reauth`, лимиты;
  виртуализация сессий (idle → пересоздание), кэш `tools/list`, фильтр инструментов;
  нагрузочный smoke (100 параллельных SSE-потоков через мок) в отдельном маркере.
- P-19. Все новые настройки — в `deploy/.env.example`; `docs/` — «как подключить facade-сервер
  из OpenCode / Claude Desktop / Cursor», «как добавить сервер в каталог».
