#!/usr/bin/env bash
# End-to-end deploy on a local kind cluster, and the Kubernetes certification:
#   1. create the kind cluster (scripts/e2e/kind-cluster.yaml; reused when it exists);
#   2. build the images (orchestrator tag `e2e`, agent `node`) and `kind load` them;
#   3. create the namespace, the Secrets (Bedrock from the environment into model-provider)
#      and a bootstrap admin token;
#   4. publish the certification WORKFLOW.md (memory tracker, empty board, local git hook);
#   5. apply scripts/e2e (kustomize overlay of deploy/) and wait for redis, api, gateway, daemon;
#   6. port-forward the api and prove it answers (`/healthz`, `/v1/me` with the token);
#   7. with Bedrock credentials: one REAL attempt, scripts/e2e/certify.py (see its docstring).
#
#   scripts/e2e_kind.sh                    # everything, then keep the port-forward open
#   E2E_KEEP=0 scripts/e2e_kind.sh         # exit after the checks
#   E2E_ATTEMPT=0 scripts/e2e_kind.sh      # deploy smoke only (no model calls)
#   E2E_TEARDOWN=1 scripts/e2e_kind.sh     # delete the cluster when done (implies E2E_KEEP=0)
#   E2E_SKIP_BUILD=1 scripts/e2e_kind.sh   # reuse already-built images
#   E2E_DELETE=1 scripts/e2e_kind.sh       # delete the cluster and exit
#
# Credentials (never printed):
#   AWS_REGION + AWS_BEARER_TOKEN_BEDROCK   the attempt's model (Secret model-provider)
#   LINEAR_API_KEY, GITHUB_APP_ID/GITHUB_APP_INSTALLATION_ID/GITHUB_APP_PRIVATE_KEY_FILE,
#   SLACK_BOT_TOKEN + SLACK_CHANNEL | SLACK_WEBHOOK_URL   optional; empty otherwise
#   DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID | DISCORD_WEBHOOK_URL   optional; empty otherwise
#   E2E_MERGER_REPO=owner/name   scales the merger up (needs the real GitHub App)
# E2E_PROVIDER / E2E_MODEL override amazon-bedrock / global.anthropic.claude-opus-5-5.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${E2E_CLUSTER:-symphony-e2e}"
NAMESPACE=symphony
PORT="${E2E_PORT:-8080}"
STATE_DIR="${E2E_STATE_DIR:-$HOME/.cache/symphony-e2e}"
CONTEXT="kind-$CLUSTER"
PROVIDER="${E2E_PROVIDER:-amazon-bedrock}"
MODEL="${E2E_MODEL:-global.anthropic.claude-opus-5-5}"
TEARDOWN="${E2E_TEARDOWN:-0}"
KEEP="${E2E_KEEP:-1}"
[ "$TEARDOWN" = "1" ] && KEEP=0
if [ -n "${E2E_ATTEMPT:-}" ]; then
  ATTEMPT="$E2E_ATTEMPT"
elif [ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]; then
  ATTEMPT=1
else
  ATTEMPT=0
fi
kc() { kubectl --context "$CONTEXT" "$@"; }
stamp() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
elapsed() { python3 -c "import sys, time; print(f'{time.time() - float(sys.argv[1]):.1f}')" "$1"; }

for tool in kind kubectl docker uv curl python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

if [ "${E2E_DELETE:-0}" = "1" ]; then
  kind delete cluster --name "$CLUSTER"
  exit 0
fi

pf=""
cleanup() {
  [ -n "$pf" ] && kill "$pf" 2>/dev/null || true
  if [ "$TEARDOWN" = "1" ]; then kind delete cluster --name "$CLUSTER" || true; fi
}
trap cleanup EXIT

# 1. cluster
t_start=$(stamp)
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config scripts/e2e/kind-cluster.yaml --wait 120s
fi
echo "timing: cluster ready in $(elapsed "$t_start")s"

# 2. images
t_images=$(stamp)
if [ "${E2E_SKIP_BUILD:-0}" != "1" ]; then
  TAG=e2e RUNTIMES=node scripts/build_images.sh
fi
kind load docker-image --name "$CLUSTER" symphony-orchestrator:e2e symphony-agent:node
echo "timing: images built and loaded in $(elapsed "$t_images")s"

# 3. namespace, secrets, bootstrap token
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
kc create namespace "$NAMESPACE" --dry-run=client -o yaml | kc apply -f -

TOKEN_FILE="$STATE_DIR/bootstrap-token"
if [ ! -s "$TOKEN_FILE" ]; then
  python3 -c "import secrets; print(f'sym_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}')" >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"
random() { python3 -c "import secrets; print(secrets.token_hex(16))"; }

# Replace (or create) — never `kubectl apply`: deploy/integrations.yaml ships these Secrets
# empty, and an applied value would be erased by the next `kubectl apply -k`. The manifest
# stays in a shell variable (printf is a builtin): values never reach argv or the terminal.
apply_secret() { # name, then --from-literal/--from-file args
  local name="$1" manifest
  shift
  manifest="$(kc -n "$NAMESPACE" create secret generic "$name" "$@" --dry-run=client -o yaml)"
  printf '%s\n' "$manifest" | kc -n "$NAMESPACE" replace -f - >/dev/null 2>&1 ||
    printf '%s\n' "$manifest" | kc -n "$NAMESPACE" create -f - >/dev/null
  echo "secret/$name set"
}

# Without GITHUB_APP_ID the memory-tracker runner skips the code-host credential.
github_key=(--from-literal "GITHUB_APP_PRIVATE_KEY=")
[ -n "${GITHUB_APP_PRIVATE_KEY_FILE:-}" ] && github_key=(--from-file "GITHUB_APP_PRIVATE_KEY=$GITHUB_APP_PRIVATE_KEY_FILE")
apply_secret linear-api-key \
  --from-literal "LINEAR_API_KEY=${LINEAR_API_KEY:-}" \
  --from-literal "LINEAR_WEBHOOK_SECRET=$(random)"
apply_secret github-app \
  --from-literal "GITHUB_APP_ID=${GITHUB_APP_ID:-}" \
  --from-literal "GITHUB_APP_INSTALLATION_ID=${GITHUB_APP_INSTALLATION_ID:-}" \
  "${github_key[@]}"
apply_secret symphony-api \
  --from-literal "GITHUB_WEBHOOK_SECRET=$(random)" \
  --from-literal "SYMPHONY_PREVIEW_LINK_SECRET=$(random)" \
  --from-literal "SYMPHONY_BOOTSTRAP_TOKEN=$TOKEN"
if [ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]; then
  apply_secret model-provider \
    --from-literal "AWS_REGION=${AWS_REGION:-us-east-1}" \
    --from-literal "AWS_BEARER_TOKEN_BEDROCK=$AWS_BEARER_TOKEN_BEDROCK"
fi
if [ -n "${SLACK_BOT_TOKEN:-}${SLACK_WEBHOOK_URL:-}" ]; then
  apply_secret symphony-slack \
    --from-literal "SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-}" \
    --from-literal "SLACK_CHANNEL=${SLACK_CHANNEL:-}" \
    --from-literal "SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL:-}"
fi
if [ -n "${DISCORD_BOT_TOKEN:-}${DISCORD_WEBHOOK_URL:-}" ]; then
  apply_secret symphony-discord \
    --from-literal "DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}" \
    --from-literal "DISCORD_CHANNEL_ID=${DISCORD_CHANNEL_ID:-}" \
    --from-literal "DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL:-}"
fi

# 4. the certification WORKFLOW.md, with an empty board
E2E_WORKFLOW="$STATE_DIR/WORKFLOW.md"
uv run --quiet python scripts/e2e/certify.py render --issues none --out "$E2E_WORKFLOW" \
  --provider "$PROVIDER" --model "$MODEL"
KUBE_CONTEXT="$CONTEXT" scripts/sync_workflow.sh "$E2E_WORKFLOW" "$NAMESPACE"

# 5. deploy
t_deploy=$(stamp)
kc apply -k scripts/e2e
if [ -n "${E2E_MERGER_REPO:-}" ]; then
  kc -n "$NAMESPACE" set env deployment/symphony-merger "SYMPHONY_MERGER_REPO=$E2E_MERGER_REPO"
  kc -n "$NAMESPACE" scale deployment/symphony-merger --replicas 1
fi
deployments=(symphony-redis symphony-api symphony-gateway symphony-daemon)
[ -n "${E2E_MERGER_REPO:-}" ] && deployments+=(symphony-merger)
for deployment in "${deployments[@]}"; do
  if ! kc -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout 240s; then
    kc -n "$NAMESPACE" describe "deployment/$deployment" | tail -20 >&2
    kc -n "$NAMESPACE" logs "deployment/$deployment" --tail 50 >&2 || true
    exit 1
  fi
done
echo "timing: deploy applied to rollouts complete in $(elapsed "$t_deploy")s"

# 6. port-forward + checks
kc -n "$NAMESPACE" port-forward svc/symphony-api "$PORT:8080" >"$STATE_DIR/port-forward.log" 2>&1 &
pf=$!
until curl -fsS "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1; do
  kill -0 "$pf" 2>/dev/null || { cat "$STATE_DIR/port-forward.log" >&2; exit 1; }
  sleep 0.2
done
me="$(curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/v1/me")"
echo "GET /v1/me -> $me"

# 7. the real attempt
if [ "$ATTEMPT" = "1" ]; then
  uv run --quiet python scripts/e2e/certify.py run --context "$CONTEXT" \
    --url "http://127.0.0.1:$PORT" --token-file "$TOKEN_FILE" \
    --workflow-out "$E2E_WORKFLOW" --summary-out "$STATE_DIR/certification.json" \
    --provider "$PROVIDER" --model "$MODEL"
  echo "certification summary: $STATE_DIR/certification.json"
fi

cat <<EOF

Symphony no kind: http://127.0.0.1:$PORT
  token admin (bootstrap): $TOKEN_FILE
  export SYMPHONY_URL=http://127.0.0.1:$PORT SYMPHONY_TOKEN=\$(cat $TOKEN_FILE)
  ou: symphony login --url http://127.0.0.1:$PORT
EOF

if [ "$KEEP" = "1" ]; then
  echo "port-forward ativo (Ctrl-C encerra; E2E_DELETE=1 $0 apaga o cluster)"
  wait "$pf"
fi
