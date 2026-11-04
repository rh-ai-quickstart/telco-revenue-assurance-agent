{{- define "telco.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "telco.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "telco.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "telco.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "telco.labels" -}}
app.kubernetes.io/name: {{ include "telco.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "telco.selectorLabels" -}}
app.kubernetes.io/name: {{ include "telco.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "telco.componentLabels" -}}
{{- $root := .root | default . -}}
{{ include "telco.labels" $root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "telco.componentSelector" -}}
{{- $root := .root | default . -}}
app.kubernetes.io/name: {{ include "telco.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "telco.serviceAccount" -}}
{{- $root := .root | default . -}}
{{- printf "%s-%s" (include "telco.fullname" $root) .suffix | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "telco.oauthProxyTokenSecret" -}}
{{- printf "%s-oauth-proxy-token" (include "telco.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "telco.modelUrl" -}}
{{- default (printf "https://%s-predictor.%s.svc:8443/v2/models/%s/infer" .model.serviceName .root.Release.Namespace .model.modelName) .override -}}
{{- end -}}
