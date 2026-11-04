{{- define "telco-training.labels" -}}
app.kubernetes.io/name: telco-training
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: telco-revenue-assurance-agent
{{- end -}}

{{- define "telco-training.storageEndpoint" -}}
{{- include "configure-pipeline.storageEndpoint" (index .Subcharts "configure-pipeline") -}}
{{- end -}}

{{- define "telco-training.storageSecret" -}}
{{- required "configure-pipeline.aws-compatible-storage.s3.existingSecret must name prepared credentials" (index (index .Values "configure-pipeline") "aws-compatible-storage").s3.existingSecret -}}
{{- end -}}
