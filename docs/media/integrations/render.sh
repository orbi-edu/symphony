#!/usr/bin/env bash
# Rebuilds the integrations explainer:
#   docs/media/symphony-integrations.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-integrations.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/integrations/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-integrations}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-integrations.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-integrations.mp4" "Symphony: trackers and notifications"
media_report "$MEDIA/symphony-integrations.mp4" "$MEDIA/symphony-integrations.vtt"
