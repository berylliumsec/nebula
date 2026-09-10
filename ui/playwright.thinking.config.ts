import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
export default defineConfig({ ...model, testMatch: "thinking.spec.ts", webServer: { ...model.webServer, command: `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/thinking_fixture.py` } });
