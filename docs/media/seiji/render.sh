#!/usr/bin/env bash
# Rebuilds "Symphony and Seiji":
#   docs/media/symphony-seiji.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-seiji.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/why/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-seiji}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-seiji.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-seiji.mp4" "Symphony and Seiji"
media_report "$MEDIA/symphony-seiji.mp4" "$MEDIA/symphony-seiji.vtt"
