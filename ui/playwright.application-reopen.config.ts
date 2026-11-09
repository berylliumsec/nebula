import {defineConfig} from "@playwright/test";
import model from "./playwright.application-model.config";
import {assertPlaywrightInvocation} from "../scripts/test_scope_guard.mjs";

assertPlaywrightInvocation(process.argv.slice(2));
const host = process.env.NEBULA_REOPEN_TEST_HOST ?? "127.0.0.1";
const port = process.env.NEBULA_REOPEN_TEST_PORT ?? "19553";
export default defineConfig({
  ...model,
  testMatch: "application-reopen.spec.ts",
  use: {...model.use, baseURL: `http://${host}:${port}`, serviceWorkers: "block"},
  projects: model.projects?.map(project => ({...project, use: {...project.use,
    ...(!project.name.startsWith("model-webkit-") && process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE
      ? {launchOptions: {executablePath: process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE}} : {}),
  }})),
  webServer: {command: `npm run preview -- --host 0.0.0.0 --port ${port} --strictPort`, url: `http://127.0.0.1:${port}`, reuseExistingServer: false},
});
