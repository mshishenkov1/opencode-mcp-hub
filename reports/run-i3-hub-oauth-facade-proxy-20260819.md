# Отчёт конвейера: I-3 «Hub — стандартная MCP-авторизация (OAuth-фасад), брокер токенов, MCP-proxy, веб, миграции»

run_id `i3-hub-oauth-facade-proxy-20260819` · ветка `pipeline/i3-hub-oauth-facade-proxy` (от I-1) · 2026-08-19…20

## Реализовано
Hub как OAuth authorization server для facade-серверов (PRM/AS-метаданные, DCR public client, PKCE S256, code→JWT
HS256 1 ч + refresh 30 д с ротацией и отзывом цепочки, revoke, denylist jti в KV, горячий путь без БД); брокер токенов
целевых систем (authorization_code по `catalog.yaml`: GitLab PKCE, Jira/Confluence DC; Fernet-шифрование, фоновое
обновление с блокировкой, `needs_reauth`, смена пресета прав); MCP-proxy `POST|GET|DELETE /mcp/{alias}` (потоковый
streamable-http/SSE, удаление клиентского Authorization, подстановка заголовков каталога и групп, виртуализация сессий
с idle TTL и пересозданием, кэш `tools/list`, фильтр инструментов по имени (R-P8), rate-limit, лимит SSE, 413,
circuit-breaker с half-open, матрица JSON-RPC ошибок); веб (`HUB_WEB_AUTH=litellm|keycloak`, OIDC с проверкой alg/nonce,
веб-сессии Secure/HttpOnly, CSRF double-submit, экран прав, «мои подключения», карточки; Jinja2+HTMX, русский);
миграции Alembic внутри пакета (штамп БД I-1, advisory lock для PostgreSQL), `HUB_TRUST_PROXY`, новые метрики/аудит.

## Спецификация
`spec.md` ревизия 2 → 2.1 (52 + уточнения правил; решения №30–60), AC-70…AC-156 (87 новых) — все покрыты.
Диспут `spec-dispute-R-P8-tool-filter` → uphold_test (реализация по AC-122, R-P8 переписан по имени инструмента).

## Баги и ревью
Багов кода не заведено (все падения при разработке тестов — test_bug). Review-i3-1: request_changes (4 must_fix: CB
half-open, маски групп по имени, AC-115 утечка Authorization, AC-131 Secure cookie) — исправлены; review-i3-2: approve
(4 minor: TTL ключа пробы vs длинная SSE-проба, валидация X-Forwarded-For при HUB_TRUST_PROXY, лишний delete в
record_success, избыточная страховка в record_failure — в бэклог I-5).

## Гейты (reports/gates-i3-hub-oauth-facade-proxy-20260819.json)
G1 PASS 633 passed/0 skip · G2 PASS diff-cover 92 % · G3 PASS mutation 74.0 % (killed 6191, survived 2179) ·
G4 PASS · G5 PASS approve · G6 PASS 156/156.

## Метрики здоровья
Итераций на баг: — · mutation 74 % · flake 0 · ложных срабатываний защиты зон 0 · особенности: mutmut на macOS —
`no_proxy='*'`, `also_copy` включает `catalog.yaml` и `deploy/`.

## Что дальше
I-5 (установщики/пакет, minor из review-2), I-6 (Windows-стенд, Helm, k6). Merge в `main` — решение человека.
