#!/usr/bin/env bash
# Publish WORKFLOW.md into the cluster as the symphony-workflow ConfigMap, bypassing the API.
# Normally the first-run setup seeds it and later changes go through the audited API
# (PUT /v1/workflow, admin); this script is for tests and recovery.
#
# Every consumer re-reads the mounted file and keeps the last-known-good version when a
# new one is invalid, so this is safe at any time; the kubelet delivers updates to the
# mounts within about a minute.
#
#   scripts/sync_workflow.sh [path/to/WORKFLOW.md] [namespace]
#   KUBE_CONTEXT=kind-symphony-e2e scripts/sync_workflow.sh ...   # explicit kubectl context
set -euo pipefail

if [ -n "${1:-}" ]; then
  WORKFLOW="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
fi
cd "$(dirname "$0")/.."
WORKFLOW="${WORKFLOW:-$PWD/WORKFLOW.md}"
NAMESPACE="${2:-symphony}"
CONFIGMAP="${SYMPHONY_WORKFLOW_CONFIGMAP:-symphony-workflow}"

# Parse BEFORE shipping: an invalid file would be rejected by every consumer anyway, but
# failing here names the error instead of hiding it in pod logs.
uv run --quiet python - "$WORKFLOW" <<'EOF'
import pathlib
import sys

from symphony.config import parse_workflow

parse_workflow(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"{sys.argv[1]} parses cleanly")
EOF

# Replace (or create on a cluster without deploy/integrations.yaml) — never `kubectl
# apply`: the deploy ships this ConfigMap empty, and an applied document would be erased
# by the next `kubectl apply -k deploy/`.
context=()
[ -n "${KUBE_CONTEXT:-}" ] && context=(--context "$KUBE_CONTEXT")
manifest="$(kubectl ${context[@]+"${context[@]}"} create configmap "$CONFIGMAP" \
  --namespace "$NAMESPACE" --from-file "WORKFLOW.md=$WORKFLOW" --dry-run=client -o yaml)"
printf '%s\n' "$manifest" | kubectl ${context[@]+"${context[@]}"} replace --namespace "$NAMESPACE" -f - >/dev/null 2>&1 ||
  printf '%s\n' "$manifest" | kubectl ${context[@]+"${context[@]}"} create --namespace "$NAMESPACE" -f - >/dev/null

echo "synced $WORKFLOW -> configmap/$CONFIGMAP in namespace $NAMESPACE"
