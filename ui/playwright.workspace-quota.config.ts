import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
export default defineConfig({ ...model, testMatch: "workspace-quota.spec.ts", webServer: { ...model.webServer, env: { NEBULA_TEST_LARGE_WORKSPACE: "1" } } });
