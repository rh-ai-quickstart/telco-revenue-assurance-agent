{{/*
Expand the name of the chart.
*/}}
{{- define "revenue-assurance-dashboard.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Full name of the release, capped at 63 characters.
*/}}
{{- define "revenue-assurance-dashboard.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels applied to every rendered resource.
*/}}
{{- define "revenue-assurance-dashboard.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "revenue-assurance-dashboard.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels used by the Deployment, Service, and Route.
*/}}
{{- define "revenue-assurance-dashboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "revenue-assurance-dashboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Optional image pull secret names.
*/}}
{{- define "revenue-assurance-dashboard.imagePullSecrets" -}}
{{- if .Values.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.imagePullSecrets }}
  - name: {{ .name | quote }}
{{- end }}
{{- end }}
{{- end }}
