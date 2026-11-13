#!/usr/bin/env bash
# Rebuilds the testing explainer:
#   docs/media/symphony-testing.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-testing.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/testing/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-testing}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-testing.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-testing.mp4" "Symphony: how it is proven"
media_report "$MEDIA/symphony-testing.mp4" "$MEDIA/symphony-testing.vtt"
