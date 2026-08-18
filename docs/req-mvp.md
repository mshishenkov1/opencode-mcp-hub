# OpenCode MCP Hub — требования и архитектура MVP

Ревизия 0.2, 2026-08-18. Заменяет rev 0/0.1 целиком.
Репозитории (оба — GitHub `mshishenkov1/*` и GitLab `coderepo.corp.tander.ru/shishenkov_ma/*`, private):
`opencode` — закрытая копия upstream OpenCode с нашими правками; `opencode-mcp-hub` — Hub,
установщики, деплой, документация. `magnit-tag-mcp` — существующий репозиторий (режим OAuth
добавляется в нём).

Документ — вход для `spec-agent` конвейера. **[проверено]** — снято с живых систем
2026-08-18; **[проверить]** — гипотеза, подтверждается до реализации требования.

---

## 1. Что делаем (в одном абзаце)

Продукт для 50 000+ сотрудников (до 30 000+ одновременно): корпоративная сборка OpenCode
(наш форк) со встроенной **витриной MCP-коннекторов**, входом через корпоративный SSO и
автоматической настройкой корпоративной модели; серверная часть — **Hub** (каталог,
OAuth-фасад и proxy для существующих MCP-серверов, права, наблюдаемость); **ТЭГ-MCP** — эталонный
нативный OAuth-сервер («как у Anthropic»). Существующие облачные MCP AI Lab не меняются.

### 1.1. Пользовательский путь

1. Скачал пакет с портала (установщик + наша сборка OpenCode + сертификаты) → запустил.
   Установщик ставит сертификаты, OpenCode, умолчания.
2. Первый запуск OpenCode: «Войти через корпоративный SSO» → браузер → Keycloak → готово.
   Ключ LiteLLM создан автоматически, модель `MagnitCopilot` подключена.
3. В OpenCode `/connectors` — витрина: карточки серверов (название, описание, владелец,
   статус). Выбрал → «Подключить» → браузер: SSO (обычно уже залогинен) → вход/согласие в
   целевой системе (GitLab / Jira / Confluence / ТЭГ) → экран прав («только чтение» по
   умолчанию) → возврат в OpenCode. Сервер подключён, инструменты работают.
4. Права меняются в карточке (в OpenCode или на странице Hub); «Отключить» — там же.
5. Повторных логинов нет, пока живы refresh-токены; протух — статус «нужна авторизация»,
   те же 2 клика. На втором компьютере: установка → SSO → подключение сервера в один
   клик (в систему логиниться не надо — токен уже у Hub).

---

## 2. Границы MVP

**Входит:** форк OpenCode (вход по SSO, витрина, умолчания, сборки CLI/TUI для macOS,
Windows, Linux и Desktop для Windows/macOS); Hub (каталог, well-known/remote-config,
оркестрация входа и ключ LiteLLM, OAuth-фасад + proxy для GitLab ×2 / Jira / Confluence,
права, страницы «мои подключения», админ-каталог, метрики); ТЭГ-MCP в нативном OAuth-режиме;
установщики; стенд на macOS (docker compose) → Windows → k8s (Helm).
**Не входит:** Outlook (EWS без OAuth **[проверено]**), SmartIT, обновление GitLab MCP до
2.1.x с нативным OAuth и доработка Atlassian MCP (зависят от AI Lab — см. §9), перенос
реестра в LiteLLM MCP Gateway, интеграция витрины в портал.

---

## 3. Факты об окружении [проверено]

| Система | Факт |
|---|---|
| LiteLLM 1.94.0 `llmlite.ailab-copilot-prod.corp.tander.ru` | SSO Keycloak `hi.tander.ru` realm `Tander-SSO`, client `copilot`. CLI-SSO: `POST /sso/cli/start` → `{login_id, poll_secret, user_code}`; браузер `/sso/key/generate?source=litellm-cli&key=<login_id>`; `GET /sso/cli/poll/<login_id>` (`x-litellm-cli-poll-secret`) → JWT (24 ч), при >1 команды `requires_team_selection`. `POST /key/generate`. |
| OpenCode 1.17.9 (upstream anomalyco/opencode, MIT) | Портал раздаёт upstream-бинарники 1.17.9 + свои `install-opencode.sh/.bat`, `autoupdate:false`. `.well-known/opencode` (`auth.command`, `config`, `remote_config`), `mcp.<name>` remote с `oauth` (RFC 7591 DCR, PKCE), `/mcps` (имя+статус+тумблер), `tools`/`permission` по маскам, plugin-хуки `auth`/`config`. TUI/Desktop — SolidJS; сборка Bun (CLI) и Tauri (Desktop). |
| GitLab MCP (облако) | `better-gitlab-mcp-server` 2.0.32, `STREAMABLE_HTTP`, per-request `Private-Token`/`Authorization: Bearer`, `Enabled-Groups`; `maxSessions=1000` (~660 занято). Upstream ≥ 2.1.10 умеет нативный MCP-OAuth (`GITLAB_MCP_OAUTH`, `GITLAB_OAUTH_CALLBACK_PROXY`, `OAUTH_STATELESS_MODE`). |
| Atlassian MCP (облако) | «Atlassian MCP» 2.14.5, streamable-http, заголовки `X-Atlassian-{Confluence,Jira}-{Personal-Token,Url}`, `Enabled-Groups`. Реализация не публичная — уточнить у AI Lab. |
| Jira 9.12.15, Confluence 7.19.6 | OAuth 2.0 provider (incoming links): `/rest/oauth2/latest/{authorize,token}`; PAT API. |
| ТЭГ Mattermost 11.9.4 `tag.magnit.ru` | вход Keycloak realm `Magnit-workplace` + 2FA; OAuth 2.0 Service Provider — включить. `magnit-tag-mcp`: FastMCP 3.4.6, `MM_HTTP_AUTH=passthrough`; в FastMCP 3.4.6 есть `OAuthProxy` (DCR, PKCE, consent, Redis-storage, refresh). |
| GitLab CE ×2 | вход через Keycloak; OAuth-приложения доступны; OAuth-токен = `Authorization: Bearer`. |
| Mac для стенда | arm64, git, Homebrew, Xcode CLT, Node 26; нет Bun/Rust/Docker/Go/Python 3.12/gh. |

---

## 4. Архитектура

```
[Установщик] ── наша сборка OpenCode + CA + умолчания
[OpenCode (форк)] ── первый запуск: SSO ──► Hub /cli/* ──► LiteLLM CLI-SSO ──► Keycloak ──► постоянный ключ
                  ── /connectors ── GET Hub /api/catalog, /api/me/connections
                  ── «Подключить» ── встроенный MCP-OAuth-клиент ──► сервер:
                        • нативный (ТЭГ): tag-mcp OAuthProxy ──► Mattermost (Keycloak+2FA) ──► JWT tag-mcp
                        • через фасад (GitLab×2, Jira, Confluence): Hub /mcp/<alias> OAuth ──► система ──► JWT Hub
                  ── вызовы: Bearer ──► tag-mcp напрямую | Hub proxy ──► облачные MCP AI Lab (персональные заголовки)
[Hub] FastAPI + Postgres + Redis: каталог, well-known/remote-config, OAuth-фасад, proxy, права, страницы, метрики
[Стенд] docker compose (hub, postgres, redis, tag-mcp, reverse-proxy TLS) ── тот же compose на Windows ── Helm в k8s
```

Секреты у пользователя: ключ LiteLLM + OAuth-токены клиентов (JWT Hub/tag-mcp с refresh).
Токены к системам (GitLab/Jira/Confluence — в Hub; Mattermost — в tag-mcp) — зашифрованы,
обновляются сервером. Существующие MCP не меняются.

---

## 5. Компоненты и репозитории

```
opencode/ (форк)               packages/opencode, packages/app, packages/desktop + наши правки:
                               corp/ (умолчания), первый запуск SSO, /connectors, сборочный конвейер
opencode-mcp-hub/
├── hub/                       Python 3.12, FastAPI — зона конвейера (src/, tests/), catalog.yaml
├── installers/                install.sh / install.ps1 / .bat (Desktop и CLI)
├── deploy/                    docker-compose.yml, Caddyfile/traefik, helm/, .env.example, README-windows.md
├── docs/                      этот документ, ADR, чек-лист публикации сервера
└── .claude/, pipeline.config.yaml, spec.md, acceptance-criteria.yaml
magnit-tag-mcp/                режим MM_HTTP_AUTH=oauth (OAuthProxy), consent с правами, Redis, образ
```

---

## 6. Функциональные требования

### 6.1. Форк OpenCode
- F-01. Закрытое зеркало upstream по тегу `v1.17.9` (remote `upstream` для rebase); наши
  изменения — отдельные коммиты/патчи минимального объёма, каталог `corp/`.
- F-02. Умолчания в сборке: адрес Hub, провайдер `magnit_prod` (`@ai-sdk/openai-compatible`,
  baseURL LiteLLM, модель `MagnitCopilot`, лимиты), `autoupdate:false`, CA-bundle,
  `enabled_providers`. Пользовательский `opencode.json` не перезаписывается.
- F-03. Первый запуск без ключа: экран «Войти через корпоративный SSO» (TUI и Desktop):
  форк вызывает Hub `POST /cli/start`, открывает браузер, poll'ит, получает **постоянный**
  ключ LiteLLM, сохраняет в auth-store; `/connect` содержит тот же метод. Команда
  `opencode corp login` — то же из CLI.
- F-04. Витрина `/connectors` (TUI + Desktop/web): карточки из Hub `/api/catalog` и
  `/api/me/connections`: название, описание, владелец, статус
  (`not_connected | connected | needs_reauth | disabled_by_admin`), пресет прав; действия
  «Подключить» (запускает встроенный MCP-OAuth флоу к URL сервера, затем включает
  `mcp.<alias>` в конфиге), «Отключить» (revoke + выключить), «Права» (ссылка/экран).
  Поиск, группировка по владельцу/тегам, пустое состояние с подсказкой.
- F-05. `/mcps` продолжает работать (совместимость); витрина — надстройка над ним.
- F-06. Сборки: CLI/TUI `bun build --compile` (darwin-arm64/x64, linux-x64, win-x64),
  Desktop (Tauri) win-x64, mac-arm64; версия = upstream + суффикс `-magnit.N`;
  скрипт обновления от upstream (rebase + прогон наших тестов).
- F-07. Тесты на наши правки (unit + smoke TUI), CI сборки артефактов.

### 6.2. Hub — вход и ключ
- R-01. `POST /cli/start` → `{login_id, browser_url, poll_secret}`; `GET /cli/poll/<id>`
  (секрет в заголовке) → `pending | ready{key,user}`; Hub оркестрирует LiteLLM CLI-SSO,
  выбирает команду по правилу (единственная / `not-ai-lab` / первая — **уточнить**), получает
  JWT, `POST /key/generate` (alias `opencode-<login>-<hostname>`), возвращает постоянный
  ключ. **[проверить]**: право CLI-JWT на `/key/generate`; fallback — сервисный ключ Hub.
- R-02. Хранит `sha256(ключ) → user`; повторный вход — новый ключ, старый помечается
  устаревшим. Ключ используется клиентом для LiteLLM и для аутентификации в Hub API
  (`/api/*`, `/remote-config`).

### 6.3. Каталог
- R-03. `hub/catalog.yaml`: `alias, title, description, icon, owner, contact, docs_url,
  status(beta|ga|deprecated), audience(группы Keycloak), mode(native|facade),
  mcp_url (native) | upstream_url + credential_headers + static_headers (facade),
  auth (oauth2: authorize/token/revoke endpoints, client_id, secret_ref, scopes,
  pkce, consent), permission_model (header_groups | tool_filter; пресеты), contours`.
  Валидация схемы, `version`, перечитывание без рестарта.
- R-04. Стартовый каталог: `gitlab` (coderepo), `gitlab-platform`, `jira`, `confluence`
  (facade → Atlassian MCP, два alias'а с разными заголовками/группами), `tag` (native).
- R-05. `GET /api/catalog` (с учётом `audience`), `GET /api/me/connections`,
  `POST /api/me/connections/<alias>/disconnect`, `PUT /api/me/connections/<alias>/permissions`.
- R-06. `GET /.well-known/opencode` (для стандартного OpenCode и совместимости):
  `config` = провайдер + все `mcp.<alias>` (`enabled:false`, `url`, `oauth:{}`),
  `remote_config` (Bearer ключ) → подключённые = `enabled:true`, `permission`/`tools`.
- R-07. Сопровождение: GitOps (MR → CI: схема, `initialize` upstream, секреты →
  выкладка); карточка с владельцем; `deprecated` с датой; чек-лист публикации
  (streamable-http, per-request auth заголовком **или** нативный MCP-OAuth, группы
  инструментов, health, владелец); `/admin/catalog` (подключения, ошибки, p95, health).

### 6.4. OAuth-фасад и proxy (GitLab ×2, Jira, Confluence)
- R-08. Для каждого facade-alias Hub — MCP resource server + authorization server:
  `401` + `WWW-Authenticate: Bearer resource_metadata=…`;
  `/.well-known/oauth-protected-resource/mcp/<alias>`;
  `/.well-known/oauth-authorization-server`; `/oauth/register` (DCR, ограничение по IP);
  `/oauth/authorize` (PKCE S256, привязка к сессии Keycloak Hub, затем OAuth в целевую
  систему с фиксированным callback `/oauth/callback/<alias>`, затем экран прав);
  `/oauth/token` (code, refresh с ротацией); `/oauth/revoke`. Токены клиента — JWT Hub
  (access 1 ч, refresh 30 дней), проверка подписи без БД.
- R-09. Хранение токенов систем per-user, шифрование AES-GCM, фоновое обновление по refresh
  (блокировки в Redis), при провале — статус `needs_reauth`.
- R-10. `POST|GET|DELETE /mcp/<alias>` — потоковое проксирование streamable-http на upstream,
  подстановка персональных заголовков (`Private-Token`, `X-Atlassian-*`), `Enabled-Groups`
  по правам, удаление клиентского `Authorization`; фильтр `tools/list` и отказ `tools/call`
  для запрещённых инструментов; понятные JSON-RPC ошибки (не подключён / переподключить).
- R-11. Провайдеры: GitLab (scopes по пресету: `read_api read_user read_repository` /
  `api`), Jira DC, Confluence DC (`/rest/oauth2/latest/*`, `WRITE`). Резервные `pat` /
  `pat_via_password` — в модели, за флагом, в UI выключены.

### 6.5. ТЭГ-MCP — нативный OAuth (эталон)
- T-01. Режим `MM_HTTP_AUTH=oauth`: FastMCP `OAuthProxy` (upstream `tag.magnit.ru/oauth/authorize`,
  `/oauth/access_token`, фиксированный `client_id/secret`, `redirect_path=/auth/callback`,
  `client_storage`=Redis, `require_authorization_consent="remember"`), `TokenVerifier`
  через `GET /users/me`; токен Mattermost берётся из контекста OAuth, не из заголовка.
- T-02. Экран согласия с выбором прав (`readonly | full`, DM, allow-list каналов) → claims
  JWT → персональные политики записи (без хранения настроек).
- T-03. Реплики без sticky-сессий (Redis), health/ready, образ, compose-сервис, публичный
  HTTPS; в каталоге Hub — `mode: native`.
- T-04. Проверить: срок и ротация refresh-токенов Mattermost, поведение при 2FA.

### 6.6. Права
- R-12. Пресеты «только чтение» (по умолчанию) / «чтение и запись»; группы (`GitLab`:
  `core, code_review, devops, repo_write, issue_management, releases, wiki, admin, draft_notes,
  users`; `Atlassian`: `confluence_read/write, jira_read/write/agile/service_desk`);
  маски allow/ask/deny; применение на сервере (Hub/tag-mcp) **и** в конфиге OpenCode.

### 6.7. Установщики и распространение
- R-13. Пакет: наша сборка OpenCode + `tander-ca-bundle.pem` + установщик (`install.sh`,
  `install.ps1`/`.bat`); ставит CA (`NODE_EXTRA_CA_CERTS`, для Desktop — переменная
  пользователя), бинарник/приложение, умолчания; запускает первый запуск (SSO). Идемпотентен,
  бэкапит пользовательский конфиг, не спрашивает ключи. Публикуется на портале.
- R-14. Подпись сборок (Windows Authenticode, macOS Developer ID + notarization) — решение
  ИБ/инфраструктуры; без подписи — инструкции по обходу предупреждений (только пилот).

### 6.8. Развёртывание
- R-15. `deploy/docker-compose.yml`: hub ×N, postgres, redis, tag-mcp, reverse-proxy TLS;
  один compose для macOS (стенд), Windows (Docker Desktop/WSL2) и Linux; `deploy/helm/`
  — те же образы для k8s (HPA, PDB). Секреты — только env/файлы, gitleaks в CI.

---

## 7. Нефункциональные требования
- N-01. Токены систем — шифрование в БД; ключ LiteLLM — только хеш; JWT подписаны, ротация ключей.
- N-02. Аудит действий без секретов и содержимого; JSON-логи; OpenTelemetry.
- N-03. Hub не хранит содержимое MCP-вызовов.
- N-04. Тесты — только против локальных моков (без боевых систем в CI); контрактные тесты
  MCP-proxy на записанных JSON-RPC-обменах; smoke-тесты форка.
- N-05. Пороги гейтов конвейера для `hub/`; для форка и tag-mcp — свои CI.
- N-06. Совместимость: стандартные клиенты (Claude Desktop, Cursor, VS Code, ванильный OpenCode)
  подключаются к facade/native серверам без наших правок.

## 8. Масштаб: 30 000+ одновременно
- S-01. Hub stateless, горизонтально; горячий путь proxy без БД (Redis-кэш прав/кредов, JWT
  без БД); потоковый proxy, HTTP/2 пулы; цель: p50 ≤ 15 мс, p95 ≤ 50 мс добавки.
- S-02. Статичные `well-known`/каталог с ETag/CDN; `remote-config`/`api/me` p95 ≤ 100 мс.
- S-03. Фоновое обновление refresh-токенов с блокировками; на горячем пути — только при 401.
- S-04. Rate-limit на пользователя/сервер, circuit-breaker на upstream, таймауты, лимиты тел.
- S-05. Upstream-ёмкость (GitLab MCP `maxSessions`, Atlassian MCP, LiteLLM) — метрики и
  запросы на масштабирование к AI Lab заранее; tag-mcp — реплики.
- S-06. Postgres (индексы `key_hash`, `(user_id, alias)`), партиции аудита; Redis HA.
- S-07. SLO 99.9 %, нагрузочный тест (k6: 30 000 VU, ~5 000 вызовов/с) перед релизом.
- S-08. Раскатка: стенд macOS → Windows (пилот ≤ 100) → k8s волнами по `audience`.
- S-09. При недоступности Hub модель продолжает работать (клиент кэширует конфиг —
  **[проверить]**), нативные серверы (ТЭГ) не зависят от Hub.

## 9. Зависимости от людей
| # | Что | От кого | Если нет |
|---|---|---|---|
| D-1 | Хост Hub и tag-mcp (DNS, корп. сертификат), для dev — `https://localhost:8443` в redirect URI | инфраструктура / ты | самоподписанный + добавление в CA (пилот) |
| D-2 | OAuth-приложения GitLab ×2 (redirect `<hub>/oauth/callback/gitlab`, `…/gitlab-platform`, confidential, scopes `api read_api read_user read_repository`, trusted) | админы GitLab | — |
| D-3 | Incoming links Jira и Confluence (`<hub>/oauth/callback/jira`, `…/confluence`, WRITE) | админы Jira/Confluence | `pat_via_password` за флагом |
| D-4 | ТЭГ: включить OAuth 2.0 Service Provider; приложение с callback `<tag-mcp>/auth/callback`, trusted | админ ТЭГ | — |
| D-5 | Keycloak `Tander-SSO`: OIDC-клиент `opencode-mcp-hub` (redirect `<hub>/auth/callback`, группы для админов) | админы Keycloak | сессия через LiteLLM CLI-SSO |
| D-6 | LiteLLM: право `/key/generate` из CLI-JWT (или сервисный ключ), политика ключей, лимиты GitLab MCP, реализация Atlassian MCP, сеть от Hub к MCP | AI Lab | JWT + автообновление |
| D-7 | Согласие ИБ: хранение OAuth-токенов в Hub/tag-mcp (шифрование), распространение форка, подпись сборок | ИБ | пилот без подписи |
| D-8 | Сборка OpenCode: собирает ли AI Lab сам; размещение нашего пакета на портале | AI Lab | собственный конвейер сборки |
| D-9 | Windows-машина для стенда №2, затем k8s namespace | инфраструктура | — |

## 10. Что дальше (после MVP)
Outlook (impersonation / логин-пароль); нативный OAuth в GitLab MCP (обновление AI Lab до
2.1.x) и Atlassian MCP (`OAuthProxy` или наш фасад-sidecar) — Hub становится только
каталогом; LiteLLM MCP Gateway; витрина на портале; PR в upstream OpenCode с витриной.

## 11. План итераций
| Итерация | Результат | Готово, когда |
|---|---|---|
| I-0 «Стенд и репозитории» | зеркала OpenCode в GitHub/GitLab, toolchain на Mac, compose (hub-скелет, postgres, redis, proxy), CI | `docker compose up` на Mac; форк собирается локально |
| I-1 «Вход и каталог» | Hub: /cli/*, /key/generate, catalog.yaml, /api/catalog, well-known; форк: SSO на первом запуске, `/connectors` (только просмотр) | чистый Mac: пакет → SSO → модель отвечает; витрина показывает каталог |
| I-2 «ТЭГ — эталон» | tag-mcp OAuth-режим + consent с правами, в compose; «Подключить» из витрины | ТЭГ подключается из форка и из ванильного OpenCode/Claude Desktop |
| I-3 «GitLab через фасад» | OAuth-фасад Hub, proxy, права/группы, страницы Hub | GitLab подключён «в 2 клика», `repo_write` выключен по умолчанию |
| I-4 «Jira, Confluence, GitLab Platform» | провайдеры Atlassian, два alias'а, GitLab platform | все пять серверов в витрине работают |
| I-5 «Установщики и сборки» | сборки CLI/Desktop win/mac/linux, установщики, пакет на портал | сквозной сценарий §1.1 на трёх ОС |
| I-6 «Windows-стенд и k8s» | тот же compose на Windows, Helm, нагрузочный тест | пилот на 100 человек; k6 30 000 VU в SLO |
