import {spawn, spawnSync, type ChildProcessWithoutNullStreams} from "node:child_process";
import {mkdtemp, rm} from "node:fs/promises";
import {createServer} from "node:http";
import type {AddressInfo} from "node:net";
import {tmpdir} from "node:os";
import path from "node:path";
import {expect, request} from "@playwright/test";

/** Real Core/API with a synthetic terminal process; no OCI runtime is used. */
export async function startTerminalCore(browserHost: string): Promise<{
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
}> {
  const repository = path.resolve(import.meta.dirname, "../../..");
  const common = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], {encoding: "utf8"}).stdout.trim();
  const python = process.env.NEBULA_TEST_PYTHON ?? path.join(path.dirname(common), ".venv/bin/python");
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-playwright-real-core-terminal-"));
  const reservation = createServer();
  await new Promise<void>(resolve => reservation.listen(0, "0.0.0.0", resolve));
  const port = (reservation.address() as AddressInfo).port;
  await new Promise<void>((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()));
  const origin = `http://${browserHost}:${port}`;
  const token = "terminal-fixture";
  const child = spawn(python, [path.join(repository, "ui/tests/fixtures/terminal_core.py"),
    "--root", dataDir, "--static-dir", path.join(repository, "ui/dist"), "--port", String(port)],
  {cwd: repository, env: {...process.env, PYTHONPATH: path.join(repository, "src")}});
  let logs = "";
  child.stdout.on("data", chunk => {logs += chunk.toString();});
  child.stderr.on("data", chunk => {logs += chunk.toString();});
  const api = await request.newContext({baseURL: `${origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${token}`}});
  try {
    await expect.poll(async () => {
      if (child.exitCode !== null || child.signalCode !== null) throw new Error(logs);
      try {return (await api.get("health")).ok();} catch {return false;}
    }, {timeout: 45_000}).toBe(true);
  } catch (error) {
    child.kill("SIGKILL");
    await rm(dataDir, {recursive: true, force: true});
    throw error;
  } finally {
    await api.dispose();
  }
  return {process: child, dataDir, origin, token};
}
