/** Render the fictional Butler Voice README scene with deterministic frames. */
import { chromium } from "@playwright/test";
import { existsSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const source = resolve(root, "docs/design/butler-voice-readme-demo.html");
const output = resolve(root, "docs/images/butler-voice-sample.gif");
const still = resolve(root, "docs/images/butler-voice-sample.png");
const ffmpeg = process.env.NEBULA_FFMPEG
  ?? (existsSync("/usr/bin/ffmpeg") ? "/usr/bin/ffmpeg" : "ffmpeg");
const frameRate = 8;
const durationSeconds = 7;
const frames = mkdtempSync(resolve(tmpdir(), "nebula-butler-voice-"));
const palette = resolve(frames, "palette.png");

function runFfmpeg(args) {
  const result = spawnSync(ffmpeg, args, { encoding: "utf8" });
  if (result.error || result.status !== 0) {
    throw new Error(result.error?.message ?? result.stderr ?? "ffmpeg failed");
  }
}

let browser;
try {
  mkdirSync(dirname(output), { recursive: true });
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: 1200, height: 675 },
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(pathToFileURL(source).href);
  for (let index = 0; index < durationSeconds * frameRate; index += 1) {
    const seconds = index / frameRate;
    await page.evaluate((time) => window.renderFrame(time), seconds);
    await page.screenshot({ path: resolve(frames, `frame-${String(index).padStart(3, "0")}.png`) });
    if (index === 50) await page.screenshot({ path: still });
  }
  if (errors.length) throw new Error(`Scene failed: ${errors.join("; ")}`);
  await browser.close();
  browser = undefined;

  const input = resolve(frames, "frame-%03d.png");
  runFfmpeg(["-hide_banner", "-loglevel", "error", "-y", "-framerate", String(frameRate), "-i", input,
    "-vf", "scale=850:-1:flags=lanczos,palettegen=max_colors=128:stats_mode=diff", palette]);
  runFfmpeg(["-hide_banner", "-loglevel", "error", "-y", "-framerate", String(frameRate), "-i", input,
    "-i", palette, "-lavfi", "scale=850:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle",
    "-loop", "0", output]);
  process.stdout.write(`${output}\n${still}\n`);
} finally {
  await browser?.close();
  rmSync(frames, { recursive: true, force: true });
}
