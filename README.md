# OpenCode MCP Hub

Серверная часть корпоративного OpenCode: каталог MCP-серверов, вход через корпоративный SSO
и ключ LiteLLM, OAuth-фасад и proxy для существующих MCP-серверов, права, наблюдаемость.
Требования и архитектура — `docs/req-mvp.md`. Разработка ведётся агентным конвейером
(`CLAUDE.md`, `pipeline.config.yaml`): `spec.md` / `acceptance-criteria.yaml` порождаются
spec-agent'ом из требований, код — dev-agent, тесты — test-agent, ревью — review-agent, гейты — CI.

Состав репозитория:

- `src/hub/`, `tests/` — Hub (Python 3.12, FastAPI); зона конвейера.
- `catalog.yaml` — каталог серверов (GitOps).
- `deploy/` — docker-compose для стенда (macOS/Windows/Linux), Dockerfile, Caddy, Helm.
- `installers/` — установщики для Windows/macOS/Linux.
- `docs/` — требования, ADR, чек-лист публикации сервера в каталог.
- Форк OpenCode с витриной — отдельный репозиторий `opencode`.

Быстрый старт разработки:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Стенд: `cp deploy/.env.example deploy/.env`, положить сертификат в `deploy/caddy/certs/`,
`docker compose -f deploy/docker-compose.yml up -d`.
