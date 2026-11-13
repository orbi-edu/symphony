# syntax=docker/dockerfile:1
# Orchestrator image: every host-side process runs from it — symphony-api,
# symphony-gateway, symphony-daemon, symphony-merger, the nightly sweep CronJob and the
# runner/reflector Jobs the daemon spawns. There is no default command: each manifest
# names its entrypoint (`python -m symphony.entry.<serve|daemon|merger|run_issue|reflect|sweep>`).
#
# Contents beyond Symphony itself:
#   omp       the pinned engine (merger agents run it here; edge.OMP_VERSION)
#   git       merger mirror/worktrees, reflector learnings PRs
#   /opt/symphony/omp-catalog/{models,rules}.json the pinned omp model catalog and provider rules (api /v1/admin/catalog)
#   /opt/symphony/WORKFLOW.default.md     the repo's WORKFLOW.md; first-run setup seeds the
#                                         empty symphony-workflow ConfigMap from it
#
# Build from the repository root: docker build -f docker/orchestrator.Dockerfile .
# (scripts/build_images.sh passes OMP_VERSION read from src/symphony/edge.py).
ARG OMP_VERSION=18.20.2

FROM ghcr.io/astral-sh/uv:0.10.7 AS uv

FROM python:3.12-slim-bookworm
ARG OMP_VERSION

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY docker/install-omp.sh docker/install-omp-catalog.sh /usr/local/lib/symphony/
RUN /usr/local/lib/symphony/install-omp.sh "${OMP_VERSION}" && \
    /usr/local/lib/symphony/install-omp-catalog.sh "${OMP_VERSION}" /opt/symphony/omp-catalog

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
# Dependencies first (cached across source edits), exactly as locked.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
COPY WORKFLOW.md /opt/symphony/WORKFLOW.default.md
ENV SYMPHONY_DEFAULT_WORKFLOW=/opt/symphony/WORKFLOW.default.md

# Refuse an image whose engine, catalog and code disagree on the pin. The pin is read
# from edge.py's source so this guard depends on nothing but the constant.
RUN python - <<'EOF'
import ast, subprocess, sys
from importlib.util import find_spec
from pathlib import Path

edge = ast.parse(Path(find_spec("symphony").submodule_search_locations[0], "edge.py").read_text())
OMP_VERSION = next(
    node.value.value for node in edge.body
    if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["OMP_VERSION"]
)

engine = subprocess.run(["omp", "--version"], capture_output=True, text=True, check=True).stdout.strip()
from symphony.api.catalog import Catalog

keyed = Catalog().provider_ids()  # decodes models.json + rules.json exactly as the API does
wanted = {"anthropic", "openai", "amazon-bedrock"}
login_only = {"openai-codex", "github-copilot", "cursor", "google-gemini-cli", "xai-oauth"}
if engine != f"omp/{OMP_VERSION}" or not wanted <= keyed or login_only & keyed:
    sys.exit(f"omp pin mismatch: edge={OMP_VERSION} engine={engine!r} "
             f"keyed={sorted(wanted & keyed)} login_only={sorted(login_only & keyed)}")
print(f"omp pin verified: {OMP_VERSION}")
EOF

# Non-root at runtime; Deployments add fsGroup so PVCs are writable.
RUN useradd --create-home --uid 10001 --user-group symphony
USER 10001
ENV HOME=/home/symphony

# The source commit (scripts/build_images.sh passes it); symphony_build_info reports it.
# Last, so a new commit rebuilds no other layer.
ARG SYMPHONY_COMMIT=
ENV SYMPHONY_COMMIT=${SYMPHONY_COMMIT}
