#!/usr/bin/env bash
# Rebuilds the assistant video:
#   docs/media/symphony-assistant.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-assistant.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/assistant/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-assistant}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-assistant.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-assistant.mp4" "Symphony: the assistant in Slack and Discord"
media_report "$MEDIA/symphony-assistant.mp4" "$MEDIA/symphony-assistant.vtt"
