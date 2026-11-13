# syntax=docker/dockerfile:1
# Vendor mocks for the kind setup certification (scripts/e2e_kind_setup.sh). Test only: never
# deploy it next to a production install. It reuses the orchestrator image (Python, git and the
# locked dependencies), adds the `dev` extra the mocks need (graphql-core, jsonschema), and
# copies scripts/mocks with the seed repositories of the mocked certification.
#
# Build from the repository root, after scripts/build_images.sh (TAG=e2e):
#   docker build -f scripts/e2e_kind/mocks.Dockerfile -t symphony-e2e-mocks:e2e .
ARG BASE=symphony-orchestrator:e2e
FROM ${BASE}
USER root
RUN uv sync --frozen --extra dev --no-editable
COPY scripts/__init__.py ./scripts/__init__.py
COPY scripts/mocks ./scripts/mocks
COPY scripts/e2e_mocked/__init__.py ./scripts/e2e_mocked/__init__.py
COPY scripts/e2e_mocked/seed ./scripts/e2e_mocked/seed
USER 10001
