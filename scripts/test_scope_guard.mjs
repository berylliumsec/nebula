import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

export function assertFocused(kind, args, env = process.env) {
  if (args.includes("--list") || args.includes("--help")) return;
  const approved = env.NEBULA_FULL_SUITE_APPROVAL === "RUN_FULL_SUITE"
    && Boolean(env.NEBULA_FULL_SUITE_REASON?.trim());
  if (approved) return;
  const file = args.some(arg => kind === "e2e"
    ? /^tests\/[\w/.-]+\.spec\.tsx?$/.test(arg)
    : /^(?:src\/)?[\w/.-]+\.test\.tsx?$/.test(arg));
  const project = args.some((arg, i) => arg.startsWith("--project=")
    ? Boolean(arg.slice(10)) && !/[?*]/.test(arg)
    : arg === "--project" && Boolean(args[i + 1]) && !/^[\-*]|[?*]/.test(args[i + 1]));
  if (!file || (kind === "e2e" && !project)) {
    throw new Error("Focused tests required: supply an individual test file" +
      (kind === "e2e" ? " and explicit --project (use --grep for the changed feature)" : "") +
      ". Full suites require explicit user approval and NEBULA_FULL_SUITE_APPROVAL=RUN_FULL_SUITE plus NEBULA_FULL_SUITE_REASON.");
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [kind, ...args] = process.argv.slice(2);
  assertFocused(kind, args);
  const command = kind === "e2e" ? "playwright" : "vitest";
  const result = spawnSync(command, [kind === "e2e" ? "test" : "run", ...args], { stdio: "inherit", shell: false });
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
}
