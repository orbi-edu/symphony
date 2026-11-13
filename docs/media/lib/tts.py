# Speaks narration lines with Kokoro-82M (ONNX) and writes one 24 kHz mono 16-bit WAV per line.
# Run through uv with the pinned packages that render.sh lists, for example:
#   uv run --no-project --python 3.12 --with kokoro-onnx==0.6.1 ... python tts.py MODEL VOICES JOBS.json
# JOBS.json: [{"text": "...", "voice": "af_heart", "speed": 1.0, "out": "/path/line.wav"}, ...]
import json
import sys

import soundfile as sf
from kokoro_onnx import Kokoro


def main() -> None:
    model, voices, jobs_path = sys.argv[1:4]
    kokoro = Kokoro(model, voices)
    jobs = json.load(open(jobs_path, encoding="utf-8"))
    for job in jobs:
        samples, rate = kokoro.create(job["text"], voice=job["voice"], speed=job.get("speed", 1.0), lang="en-us")
        if rate != 24000:
            raise SystemExit(f"unexpected sample rate {rate}")
        sf.write(job["out"], samples, rate, subtype="PCM_16")
    print(f"tts: {len(jobs)} lines")


if __name__ == "__main__":
    main()
