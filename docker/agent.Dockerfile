# syntax=docker/dockerfile:1
# Agent (workspace pod) image: where `omp --mode rpc` edits, builds and tests the target
# repository. The runner execs into it through the Kubernetes API; it holds no Symphony
# credential (only the derived, per-attempt ones the runner stages).
#
# Variants via BASE_IMAGE (scripts/build_images.sh builds them):
#   node:     debian:bookworm-slim               -> symphony-agent:node
#   dotnet9:  mcr.microsoft.com/dotnet/sdk:9.0   -> symphony-agent:dotnet9
#   dotnet10: mcr.microsoft.com/dotnet/sdk:10.0  -> symphony-agent:dotnet10
#
# WITH_DOCKER=1 (default) installs the Docker engine for repositories whose tests need
# containers. It only starts when the pod runs privileged (kubernetes.privileged_agents)
# or under a runtime class such as sysbox; otherwise before_run reports it unavailable.
ARG BASE_IMAGE=debian:bookworm-slim
FROM ${BASE_IMAGE}

# Pin = src/symphony/edge.py OMP_VERSION (scripts/build_images.sh passes it).
ARG OMP_VERSION=18.20.2
ARG WITH_DOCKER=1

ENV DEBIAN_FRONTEND=noninteractive \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_XMLDOC_MODE=skip

RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl procps ca-certificates iptables gnupg && \
    rm -rf /var/lib/apt/lists/*

# Node for target repositories (JS/TS toolchains, hooks).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

COPY docker/install-omp.sh /usr/local/lib/symphony/install-omp.sh
RUN /usr/local/lib/symphony/install-omp.sh "${OMP_VERSION}"

RUN if [ "${WITH_DOCKER}" = "1" ]; then \
      curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && \
      sh /tmp/get-docker.sh && \
      rm -rf /tmp/get-docker.sh /var/lib/apt/lists/*; \
    fi

CMD ["sleep", "infinity"]
