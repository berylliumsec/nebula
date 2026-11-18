import {spawn, spawnSync, type ChildProcessWithoutNullStreams} from "node:child_process";
import {existsSync} from "node:fs";
import {mkdir, mkdtemp, open, readdir, rm, stat} from "node:fs/promises";
import {networkInterfaces, tmpdir} from "node:os";
import path from "node:path";
import {request as unkeyedRequest} from "@playwright/test";
import {withOwnedCleanup} from "./owned-cleanup";
import {waitForCoreApi, removeCoreData} from "./core-api";
import {stopCoreWorkers, removeCoreSockets} from "./core-harness";

export interface RealCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
  logs?: () => string | Promise<string>;
}

export async function startRealCore(options: { bindHost?: string; browserHost?: string; dataDir?: string; token?: string; env?: Record<string, string>; tls?: boolean; startupTimeoutMs?: number; fixtureScript?: string } = {}): Promise<RealCore> {
  const repository = path.resolve(import.meta.dirname, "../../..");
  // Match the production-test override used by the shared real-Core harness.
  const coreCandidates = [
    process.env.NEBULA_TEST_CORE_BIN,
    process.env.NEBULA_CORE_BINARY,
    path.join(repository, ".venv/bin/nebula-core"),
  ].filter((candidate): candidate is string => Boolean(candidate));
  let coreBinary = coreCandidates.find(existsSync);
  if (!coreBinary) {
    const gitResult = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], { encoding: "utf8" });
    const commonGitDir = gitResult.status === 0 ? gitResult.stdout?.trim() : "";
    if (commonGitDir) {
      const worktreeBinary = path.join(path.dirname(commonGitDir), ".venv/bin/nebula-core");
      coreCandidates.push(worktreeBinary);
      if (existsSync(worktreeBinary)) coreBinary = worktreeBinary;
    }
  }
  if (!coreBinary) throw new Error(`No nebula-core test binary was found in: ${coreCandidates.join(", ")}`);
  const dataDir = options.dataDir ?? await mkdtemp(path.join(tmpdir(), "nebula-playwright-real-core-"));
  let token = options.token ?? "playwright-real-core-token-2026-fixture";
  const bindHost = options.bindHost ?? "127.0.0.1";
  const browserHost = options.browserHost ?? bindHost;
  let tlsCert: string | undefined;
  let tlsKey: string | undefined;
  if (options.tls) {
    await mkdir(dataDir, {recursive: true});
    tlsCert = path.join(dataDir, "test-lan-cert.pem");
    tlsKey = path.join(dataDir, "test-lan-key.pem");
    const generated = spawnSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
      "-keyout", tlsKey, "-out", tlsCert, "-subj", "/CN=Nebula test LAN",
      "-addext", `subjectAltName=IP:${browserHost},IP:127.0.0.1`], {encoding: "utf8"});
    if (generated.status !== 0) {
      if (!options.dataDir) await rm(dataDir, {recursive: true, force: true});
      throw new Error(`Could not create a temporary LAN HTTPS certificate: ${generated.stderr || generated.error || generated.status}`);
    }
  }
  const embedded = process.env.NEBULA_TEST_CORE_EMBEDDED_UI === "1";
  const coreEnvironment = {
    ...process.env,
    ...options.env,
    PYTHONUNBUFFERED: "1",
    ...(embedded ? {NEBULA_V3_UI_DIR: ""} : {}),
    PYTHONPATH: embedded ? "" : [path.join(repository, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  // A database a journey seeded before Core's first start is an existing root to Core, which starts on
  // it only after the operator's coordinated adoption. Adoption also saves this fixture's token.
  if (!options.fixtureScript && !embedded && existsSync(path.join(dataDir, "nebula.db")) && !existsSync(path.join(dataDir, "runtime"))) {
    const adopted = spawnSync(coreBinary, ["runtime-adopt", "--confirm-core-stopped", "--data-dir", dataDir],
      {cwd: repository, env: {...coreEnvironment, NEBULA_V3_API_TOKEN: token}, encoding: "utf8"});
    if (adopted.status !== 0) throw new Error(`Seeded Core data could not be adopted: ${adopted.stderr || adopted.stdout || adopted.error}`);
  }
  const child = spawn(
    options.fixtureScript ? path.join(path.dirname(coreBinary), "python") : coreBinary,
    [...(options.fixtureScript ? [options.fixtureScript] : []), ...(embedded ? [
      // `serve` is intentionally API-only unless a static directory is given.
      // The packaged product's `ui` entry point resolves its embedded assets.
      "ui", "--no-browser", "--host", bindHost, "--port", "0",
      "--data-dir", dataDir,
      ...(bindHost === "127.0.0.1" ? [] : ["--lan", "--allow-insecure-lan"]),
    ] : [
      "serve",
      "--host", bindHost,
      "--port", "0",
      "--token", token,
      "--allow-insecure-device-pairing",
      "--allow-browser-diagnostics",
      ...(bindHost === "127.0.0.1" ? [] : ["--allow-remote"]),
      ...(tlsCert && tlsKey ? ["--tls-cert", tlsCert, "--tls-key", tlsKey] : []),
      "--data-dir", dataDir,
      "--static-dir", path.join(repository, "ui/dist"),
    ])],
    {cwd: repository, env: coreEnvironment},
  );
  let output = "";
  // A slow host can raise every Core startup wait at once, as the approval fixture already allows.
  const startupTimeout = Math.max(options.startupTimeoutMs ?? 30_000, Number(process.env.NEBULA_TEST_CORE_STARTUP_TIMEOUT_MS) || 0);
  const origin = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Real Core did not become ready.\n${output}`)), startupTimeout);
    const inspect = (chunk: Buffer) => {
      output += chunk.toString("utf8");
      const match = output.match(/"url"\s*:\s*"(https?):\/\/[^:\"]+:(\d+)"/);
      const generatedToken = output.match(/"token"\s*:\s*"([^"]+)"/);
      if (match && (!embedded || generatedToken)) {
        if (embedded) token = generatedToken![1];
        clearTimeout(timeout);
        resolve(`${match[1]}://${browserHost}:${match[2]}`);
      }
    };
    child.stdout.on("data", inspect);
    child.stderr.on("data", (chunk) => {
      output += chunk.toString("utf8");
      if (process.env.NEBULA_TEST_CORE_LOG === "1") process.stderr.write(chunk);
    });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Real Core exited with ${code}.\n${output}`));
    });
  }).catch(error => withOwnedCleanup(async () => {throw error;}, [
    {name: "failed Core startup", dispose: () => stopRealCore({process: child, dataDir, origin: "", token}, {keepData: Boolean(options.dataDir)})},
  ]));
  const core = { process: child, dataDir, origin, token, logs: async () => {
    // Detached worker failures do not appear on the supervisor's stdout.
    // Read bounded tails from the three newest worker logs only on failure.
    const runtime = path.join(dataDir, "runtime");
    const logs = await Promise.all((await readdir(runtime).catch(() => [] as string[])).filter(name => name.endsWith(".log"))
      .map(async name => ({name, modified: await stat(path.join(runtime, name)).then(value => value.mtimeMs, () => 0)})));
    const sections = [output.slice(-16_000)];
    for (const {name} of logs.sort((a, b) => b.modified - a.modified).slice(0, 3)) {
      const file = await open(path.join(runtime, name), "r").catch(() => undefined);
      if (!file) continue;
      try {
        const size = (await file.stat()).size;
        const buffer = Buffer.alloc(Math.min(size, 16_000));
        const {bytesRead} = await file.read(buffer, 0, buffer.length, Math.max(0, size - buffer.length));
        sections.push(`${name}\n${buffer.subarray(0, bytesRead).toString("utf8")}`);
      } finally { await file.close(); }
    }
    return sections.join("\n\n").replaceAll(token, "[redacted]");
  } };
  // Core prints its address before its API process is admitted; a read sent before that is a reconnecting 503.
  const probe = await unkeyedRequest.newContext({baseURL: `${origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${token}`}, ignoreHTTPSErrors: true});
  await withOwnedCleanup(() => waitForCoreApi(probe, startupTimeout), [
    {name: "Core readiness probe", dispose: () => probe.dispose()},
  ]).catch(error => withOwnedCleanup(async () => {throw new Error(`Real Core did not answer an API read.\n${output}`, {cause: error});}, [
    {name: "Core that never became ready", dispose: () => stopRealCore(core, {keepData: Boolean(options.dataDir)})},
  ]));
  return core;
}

export async function stopRealCore(core: RealCore, options: { keepData?: boolean } = {}): Promise<void> {
  await withOwnedCleanup(async () => undefined, [
    {name: "Core process", dispose: async () => {
      const child = core.process;
      const exited = () => child.exitCode !== null || child.signalCode !== null;
      if (exited()) return;
      const wait = (signal: NodeJS.Signals) => new Promise<void>((resolve, reject) => {
        const done = () => {clearTimeout(timeout); resolve();};
        const timeout = setTimeout(() => {child.off("exit", done); reject(new Error(`Owned Core process did not exit after ${signal}`));}, 5_000);
        child.once("exit", done);
        child.kill(signal);
      });
      try {await wait("SIGTERM");} catch (error) {
        if (exited()) return;
        await wait("SIGKILL");
      }
    }},
    // Stopping Core means its whole runtime: the supervisor alone would leave its detached workers running.
    {name: "Core workers", dispose: () => stopCoreWorkers(core.dataDir)},
    {name: "Core sockets", dispose: async () => { if (!options.keepData) await removeCoreSockets(core.dataDir); }},
    {name: "Core data", dispose: async () => {
      if (!options.keepData && process.env.NEBULA_TEST_KEEP_DATA !== "1" && path.basename(core.dataDir).startsWith("nebula-playwright-real-core-")) {
        await removeCoreData(core.dataDir);
      }
    }},
  ]);
}

export function localNetworkIpv4(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for LAN acceptance.");
}
