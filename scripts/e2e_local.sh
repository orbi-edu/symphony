#!/usr/bin/env bash
# Local-mode certification (docs/install.md, local mode): Redis + `symphony.entry.local` +
# the real omp on Amazon Bedrock + a headless Tern driving the ACP chat. See
# scripts/e2e_local/run.py for the steps.
#
#   scripts/e2e_local.sh                  # screenshots and logs stay in /tmp/sym-e2e-local-*
#   scripts/e2e_local.sh --out DIR        # screenshots in DIR; the temp dir goes on success
#   scripts/e2e_local.sh --out DIR --keep
#
# Needs: uv, redis-server, git, tern, AWS credentials in the environment (or whatever the
# chosen provider needs), and omp (OMP_BIN, default the Tern-managed one).
# E2E_PROVIDER / E2E_MODEL override amazon-bedrock / global.anthropic.claude-opus-5-5.
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in uv redis-server git tern; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

exec uv run python scripts/e2e_local/run.py "$@"
