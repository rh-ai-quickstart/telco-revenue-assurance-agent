{{- define "predictor.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if eq .Release.Name .Chart.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Preserve the existing Deployment's immutable selector for Helm adoption. */}}
{{- define "predictor.selectorLabels" -}}
app: {{ include "predictor.fullname" . }}
{{- end -}}

{{- define "predictor.labels" -}}
{{ include "predictor.selectorLabels" . }}
app.kubernetes.io/name: predictor
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- end -}}

{{- define "predictor.serviceAccountName" -}}
{{- default (include "predictor.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{- define "predictor.httpCAName" -}}
{{- if .Values.tls.http.openShiftServiceCA -}}
{{- printf "%s-service-ca" (include "predictor.fullname" .) -}}
{{- else -}}
{{- .Values.tls.http.existingConfigMap -}}
{{- end -}}
{{- end -}}

{{- define "predictor.validate" -}}
{{- $_ := required "image.repository is required (build predictor/Dockerfile first)" .Values.image.repository -}}
{{- $_ := required "dataFetch.url is required (DATA_FETCH_URL)" .Values.dataFetch.url -}}
{{- if not (or .Values.models.churn.url .Values.models.fraud.url .Values.models.qoe.url) -}}
{{- fail "Configure at least one models.<model>.url (complete KServe v2 inference URL)" -}}
{{- end -}}
{{- if and (not .Values.serviceAccount.create) (not .Values.serviceAccount.name) -}}
{{- fail "serviceAccount.name is required when serviceAccount.create=false" -}}
{{- end -}}
{{- if and (eq .Values.inference.auth.mode "serviceAccount") (not .Values.serviceAccount.automountServiceAccountToken) -}}
{{- fail "inference.auth.mode=serviceAccount requires serviceAccount.automountServiceAccountToken=true" -}}
{{- end -}}
{{- if eq .Values.inference.auth.mode "secret" -}}
{{- $_ := required "inference.auth.existingSecret is required for secret authentication" .Values.inference.auth.existingSecret -}}
{{- else if .Values.inference.auth.existingSecret -}}
{{- fail "inference.auth.existingSecret requires inference.auth.mode=secret" -}}
{{- end -}}
{{- if and .Values.tls.http.openShiftServiceCA .Values.tls.http.existingConfigMap -}}
{{- fail "Choose tls.http.openShiftServiceCA or tls.http.existingConfigMap, not both" -}}
{{- end -}}
{{- if ge (float64 .Values.qoe.poorThreshold) (float64 .Values.qoe.goodThreshold) -}}
{{- fail "qoe.poorThreshold must be less than qoe.goodThreshold" -}}
{{- end -}}
{{- if and .Values.qoe.artifacts.bucket .Values.qoe.artifacts.bucketSecret.name -}}
{{- fail "Choose qoe.artifacts.bucket or qoe.artifacts.bucketSecret.name, not both" -}}
{{- end -}}
{{- if .Values.models.qoe.url -}}
{{- $_ := required "qoe.artifacts.endpoint is required for QoE (AWS_S3_ENDPOINT)" .Values.qoe.artifacts.endpoint -}}
{{- $_ := required "qoe.artifacts.prefix is required for QoE (S3_MODEL_PATH)" .Values.qoe.artifacts.prefix -}}
{{- if not (or .Values.qoe.artifacts.bucket .Values.qoe.artifacts.bucketSecret.name) -}}
{{- fail "QoE requires qoe.artifacts.bucket or qoe.artifacts.bucketSecret.name (S3_BUCKET)" -}}
{{- end -}}
{{- end -}}
{{- end -}}
