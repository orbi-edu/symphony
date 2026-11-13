# Shared build steps for the videos in docs/media. A project's render.sh sources this file.
#
# Narration: Kokoro-82M (Apache-2.0) through kokoro-onnx 0.6.1, on the CPU. The model files come
# from the kokoro-onnx release "model-files-v1.1" and are checked against pinned sha256 sums.
# The Python packages are pinned in narrate.mjs; uv runs them in a throwaway environment.
# Frames: playwright-core 1.65.1 (pinned in package-lock.json) with its Chromium headless shell.
#
# Tested with: macOS 26 (arm64), ffmpeg 8.1, Node 22.18 and npm 11, uv 0.10, Python 3.12.
# Nothing here is macOS-only. On Linux the pages fall back to the installed sans-serif and monospace
# fonts, so the frames differ slightly from the published ones.
# The Node tooling and the models go to $CACHE; the frames and the audio of a project go to its $WORK.
# Neither is in the repository.

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA="$(dirname "$LIB")"
CACHE="${CACHE:-${TMPDIR:-/tmp}/symphony-media-cache}"
WORKERS="${WORKERS:-6}"

KOKORO_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
KOKORO_FILES=(
  "kokoro-v1.0.onnx beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a"
  "voices-v1.0.bin bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
)

need() { command -v "$1" >/dev/null || { echo "missing tool: $1" >&2; exit 1; }; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

# Checks the tools, installs the pinned Node tooling and downloads the checked Kokoro files.
media_setup() {
  need ffmpeg; need ffprobe; need node; need npm; need uv; need curl
  ffmpeg -version | head -1 | grep -q '^ffmpeg version 8\.1' || echo "warning: tested with ffmpeg 8.1" >&2
  node --version | grep -q '^v22\.' || echo "warning: tested with Node 22" >&2
  mkdir -p "$CACHE/node" "$CACHE/tts"
  cp "$LIB/package.json" "$LIB/package-lock.json" "$CACHE/node/"
  (cd "$CACHE/node" && npm ci --no-audit --no-fund && npx --no-install playwright-core install chromium-headless-shell)
  local entry name sum
  for entry in "${KOKORO_FILES[@]}"; do
    read -r name sum <<<"$entry"
    [ -f "$CACHE/tts/$name" ] || curl -fsSL -o "$CACHE/tts/$name" "$KOKORO_URL/$name"
    [ "$(sha256 "$CACHE/tts/$name")" = "$sum" ] || { echo "sha256 mismatch: $name" >&2; exit 1; }
  done
}

# media_narrate PROJECT WORK VTT: one Kokoro line per caption, the cue timing, the captions, and
# the audio track normalized to -16 LUFS (two-pass EBU R128, linear gain) in WORK/narration-16lufs.wav.
media_narrate() {
  local project="$1" work="$2" vtt="$3" measured
  mkdir -p "$work"
  node "$LIB/narrate.mjs" "$project" "$work" "$CACHE/tts" "$vtt"
  measured="$(ffmpeg -hide_banner -nostats -i "$work/narration.wav" -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 | sed -n '/^{/,/^}/p')"
  field() { node -e 'process.stdout.write(JSON.parse(process.argv[1])[process.argv[2]])' "$measured" "$1"; }
  ffmpeg -hide_banner -loglevel warning -y -i "$work/narration.wav" \
    -af "loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:measured_I=$(field input_i):measured_TP=$(field input_tp):measured_LRA=$(field input_lra):measured_thresh=$(field input_thresh):offset=$(field target_offset)" \
    -ar 48000 "$work/narration-16lufs.wav"
}

# media_frames PROJECT WORK MODE: headless Chromium seeks the page to each frame time and takes a screenshot.
media_frames() {
  local project="$1" work="$2" mode="$3"
  rm -rf "$work/$mode-frames"
  node "$LIB/capture.mjs" --project "$project" --mode "$mode" --work "$work" --node "$CACHE/node" --workers "$WORKERS"
}

# media_encode WORK OUT TITLE: 1080p30 H.264 + AAC, BT.709, fast start.
media_encode() {
  local work="$1" out="$2" title="$3"
  ffmpeg -hide_banner -loglevel warning -y \
    -framerate 30 -i "$work/video-frames/%05d.jpg" -i "$work/narration-16lufs.wav" \
    -map 0:v -map 1:a \
    -vf "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p" \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
    -c:v libx264 -preset slow -crf 22 -tune animation -profile:v high -level 4.1 \
    -c:a aac -b:a 128k -ar 48000 -ac 2 \
    -movflags +faststart -metadata title="$title" \
    "$out"
}

# media_gif WORK OUT: the 15 fps teaser frames as an 800 px looping GIF.
media_gif() {
  local work="$1" out="$2"
  ffmpeg -hide_banner -loglevel warning -y \
    -framerate 15 -i "$work/teaser-frames/%05d.png" \
    -vf "scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle" \
    -loop 0 "$out"
}

# media_report VIDEO FILE...: streams, duration and loudness of the video, then the file sizes.
media_report() {
  local video="$1"
  ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate,duration \
    -of compact "$video"
  ffmpeg -hide_banner -nostats -i "$video" -vn -af loudnorm=print_format=summary -f null - 2>&1 | grep -E 'Input (Integrated|True Peak)'
  ls -l "$@"
}
