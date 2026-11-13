#!/bin/sh
# Bake the pinned omp model catalog (`models.json` of the npm package @oh-my-pi/pi-catalog,
# the same database the engine bundles) and its compiled provider rules (`src/compat/rules.json`:
# which env var holds each provider's API key, which providers only log in by OAuth) for the
# API's /v1/admin/catalog. The tarball is verified against a checksum pinned HERE.
#
#   install-omp-catalog.sh <version> <dest-dir>   # version = src/symphony/edge.py OMP_VERSION
#
# Bumping the engine: add the new version's sha256 of
# https://registry.npmjs.org/@oh-my-pi/pi-catalog/-/pi-catalog-<version>.tgz below
# (tests/test_deploy.py fails until it exists).
set -eu

version="${1:?usage: install-omp-catalog.sh <version> <dest-dir>}"
dest="${2:?usage: install-omp-catalog.sh <version> <dest-dir>}"

case "$version" in
  18.20.2) sha256=7d3395377cb1a43e9a814d405faa656cb5b46451f3e6ba271b685adcd2c73bc5 ;;
  *) echo "no pinned checksum for pi-catalog ${version}: add it to docker/install-omp-catalog.sh" >&2; exit 1 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp/catalog.tgz" \
  "https://registry.npmjs.org/@oh-my-pi/pi-catalog/-/pi-catalog-${version}.tgz"
echo "${sha256}  $tmp/catalog.tgz" | sha256sum -c -
tar -xzf "$tmp/catalog.tgz" -C "$tmp" package/src/models.json package/src/compat/rules.json \
  package/LICENSE package/package.json
installed="$(sed -n 's/^[[:space:]]*"version": "\([^"]*\)",*$/\1/p' "$tmp/package/package.json" | head -n 1)"
[ "$installed" = "$version" ] || {
  echo "pi-catalog tarball reports '${installed}', expected ${version}" >&2
  exit 1
}
mkdir -p "$dest"
install -m 644 "$tmp/package/src/models.json" "$dest/models.json"
install -m 644 "$tmp/package/src/compat/rules.json" "$dest/rules.json"
install -m 644 "$tmp/package/LICENSE" "$dest/LICENSE"
echo "pi-catalog ${version} -> $dest/models.json, $dest/rules.json"
