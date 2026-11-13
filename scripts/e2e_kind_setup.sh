#!/usr/bin/env bash
# Kubernetes setup certification on kind (scripts/e2e_kind/certify.py has the checks):
#   1. create the kind cluster with the Kubernetes audit log on (Metadata level, Symphony
#      ServiceAccounts only; scripts/e2e_kind/audit-policy.yaml). kindnet enforces
#      NetworkPolicy; area 0 of certify.py proves it;
#   2. build every image (scripts/build_images.sh, tag `e2e`) and the mock image, and
#      `kind load` the ones the cluster runs;
#   3. the minimal install: the namespace, ONLY the Secrets symphony-api (bootstrap admin token)
#      and model-provider (Bedrock from the environment), then `kubectl apply -k scripts/e2e_kind`
#      (deploy/ with the documented test deviations; Redis comes from the manifests);
#   4. the vendor mocks in the namespace symphony-e2e-mocks (scripts/e2e_kind/mocks.yaml);
#   5. certify.py: NetworkPolicy, setup through the API, MCP, the assistant and Seiji.
#
#   E2E_TEARDOWN=1 scripts/e2e_kind_setup.sh     # everything, then delete the cluster
#   E2E_SKIP_BUILD=1 scripts/e2e_kind_setup.sh   # reuse the images built before
#   E2E_DELETE=1 scripts/e2e_kind_setup.sh       # delete the cluster and exit
#
# Needs AWS_REGION and AWS_BEARER_TOKEN_BEDROCK (never printed): the attempt and the assistant
# run real models. The mock tokens are random, in $STATE_DIR/mock-tokens.json (mode 0600).
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${E2E_CLUSTER:-symphony-e2e-setup}"
CONTEXT="kind-$CLUSTER"
STATE_DIR="${E2E_STATE_DIR:-$HOME/.cache/symphony-e2e-setup}"
TEARDOWN="${E2E_TEARDOWN:-0}"
kc() { kubectl --context "$CONTEXT" "$@"; }
stamp() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
elapsed() { python3 -c "import sys, time; print(f'{time.time() - float(sys.argv[1]):.1f}')" "$1"; }

for tool in kind kubectl docker uv python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

if [ "${E2E_DELETE:-0}" = "1" ]; then
  kind delete cluster --name "$CLUSTER"
  exit 0
fi
[ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ] || { echo "AWS_BEARER_TOKEN_BEDROCK is not set" >&2; exit 1; }

cleanup() {
  if [ "$TEARDOWN" = "1" ]; then kind delete cluster --name "$CLUSTER" || true; fi
}
trap cleanup EXIT

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
t_start=$(stamp)

# 1. cluster
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  cat >"$STATE_DIR/kind-cluster.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: KubeletConfiguration
        syncFrequency: 5s
      - |
        kind: ClusterConfiguration
        apiServer:
          extraArgs:
            audit-log-path: /var/log/kubernetes/kube-apiserver-audit.log
            audit-policy-file: /etc/kubernetes/policies/audit-policy.yaml
          extraVolumes:
            - name: audit-policies
              hostPath: /etc/kubernetes/policies
              mountPath: /etc/kubernetes/policies
              readOnly: true
              pathType: DirectoryOrCreate
            - name: audit-logs
              hostPath: /var/log/kubernetes
              mountPath: /var/log/kubernetes
              readOnly: false
              pathType: DirectoryOrCreate
    extraMounts:
      - hostPath: $PWD/scripts/e2e_kind/audit-policy.yaml
        containerPath: /etc/kubernetes/policies/audit-policy.yaml
        readOnly: true
EOF
  kind create cluster --name "$CLUSTER" --config "$STATE_DIR/kind-cluster.yaml" --wait 120s
fi
echo "timing: cluster ready in $(elapsed "$t_start")s"

# 2. images
t_images=$(stamp)
if [ "${E2E_SKIP_BUILD:-0}" != "1" ]; then
  TAG=e2e scripts/build_images.sh
fi
docker build -q -f scripts/e2e_kind/mocks.Dockerfile -t symphony-e2e-mocks:e2e . >/dev/null
kind load docker-image --name "$CLUSTER" symphony-orchestrator:e2e symphony-agent:node symphony-e2e-mocks:e2e
echo "timing: images built and loaded in $(elapsed "$t_images")s"

# 3. the minimal install: only symphony-api and model-provider, made by the operator
t_deploy=$(stamp)
kc create namespace symphony --dry-run=client -o yaml | kc apply -f - >/dev/null
TOKEN_FILE="$STATE_DIR/bootstrap-token"
if [ ! -s "$TOKEN_FILE" ]; then
  python3 -c "import secrets; print(f'sym_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}', end='')" >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
# The manifest stays in a shell variable (printf is a builtin): values never reach argv.
create_secret() { # namespace, name, then --from-literal/--from-env-file args
  local ns="$1" name="$2" manifest
  shift 2
  manifest="$(kc -n "$ns" create secret generic "$name" "$@" --dry-run=client -o yaml)"
  printf '%s\n' "$manifest" | kc -n "$ns" create -f - >/dev/null 2>&1 ||
    printf '%s\n' "$manifest" | kc -n "$ns" replace -f - >/dev/null
  echo "secret/$name set"
}
create_secret symphony symphony-api --from-file "SYMPHONY_BOOTSTRAP_TOKEN=$TOKEN_FILE"
create_secret symphony model-provider --from-literal "AWS_REGION=${AWS_REGION:-us-east-1}" \
  --from-literal "AWS_BEARER_TOKEN_BEDROCK=$AWS_BEARER_TOKEN_BEDROCK"
kc apply -k scripts/e2e_kind >/dev/null

# 4. the mocks, with random bearer tokens (an MCP token, and Seiji's `sj_` shape)
if [ ! -s "$STATE_DIR/mock-tokens.json" ]; then
  (umask 077 && python3 - "$STATE_DIR" <<'EOF'
import json, secrets, sys
tokens = {"MCP_MOCK_TOKEN": secrets.token_urlsafe(24), "SEIJI_MOCK_TOKEN": "sj_" + secrets.token_urlsafe(32)[:43]}
open(f"{sys.argv[1]}/mock-tokens.json", "w").write(json.dumps(tokens))
open(f"{sys.argv[1]}/mock-tokens.env", "w").write("".join(f"{k}={v}\n" for k, v in tokens.items()))
EOF
  )
fi
kc create namespace symphony-e2e-mocks --dry-run=client -o yaml | kc apply -f - >/dev/null
create_secret symphony-e2e-mocks mock-tokens --from-env-file "$STATE_DIR/mock-tokens.env"
kc apply -f scripts/e2e_kind/mocks.yaml >/dev/null
for deployment in linear github slack mcp seiji; do
  kc -n symphony-e2e-mocks rollout status "deployment/$deployment" --timeout 180s
done
for deployment in symphony-redis symphony-api symphony-gateway symphony-daemon symphony-merger symphony-assistant; do
  if ! kc -n symphony rollout status "deployment/$deployment" --timeout 240s; then
    kc -n symphony describe "deployment/$deployment" | tail -20 >&2
    kc -n symphony logs "deployment/$deployment" --tail 50 >&2 || true
    exit 1
  fi
done
echo "timing: minimal install applied to rollouts complete in $(elapsed "$t_deploy")s"

# 5. the certification
t_cert=$(stamp)
status=0
uv run --quiet python scripts/e2e_kind/certify.py --context "$CONTEXT" --token-file "$TOKEN_FILE" \
  --state-dir "$STATE_DIR" --summary-out "$STATE_DIR/certification.json" || status=$?
echo "timing: certification in $(elapsed "$t_cert")s"
echo "timing: total $(elapsed "$t_start")s"
echo "certification summary: $STATE_DIR/certification.json"
exit "$status"
