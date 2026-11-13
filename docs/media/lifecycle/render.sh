#!/usr/bin/env bash
# Rebuilds the ticket lifecycle explainer:
#   docs/media/symphony-lifecycle.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-lifecycle.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/lifecycle/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-lifecycle}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-lifecycle.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-lifecycle.mp4" "Symphony: one ticket, end to end"
media_report "$MEDIA/symphony-lifecycle.mp4" "$MEDIA/symphony-lifecycle.vtt"
