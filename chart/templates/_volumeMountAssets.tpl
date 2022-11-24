# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

{{- define "volumeMountAssetsRO" -}}
- mountPath: {{ .Values.cache.assetsDirectory | quote }}
  mountPropagation: None
  name: data
  subPath: "{{ include "assets.subpath" . }}"
  readOnly: true
{{- end -}}

{{- define "volumeMountAssetsRW" -}}
- mountPath: {{ .Values.cache.assetsDirectory | quote }}
  mountPropagation: None
  name: data
  subPath: "{{ include "assets.subpath" . }}"
  readOnly: false
{{- end -}}