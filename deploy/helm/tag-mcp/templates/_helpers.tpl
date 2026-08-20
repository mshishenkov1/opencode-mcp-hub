{{- define "tagmcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tagmcp.fullname" -}}
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

{{- define "tagmcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tagmcp.labels" -}}
helm.sh/chart: {{ include "tagmcp.chart" . }}
{{ include "tagmcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: opencode-mcp-hub
{{- end -}}

{{- define "tagmcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tagmcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tagmcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "tagmcp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "tagmcp.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{- define "tagmcp.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "tagmcp.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Есть ли ключ в externalSecret.data. Аргумент: dict "ctx" $ "key" "ИМЯ".
*/}}
{{- define "tagmcp.esoHasKey" -}}
{{- $key := .key -}}
{{- $found := "" -}}
{{- if .ctx.Values.externalSecret.enabled -}}
{{- range .ctx.Values.externalSecret.data -}}
{{- if eq .secretKey $key -}}{{- $found = "1" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{/* Согласованность источника секретов: при ESO обязательные ключи должны быть в data */}}
{{- define "tagmcp.validateSecrets" -}}
{{- $keys := .Values.secrets.keys -}}
{{- if .Values.externalSecret.enabled }}
{{- range list $keys.mmOauthClientId $keys.mmOauthClientSecret }}
{{- if not (include "tagmcp.esoHasKey" (dict "ctx" $ "key" .)) }}
{{- fail (printf "externalSecret.enabled=true, но ключа %s нет в externalSecret.data" .) }}
{{- end }}
{{- end }}
{{- if and (not $.Values.redis.existingSecret.name) (not $.Values.redis.url) (not (include "tagmcp.esoHasKey" (dict "ctx" $ "key" $keys.redisUrl))) }}
{{- fail (printf "не задан адрес Redis: ни redis.url, ни redis.existingSecret.name, ни ключ %s в externalSecret.data" $keys.redisUrl) }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "tagmcp.env" -}}
- name: TAG_MCP_PUBLIC_URL
  value: {{ .Values.tag.publicUrl | quote }}
- name: MM_URL
  value: {{ .Values.tag.mattermostUrl | quote }}
- name: MM_HTTP_AUTH
  value: {{ .Values.tag.httpAuth | quote }}
- name: TAG_MCP_LOG_LEVEL
  value: {{ .Values.tag.logLevel | quote }}
{{- if .Values.redis.existingSecret.name }}
- name: TAG_MCP_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.existingSecret.name }}
      key: {{ .Values.redis.existingSecret.key }}
{{- else if or .Values.secrets.redisUrl (include "tagmcp.esoHasKey" (dict "ctx" . "key" .Values.secrets.keys.redisUrl)) }}
- name: TAG_MCP_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "tagmcp.secretName" . }}
      key: {{ .Values.secrets.keys.redisUrl }}
{{- else if .Values.redis.url }}
- name: TAG_MCP_REDIS_URL
  value: {{ .Values.redis.url | quote }}
{{- else }}
{{- fail "не задан адрес Redis (redis.url / redis.existingSecret.name / secrets.redisUrl): без общего Redis реплики tag-mcp потребуют sticky-сессий" }}
{{- end }}
{{- if .Values.tag.caBundle.enabled }}
- name: SSL_CERT_FILE
  value: /etc/ssl/certs/corp-ca.pem
- name: REQUESTS_CA_BUNDLE
  value: /etc/ssl/certs/corp-ca.pem
{{- end }}
{{- range $name, $value := .Values.tag.env }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{ include "tagmcp.secretEnv" . }}
{{- end -}}

{{- define "tagmcp.secretEnv" }}
{{- $keys := .Values.secrets.keys }}
- name: MM_OAUTH_CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "tagmcp.secretName" . }}
      key: {{ $keys.mmOauthClientId }}
- name: MM_OAUTH_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "tagmcp.secretName" . }}
      key: {{ $keys.mmOauthClientSecret }}
{{- range $name, $_ := .Values.secrets.extra }}
- name: {{ $name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "tagmcp.secretName" $ }}
      key: {{ $name }}
{{- end }}
{{- end -}}

{{- define "tagmcp.volumes" -}}
- name: tmp
  emptyDir: {}
{{- if .Values.tag.caBundle.enabled }}
- name: corp-ca
  configMap:
    name: {{ .Values.tag.caBundle.existingConfigMap }}
    items:
      - key: {{ .Values.tag.caBundle.key }}
        path: corp-ca.pem
{{- end }}
{{- end -}}

{{- define "tagmcp.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if .Values.tag.caBundle.enabled }}
- name: corp-ca
  mountPath: /etc/ssl/certs/corp-ca.pem
  subPath: corp-ca.pem
  readOnly: true
{{- end }}
{{- end -}}
