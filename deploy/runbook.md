# Runbook Hub (D6-11, D6-12)

Эксплуатация Hub на стенде (`deploy/docker-compose.yml`) и в k8s (`deploy/helm`).
Установка стенда на Windows — `deploy/README-windows.md`, нагрузочные цифры —
`reports/loadtest-2026-08-20.md`.

## 0. Быстрая диагностика

| Команда | Стенд (docker compose) | k8s |
|---|---|---|
| Состояние | `docker compose ps` | `kubectl -n <ns> get pods,hpa` |
| Живость | `curl -k https://<хост>/health` | то же через ingress |
| Готовность | `curl -k https://<хост>/ready` | `kubectl describe pod` (readinessProbe) |
| Полная проверка | `./deploy/smoke.sh https://<хост>` | `./deploy/smoke.sh https://<хост>` |
| Логи | `docker compose logs -f hub` | `kubectl -n <ns> logs -f deploy/<release>-hub` |
| Метрики | `curl -k https://<хост>/metrics` | `/metrics` через ServiceMonitor |

Логи — JSON, по одной строке на событие, у каждого запроса `request_id`
(он же в заголовке `X-Request-ID` ответа — просить его у пользователя при обращении).
Логгеры: `hub.app`, `hub.http`, `hub.mcp`, `hub.proxy`, `hub.broker`, `hub.oauth`,
`hub.oauth.routes`, `hub.web`, `hub.oidc`, `hub.login`, `hub.migrate`, `hub.admin`.

`/health` отвечает всегда, пока процесс жив; `/ready` — только когда загружен каталог
и доступна база. Расхождение «health OK, ready 503» означает проблему с БД.

## 1. Hub не стартует

**Симптом.** Контейнер/pod в `CrashLoopBackOff`, в логе строка об ошибке конфигурации,
`/health` недоступен.

**Что смотреть.** Первые строки лога: настройки валидируются до старта HTTP-сервера,
сообщение называет конкретную переменную.

Обязательные переменные (без значений по умолчанию):

| Переменная | Что это | Типичная ошибка |
|---|---|---|
| `HUB_PUBLIC_URL` | публичный адрес, попадает в `aud` токенов и в метаданные | адрес не совпал с тем, по которому ходят клиенты → токены отвергаются с 403 |
| `HUB_SECRET_KEY` | подпись JWT Hub | пусто → отказ старта; смена ключа обесценивает все выданные токены |
| `HUB_ENCRYPTION_KEY` | шифрование токенов систем | не Fernet-ключ (44 символа urlsafe-base64) → отказ старта; смена ключа делает сохранённые токены нечитаемыми, все подключения уходят в `needs_reauth` |
| `HUB_LITELLM_BASE_URL` | адрес LiteLLM | недоступен → вход и `/api/*` не работают, но Hub стартует |
| `HUB_DATABASE_URL` | БД (по умолчанию SQLite в рабочем каталоге) | для нескольких реплик обязателен PostgreSQL |
| `HUB_CATALOG_PATH` | путь к `catalog.yaml` | файл не примонтирован или не проходит схему → `CatalogError` при старте |

**Действия.**

1. `docker compose config` / `kubectl describe pod` — убедиться, что переменные и
   монтирования дошли до контейнера.
2. Проверить каталог, не поднимая сервис: `mcp-hub catalog validate --path catalog.yaml`.
3. `HUB_REDIS_URL` не задан и реплик больше одной — в логе будет `WARNING kv_in_memory`:
   реплики не делят denylist, MCP-сессии, окна лимитов и circuit-breaker. Задать Redis.

## 2. Миграции не применились

**Симптом.** `/ready` отдаёт 503 «база данных недоступна», в логе `hub.migrate`,
ошибки SQL про отсутствующие таблицы или колонки.

**Действия.**

1. Текущая ревизия: `docker compose exec hub mcp-hub db current`
   (в k8s — `kubectl exec deploy/<release>-hub -- mcp-hub db current`).
2. Применить: `mcp-hub db upgrade` (по умолчанию до `head`).
3. В k8s миграции выполняет Job `<release>-migrate` (hook `pre-install,pre-upgrade`).
   Упал — `kubectl logs job/<release>-migrate`; типовая причина: не задан
   `external.postgres.url` / `existingSecret`.
4. При одновременном старте реплик миграция берёт advisory-блокировку PostgreSQL
   (R-M1): вторая реплика ждёт и видит схему уже на `head`. «Зависший» на минуту
   старт второй реплики — это ожидаемое поведение, а не инцидент.
5. Откат схемы отдельной командой не предусмотрен: восстанавливать из дампа
   (`deploy/README-windows.md`, раздел 8).

## 3. Redis недоступен

**Симптом.** В логе ошибки подключения к Redis; растёт время ответа; клиенты получают
`-32000` «сессия не найдена» и переоткрывают MCP-сессии.

**Чем это грозит.** В Redis лежат: кэш подключений и прав, кэш аутентификации по
ключу LiteLLM, виртуальные MCP-сессии, кэш `tools/list`, окна rate-limit, состояние
circuit-breaker, denylist отозванных `jti`, транзакции `/oauth/authorize`.
Потеря Redis **не** теряет пользовательские данные (они в Postgres), но:
отозванные токены перестают быть отозванными до истечения `exp`, а MCP-клиенты
переустанавливают сессии.

**Действия.**

1. Проверить доступность: `docker compose exec redis redis-cli ping`.
2. Поднять/перезапустить Redis; Hub переподключается сам, рестарт не нужен.
3. Если Redis потерян надолго и есть подозрение на утечку токена — отозвать
   подключения затронутых пользователей (раздел 7) сразу после восстановления.
4. Проверить `HUB_REDIS_URL` во всех репликах: они обязаны смотреть в один Redis.

## 4. Upstream в circuit-breaker

**Симптом.** Клиенты получают 503 с `retry_after`, в метриках растёт
`hub_upstream_errors_total{kind="circuit_open"}`, в логе `hub.mcp` —
`upstream_timeout` / `upstream_network_error`.

**Как это устроено.** `HUB_CB_FAILURES` (по умолчанию 5) подряд идущих ошибок
открывают выключатель на `HUB_CB_RESET` секунд (30). После этого один запрос-проба
идёт наверх; успех закрывает выключатель, ошибка открывает снова.

**Действия.**

1. Понять, чьи ошибки: метка `alias` у `hub_upstream_errors_total`, разрез по `kind`
   (`timeout`, `network`, `http_5xx`, `circuit_open`).
2. Проверить сам upstream снаружи Hub (curl с пода/стенда до `upstream_url`).
3. Сетевые причины: DNS, корпоративный CA (`SSL_CERT_FILE`), NetworkPolicy в k8s.
4. Если upstream живой, а таймауты идут — поднять `HUB_UPSTREAM_TIMEOUT` и проверить
   лимиты сессий на стороне upstream (`maxSessions` у GitLab MCP AI Lab).
5. Ускорить восстановление вручную: `docker compose restart hub` сбрасывает только
   память процесса, состояние выключателя лежит в Redis и переживает рестарт.
   Точечно — удалить ключ `cb:<alias>` в Redis.

## 5. `needs_reauth` у пользователей

**Симптом.** Пользователь видит «Подключение требует повторной авторизации»,
в MCP-ответе `reason: needs_reauth` и `hint_url` на `/ui/servers/{alias}`.

**Причины** (поле `needs_reauth_reason` в таблице `connections`, аудит
`connection_needs_reauth`):

| Причина | Что произошло |
|---|---|
| нет refresh-токена | целевая система не выдала refresh, access истёк |
| обновление отклонено | целевая система вернула `invalid_grant`: пользователь отозвал доступ, сменил пароль, приложение переиздано |
| расширение прав | пользователь запросил пресет шире выданного |

**Действия.**

1. Массовость: `hub_token_refresh_total{result="failed"}` по `alias`. Всплеск по
   одному alias — проблема на стороне системы или переиздано OAuth-приложение
   (тогда надо обновить `*_OAUTH_CLIENT_ID/SECRET` и перевыкатить).
2. Единичный случай — пользователь переподключается сам на `/ui/servers/{alias}`.
3. Проверить, что `HUB_ENCRYPTION_KEY` не менялся: смена ключа делает сохранённые
   токены нечитаемыми и переводит **все** подключения в `needs_reauth`.

## 6. Истёк корпоративный сертификат

**Стенд.** Клиенты получают ошибку доверия TLS, Hub при этом здоров.
Заменить `deploy/caddy/certs/server.crt` и `server.key`, затем
`docker compose ... up -d proxy` (перезапуск только прокси, Hub не трогаем).
Имя в сертификате обязано совпадать с `HUB_SITE_ADDRESS` и `HUB_PUBLIC_URL`.

**k8s.** Обновить TLS-секрет ingress (`ingress.tls.secretName`) или, при istio,
секрет Gateway. Под Hub перезапускать не нужно.

**Истёк CA, которому доверяет Hub** (`SSL_CERT_FILE`): симптом обратный — Hub не может
позвонить в LiteLLM и целевые системы, в логе TLS-ошибки, `/auth/login` отдаёт 502.
Обновить `deploy/ca/tander-ca-bundle.pem` (в k8s — соответствующий ConfigMap/Secret)
и перезапустить Hub.

Профилактика: следить за сроком заранее, срок виден в
`echo | openssl s_client -connect <хост>:443 2>/dev/null | openssl x509 -noout -dates`.

## 7. Как отозвать ключ или подключение пользователя

* **Подключение к системе** (штатно, самим пользователем): кнопка «Отключить» на
  `/ui/servers/{alias}` или `DELETE /api/me/connections/{alias}`. Отзывает токен в
  целевой системе, удаляет его из БД и гасит **все** токены Hub, выданные на это
  подключение (аудит `connection_disconnected`).
* **Один токен MCP-клиента**: `POST /oauth/revoke` с `token=<access|refresh>` —
  отзывается вся цепочка ротации.
* **Ключ LiteLLM** отзывается на стороне LiteLLM. Hub кэширует результат проверки
  ключа 60 секунд (`AUTH_CACHE_TTL`), то есть отзыв вступает в силу не позднее
  минуты; ускорить — удалить ключи `keyauth:*` в Redis.
* **Скомпрометирован `HUB_SECRET_KEY`**: сменить и перевыкатить — все access- и
  refresh-токены Hub становятся недействительными, пользователи переавторизуют
  MCP-клиентов. Токены целевых систем при этом сохраняются.

Кто что делал, видно в таблице `audit_log`: `connection_connected`,
`connection_disconnected`, `connection_permissions_changed`, `connection_refreshed`,
`connection_refresh_failed`, `connection_needs_reauth`, `oauth_client_registered`,
`oauth_code_issued`, `oauth_token_issued`, `oauth_refresh_reuse_detected`,
`login_started`, `login_completed`, `catalog_reloaded`.

`oauth_refresh_reuse_detected` — повторное использование refresh-токена: цепочка
отзывается автоматически, но событие требует разбора (украденный токен либо
некорректный клиент).

## 8. Как выкатить каталог

1. Изменения `catalog.yaml` — только через merge request, CI валидирует схему.
2. Локальная проверка: `mcp-hub catalog validate --path catalog.yaml`.
3. **Стенд.** Файл монтируется в контейнер, поэтому достаточно
   `docker compose restart hub`, либо без рестарта:

   ```bash
   curl -k -X POST https://<хост>/admin/catalog/reload -H "X-Admin-Token: $HUB_ADMIN_TOKEN"
   ```

   Эндпоинт существует только при заданном `HUB_ADMIN_TOKEN` (иначе 404), атомарно
   заменяет каталог и чистит кэш `tools/list`. Каждую реплику надо дёрнуть отдельно —
   за балансировщиком запрос дойдёт лишь до одной.
4. **k8s.** `helm upgrade` с `--set-file catalog.content=catalog.yaml`: аннотация
   `checksum/catalog` перекатывает Deployment. Быстрый вариант без рестарта — тот же
   `POST /admin/catalog/reload` на каждый под.
5. После выкатки — `./deploy/smoke.sh https://<хост>`: новые facade-alias'ы обязаны
   отдавать AS-метаданные и PRM.

Сервер с незаполненными `${VAR}` считается ненастроенным: пропадает из каталога,
`/.well-known/opencode` и метаданных. Именно так выглядит «сервер исчез после выкатки»,
если забыли добавить переменную окружения.

## 9. Набор метрик для дашборда (D6-12)

Все серии отдаёт `GET /metrics` в формате Prometheus 0.0.4. Имена — фактические,
из `src/hub/metrics.py` и мест наблюдения.

| Метрика | Тип | Метки | Что показывает |
|---|---|---|---|
| `hub_http_requests_total` | counter | `method`, `path`, `status` | все HTTP-запросы к Hub |
| `hub_http_request_duration_seconds` | histogram | `method`, `path` | латентность HTTP |
| `hub_mcp_requests_total` | counter | `alias`, `method`, `status` | запросы к MCP-proxy; `method` — метод JSON-RPC |
| `hub_mcp_request_duration_seconds` | histogram | `alias` | латентность MCP-proxy |
| `hub_upstream_errors_total` | counter | `alias`, `kind` | ошибки upstream: `timeout`, `network`, `http_5xx`, `circuit_open` |
| `hub_upstream_sessions_active` | gauge | `alias` | активные upstream-сессии по серверам |
| `hub_token_refresh_total` | counter | `alias`, `result` (`ok`/`failed`) | обновления токенов целевых систем |
| `hub_oauth_tokens_issued_total` | counter | `grant` (`authorization_code`/`refresh_token`) | выданные токены Hub |
| `hub_login_sessions_active` | gauge | — | живые сессии входа через CLI-SSO |

Панели дашборда:

1. **RPS и p95 по alias** — `rate(hub_mcp_requests_total[5m])` по `alias` и
   `histogram_quantile(0.95, rate(hub_mcp_request_duration_seconds_bucket[5m]))` по `alias`.
2. **Латентность API** — тот же квантиль по `hub_http_request_duration_seconds_bucket`
   с фильтром `path=~"/api/.*|/remote-config"`; цель p95 ≤ 100 мс (S-02).
3. **Ошибки 4xx/5xx** — `rate(hub_http_requests_total{status=~"4..|5.."}[5m])`,
   доля от общего; цель < 0,1 %.
4. **Активные upstream-сессии** — `hub_upstream_sessions_active` по `alias`; сравнивать
   с лимитами upstream (`maxSessions`).
5. **Состояние circuit-breaker** — отдельной серии нет: индикатор —
   `increase(hub_upstream_errors_total{kind="circuit_open"}[5m]) > 0` по `alias`
   (алерт «выключатель открыт»), рядом разрез по остальным `kind`.
6. **Число подключений по серверам** — прямой метрики нет: считать из БД
   (`select alias, status, count(*) from connections group by 1,2`) экспортёром БД либо
   принять как прокси-показатель `hub_upstream_sessions_active`. Явный gauge подключений —
   кандидат в следующую итерацию.
7. **Отказы обновления токенов** — `rate(hub_token_refresh_total{result="failed"}[15m])`
   по `alias`; всплеск = переиздано OAuth-приложение или сломался upstream.
8. **Вход** — `hub_login_sessions_active` и
   `rate(hub_oauth_tokens_issued_total[5m])` по `grant`: шторм `refresh_token` виден здесь.

Алерты-минимум: `/ready` не 200 дольше 2 минут; доля 5xx > 1 % за 5 минут;
`circuit_open` по любому alias; `hub_token_refresh_total{result="failed"}` больше 10
за 15 минут по одному alias; p95 MCP-proxy > 200 мс за 10 минут.

Семейства с метками появляются в выдаче только после первого наблюдения — на свежем
поде часть серий отсутствует, это не поломка экспорта.
