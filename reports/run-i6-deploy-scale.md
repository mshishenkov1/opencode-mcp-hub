# Отчёт конвейера: I-6 «Развёртывание на тестовом сервере, Helm для k8s, нагрузочная проверка»

Ветка `pipeline/i3-hub-oauth-facade-proxy` (I-3+H5+I-6), 2026-08-20.

## Реализовано
**Windows-стенд:** `deploy/docker-compose.windows.yml` (DNS-имя, 443/8443, `HUB_TRUST_PROXY`), параметризованный
`Caddyfile`, `deploy/README-windows.md` (установка, типовые проблемы, бэкап), `deploy/backup.sh`/`backup.ps1`
(pg_dump custom, ротация, права 600, проверка сигнатуры), `deploy/smoke.sh`/`smoke.ps1` (health/ready/well-known/
AS-метаданные/PRM по alias/каталог/UI-редирект; внешние проверки под флагом `--external`).
**Helm:** чарты `opencode-mcp-hub` и `tag-mcp` — профили default/pilot/prod, все секреты в одном Secret релиза
(ExternalSecret с `creationPolicy: Orphan` + hook-схема доставки до Job миграций), `validateSecrets` с `fail`,
OAuth-креды каталога по alias, HPA/PDB/NetworkPolicy/ServiceMonitor/Ingress TLS, non-root + readOnlyRootFilesystem.
**Нагрузка:** `loadtest/` (k6-сценарии well-known/remote-config, MCP-трафик, шторм refresh; мок-upstream на FastAPI;
`check_no_prod.sh`), реальный прогон 1/10 — `reports/loadtest-2026-08-20.md`.
**Эксплуатация:** `deploy/runbook.md` (инциденты, логи/метрики, отзыв доступа, выкатка каталога, дашборд).

## Результаты нагрузки (1/10, одна реплика, macOS)
Добавка proxy p50 **3,3 мс**, p95 **5,0 мс** при порогах 15/50 мс; ошибок 0 %; well-known p95 5,2 мс,
`/remote-config` p95 8,7 мс; потолок одной реплики ≈ **480 rps** (упор в одно ядро, RSS 91 МиБ).
Шторм refresh: p95 242 мс (порога в спеке нет — зафиксировано как ограничение).
Наблюдение для AI Lab: upstream-сессии 1:1 с клиентскими (12 015 → 12 015) — при полной раскатке до 30 000 сессий,
`maxSessions` GitLab MCP (1000) требует согласования.

## Ревью
`review-i6-1` request_changes (5 must_fix: рассогласование Secret'ов, `KEYCLOAK_CLIENT_SECRET`, OAuth-креды каталога,
нерабочий шаг loadtest, отсутствующий `backup.sh`) → исправлено; `review-i6-2` request_changes
(1 must_fix: `ExternalSecret` Owner + before-hook-creation уносил живой Secret при upgrade) → исправлено;
`review-i6-3` **approve**.

## Гейты
pytest 649 passed · ruff/mypy чисто · helm lint 6/6 · `docker compose config -q` 3/3 · smoke 10 OK / 0 FAIL ·
backup.sh проверен на живом стенде.

## Бэклог
Полный прогон S-07 перед релизом; SSE и rate-limit под нагрузкой; прогон с `MOCK_FAIL_RATE` для circuit-breaker;
измеренная линейность на нескольких репликах; порог для `/oauth/token`; метрики `hub_circuit_breaker_state` и gauge
подключений; `helm --dry-run` в реальном namespace; прогон `backup.ps1`/`smoke.ps1` на Windows с pwsh.
