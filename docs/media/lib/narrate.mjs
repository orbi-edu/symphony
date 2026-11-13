// Narration, timing, audio track and WebVTT captions, shared by every video in docs/media.
// Usage: node narrate.mjs <project dir> <work dir> <models dir> <vtt output>
// Reads <project dir>/script.json and the Kokoro files in <models dir> (lib/common.sh downloads them).
// Writes <work>/timing.json and <work>/narration.wav (24 kHz mono; common.sh normalizes the loudness).
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const [project, work, models, vttOut] = process.argv.slice(2);
if (!project || !work || !models || !vttOut) throw new Error("usage: node narrate.mjs <project dir> <work dir> <models dir> <vtt output>");

const script = JSON.parse(readFileSync(join(project, "script.json"), "utf8"));
const RATE = 24000; // Hz, mono, 16-bit PCM (the Kokoro output rate)
const MODEL = join(models, "kokoro-v1.0.onnx");
const VOICES = join(models, "voices-v1.0.bin");
// The Python side of the speech, pinned. uv makes a throwaway environment; nothing enters the project.
const PYTHON = "3.12";
const PY_DEPS = ["kokoro-onnx==0.6.1", "onnxruntime==1.32.0", "numpy==2.5.5", "soundfile==0.14.0", "espeakng-loader==0.2.4"];
const voiceDir = join(work, "voice");
mkdirSync(voiceDir, { recursive: true });

function pcmOf(wavPath) {
  const buf = readFileSync(wavPath);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${wavPath}: not a WAVE file`);
  }
  let off = 12;
  let fmt = null;
  while (off + 8 <= buf.length) {
    const id = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    if (id === "fmt ") {
      fmt = { channels: buf.readUInt16LE(off + 10), rate: buf.readUInt32LE(off + 12), bits: buf.readUInt16LE(off + 22) };
    } else if (id === "data") {
      if (!fmt || fmt.channels !== 1 || fmt.rate !== RATE || fmt.bits !== 16) {
        throw new Error(`${wavPath}: expected mono 16-bit ${RATE} Hz, got ${JSON.stringify(fmt)}`);
      }
      return buf.subarray(off + 8, off + 8 + size);
    }
    off += 8 + size + (size % 2);
  }
  throw new Error(`${wavPath}: no data chunk`);
}

// Trim leading and trailing near-silence so the gaps in the timeline are exact.
function trim(pcm) {
  const n = pcm.length / 2;
  const loud = (i) => Math.abs(pcm.readInt16LE(i * 2)) > 300;
  let a = 0;
  while (a < n && !loud(a)) a++;
  let b = n - 1;
  while (b > a && !loud(b)) b--;
  const pad = Math.round(RATE * 0.04);
  return pcm.subarray(Math.max(0, a - pad) * 2, Math.min(n, b + pad) * 2);
}

const wavFor = (text) =>
  join(voiceDir, `${createHash("sha256").update(`kokoro-v1.0|${script.voice}|${script.speed}|${text}`).digest("hex").slice(0, 16)}.wav`);

// Speak every line that is not in the cache, in one Python run.
function synthesize() {
  const texts = script.scenes.flatMap((s) => s.cues.map((c) => c.say ?? c.text));
  const jobs = [...new Set(texts)].filter((t) => !existsSync(wavFor(t))).map((text) => ({ text, voice: script.voice, speed: script.speed, out: wavFor(text) }));
  if (!jobs.length) return;
  const jobsPath = join(work, "tts-jobs.json");
  writeFileSync(jobsPath, JSON.stringify(jobs));
  execFileSync("uv", ["run", "--no-project", "--python", PYTHON, ...PY_DEPS.flatMap((d) => ["--with", d]), "python", join(here, "tts.py"), MODEL, VOICES, jobsPath], { stdio: "inherit" });
}

function speak(text) {
  return trim(pcmOf(wavFor(text)));
}

synthesize();

const P = script.pads;
let t = P.intro;
const pieces = [];
const scenes = script.scenes.map((scene, si) => {
  const start = t;
  t += P.sceneLead;
  const cues = scene.cues.map((cue, ci) => {
    const pcm = speak(cue.say ?? cue.text);
    const dur = pcm.length / 2 / RATE;
    const c = { id: `${scene.id}.${ci + 1}`, text: cue.text, start: +t.toFixed(3), end: +(t + dur).toFixed(3) };
    pieces.push({ at: t, pcm });
    t += dur + P.cueGap + (cue.after ?? 0); // "after": extra seconds of picture after this cue
    return c;
  });
  t += P.sceneTail - P.cueGap;
  if (si === script.scenes.length - 1) t += P.outro;
  return { id: scene.id, kicker: scene.kicker, title: scene.title, index: si + 1, start: +start.toFixed(3), end: +t.toFixed(3), cues };
});
const duration = Math.ceil(t * 30) / 30;

// Mix the cues into one track.
const total = Math.round(duration * RATE);
const out = Buffer.alloc(44 + total * 2);
for (const { at, pcm } of pieces) {
  const o = Math.round(at * RATE);
  for (let i = 0; i < pcm.length / 2 && o + i < total; i++) out.writeInt16LE(pcm.readInt16LE(i * 2), 44 + (o + i) * 2);
}
out.write("RIFF", 0, "ascii");
out.writeUInt32LE(36 + total * 2, 4);
out.write("WAVEfmt ", 8, "ascii");
out.writeUInt32LE(16, 16);
out.writeUInt16LE(1, 20);
out.writeUInt16LE(1, 22);
out.writeUInt32LE(RATE, 24);
out.writeUInt32LE(RATE * 2, 28);
out.writeUInt16LE(2, 32);
out.writeUInt16LE(16, 34);
out.write("data", 36, "ascii");
out.writeUInt32LE(total * 2, 40);
writeFileSync(join(work, "narration.wav"), out);

writeFileSync(join(work, "timing.json"), JSON.stringify({ duration, scenes }, null, 2));

// Captions stay on screen until the next cue starts (or 0.6 s after the last word of a scene).
const stamp = (s) => {
  const ms = Math.round(s * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const sec = Math.floor((ms % 60000) / 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}.${String(ms % 1000).padStart(3, "0")}`;
};
const lines = ["WEBVTT", ""];
for (const scene of scenes) {
  scene.cues.forEach((c, i) => {
    const next = scene.cues[i + 1];
    const end = next ? next.start : Math.min(c.end + 0.6, scene.end);
    lines.push(c.id, `${stamp(c.start)} --> ${stamp(end)}`, c.text, "");
  });
}
writeFileSync(vttOut, lines.join("\n"));

console.log(JSON.stringify({ duration, scenes: scenes.map((s) => [s.id, s.start, s.end]) }));
