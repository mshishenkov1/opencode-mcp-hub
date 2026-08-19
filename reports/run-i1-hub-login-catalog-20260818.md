# Отчёт конвейера: I-1 «Hub — вход по SSO, постоянный ключ LiteLLM, каталог, well-known / remote-config»

run_id: `i1-hub-login-catalog-20260818` · ветка `pipeline/i1-hub-login-catalog-20260818` · 2026-08-18…2026-08-19

## Реализовано
`src/hub/`: настройки `HUB_*` (pydantic-settings, Fernet-валидация), каталог (`$ref`, `${VAR}`, `env:VAR`, строгая схема,
`unconfigured`, ETag, `/admin/catalog/reload`, `mcp-hub catalog validate`), вход через LiteLLM CLI-SSO
(`POST /cli/start`, `GET /cli/poll/{id}` с дросселированием 2 с, выбор команды, `POST /key/generate` → постоянный
ключ, fallback JWT), хранение `sha256(ключ)→user` (SQLAlchemy async, SQLite/Postgres; users, api_keys, connections,
audit_log), Bearer-аутентификация (кэш 60 с), rate-limit 30/мин/IP, `GET /.well-known/opencode`, `GET /remote-config`,
`GET /api/catalog|me|me/connections`, `/health`, `/ready`, `/metrics`, JSON-логи и аудит без секретов, catch-all
обработчик ошибок, CLI `mcp-hub serve|catalog validate`. Стенд: `deploy/docker-compose.yml` (hub ×2, postgres, redis,
caddy TLS, корпоративный CA) — `/cli/start` работает против реального LiteLLM.

## Спецификация
`spec.md` ревизия 1 → 1.1 (уточнения R-K3/R-C1/R-C2/R-A5/R-L9, AC-09/47/59 по противоречиям из тест-отчёта).
69 AC, все покрыты.

## Баги и итерации
Багов кода не выявлено (0 `bugs/`). Фикс-цикл не потребовался. Замечания review-1 (10, все non-blocking)
исправлены одним dev-шагом (catch-all 500, комментарии, `include_deprecated`, логгер, мёртвый флаг, атомарный Redis
rate-limit) и двумя test-шагами (ключ кэша `keyauth:<sha256>`, вывод CLI, усиление против мутантов).

## Эскалации
Нет. Ложные срабатывания защиты зон: артефакты `*.egg-info` и коммиты оркестратора (требования I-2…I-4,
деплой-правки) в общей ветке — задокументированы в `state.json`.

## Гейты (reports/gates-i1-hub-login-catalog-20260818.json)
| Гейт | Результат |
|---|---|
| G1 Тесты | PASS — 408 passed, 0 skip/xfail |
| G2 Coverage-diff | PASS — 95 % ≥ 90 % |
| G3 Mutation | PASS — 89.3 % (killed 2595, survived 310), порог 70 % |
| G4 Линт/типизация | PASS |
| G5 Ревью | PASS — review-1.json approve |
| G6 AC-трассировка | PASS — 69/69 |

## Метрики здоровья
Среднее итераций на баг: — (багов нет) · mutation score 89.3 % · flake rate 0 (сьют прогнан многократно) ·
срабатываний защиты от подгонки: 0 реальных (2 ложных, см. выше). Особенность окружения: mutmut на macOS требует
`no_proxy='*'` (иначе ~15 % мутантов падают segfault'ом в `getproxies_macosx_sysconf` после fork).

## Что дальше
I-3 (OAuth-фасад + MCP-proxy) — ветка `pipeline/i3-hub-oauth-facade-proxy` от этого HEAD (spec-agent в работе).
Merge в `main` — решение человека.
