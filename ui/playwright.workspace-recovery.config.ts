import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19430";
export default defineConfig({
  ...model,
  testMatch: "workspace-recovery.spec.ts",
  use: { ...model.use, baseURL: `http://${host}:${port}` },
  webServer: { command: `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/workspace_recovery_fixture.py`, url: `http://${host}:${port}`, timeout: 60000 },
});
