import { access, readdir, rename, rm, unlink } from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import process from "node:process";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const videoDirectory = path.resolve(scriptDirectory, "../usage-videos");

function run(command, args, stdio = "inherit") {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} exited with status ${code}`));
    });
  });
}

async function findFfmpeg() {
  const candidates = [
    process.env.NEBULA_FFMPEG_BIN,
    process.platform === "win32" ? undefined : "/usr/bin/ffmpeg",
    "ffmpeg",
  ].filter(Boolean);

  for (const candidate of [...new Set(candidates)]) {
    try {
      if (path.isAbsolute(candidate)) await access(candidate, constants.X_OK);
      await run(candidate, ["-version"], "ignore");
      return candidate;
    } catch {
      // Try the next configured or platform-provided encoder.
    }
  }
  throw new Error(
    "A working ffmpeg with H.264 support is required. Set NEBULA_FFMPEG_BIN to its executable path.",
  );
}

await access(videoDirectory, constants.R_OK | constants.W_OK);
const inputs = (await readdir(videoDirectory))
  .filter((name) => name.endsWith(".webm"))
  .sort();

if (!inputs.length) {
  console.log("No usage WebMs need conversion.");
  process.exit(0);
}

const ffmpeg = await findFfmpeg();
const conversions = inputs.map((input) => {
  const stem = input.slice(0, -".webm".length);
  return {
    input: path.join(videoDirectory, input),
    output: path.join(videoDirectory, `${stem}.mp4`),
    temporary: path.join(videoDirectory, `.${stem}.converting.mp4`),
  };
});

try {
  for (const conversion of conversions) {
    await run(ffmpeg, [
      "-hide_banner", "-loglevel", "error", "-y",
      "-i", conversion.input,
      "-map", "0:v:0",
      "-c:v", "libx264",
      "-preset", "medium",
      "-crf", "20",
      "-pix_fmt", "yuv420p",
      "-movflags", "+faststart",
      "-an",
      conversion.temporary,
    ]);
  }

  for (const conversion of conversions) {
    await rm(conversion.output, { force: true });
    await rename(conversion.temporary, conversion.output);
    await unlink(conversion.input);
    console.log(`Created ${path.basename(conversion.output)}`);
  }
} catch (error) {
  await Promise.all(conversions.map(({ temporary }) => rm(temporary, { force: true })));
  throw error;
}
