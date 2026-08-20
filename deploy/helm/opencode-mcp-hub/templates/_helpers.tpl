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
{{- include "hub.secretEnv" . }}
{{- end -}}

{{/* HUB_DATABASE_URL и HUB_REDIS_URL: из отдельного Secret или из управляемого чартом */}}
{{- define "hub.dbEnv" -}}
{{- with .Values.external.postgres }}
{{- if .existingSecret.name }}
- name: HUB_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .existingSecret.name }}
      key: {{ .existingSecret.key }}
{{- else if .url }}
- name: HUB_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" $ }}
      key: HUB_DATABASE_URL
{{- end }}
{{- end }}
{{- with .Values.external.redis }}
{{- if .existingSecret.name }}
- name: HUB_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .existingSecret.name }}
      key: {{ .existingSecret.key }}
{{- else if .url }}
- name: HUB_REDIS_URL
  value: {{ .url | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Секретные переменные: всегда из Secret, значений в манифестах нет */}}
{{- define "hub.secretEnv" }}
- name: HUB_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: HUB_SECRET_KEY
- name: HUB_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: HUB_ENCRYPTION_KEY
{{- if .Values.secrets.adminToken }}
- name: HUB_ADMIN_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: HUB_ADMIN_TOKEN
{{- end }}
{{- if eq .Values.hub.webAuth "keycloak" }}
- name: KEYCLOAK_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" . }}
      key: KEYCLOAK_CLIENT_SECRET
      optional: true
{{- end }}
{{- range $name, $_ := .Values.secrets.oauth }}
- name: {{ $name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "hub.secretName" $ }}
      key: {{ $name }}
      optional: true
{{- end }}
{{- end -}}

{{/*
Окружение Job миграций. Job — hook pre-install/pre-upgrade, то есть создаётся ДО
ConfigMap каталога и управляемого чартом Secret. Поэтому здесь только то, что нужно
команде `mcp-hub db upgrade`: адрес БД и корпоративный CA. Адрес БД берётся из
существующего Secret (external.postgres.existingSecret) — так он не попадает в манифест;
если задан только external.postgres.url, он подставится значением в открытом виде.
*/}}
{{- define "hub.migrateEnv" -}}
- name: HUB_LOG_LEVEL
  value: {{ .Values.hub.logLevel | quote }}
{{- with .Values.external.postgres }}
{{- if .existingSecret.name }}
- name: HUB_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .existingSecret.name }}
      key: {{ .existingSecret.key }}
{{- else if .url }}
- name: HUB_DATABASE_URL
  value: {{ .url | quote }}
{{- else }}
{{- fail "миграции включены, но не задан ни external.postgres.existingSecret.name, ни external.postgres.url" }}
{{- end }}
{{- end }}
{{- if .Values.hub.caBundle.enabled }}
- name: SSL_CERT_FILE
  value: /etc/ssl/certs/corp-ca.pem
- name: REQUESTS_CA_BUNDLE
  value: /etc/ssl/certs/corp-ca.pem
{{- end }}
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
