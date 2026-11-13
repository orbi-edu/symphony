#!/bin/sh
# Install the official standalone `omp` release binary (Bun runtime and native tools
# included) into /usr/local/bin, verified against a checksum pinned HERE rather than one
# downloaded next to the binary. Both images run this script, so the pin lives once.
#
#   install-omp.sh <version>     # version = src/symphony/edge.py OMP_VERSION
#
# Bumping the engine: change edge.OMP_VERSION, then add the new version's lines below
# from https://github.com/can1357/oh-my-pi/releases/download/v<version>/SHA256SUMS.txt
# (tests/test_deploy.py fails until both exist).
set -eu

version="${1:?usage: install-omp.sh <version>}"

case "$(uname -m)" in
  x86_64 | amd64) arch=x64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "unsupported omp architecture: $(uname -m)" >&2; exit 1 ;;
esac

case "${version}/${arch}" in
  18.20.2/x64) sha256=f34cfa57311ed67a45f01bb1a21519cad4d74875d181316f00fa0cd6a4abcaaa ;;
  18.20.2/arm64) sha256=7f809e51638c2ace701244923d5ec62485bbafd74db75da742ace30c87a91ebf ;;
  *) echo "no pinned checksum for omp ${version} (${arch}): add it to docker/install-omp.sh" >&2; exit 1 ;;
esac

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp" \
  "https://github.com/can1357/oh-my-pi/releases/download/v${version}/omp-linux-${arch}"
echo "${sha256}  ${tmp}" | sha256sum -c -
install -m 755 "$tmp" /usr/local/bin/omp

installed="$(omp --version)"
[ "$installed" = "omp/${version}" ] || {
  echo "installed omp reports '${installed}', expected omp/${version}" >&2
  exit 1
}
echo "$installed"
