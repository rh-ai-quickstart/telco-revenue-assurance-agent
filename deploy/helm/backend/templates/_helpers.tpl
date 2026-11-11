{{- define "backend.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "backend.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "backend.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "backend.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "backend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "backend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "backend.secretName" -}}
{{- if .Values.database.existingSecret }}
{{- .Values.database.existingSecret }}
{{- else }}
{{- include "backend.fullname" . }}-db
{{- end }}
{{- end }}

{{- define "backend.secretKey" -}}
{{- if .Values.database.existingSecret }}
{{- .Values.database.existingSecretKey }}
{{- else }}
DATABASE_URL
{{- end }}
{{- end }}
