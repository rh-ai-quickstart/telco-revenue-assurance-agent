{{- define "telco-artifacts.labels" -}}
app.kubernetes.io/name: telco-feature-artifacts
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: telco-revenue-assurance-agent
app.kubernetes.io/component: model-artifact-loader
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "telco-artifacts.jobName" -}}
{{- $manifest := required "Supply the actual bundleManifest with --set-file" .Values.bundleManifest -}}
{{- $secret := required "Supply the verified readonly connectionSecret" .Values.connectionSecret -}}
{{- $content := printf "%s\n%s\n%s" (.Values | toJson) (.Files.Get "files/load_bundle.py") (.Files.Get "files/s3-requirements.txt") -}}
{{- printf "%s-%s" (.Release.Name | trunc 40 | trimSuffix "-") ($content | sha256sum | trunc 12) -}}
{{- end -}}
