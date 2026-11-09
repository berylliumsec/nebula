import {defineConfig} from "@playwright/test";
import {randomUUID} from "node:crypto";
import {tmpdir} from "node:os";
import {join} from "node:path";
import model from "./playwright.application-model.config";
import {assertPlaywrightInvocation} from "../scripts/test_scope_guard.mjs";

assertPlaywrightInvocation(process.argv.slice(2));
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
const port = Number(process.env.NEBULA_MODEL_TEST_PORT ?? "19489");
process.env.NEBULA_MODEL_TEST_AUTH_FILE ??= join(tmpdir(), `nebula-continuity-${randomUUID()}.json`);
const quote = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`;
const python = process.env.NEBULA_ACCEPTANCE_PYTHON ?? "../.venv/bin/python";

export default defineConfig({
  ...model,
  testMatch: "runtime-continuity.spec.ts",
  timeout: 180_000,
  workers: 1,
  use: {...model.use, baseURL: `http://${host}:${port}`},
  projects: model.projects?.map(project => ({...project, use: {...project.use,
    ...(project.use?.browserName !== "webkit" && process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE
      ? {launchOptions: {executablePath: process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE}} : {}),
  }})),
  webServer: {
    command: `${quote(python)} ../tests/v3/runtime_continuity_browser.py`,
    url: `http://${host}:${port + 1}/health`,
    timeout: 240_000,
    reuseExistingServer: false,
  },
});
