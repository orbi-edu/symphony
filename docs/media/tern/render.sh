#!/usr/bin/env bash
# Rebuilds the Tern tour:
#   docs/media/symphony-tern.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-tern.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/tern/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-tern}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-tern.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-tern.mp4" "Operating Symphony from Tern"
media_report "$MEDIA/symphony-tern.mp4" "$MEDIA/symphony-tern.vtt"
