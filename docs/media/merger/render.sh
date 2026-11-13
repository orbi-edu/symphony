#!/usr/bin/env bash
# Rebuilds the merger deep dive:
#   docs/media/symphony-merger.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-merger.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/merger/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-merger}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-merger.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-merger.mp4" "Symphony: the merger in depth"
media_report "$MEDIA/symphony-merger.mp4" "$MEDIA/symphony-merger.vtt"
