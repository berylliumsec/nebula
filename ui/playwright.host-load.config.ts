import {defineConfig} from "@playwright/test";
import model from "./playwright.application-model.config";
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19480";
export default defineConfig({
  ...model,
  testMatch: "host-load.spec.ts",
  timeout: 90000,
  use: {...model.use, baseURL: `http://${host}:${port}`, serviceWorkers: "block"},
  projects: model.projects?.map(project => ({...project, use: {
    ...project.use,
    ...(project.use?.browserName !== "webkit" && process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE
      ? {launchOptions: {executablePath: process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE}} : {}),
  }})),
  webServer: {command: `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/host_load_fixture.py`, url: `http://127.0.0.1:${port}`, timeout: 60000},
});
