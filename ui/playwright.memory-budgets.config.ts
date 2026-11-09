import {defineConfig} from "@playwright/test";
import model from "./playwright.application-model.config";
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19486";
export default defineConfig({
  ...model, testMatch: "memory-budgets.spec.ts", timeout: 90000,
  use: {...model.use, baseURL: `http://${host}:${port}`, serviceWorkers: "block"},
  projects: model.projects?.map(project => ({...project, use: {...project.use,
    ...(project.use?.browserName !== "webkit" && process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE
      ? {launchOptions: {executablePath: process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE}} : {}),
  }})),
  webServer: {command: `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/memory_budget_fixture.py`, url: `http://${host}:${port}`, timeout: 60000},
});
