# Helm-чарты (D6-05…D6-07)

Два независимых чарта:

| Чарт | Что разворачивает | Профили |
|---|---|---|
| `opencode-mcp-hub/` | Hub: каталог, OAuth-фасад, MCP-proxy, веб-интерфейс | `values-pilot.yaml`, `values-prod.yaml` |
| `tag-mcp/` | ТЭГ-MCP (`magnit-tag-mcp`, `MM_HTTP_AUTH=oauth`) | `values-pilot.yaml`, `values-prod.yaml` |

Postgres и Redis чартами **не** разворачиваются: адреса приходят секретами в общем
Secret релиза (`secrets.databaseUrl`, `secrets.redisUrl`) либо из готового Secret
оператора (`external.postgres.existingSecret`, `external.redis.existingSecret`,
в `tag-mcp` — `redis.existingSecret`). Subchart для теста — вне объёма I-6.

## Проверка без кластера

```bash
helm lint deploy/helm/opencode-mcp-hub
helm lint deploy/helm/opencode-mcp-hub -f deploy/helm/opencode-mcp-hub/values-pilot.yaml
helm lint deploy/helm/opencode-mcp-hub -f deploy/helm/opencode-mcp-hub/values-prod.yaml

helm template hub deploy/helm/opencode-mcp-hub \
  -f deploy/helm/opencode-mcp-hub/values-prod.yaml \
  --set-file catalog.content=catalog.yaml

helm lint deploy/helm/tag-mcp
helm template tag deploy/helm/tag-mcp -f deploy/helm/tag-mcp/values-prod.yaml
```

`--dry-run` против настоящего kubeconfig — вне объёма I-6 (D6-06).

## Каталог MCP-серверов

`catalog.yaml` попадает в ConfigMap. Значение по умолчанию — заглушка `servers: []`,
чтобы `helm lint`/`template` проходили без внешних файлов. Настоящий каталог:

```bash
--set-file catalog.content=catalog.yaml
# либо готовый ConfigMap:
--set catalog.existingConfigMap=hub-catalog --set catalog.create=false
```

Выкладка нового каталога — `helm upgrade` с новым `--set-file`; аннотация
`checksum/catalog` на подах перекатывает Deployment. Быстрая альтернатива без рестарта —
`POST /admin/catalog/reload` с `HUB_ADMIN_TOKEN` (см. `deploy/runbook.md`).

## Секреты

**Все секретные переменные обоих чартов лежат в одном Secret на релиз.** Имя —
`secrets.existingSecret`, а по умолчанию `<release>-secrets`. Отдельных Secret'ов
под БД, Redis или Keycloak чарты не создают и не ждут.

Три взаимоисключающих режима наполнения:

1. `secrets.create=true` — Secret создаёт чарт из values (`--set secrets.secretKey=…`).
   Годится для пилота и тестового namespace; значения в git не коммитятся.
2. `secrets.create=false` при выключенном `externalSecret` — Secret заведён вне чарта
   (`kubectl create secret generic <release>-secrets --from-literal=…`).
3. `externalSecret.enabled=true` — Secret наполняет оператор external-secrets из Vault;
   `externalSecret.data[].secretKey` обязан совпадать с именами ключей ниже.

Одновременно `secrets.create=true` и `externalSecret.enabled=true` — ошибка шаблона.

### Ключи Secret `<release>-secrets` (чарт `opencode-mcp-hub`)

Имена ключей задаются в `secrets.keys` (по умолчанию совпадают с именами переменных
окружения Hub — менять стоит только при переиспользовании чужого Secret).

| Ключ | Обязателен | Значение при `secrets.create=true` | Что будет без него |
|---|---|---|---|
| `HUB_SECRET_KEY` | да | `secrets.secretKey` | Hub не стартует (R-T2) |
| `HUB_ENCRYPTION_KEY` | да | `secrets.encryptionKey` | Hub не стартует (R-T2) |
| `HUB_DATABASE_URL` | да | `secrets.databaseUrl` | Job миграций и Hub не находят БД |
| `HUB_REDIS_URL` | при `replicaCount > 1` | `secrets.redisUrl` | KeyValueStore в памяти процесса, реплики не делят состояние (R-N5) |
| `KEYCLOAK_CLIENT_SECRET` | при `hub.webAuth=keycloak` | `secrets.keycloakClientSecret` | Hub не стартует: `ConfigError` в `settings.py` |
| `HUB_ADMIN_TOKEN` | нет | `secrets.adminToken` | `POST /admin/catalog/reload` выключен |
| `<ALIAS>_OAUTH_CLIENT_SECRET` | по одному на facade-сервер каталога | `catalogOAuth.servers.<alias>.clientSecret` | сервер остаётся «ненастроенным» (см. ниже) |

Проверки на этапе шаблона (`_helpers.tpl`, `hub.validateSecrets`) — `{{ fail }}`, если:

* `externalSecret.enabled=true`, а в `externalSecret.data` нет `HUB_SECRET_KEY`,
  `HUB_ENCRYPTION_KEY` или `HUB_DATABASE_URL` (последний — если не задан
  `external.postgres.existingSecret`);
* `hub.webAuth=keycloak`, а источника `KEYCLOAK_CLIENT_SECRET` нет вовсе
  (при ESO — ключ отсутствует в `data`).

Остальное отбивается предупреждениями `NOTES.txt` при установке.

### Ключи Secret `<release>-secrets` (чарт `tag-mcp`)

| Ключ | Обязателен | Значение при `secrets.create=true` |
|---|---|---|
| `MM_OAUTH_CLIENT_ID` | да | `secrets.mmOauthClientId` |
| `MM_OAUTH_CLIENT_SECRET` | да | `secrets.mmOauthClientSecret` |
| `TAG_MCP_REDIS_URL` | если Redis с паролем | `secrets.redisUrl` |

### Когда БД или Redis заводит отдельный оператор

`external.postgres.existingSecret` / `external.redis.existingSecret` (в `tag-mcp` —
`redis.existingSecret`) переключают чтение конкретного адреса на чужой Secret.
Тогда одноимённого ключа в общем Secret быть не должно. Redis **без пароля** можно
задать открытым значением `external.redis.url` — оно попадёт в манифест Deployment;
Redis с паролем — только через Secret.

## OAuth-приложения целевых систем (facade-серверы каталога)

Боевой `catalog.yaml` подставляет `client_id` из `${<ALIAS>_OAUTH_CLIENT_ID}` и читает
`client_secret` из `env:<ALIAS>_OAUTH_CLIENT_SECRET`. Обе переменные задаются секцией
`catalogOAuth.servers` чарта `opencode-mcp-hub`: `client_id` — обычная переменная
окружения Deployment (не секрет), `client_secret` — ключ общего Secret.

| alias в каталоге | client_id (env) | client_secret (ключ Secret) |
|---|---|---|
| `gitlab` | `GITLAB_OAUTH_CLIENT_ID` | `GITLAB_OAUTH_CLIENT_SECRET` |
| `gitlab-platform` | `GITLAB_PLATFORM_OAUTH_CLIENT_ID` | `GITLAB_PLATFORM_OAUTH_CLIENT_SECRET` |
| `jira` | `JIRA_OAUTH_CLIENT_ID` | `JIRA_OAUTH_CLIENT_SECRET` |
| `confluence` | `CONFLUENCE_OAUTH_CLIENT_ID` | `CONFLUENCE_OAUTH_CLIENT_SECRET` |
| `tag` (нативный) | `TAG_MCP_URL` — адрес сервера, задаётся в `hub.env` | нет: OAuth-креды живут в чарте `tag-mcp` |

Выдали приложение — при пилоте достаточно `--set`:

```bash
helm upgrade hub deploy/helm/opencode-mcp-hub \
  -f deploy/helm/opencode-mcp-hub/values-pilot.yaml \
  --set-file catalog.content=catalog.yaml \
  --set catalogOAuth.servers.jira.clientId=jira-app-id \
  --set catalogOAuth.servers.jira.clientSecret=…
```

В продуктиве `client_secret` берётся только из Vault: в `externalSecret.data` ключи для
всех четырёх серверов уже описаны, а `client_id` задаётся в `values-prod.yaml`
(`catalogOAuth.servers.<alias>.clientId`).

**Сервер без выданного приложения — это штатное состояние, а не сбой.** Пока
`${<ALIAS>_OAUTH_CLIENT_ID}` не задан, запись каталога помечена `status: beta`, и Hub
считает сервер `unconfigured`: стартует нормально, но сервер не показывается в
`/api/catalog`, не публикует метаданные OAuth/PRM и не попадает в `/.well-known/opencode`.
Симптом «каталог пуст после выкатки» (`deploy/runbook.md` §8) означает именно это.
`NOTES.txt` предупреждает, если не задан ни один `clientId`, а `mcp-hub catalog validate`
печатает список `unconfigured`. `client_secret` подключается с `optional: true` — id и
secret выдаются парой, и до выдачи отсутствие ключа не должно мешать поду стартовать.

## Миграции

`Job {release}-migrate` c хуком `pre-install,pre-upgrade` (weight `-5`) выполняет
`mcp-hub db upgrade`. Адрес БД он берёт из того же Secret и того же ключа, что и
Deployment (`HUB_DATABASE_URL`), — миграция гарантированно идёт в ту же базу.

Job — hook, то есть создаётся раньше обычных ресурсов релиза. Чтобы Secret к этому
моменту существовал, `secrets.preInstallHook=true` (по умолчанию) вешает тот же hook
с весом `-10` на Secret и на ExternalSecret. Плата: hook-ресурс не входит в манифест
релиза, поэтому `helm uninstall` его не удаляет — Secret снимается вручную.
Отключить: `--set secrets.preInstallHook=false` (тогда при `migrations.enabled=true`
Secret нужно завести до установки).

Если БД заводит отдельный оператор, `external.postgres.existingSecret` переключает на
его Secret и Deployment, и Job.

`hub.autoMigrate` (`HUB_DB_AUTO_MIGRATE`) по умолчанию `false`. Значение `true` тоже
рабочее — миграция берёт advisory-блокировку PostgreSQL (R-M1), и вторая реплика ждёт;
но при выкатке волнами предсказуемее один контролируемый шаг.

## Ingress и istio

По умолчанию — обычный `Ingress` с корпоративным TLS-секретом. При `istio.enabled=true`
вместо него рендерится `VirtualService` на общий Gateway (`istio.gateway`), TLS
терминирует Gateway. Sidecar в Job миграций отключается аннотацией — иначе Job не
завершается.

Буферизация ответов на ingress должна быть выключена (`proxy-buffering: off`): MCP-proxy
отдаёт SSE потоково.

`hub.trustProxy` (`HUB_TRUST_PROXY`) — значение профиля, а не константа чарта. `true`
допустимо, только пока граничный прокси **перезаписывает** `X-Forwarded-For`:
nginx-ingress по умолчанию — да, с `use-forwarded-headers: true` — уже нет, а Envoy в
istio заголовок дополняет. Если левый элемент заголовка задаёт клиент, лимиты
`HUB_RATE_LIMIT_REGISTER`/`HUB_RATE_LIMIT_TOKEN` обходятся подделкой. При
`istio.enabled=true` ставьте `hub.trustProxy: false`, пока у Gateway не настроен
`numTrustedHops`; `NOTES.txt` печатает предупреждение об этом сочетании.

## Что осталось за рамками

- `helm --dry-run`/установка в настоящий кластер: namespace и доступ ещё не выданы.
- Схема HPA по RPS (`autoscaling.requestsPerSecond`) требует prometheus-adapter;
  по умолчанию выключена, работает автоскейл по CPU.
- `tag-mcp` пока не отдаёт `/metrics`, поэтому `serviceMonitor.enabled=false` в обоих профилях.
