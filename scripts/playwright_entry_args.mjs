import {resolve} from "node:path";
import {pathToFileURL} from "node:url";

/** Audited cases with a separate database, server, port and browser per test. */
export const parallelCoreSpecs = [
  "tests/authentication-real-core.spec.ts",
  "tests/first-user-real-core.spec.ts",
  "tests/runtime-settings-real-core.spec.ts",
];

/** Collection and execution must use identical project, grep and shard arguments. */
export function entryArgs({project, testMatch, grep = "", shard = "1/1"}) {
  if (!project || !/^tests\/.+\.spec\.tsx?$/.test(testMatch ?? "") || !/^[1-9]\d*\/[1-9]\d*$/.test(shard)) {
    throw new Error("A catalogued project, spec and positive shard are required");
  }
  const [part, total] = shard.split("/").map(Number);
  if (part > total) throw new Error("Shard index exceeds shard count");
  if ([project, testMatch, grep, shard].some(value => /[\r\n]/.test(value))) throw new Error("Matrix arguments must be single-line values");
  const args = [testMatch, "--project", project, "--workers", parallelCoreSpecs.includes(testMatch) ? "2" : "1", "--shard", shard];
  if (grep) args.push("--grep", grep);
  // Legacy monolith shards need test-level collection even with one worker.
  if (testMatch === "tests/real-core.spec.ts" && total > 1) args.push("--fully-parallel");
  return args;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const {PROJECT: project, TEST_MATCH: testMatch, TEST_GREP: grep, TEST_SHARD: shard} = process.env;
  process.stdout.write(entryArgs({project, testMatch, grep, shard}).join("\n") + "\n");
}
