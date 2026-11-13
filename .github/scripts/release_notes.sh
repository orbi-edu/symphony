#!/usr/bin/env bash
# Print the CHANGELOG.md section of one version, without its heading: the GitHub Release
# notes (.github/workflows/release.yml). Fails when the section is missing or empty.
#
#   .github/scripts/release_notes.sh 0.1.0
set -euo pipefail
cd "$(dirname "$0")/../.."

version="${1:?usage: release_notes.sh <version>}"

notes=$(awk -v heading="## [$version]" '
  index($0, "## [") == 1 { inside = (index($0, heading) == 1); next }
  /^\[[^]]+\]: / { next }
  inside { print }
' CHANGELOG.md | sed -e '/./,$!d')

if [ -z "$notes" ]; then
  echo "CHANGELOG.md has no notes for $version" >&2
  exit 1
fi
printf '%s\n' "$notes"
