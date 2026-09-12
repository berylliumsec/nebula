import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19420";
export default defineConfig({ ...model, testMatch: "thinking.spec.ts", webServer: { ...model.webServer, command: `NEBULA_MODEL_TEST_PORT=${port} ${process.env.NEBULA_TEST_PYTHON ?? "poetry run python"} ../tests/v3/thinking_fixture.py` } });
