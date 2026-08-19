# Отчёт конвейера: I-2 «ТЭГ-MCP — нативная MCP-авторизация (MM_HTTP_AUTH=oauth)»

Репозиторий `magnit-tag-mcp`, ветка `feature/oauth-mode` (запушена в GitHub; merge в main — решение человека).

## Реализовано
Режим `oauth`: `TagOAuthProxy` над FastMCP 3.4.7 `OAuthProxy` (PRM/AS-метаданные, DCR без секрета, PKCE, allow-list
redirect по сырой строке и по разобранному URI, русский экран согласия, scopes `tag:read|write|destructive|dm|admin`
поверх глобальной политики, refresh с ротацией/сужением, revoke всего гранта + `users/logout`, индекс гранта без
роста, Redis+Fernet/in-memory хранилище, `/ready`, события `oauth.*` без токенов/userinfo), настройки `MM_OAUTH_*`/
`TAG_MCP_*`, compose-профиль `oauth`, раздел в `docs/HTTP_AUTH.md`.

## Спецификация и тесты
`docs/oauth-mode/spec.md` 1.0 → 1.1, 79 AC — все покрыты (209 привязок). 889 passed / 55 skipped (env-флаги),
покрытие `tag_mcp` 100 %, ruff/mypy чисто.

## Баги и диспуты
BUG-I2-001 (low, AC-26: redirect с `..` принимался из-за нормализации AnyUrl до валидатора) — fixed за 1 итерацию.
Диспут `test-dispute-i2-reject-log` — uphold_dispute (не логировать userinfo), тесты приведены.

## Ревью
`reports/review-i2-1.json` — approve, 3 minor (все устранены: R-23 в спеке, userinfo в логе, рост индекса гранта).
16/16 мутаций безопасности убиты тестами.

## Что нужно для живой проверки
OAuth 2.0-приложение в Mattermost (`tag.magnit.ru`): System Console → Enable OAuth 2.0 Service Provider; приложение
с callback `<TAG_MCP_PUBLIC_URL>/auth/callback`, trusted → `MM_OAUTH_CLIENT_ID/SECRET`. Затем проверить допущения §13
спеки (expires_in/ротация refresh, scope, PKCE, 2FA).
