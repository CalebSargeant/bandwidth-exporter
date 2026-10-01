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
{{- if .Values.northSouthOn }}
{{- $_ := set $config "north_south_on" .Values.northSouthOn }}
{{- end }}
{{- if and (eq .Values.workload.kind "DaemonSet") (not (hasKey $config "startup_jitter")) }}
{{- /* Every pod of a DaemonSet restarts in one rollout; spread their first tests. */}}
{{- $_ := set $config "startup_jitter" "30m" }}
{{- end }}
{{- if .Values.responder.enabled }}
{{- $responder := deepCopy ($config.responder | default dict) }}
{{- $_ := set $responder "enabled" true }}
{{- $_ := set $responder "listen" (printf "%s:%d" $listen (int .Values.responder.port)) }}
{{- $_ := set $responder "data_ports" (dict "first" (int .Values.responder.dataPorts.first) "last" (int .Values.responder.dataPorts.last)) }}
{{- $_ := set $config "responder" $responder }}
{{- end }}
{{- if .Values.mesh.enabled }}
{{- $mesh := dict "name" .Values.mesh.name "backend" .Values.mesh.backend "schedule" .Values.mesh.schedule }}
{{- $_ := set $mesh "discovery" (dict "dns" (include "bandwidth-exporter.peersHost" .) "port" (int .Values.responder.port)) }}
{{- $_ := set $mesh "topology" (dict "random_peers" (.Values.mesh.randomPeers | default nil)) }}
{{- $mesh = merge $mesh (deepCopy (.Values.mesh.extra | default dict)) }}
{{- $_ := set $config "east_west" (append ($config.east_west | default list) $mesh) }}
{{- end }}
{{- toYaml $config }}
{{- end }}

{{/* The headless Service that returns one address per ready pod: east/west discovery. */}}
{{- define "bandwidth-exporter.peersName" -}}
{{- printf "%s-peers" (include "bandwidth-exporter.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "bandwidth-exporter.peersHost" -}}
{{- printf "%s.%s.svc.%s" (include "bandwidth-exporter.peersName" .) .Release.Namespace .Values.clusterDomain }}
{{- end }}

{{/* Whether this release does east/west work and so needs the peer keys. */}}
{{- define "bandwidth-exporter.peerRole" -}}
{{- $config := .Values.config | default dict }}
{{- if or .Values.responder.enabled .Values.mesh.enabled $config.east_west }}true{{ end }}
{{- end }}
