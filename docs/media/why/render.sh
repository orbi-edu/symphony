#!/usr/bin/env bash
# Rebuilds "Why use Symphony?":
#   docs/media/symphony-why.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-why.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/why/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-why}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-why.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-why.mp4" "Why use Symphony?"
media_report "$MEDIA/symphony-why.mp4" "$MEDIA/symphony-why.vtt"
