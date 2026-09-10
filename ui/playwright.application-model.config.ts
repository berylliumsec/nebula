import { defineConfig, devices } from "@playwright/test";
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19420";
export default defineConfig({
  testDir: "./tests", testMatch: ["working-knowledge.spec.ts", "assistant-browser-viewer.spec.ts"], workers: 1, timeout: 60000,
  use: { baseURL: `http://${host}:${port}`, reducedMotion: "reduce", trace: "retain-on-failure" },
  projects: [
    { name: "model-desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "model-compact", use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } } },
    ...[320, 390, 430].flatMap(width => [
      { name: `model-chromium-${width}`, use: { ...devices["Pixel 5"], viewport: { width, height: 844 } } },
      { name: `model-webkit-${width}`, use: { ...devices["iPhone 13"], viewport: { width, height: 844 } } },
    ]),
  ],
  webServer: { command: process.env.NEBULA_MODEL_TEST_COMMAND ?? `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/application_model_fixture.py`, url: `http://${host}:${port}`, timeout: 60000 },
});
