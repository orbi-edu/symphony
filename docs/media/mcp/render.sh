#!/usr/bin/env bash
# Rebuilds the MCP servers video:
#   docs/media/symphony-mcp.mp4  (1920x1080, 30 fps, H.264 + AAC, burned-in captions, -16 LUFS)
#   docs/media/symphony-mcp.vtt  (the same captions as WebVTT)
# Voice: Kokoro af_heart (script.json). Tooling, pins and versions: ../lib/common.sh.
#
# Usage: docs/media/mcp/render.sh        (WORK=/dir CACHE=/dir WORKERS=8 to override)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
WORK="${WORK:-${TMPDIR:-/tmp}/symphony-mcp}"

media_setup
media_narrate "$HERE" "$WORK" "$MEDIA/symphony-mcp.vtt"
media_frames "$HERE" "$WORK" video
media_encode "$WORK" "$MEDIA/symphony-mcp.mp4" "Symphony: MCP servers for agents"
media_report "$MEDIA/symphony-mcp.mp4" "$MEDIA/symphony-mcp.vtt"
