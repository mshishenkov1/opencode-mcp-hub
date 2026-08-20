# Helm-чарты (D6-05…D6-07)

Два независимых чарта:

| Чарт | Что разворачивает | Профили |
|---|---|---|
| `opencode-mcp-hub/` | Hub: каталог, OAuth-фасад, MCP-proxy, веб-интерфейс | `values-pilot.yaml`, `values-prod.yaml` |
| `tag-mcp/` | ТЭГ-MCP (`magnit-tag-mcp`, `MM_HTTP_AUTH=oauth`) | `values-pilot.yaml`, `values-prod.yaml` |

Postgres и Redis чартами **не** разворачиваются: адреса и секреты приходят из values
(`external.postgres`, `external.redis`, `redis`). Subchart для теста — вне объёма I-6.

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

Три взаимоисключающих режима:

1. `secrets.create=true` — чарт создаёт Secret из values (`--set secrets.secretKey=…`).
   Годится для пилота и тестового namespace; значения в git не коммитятся.
2. `secrets.create=false` + `secrets.existingSecret=<имя>` — Secret заведён вручную.
3. `externalSecret.enabled=true` — Secret наполняет оператор external-secrets из Vault;
   имя целевого Secret совпадает с `secrets.existingSecret`, поэтому Deployment
   подхватывает его без дополнительных настроек.

Одновременно `secrets.create=true` и `externalSecret.enabled=true` — ошибка шаблона.

## Миграции

`Job {release}-migrate` c хуком `pre-install,pre-upgrade` (weight `-5`) выполняет
`mcp-hub db upgrade`. Job — hook, поэтому создаётся **до** ConfigMap каталога и
управляемого чартом Secret, и получает только `HUB_DATABASE_URL` и корпоративный CA:

- предпочтительно — `external.postgres.existingSecret` (URL не попадает в манифест Job);
- иначе `external.postgres.url` подставится в Job значением в открытом виде.

Если ни то ни другое не задано и `migrations.enabled=true`, шаблон падает с внятной ошибкой.

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

## Что осталось за рамками

- `helm --dry-run`/установка в настоящий кластер: namespace и доступ ещё не выданы.
- Схема HPA по RPS (`autoscaling.requestsPerSecond`) требует prometheus-adapter;
  по умолчанию выключена, работает автоскейл по CPU.
- `tag-mcp` пока не отдаёт `/metrics`, поэтому `serviceMonitor.enabled=false` в обоих профилях.
