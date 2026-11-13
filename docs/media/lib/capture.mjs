// Captures a project's animation frame by frame with headless Chromium.
// node capture.mjs --project DIR --mode video|teaser --work DIR --node DIR [--workers N] [--stills t1,t2,... --out DIR]
// --node is the directory where lib/package.json was installed (playwright-core).
import { mkdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const mode = args.mode || "video";
const work = args.work;
if (!work || !args.project || !args.node) throw new Error("--project, --work and --node are required");
// playwright-core lives in the throwaway build directory, not in the repository.
const require = createRequire(join(args.node, "package.json"));
const { chromium } = require("playwright-core");

const fps = mode === "teaser" ? 15 : 30;
const scale = mode === "teaser" ? 0.5 : 1; // the teaser is captured at 960 x 540
const timing = mode === "video" ? JSON.parse(readFileSync(join(work, "timing.json"), "utf8")) : null;
const url = pathToFileURL(join(resolve(args.project), "index.html")).href;

const browser = await chromium.launch({ args: ["--force-color-profile=srgb", "--disable-lcd-text", "--font-render-hinting=none"] });

async function openPage() {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: scale });
  page.on("pageerror", (e) => { console.error(`page error: ${e.message}`); process.exit(1); });
  await page.addInitScript(([t, m]) => { window.TIMING = t; window.MODE = m; }, [timing, mode]);
  await page.goto(url);
  const info = await page.evaluate(() => window.READY);
  return { page, info };
}

if (args.stills) {
  const out = args.out;
  mkdirSync(out, { recursive: true });
  const { page } = await openPage();
  for (const t of args.stills.split(",").map(Number)) {
    await page.evaluate((x) => Engine.seek(x), t);
    await page.screenshot({ path: join(out, `t${t.toFixed(2).padStart(7, "0")}.png`) });
  }
  await browser.close();
  process.exit(0);
}

const dir = join(work, `${mode}-frames`);
mkdirSync(dir, { recursive: true });
const probe = await openPage();
const total = Math.round(probe.info.duration * fps);
await probe.page.close();
const workers = Math.max(1, Number(args.workers || 4));
const per = Math.ceil(total / workers);
const ext = mode === "teaser" ? "png" : "jpeg";
let done = 0;
const started = Date.now();

await Promise.all(Array.from({ length: workers }, async (_, w) => {
  const { page } = await openPage();
  for (let i = w * per; i < Math.min(total, (w + 1) * per); i++) {
    await page.evaluate((x) => Engine.seek(x), i / fps);
    const path = join(dir, `${String(i).padStart(5, "0")}.${ext === "jpeg" ? "jpg" : "png"}`);
    await page.screenshot(ext === "jpeg" ? { path, type: "jpeg", quality: 93 } : { path, type: "png" });
    if (++done % 300 === 0) console.log(`${mode}: ${done}/${total} frames, ${((Date.now() - started) / 1000).toFixed(0)} s`);
  }
  await page.close();
}));
await browser.close();
console.log(JSON.stringify({ mode, frames: total, fps, dir }));
