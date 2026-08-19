# Требование I-2: ТЭГ-MCP — нативная MCP-авторизация (OAuth 2.1) как эталон каталога

Репозиторий реализации: `magnit-tag-mcp` (локально `/Users/miroslavshishenkov/Documents/magnit-tag-mcp`,
правила — его `CLAUDE.md`: uv, FastMCP 3.4.x, ruff, mypy строгий, покрытие 100 %, каждый тул
с тегом политики). Контекст продукта — `docs/req-mvp.md` rev 0.2 (§4, §6.5, T-01…T-04).
**[проверено]** — снято с кода/библиотек 2026-08-19; **[проверить]** — на живом Mattermost,
когда админ ТЭГ выдаст OAuth-приложение.

## 1. Цель

Добавить в `magnit-tag-mcp` четвёртый режим HTTP-аутентификации `MM_HTTP_AUTH=oauth`, в котором
сервер сам является MCP resource server + authorization server по спецификации MCP Authorization
(RFC 9728 protected resource metadata, RFC 8414 AS metadata, RFC 7591 DCR, PKCE S256, refresh,
revoke), а вход пользователя происходит через OAuth 2.0-приложение Mattermost (`tag.magnit.ru`,
логин Keycloak + 2FA). Любой MCP-клиент (OpenCode, Claude Desktop, Cursor, VS Code) подключается
«в 2 клика» без Hub. Права выбираются на экране согласия и действуют персонально.

## 2. Факты [проверено]

- FastMCP 3.4.7 (`.venv` проекта) содержит `fastmcp.server.auth.OAuthProxy(...)` c параметрами:
  `upstream_authorization_endpoint`, `upstream_token_endpoint`, `upstream_client_id`,
  `upstream_client_secret`, `upstream_revocation_endpoint`, `token_verifier`, `base_url`,
  `resource_base_url`, `redirect_path`, `issuer_url`, `allowed_client_redirect_uris`,
  `valid_scopes`, `forward_pkce`, `forward_resource`, `token_endpoint_auth_method`,
  `extra_authorize_params`, `extra_token_params`, `client_storage` (AsyncKeyValue, напр. Redis),
  `jwt_signing_key`, `require_authorization_consent` (`True | "remember" | "external"`),
  `fallback_access_token_expiry_seconds`, `fallback_refresh_token_expiry_seconds`,
  `fastmcp_access_token_expiry_seconds`, `token_expiry_threshold_seconds`, `enable_cimd`.
- Модель токенов: клиенту выдаётся **FastMCP JWT** (reference-token, подпись `jwt_signing_key`);
  на каждом запросе `load_access_token` находит upstream-токен Mattermost по `jti` в
  `client_storage` (зашифрован Fernet-обёрткой), вызывает наш `TokenVerifier.verify_token(upstream)`
  и возвращает `AccessToken` (`.token` = upstream-токен Mattermost, `.scopes` — из верификатора).
  При истечении upstream-токена — прозрачный refresh с блокировкой.
- Запрошенные клиентом scopes сохраняются в коде авторизации и попадают в claims FastMCP JWT
  (`granted_scopes` = запрошенные, если IdP не вернул `scope`), **но в `AccessToken.scopes` не
  переносятся** — их выставляет верификатор. Значит для персональных прав нужен либо перенос
  scopes из JWT в `AccessToken` (переопределение `load_access_token`), либо иной способ.
- Consent: встроенная страница (`create_consent_html`), режимы `True/"remember"/"external"`;
  показывает клиента, redirect и запрошенные scopes. Кастомизация — через подкласс.
- В tag-mcp сегодня: `httpauth.py` (`SharedSecretVerifier`, passthrough-верификатор через
  `GET /users/me`, `ClientPool.context_for(token)`), `PassthroughMiddleware` берёт токен из
  `current_token()` и связывает `AppContext` пользователя через ContextVar; `PolicyMiddleware`
  скрывает тулы по политике `get_context().policy`; политика записи (`MM_WRITE_MODE`,
  `MM_ALLOW_DESTRUCTIVE`, `MM_CONFIRM_DM`, allow/deny каналов) — глобальная на процесс.
- Mattermost 11.9: OAuth 2.0 Service Provider (System Console → Integrations), приложение с
  фиксированными callback URL; эндпоинты `/oauth/authorize`, `/oauth/access_token`; access token —
  Bearer для API v4; refresh — `grant_type=refresh_token`; scope игнорируется **[проверить]**;
  срок жизни access token и ротация refresh **[проверить]**.

## 3. Требования

### 3.1. Режим `oauth`
- T-01. `MM_HTTP_AUTH=oauth` включает `OAuthProxy` как `auth` сервера FastMCP; обязательные
  настройки: `MM_URL`, `MM_OAUTH_CLIENT_ID`, `MM_OAUTH_CLIENT_SECRET`, `TAG_MCP_PUBLIC_URL`
  (внешний базовый URL, напр. `https://tag-mcp.<domain>`), `TAG_MCP_JWT_SIGNING_KEY`
  (≥ 32 байт), `TAG_MCP_STORAGE_URL` (Redis; при отсутствии — предупреждение и in-memory только
  для одной реплики/тестов). Отсутствие обязательных настроек — ошибка старта с понятным текстом
  (как сегодня для `token`). `MM_TOKEN` в этом режиме не требуется и не используется.
- T-02. Upstream: `authorize = {MM_URL}/oauth/authorize`, `token = {MM_URL}/oauth/access_token`,
  `redirect_path = /auth/callback`, `token_endpoint_auth_method = client_secret_post`,
  `forward_pkce = False` (Mattermost PKCE не поддерживает — **[проверить]**), `forward_resource =
  False`. Публичный URL MCP — `{TAG_MCP_PUBLIC_URL}/mcp`; метаданные
  `/.well-known/oauth-protected-resource[/mcp]` и `/.well-known/oauth-authorization-server`
  указывают на сам сервер.
- T-03. Верификатор upstream-токена — существующая проверка через `GET /users/me`
  (кэш `MM_HTTP_AUTH_CACHE_TTL`, пул клиентов) — переиспользуется; `AccessToken.subject` =
  `user_id` Mattermost, `claims` содержат `username`/`email` без секретов.
- T-04. Тулы получают токен пользователя из `AccessToken.token` (через
  `fastmcp.server.dependencies.get_access_token()`), а не из HTTP-заголовка: `PassthroughMiddleware`
  (или новая `OAuthContextMiddleware`) связывает `AppContext` пользователя тем же
  `ClientPool.context_for(token)`. Заголовок `Authorization` клиента содержит FastMCP JWT и в
  Mattermost никогда не уходит.
- T-05. Реплики без sticky-сессий: всё состояние OAuth (клиенты DCR, транзакции, коды,
  upstream-токены, consent) — только в `client_storage` (Redis); тест — два экземпляра
  приложения над одним хранилищем проходят флоу «authorize на одном, token на другом».

### 3.2. Права через scopes и экран согласия
- T-06. Набор scopes сервера: `tag:read` (все read-тулы), `tag:write` (write, кроме
  destructive), `tag:destructive`, `tag:dm` (личные сообщения), `tag:admin` (admin-тулы,
  только если `MM_ENABLE_ADMIN`). `valid_scopes` объявляются в метаданных; клиент без `scope`
  получает по умолчанию `tag:read`.
- T-07. Персональная политика: `AccessToken.scopes` = scopes, выданные этому клиенту (перенос из
  FastMCP JWT — переопределение `load_access_token` в подклассе `TagOAuthProxy`, покрыто тестами),
  и `PolicyMiddleware`/`guard_write()` учитывают их **в дополнение** к глобальной политике
  процесса (глобальная — потолок: если `MM_WRITE_MODE=readonly`, `tag:write` не даёт записи).
  Тулы, не покрытые scopes, скрываются из `tools/list`; вызов скрытого — ошибка `forbidden` с
  подсказкой «переподключитесь с правами …».
- T-08. Экран согласия: русский язык, название «ТЭГ MCP», список запрошенных прав человеческим
  языком (маппинг scope → текст в коде), имя клиента и его redirect, кнопки «Разрешить» /
  «Отмена»; `require_authorization_consent="remember"` (повторное подключение того же клиента
  без экрана; настройка `TAG_MCP_CONSENT=always|remember|external`). Реализация — подкласс с
  переопределением рендера страницы согласия; логика cookie/подписи — библиотечная.
- T-09. `allowed_client_redirect_uris`: по умолчанию `http://localhost:*`, `http://127.0.0.1:*`
  и `https://*` (настройка `TAG_MCP_ALLOWED_REDIRECTS`); всё остальное отвергается.

### 3.3. Совместимость и эксплуатация
- T-10. Режимы `off|token|passthrough` не меняются; stdio не затрагивается; существующие тесты
  проходят без правок.
- T-11. `/health`, `/ready` без аутентификации; `/ready` учитывает доступность хранилища.
- T-12. Логи: без токенов (отпечатки как сегодня), события `oauth.authorize`, `oauth.consent`,
  `oauth.token`, `oauth.refresh`, `oauth.revoke`, `oauth.reject` с `client_id`, `subject`
  (user_id), scopes.
- T-13. Docker: образ без изменений в базе; `docker-compose.yml` получает профиль/пример для
  режима `oauth` (Redis, переменные); `docs/HTTP_AUTH.md` — раздел про режим `oauth` (что нужно
  от админа ТЭГ: включить Service Provider, callback `{TAG_MCP_PUBLIC_URL}/auth/callback`,
  trusted; как подключить из OpenCode/Claude Desktop/Cursor; какие права что дают).
- T-14. Тесты — против локального мока Mattermost OAuth (authorize/access_token/refresh/`users/me`)
  и in-memory/Redis-мока хранилища: полный флоу DCR → authorize → consent → callback → token →
  MCP-вызов с JWT → refresh → revoke; негативные: чужой redirect, неверный PKCE verifier,
  повтор кода, истёкший upstream без refresh, отзыв; репликация (T-05); scopes/политика (T-07);
  покрытие 100 %, mypy/ruff чисто (правило репозитория).

## 4. Открытые вопросы к живому Mattermost [проверить]
1. `expires_in`/срок access token в ответе `/oauth/access_token`; ротация refresh.
2. Игнорируется ли параметр `scope`; допустим ли `code_challenge`.
3. Ведёт ли `is_trusted` к пропуску экрана согласия Mattermost (наш экран остаётся).
4. Поведение при 2FA и повторный вход при отзыве сессии.
