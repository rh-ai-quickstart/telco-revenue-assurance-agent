{{/*
Expand the name of the chart.
*/}}
{{- define "data-fetch-tool.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Full name: release-chart, capped at 63 chars.
*/}}
{{- define "data-fetch-tool.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "data-fetch-tool.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "data-fetch-tool.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels used by the Deployment and Service.
*/}}
{{- define "data-fetch-tool.selectorLabels" -}}
app.kubernetes.io/name: {{ include "data-fetch-tool.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the Secret that holds DATABASE_URL.
Uses existingSecret if provided; otherwise falls back to the chart-managed secret.
*/}}
{{- define "data-fetch-tool.secretName" -}}
{{- if .Values.database.existingSecret }}
{{- .Values.database.existingSecret }}
{{- else }}
{{- include "data-fetch-tool.fullname" . }}-db
{{- end }}
{{- end }}

{{/*
Key inside the secret that holds DATABASE_URL.
*/}}
{{- define "data-fetch-tool.secretKey" -}}
{{- if .Values.database.existingSecret }}
{{- .Values.database.existingSecretKey }}
{{- else }}
DATABASE_URL
{{- end }}
{{- end }}
