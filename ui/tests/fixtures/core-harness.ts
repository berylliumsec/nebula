import {spawnSync} from "node:child_process";
import {readFile, rm} from "node:fs/promises";
import path from "node:path";
import type {APIRequest, APIRequestContext, APIResponse} from "@playwright/test";
import {coreMutation} from "./core-api";

type Mutation = "post" | "put" | "patch" | "delete";
type MutationOptions = NonNullable<Parameters<APIRequestContext["post"]>[1]>;
const mutations: readonly string[] = ["post", "put", "patch", "delete"];

/** What coreMutation is shown in place of an answer that is already the route's own. */
const ownAnswer = {status: () => 200, headers: () => ({})} as unknown as APIResponse;

/**
 * Send one mutation of a request context through coreMutation, keeping the call site's other options.
 * A caller's own Idempotency-Key is kept. Follow reads go to the Core that answered, whatever the base URL.
 */
async function keyedMutation(api: APIRequestContext, method: Mutation, url: string, options: MutationOptions = {}): Promise<APIResponse> {
  const {data, headers = {}, ...rest} = options;
  const isKey = (name: string) => name.toLowerCase() === "idempotency-key";
  const key = Object.entries(headers).find(([name]) => isKey(name))?.[1];
  const kept = Object.fromEntries(Object.entries(headers).filter(([name]) => !isKey(name)));
  let first: APIResponse | undefined;
  const carrier = {
    [method]: async (target: string, keyed: MutationOptions) => {
      first = await api[method](target, {...rest, ...keyed, headers: {...kept, ...keyed.headers}});
      const marks = first.headers();
      // Only Core's API process saves requests, and it marks each answer. An in-process fixture Core
      // marks nothing, so its 202 that names no saved request is the route's own answer.
      const unmarked = marks["x-nebula-request-state"] === undefined && marks["x-nebula-command-id"] === undefined;
      return first.status() === 202 && unmarked ? ownAnswer : first;
    },
    // A follow read is sent as the same caller: it keeps the call's own headers, such as a per-call Authorization.
    get: (target: string) => api.get(new URL(`/api/v1/${target}`, first!.url()).href, {headers: kept}),
  } as unknown as APIRequestContext;
  // A mutation of a quiet project waits for its worker to start, so a slow host's Core startup wait applies.
  const timeout = Math.max(60_000, Number(process.env.NEBULA_TEST_CORE_STARTUP_TIMEOUT_MS) || 0);
  const answer = await coreMutation(carrier, method, url, data, {key, timeout});
  // A saved request that ended failed or uncertain is not the route's answer; never let a status check pass on it.
  if (answer.headers()["x-nebula-request-state"] === "needs-reconciliation") {
    throw new Error(`Core could not finish the saved ${method.toUpperCase()} ${url}: ${await answer.text()}`);
  }
  return answer === ownAnswer ? first! : answer;
}

/** A request context whose post, put, patch and delete each carry an Idempotency-Key and return Core's final answer. */
export function keyedCoreContext(api: APIRequestContext): APIRequestContext {
  return new Proxy(api, {get(target, name) {
    if (typeof name === "string" && mutations.includes(name)) {
      return (url: string, options?: MutationOptions) => keyedMutation(target, name as Mutation, url, options);
    }
    const value = Reflect.get(target, name, target) as unknown;
    return typeof value === "function" ? value.bind(target) : value;
  }});
}

/**
 * Playwright's `request` with every new context keyed for Core. Core's API process refuses a mutation
 * without a client Idempotency-Key (428), so a spec that binds this in place of `request` keeps its
 * `api.post(...)` call sites and assertions while each mutation is sent the way the GUI sends it.
 */
export function keyedCoreRequests(request: APIRequest): APIRequest {
  return new Proxy(request, {get(target, name) {
    if (name === "newContext") {
      return async (options?: Parameters<APIRequest["newContext"]>[0]) => keyedCoreContext(await target.newContext(options));
    }
    const value = Reflect.get(target, name, target) as unknown;
    return typeof value === "function" ? value.bind(target) : value;
  }});
}

/**
 * End the runtime workers a stopped Core left running for one data root. Workers are detached so
 * they outlive a supervisor restart; stopping the `serve` process alone leaves them running after
 * the test. Only processes started from this data root's own runtime directory are signalled.
 */
export async function stopCoreWorkers(dataDir: string): Promise<void> {
  const runtime = path.join(path.resolve(dataDir), "runtime") + path.sep;
  const owned = () => {
    // `ww` keeps the whole command line; a set COLUMNS would otherwise cut it before the release path.
    const listing = spawnSync("ps", ["-Awwo", "pid=,pgid=,args="], {encoding: "utf8"});
    if (listing.status !== 0) throw new Error(`Could not list Core workers: ${listing.stderr || listing.error}`);
    return listing.stdout.split("\n")
      .map(line => line.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/))
      .filter((match): match is RegExpMatchArray => match !== null && match[3].includes(runtime))
      .map(match => ({pid: Number(match[1]), leader: match[1] === match[2]}));
  };
  const signal = (name: NodeJS.Signals) => {
    for (const worker of owned()) {
      // A worker leads its own process group; signal the group so the tools it started there end with it.
      try { process.kill(worker.leader ? -worker.pid : worker.pid, name); } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
      }
    }
  };
  const gone = async (milliseconds: number) => {
    const deadline = Date.now() + milliseconds;
    while (owned().length) {
      if (Date.now() >= deadline) return false;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return true;
  };
  if (!owned().length) return;
  signal("SIGTERM");
  if (await gone(5_000)) return;
  signal("SIGKILL");
  if (!await gone(5_000)) throw new Error(`Core workers of ${dataDir} did not exit after SIGKILL`);
}

/** Remove the worker socket directory a Core created outside its data root; Core records its path. */
export async function removeCoreSockets(dataDir: string): Promise<void> {
  const origin = await readFile(path.join(dataDir, "runtime", "process-origin.json"), "utf8").catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return undefined;
    throw error;
  });
  const sockets = origin ? (JSON.parse(origin) as {socket_root?: unknown}).socket_root : undefined;
  if (typeof sockets === "string" && path.dirname(sockets) === "/tmp" && path.basename(sockets).startsWith("nebula-ipc-")) {
    await rm(sockets, {recursive: true, force: true});
  }
}
