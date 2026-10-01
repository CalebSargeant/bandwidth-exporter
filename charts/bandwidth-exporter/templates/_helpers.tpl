{{- define "bandwidth-exporter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "bandwidth-exporter.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "bandwidth-exporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bandwidth-exporter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "bandwidth-exporter.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "bandwidth-exporter.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "bandwidth-exporter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "bandwidth-exporter.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "bandwidth-exporter.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- if .Values.image.digest }}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}
{{- end }}

{{- define "bandwidth-exporter.configMapName" -}}
{{- default (include "bandwidth-exporter.fullname" .) .Values.existingConfigMap }}
{{- end }}

{{/* The exporter's config.yaml: the user's `config` plus what the chart owns. */}}
{{- define "bandwidth-exporter.config" -}}
{{- $config := deepCopy (.Values.config | default dict) }}
{{- $listen := .Values.listenAddress }}
{{- if contains ":" $listen }}
{{- $listen = printf "[%s]" $listen }}
{{- end }}
{{- $_ := set $config "listen" (printf "%s:%d" $listen (int .Values.service.port)) }}
{{- $_ := set $config "state_dir" "/var/lib/bandwidth-exporter" }}
{{- if not (hasKey $config "network_mode") }}
{{- $_ := set $config "network_mode" (ternary "host" "pod" .Values.hostNetwork) }}
{{- end }}
{{- toYaml $config }}
{{- end }}
