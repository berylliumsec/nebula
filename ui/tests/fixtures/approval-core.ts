import {spawn, spawnSync, type ChildProcessWithoutNullStreams} from "node:child_process";
import {mkdtemp, readFile} from "node:fs/promises";
import {createServer} from "node:http";
import type {AddressInfo} from "node:net";
import {tmpdir} from "node:os";
import path from "node:path";
import {expect, request} from "@playwright/test";

/** Fixed inert peer only. Restart preserves this test's DB and receipt log. */
export async function startApprovalCore(host: string, scenario: string) {
  const repository = path.resolve(import.meta.dirname, "../../..");
  const common = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], {encoding: "utf8"}).stdout.trim();
  const python = process.env.NEBULA_TEST_PYTHON ?? path.join(path.dirname(common), ".venv/bin/python");
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-stabilization-approval-failure-"));
  const reservation = createServer();
  await new Promise<void>(resolve => reservation.listen(0, "0.0.0.0", resolve));
  const port = (reservation.address() as AddressInfo).port;
  await new Promise<void>((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()));
  const origin = `http://${host}:${port}`;
  const api = await request.newContext({baseURL: `${origin}/api/v1/`, extraHTTPHeaders: {Authorization: "Bearer stabilization-fixture"}});
  let processHandle: ChildProcessWithoutNullStreams;
  let logs = "";
  const launch = async () => {
    processHandle = spawn(python, [path.join(repository, "ui/tests/fixtures/approval_core.py"), "--root", dataDir, "--static-dir", path.join(repository, "ui/dist"), "--port", String(port), "--scenario", scenario], {cwd: repository, env: {...process.env, PYTHONPATH: path.join(repository, "src")}});
    processHandle.stdout.on("data", chunk => {logs += chunk.toString();});
    processHandle.stderr.on("data", chunk => {logs += chunk.toString();});
    await expect.poll(async () => {
      if (processHandle.exitCode !== null || processHandle.signalCode !== null) throw new Error(logs);
      try {return (await api.get("health")).ok();} catch {return false;}
    }, {timeout: 30_000}).toBe(true);
  };
  const kill = async () => {
    if (processHandle?.exitCode === null && processHandle.signalCode === null) {
      const exited = new Promise<void>(resolve => processHandle.once("exit", () => resolve()));
      processHandle.kill("SIGKILL");
      await exited;
    }
  };
  try {await launch();} catch (error) {await kill(); await api.dispose(); throw error;}
  return {origin, port, dataDir, api,
    exited: () => processHandle.exitCode !== null || processHandle.signalCode !== null,
    restart: async () => {await kill(); await launch();},
    stop: async () => {await kill(); await api.dispose();},
    receipts: async (): Promise<{approval_id: string; turn_id: string; allowed: boolean}[]> => {
      try {return (await readFile(path.join(dataDir, "receipts.jsonl"), "utf8")).trim().split("\n").filter(Boolean).map(line => JSON.parse(line));}
      catch (error) {if ((error as NodeJS.ErrnoException).code === "ENOENT") return []; throw error;}
    },
  };
}
