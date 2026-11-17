#!/usr/bin/env bash
# Build (and optionally push) the Symphony images. The engine pin is read from
# src/symphony/edge.py (OMP_VERSION) so the images can never drift from the code.
#
#   scripts/build_images.sh                         # symphony-orchestrator:latest + agents
#   TAG=e2e RUNTIMES=node scripts/build_images.sh   # subset, custom orchestrator tag
#   REGISTRY=ghcr.io/you scripts/build_images.sh    # build + push ghcr.io/you/symphony-*
#   PLATFORM=linux/amd64 scripts/build_images.sh    # cross-build (e.g. for an x86 EC2)
#   IMAGES=orchestrator scripts/build_images.sh     # only one family (orchestrator|agent)
#   REGISTRY=ghcr.io/you VERSION=0.1.0 PLATFORM=linux/amd64,linux/arm64 \
#     scripts/build_images.sh                       # a release, as .github/workflows/images.yml
#
# Outputs:
#   ${REGISTRY}/symphony-orchestrator:${TAG}   api, gateway, daemon, merger, sweep, Jobs
#   ${REGISTRY}/symphony-agent:<runtime>       workspace pods (kubernetes.agent_images)
# With VERSION (it must equal the version in pyproject.toml), also:
#   ${REGISTRY}/symphony-orchestrator:${VERSION}
#   ${REGISTRY}/symphony-agent:<runtime>-${VERSION}
#
# More than one platform in PLATFORM builds with `docker buildx build --push`, with an SBOM
# and provenance attestation. That needs REGISTRY and a buildx builder that can push.
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-}"
PREFIX="${REGISTRY:+$REGISTRY/}"
TAG="${TAG:-latest}"
VERSION="${VERSION:-}"
RUNTIMES="${RUNTIMES:-node dotnet9 dotnet10 polyglot}"
IMAGES="${IMAGES:-orchestrator agent}"
PLATFORM="${PLATFORM:-}"
WITH_DOCKER="${WITH_DOCKER:-1}"
SOURCE_URL="${SOURCE_URL:-}"

OMP_VERSION=$(sed -n 's/^OMP_VERSION = "\(.*\)"$/\1/p' src/symphony/edge.py)
[ -n "$OMP_VERSION" ] || {
  echo "could not read OMP_VERSION from src/symphony/edge.py" >&2
  exit 1
}
echo "engine pin: omp@$OMP_VERSION"

PROJECT_VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
if [ -n "$VERSION" ] && [ "$VERSION" != "$PROJECT_VERSION" ]; then
  echo "VERSION=$VERSION does not match pyproject.toml version $PROJECT_VERSION" >&2
  exit 1
fi
COMMIT=$(git rev-parse HEAD 2>/dev/null || true)

multi=0
case "$PLATFORM" in *,*) multi=1 ;; esac
if [ "$multi" = 1 ] && [ -z "$REGISTRY" ]; then
  echo "a multi-platform build ($PLATFORM) needs REGISTRY: buildx pushes it" >&2
  exit 1
fi

platform_args=()
[ -n "$PLATFORM" ] && platform_args=(--platform "$PLATFORM")

labels=(
  --label "org.opencontainers.image.version=$PROJECT_VERSION"
  --label "org.opencontainers.image.licenses=MIT"
)
[ -n "$COMMIT" ] && labels+=(--label "org.opencontainers.image.revision=$COMMIT")
[ -n "$SOURCE_URL" ] && labels+=(--label "org.opencontainers.image.source=$SOURCE_URL")

base_for() {
  case "$1" in
    node) echo "debian:bookworm-slim" ;;
    dotnet9) echo "mcr.microsoft.com/dotnet/sdk:9.0" ;;
    dotnet10) echo "mcr.microsoft.com/dotnet/sdk:10.0" ;;
    polyglot) echo "mcr.microsoft.com/dotnet/sdk:10.0" ;;
    *) echo "unknown runtime: $1 (want node | dotnet9 | dotnet10 | polyglot)" >&2; return 1 ;;
  esac
}

# build <dockerfile> <title> "<ref> [<ref>…]" [docker build args…]
build() {
  local dockerfile="$1" title="$2" refs="$3" ref
  shift 3
  local args=(${platform_args[@]+"${platform_args[@]}"} "${labels[@]}"
    --label "org.opencontainers.image.title=$title" -f "$dockerfile" "$@")
  for ref in $refs; do args+=(-t "$ref"); done
  if [ "$multi" = 1 ]; then
    docker buildx build --push --sbom=true --provenance=mode=max "${args[@]}" .
  else
    docker build "${args[@]}" .
    if [ -n "$REGISTRY" ]; then
      for ref in $refs; do docker push "$ref"; done
    fi
  fi
  for ref in $refs; do echo "built $ref"; done
}

for family in $IMAGES; do
  case "$family" in
    orchestrator)
      refs="${PREFIX}symphony-orchestrator:${TAG}"
      [ -n "$VERSION" ] && refs="$refs ${PREFIX}symphony-orchestrator:${VERSION}"
      build docker/orchestrator.Dockerfile symphony-orchestrator "$refs" \
        --build-arg "OMP_VERSION=$OMP_VERSION" \
        --build-arg "SYMPHONY_COMMIT=$COMMIT"
      ;;
    agent)
      for runtime in $RUNTIMES; do
        base=$(base_for "$runtime")
        refs="${PREFIX}symphony-agent:${runtime}"
        [ -n "$VERSION" ] && refs="$refs ${PREFIX}symphony-agent:${runtime}-${VERSION}"
        python_flag=0
        [ "$runtime" = "polyglot" ] && python_flag=1
        build docker/agent.Dockerfile symphony-agent "$refs" \
          --build-arg "BASE_IMAGE=$base" \
          --build-arg "OMP_VERSION=$OMP_VERSION" \
          --build-arg "WITH_DOCKER=$WITH_DOCKER" \
          --build-arg "WITH_PYTHON=$python_flag"
      done
      ;;
    *)
      echo "unknown image family: $family (want orchestrator | agent)" >&2
      exit 1
      ;;
  esac
done

echo "done. Point WORKFLOW.md kubernetes.agent_images and deploy/kustomization.yaml images"
echo "at these references (or a kustomize overlay that rewrites them)."
