# Требование I-6: развёртывание на тестовом сервере (Windows), Helm для k8s, нагрузочная проверка

Контекст — `docs/req-mvp.md` rev 0.2 §6.8 (R-15), §8 (S-01…S-09), §9 (D-1, D-9). Состояние на 2026-08-20:
Hub I-1+I-3+H5 (`pipeline/i3-hub-oauth-facade-proxy`, 649 тестов, гейты зелёные) работает на стенде Mac через
`deploy/docker-compose.yml` (hub ×2, postgres, redis, caddy TLS, корпоративный CA); ТЭГ-MCP в режиме `oauth`
(`magnit-tag-mcp`, ветка `feature/oauth-mode`) ждёт OAuth-приложение Mattermost; форк OpenCode (`corp/i4-sso-connectors`)
собирается в бинарники; установщики и пакет — I-5. **[проверено]** — снято со стенда; **[проверить]** — на Windows.

## 1. Цель
Перенести стенд на Windows-ноутбук-«сервер», подготовить продуктивную выкладку в k8s (Helm) и подтвердить
запас по нагрузке до раскатки на пилотную группу.

## 2. Требования

### 2.1. Windows-стенд (D-9)
- D6-01. `deploy/README-windows.md`: пошагово — Docker Desktop с WSL2 (или Rancher Desktop), клон/распаковка,
  `.env` из `.env.example`, корпоративный CA в `deploy/ca/`, сертификат для DNS-имени в `deploy/caddy/certs/`,
  `docker compose up -d`, проверка `/health`, `/ready`, `/.well-known/opencode`, `/ui/login`; типовые проблемы
  (CRLF в скриптах, права на bind-mount, WSL2-память, антивирус, проброс 443/8443).
- D6-02. `deploy/docker-compose.windows.override.yml` при необходимости (пути томов, `HUB_PUBLIC_URL`,
  `TAG_MCP_PUBLIC_URL`, профиль `tag`), запуск: `docker compose -f docker-compose.yml -f ...windows... up -d`.
- D6-03. Скрипт проверки развёртывания `deploy/smoke.sh` / `deploy/smoke.ps1`: health/ready, метаданные OAuth
  (`/.well-known/oauth-authorization-server`, PRM для каждого facade-alias), `POST /cli/start` (реальный LiteLLM),
  каталог `/api/catalog`, страница `/ui/login`, `tag-mcp` `/health` и PRM — по одному запросу, итог таблицей.
- D6-04. Резервное копирование и восстановление: том Postgres (`pg_dump` в файл, восстановление), Redis не критичен
  (кэш/сессии) — задокументировать в README.

### 2.2. Helm для k8s
- D6-05. `deploy/helm/opencode-mcp-hub/`: Deployment Hub (реплики, `readinessProbe: /ready`, `livenessProbe: /health`,
  `resources`, `securityContext` non-root), Service, Ingress (TLS корпоративным сертификатом, аннотации под istio),
  Secret/ExternalSecret для `HUB_SECRET_KEY`/`HUB_ENCRYPTION_KEY`/OAuth-секретов, ConfigMap для `catalog.yaml`,
  Job для `alembic upgrade` (перед rollout, advisory lock уже в коде), HPA (CPU/RPS), PDB, ServiceMonitor
  (метрики `/metrics`), NetworkPolicy (доступ к LiteLLM, MCP AI Lab, целевым системам, Postgres/Redis).
  Postgres/Redis — внешние (values: URL/секреты), опционально subchart для теста.
- D6-06. `values.yaml` с профилями `pilot` (2 реплики, in-cluster Postgres/Redis допустим) и `prod` (внешние БД,
  HPA 4…20, PDB minAvailable 2). `helm lint` и `helm template` в CI; `--dry-run` против kubeconfig — вне объёма.
- D6-07. Helm-чарт `tag-mcp` (или общий чарт с флагом): Deployment без sticky-сессий, Redis из values, Ingress,
  переменные `MM_*`/`TAG_MCP_*` из Secret.

### 2.3. Нагрузочная проверка (S-07)
- D6-08. `loadtest/` на k6: сценарии — (а) «холодный старт» 5 000 клиентов: `GET /.well-known/opencode` +
  `GET /remote-config` (кэш/ETag), (б) MCP-трафик: 30 000 виртуальных сессий, из них 3–5 % активны,
  `initialize`/`tools/list`/`tools/call` через `/mcp/{alias}` с моком upstream (наш `MockUpstream` как отдельный
  контейнер или k6 http-мок), (в) шторм авторизаций: 500 одновременных `/oauth/token` (refresh) с ротацией.
  Цели по спеке S-01/S-02: p50 ≤ 15 мс и p95 ≤ 50 мс добавки proxy, `/api/*` p95 ≤ 100 мс, ошибок < 0,1 %.
- D6-09. Прогон на стенде (Mac и/или Windows) в уменьшенном масштабе (например, 1/10 нагрузки) с экстраполяцией и
  отчётом `reports/loadtest-<дата>.md`: цифры, узкие места, рекомендации по ресурсам pod'ов и по лимитам upstream
  (в т.ч. `maxSessions` GitLab MCP AI Lab, ёмкость Atlassian MCP, ограничения Mattermost).
- D6-10. Прогон нагрузочных тестов не должен ходить в боевые системы: все upstream — моки; проверяется отдельным
  тестом/грепом конфигурации k6.

### 2.4. Эксплуатация
- D6-11. `docs/runbook.md`: типовые инциденты (Hub не стартует — какие переменные, миграции не применились,
  Redis недоступен, upstream в circuit-breaker, `needs_reauth` у пользователей, истёк корпоративный сертификат),
  где смотреть логи/метрики, как перезапустить, как отозвать ключ/подключение пользователя, как выкатить каталог.
- D6-12. Дашборд-набор метрик (описание, без реализации): RPS и p95 по alias, активные upstream-сессии, ошибки
  4xx/5xx, состояние circuit-breaker, число подключений по серверам, отказы обновления токенов.

## 3. Зависимости
Windows-ноутбук с Docker Desktop/WSL2, DNS-имя и корпоративный сертификат (D-1/D-9); k8s namespace и доступ —
позже, для чарта достаточно `helm lint`/`template`; сетевой доступ от стенда к LiteLLM/MCP/целевым системам.
