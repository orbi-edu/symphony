#!/usr/bin/env bash
# Rebuilds the install and setup video:
#   docs/media/symphony-setup.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-setup.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/setup/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-setup}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-setup.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-setup.mp4" "Symphony: install and set up"
media_report "$MEDIA/symphony-setup.mp4" "$MEDIA/symphony-setup.vtt"
