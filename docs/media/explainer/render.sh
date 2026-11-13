#!/usr/bin/env bash
# Rebuilds the explainer video, its captions and the README teaser:
#   docs/media/symphony-explainer.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-explainer.vtt  (the same captions as WebVTT)
#   docs/media/symphony-teaser.gif     (12 s silent loop, 800x450, 15 fps)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/explainer/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-explainer}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-explainer.vtt"
media_frames "$HERE" "$WORK" video
media_frames "$HERE" "$WORK" teaser
media_encode "$WORK" "$MEDIA/symphony-explainer.mp4" "Symphony: how it works"
media_gif "$WORK" "$MEDIA/symphony-teaser.gif"
media_report "$MEDIA/symphony-explainer.mp4" "$MEDIA/symphony-explainer.vtt" "$MEDIA/symphony-teaser.gif"
