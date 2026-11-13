#!/usr/bin/env bash
# Rebuilds the security video:
#   docs/media/symphony-security.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-security.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/security/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-security}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-security.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-security.mp4" "Symphony: credentials and isolation"
media_report "$MEDIA/symphony-security.mp4" "$MEDIA/symphony-security.vtt"
