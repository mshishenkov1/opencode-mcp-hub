{{/* Имя чарта и релиза */}}
{{- define "hub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hub.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "hub.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hub.labels" -}}
helm.sh/chart: {{ include "hub.chart" . }}
{{ include "hub.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: opencode-mcp-hub
{{- end -}}

{{- define "hub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "hub.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "hub.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "hub.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* Имя Secret с секретами Hub */}}
{{- define "hub.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "hub.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Есть ли ключ в списке externalSecret.data. Аргумент: dict "ctx" $ "key" "ИМЯ".
Пустая строка — нет, "1" — есть.
*/}}
{{- define "hub.esoHasKey" -}}
{{- $key := .key -}}
{{- $found := "" -}}
{{- if .ctx.Values.externalSecret.enabled -}}
{{- range .ctx.Values.externalSecret.data -}}
{{- if eq .secretKey $key -}}{{- $found = "1" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{/*
Проверки согласованности источника секретов (D6-06).
Все секретные ключи Hub живут в одном Secret (hub.secretName); при
externalSecret.enabled=true единственный источник — externalSecret.data,
поэтому отсутствие обязательного ключа видно уже на этапе шаблона, а не
CrashLoopBackOff'ом пода.
*/}}
{{- define "hub.validateSecrets" -}}
{{- $keys := .Values.secrets.keys -}}
{{- if .Values.externalSecret.enabled }}
{{- if and (not .Values.external.postgres.existingSecret.name) (not (include "hub.esoHasKey" (dict "ctx" . "key" $keys.databaseUrl))) }}
{{- fail (printf "externalSecret.enabled=true, но ключа %s нет ни в externalSecret.data, ни в external.postgres.existingSecret: Hub и Job миграций останутся без адреса БД" $keys.databaseUrl) }}
{{- end }}
{{- if not (include "hub.esoHasKey" (dict "ctx" . "key" $keys.secretKey)) }}
{{- fail (printf "externalSecret.enabled=true, но ключа %s нет в externalSecret.data" $keys.secretKey) }}
{{- end }}
{{- if not (include "hub.esoHasKey" (dict "ctx" . "key" $keys.encryptionKey)) }}
{{- fail (printf "externalSecret.enabled=true, но ключа %s нет в externalSecret.data" $keys.encryptionKey) }}
{{- end }}
{{- end }}
{{- if eq .Values.hub.webAuth "keycloak" }}
{{- if .Values.externalSecret.enabled }}
{{- if not (include "hub.esoHasKey" (dict "ctx" . "key" $keys.keycloakClientSecret)) }}
{{- fail (printf "hub.webAuth=keycloak: %s обязателен (settings.py, R-T2), но его нет в externalSecret.data" $keys.keycloakClientSecret) }}
{{- end }}
{{- else if not (or .Values.secrets.create .Values.secrets.existingSecret) }}
{{- fail (printf "hub.webAuth=keycloak: %s обязателен (R-T2), но источник секретов не задан — включите secrets.create, secrets.existingSecret или externalSecret.enabled" $keys.keycloakClientSecret) }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Имя ConfigMap с каталогом */}}
{{- define "hub.catalogConfigMapName" -}}
{{- if .Values.catalog.existingConfigMap -}}
{{- .Values.catalog.existingConfigMap -}}
{{- else -}}
{{- printf "%s-catalog" (include "hub.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Переменные окружения Hub. Общие для Deployment и Job миграций,
чтобы миграция ходила ровно в ту же БД, что и приложение.
*/}}
{{- define "hub.env" -}}
- name: HUB_PUBLIC_URL
  value: {{ .Values.hub.publicUrl | quote }}
- name: HUB_LOG_LEVEL
  value: {{ .Values.hub.logLevel | quote }}
- name: HUB_CATALOG_PATH
  value: {{ .Values.hub.catalogPath | quote }}
- name: HUB_DB_AUTO_MIGRATE
  value: {{ .Values.hub.autoMigrate | quote }}
- name: HUB_TRUST_PROXY
  value: {{ .Values.hub.trustProxy | quote }}
- name: HUB_WEB_AUTH
  value: {{ .Values.hub.webAuth | quote }}
- name: HUB_CONSENT
  value: {{ .Values.hub.consent | quote }}
- name: HUB_LITELLM_BASE_URL
  value: {{ .Values.hub.litellm.baseUrl | quote }}
- name: HUB_LITELLM_MODEL
  value: {{ .Values.hub.litellm.model | quote }}
{{- if eq .Values.hub.webAuth "keycloak" }}
- name: KEYCLOAK_ISSUER
  value: {{ .Values.hub.keycloak.issuer | quote }}
- name: KEYCLOAK_CLIENT_ID
  value: {{ .Values.hub.keycloak.clientId | quote }}
- name: KEYCLOAK_SCOPES
  value: {{ .Values.hub.keycloak.scopes | quote }}
{{- end }}
{{- if .Values.hub.caBundle.enabled }}
- name: SSL_CERT_FILE
  value: /etc/ssl/certs/corp-ca.pem
- name: REQUESTS_CA_BUNDLE
  value: /etc/ssl/certs/corp-ca.pem
{{- end }}
{{- include "hub.dbEnv" . }}
{{- range $name, $value := .Values.hub.env }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- /* client_id OAuth-приложений каталога — не секрет, обычные переменные окружения.
       Пустое значение не подставляем: сервер останется «ненастроенным». */}}
{{- range $alias, $srv := .Values.catalogOAuth.servers }}
{{- if and $srv.idVar $srv.clientId }}
- name: {{ $srv.idVar }}
  value: {{ $srv.clientId | quote }}
{{- end }}
{{- end }}
{{- include "hub.secretEnv" . }}
{{- end -}}

{{/*
HUB_DATABASE_URL и HUB_REDIS_URL. Источник по умолчанию — общий Secret Hub
(hub.secretName), ключи из secrets.keys. Отдельный Secret (external.*.existingSecret)
используется, только если БД/Redis заводит свой оператор.
*/}}
{{- define "hub.dbEnv" -}}
{{- $keys := .Values.secrets.keys }}
- name: HUB_DATABASE_URL
  valueFrom:
    secretKeyRef:
{{- if .Values.external.postgres.existingSecret.name }}
      name: {{ .Values.external.postgres.existingSecret.name }}
      key: {{ .Values.external.postgres.existingSecret.key }}
{{- else }}
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.databaseUrl }}
{{- end }}
{{- if .Values.external.redis.existingSecret.name }}
- name: HUB_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.redis.existingSecret.name }}
      key: {{ .Values.external.redis.existingSecret.key }}
{{- else if or .Values.secrets.redisUrl (include "hub.esoHasKey" (dict "ctx" . "key" $keys.redisUrl)) }}
- name: HUB_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.redisUrl }}
{{- else if .Values.external.redis.url }}
- name: HUB_REDIS_URL
  value: {{ .Values.external.redis.url | quote }}
{{- end }}
{{- end -}}

{{/* Секретные переменные: всегда из Secret, значений в манифестах нет */}}
{{- define "hub.secretEnv" }}
{{- $keys := .Values.secrets.keys }}
- name: HUB_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.secretKey }}
- name: HUB_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.encryptionKey }}
{{- if or .Values.secrets.adminToken (include "hub.esoHasKey" (dict "ctx" . "key" $keys.adminToken)) }}
- name: HUB_ADMIN_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.adminToken }}
{{- end }}
{{- if eq .Values.hub.webAuth "keycloak" }}
{{- /* Обязателен: без него Hub не стартует (R-T2). Наличие источника проверено
       в hub.validateSecrets, поэтому optional здесь не ставим — отсутствие ключа
       должно быть видно как ошибка запуска пода, а не как молчаливый CrashLoop. */}}
- name: KEYCLOAK_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.keycloakClientSecret }}
{{- end }}
{{- /* client_secret OAuth-приложений каталога: optional — пара id/secret выдаётся
       вместе, и до выдачи сервер штатно остаётся «ненастроенным» (см. catalogOAuth). */}}
{{- range $alias, $srv := .Values.catalogOAuth.servers }}
{{- if $srv.secretVar }}
- name: {{ $srv.secretVar }}
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" $ }}
      key: {{ $srv.secretVar }}
      optional: true
{{- end }}
{{- end }}
{{- end -}}

{{/*
Окружение Job миграций. Job — hook pre-install/pre-upgrade, то есть создаётся ДО
обычных ресурсов релиза. Адрес БД он берёт из ТОГО ЖЕ Secret, что и Deployment,
чтобы миграция шла ровно в ту же базу; чтобы Secret к этому моменту существовал,
secrets.preInstallHook вешает на Secret/ExternalSecret тот же hook с весом -10.
*/}}
{{- define "hub.migrateEnv" -}}
{{- $keys := .Values.secrets.keys }}
- name: HUB_LOG_LEVEL
  value: {{ .Values.hub.logLevel | quote }}
- name: HUB_DATABASE_URL
  valueFrom:
    secretKeyRef:
{{- if .Values.external.postgres.existingSecret.name }}
      name: {{ .Values.external.postgres.existingSecret.name }}
      key: {{ .Values.external.postgres.existingSecret.key }}
{{- else }}
      name: {{ include "hub.secretName" . }}
      key: {{ $keys.databaseUrl }}
{{- end }}
{{- if .Values.hub.caBundle.enabled }}
- name: SSL_CERT_FILE
  value: /etc/ssl/certs/corp-ca.pem
- name: REQUESTS_CA_BUNDLE
  value: /etc/ssl/certs/corp-ca.pem
{{- end }}
{{- end -}}

{{/*
Аннотации hook'а для Secret/ExternalSecret: ресурс должен существовать раньше
Job'а миграций (hook-weight -5). before-hook-creation нужен, чтобы upgrade не
падал на «уже существует».
*/}}
{{- define "hub.secretHookAnnotations" -}}
{{- if and .Values.secrets.preInstallHook .Values.migrations.enabled -}}
annotations:
  "helm.sh/hook": pre-install,pre-upgrade
  "helm.sh/hook-weight": "-10"
  "helm.sh/hook-delete-policy": before-hook-creation
{{- end -}}
{{- end -}}

{{- define "hub.caVolume" -}}
{{- if .Values.hub.caBundle.enabled }}
- name: corp-ca
  configMap:
    name: {{ .Values.hub.caBundle.existingConfigMap }}
    items:
      - key: {{ .Values.hub.caBundle.key }}
        path: corp-ca.pem
{{- end }}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "hub.caVolumeMount" -}}
{{- if .Values.hub.caBundle.enabled }}
- name: corp-ca
  mountPath: /etc/ssl/certs/corp-ca.pem
  subPath: corp-ca.pem
  readOnly: true
{{- end }}
- name: tmp
  mountPath: /tmp
{{- end -}}

{{/* Тома: каталог, корпоративный CA, writable tmp (readOnlyRootFilesystem) */}}
{{- define "hub.volumes" -}}
- name: catalog
  configMap:
    name: {{ include "hub.catalogConfigMapName" . }}
    items:
      - key: {{ .Values.catalog.key }}
        path: catalog.yaml
- name: tmp
  emptyDir: {}
{{- if .Values.hub.caBundle.enabled }}
- name: corp-ca
  configMap:
    name: {{ .Values.hub.caBundle.existingConfigMap }}
    items:
      - key: {{ .Values.hub.caBundle.key }}
        path: corp-ca.pem
{{- end }}
{{- with .Values.extraVolumes }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end -}}

{{- define "hub.volumeMounts" -}}
- name: catalog
  mountPath: {{ .Values.hub.catalogPath }}
  subPath: catalog.yaml
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- if .Values.hub.caBundle.enabled }}
- name: corp-ca
  mountPath: /etc/ssl/certs/corp-ca.pem
  subPath: corp-ca.pem
  readOnly: true
{{- end }}
{{- with .Values.extraVolumeMounts }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end -}}
