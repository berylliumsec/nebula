import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19441";
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
export default defineConfig({ ...model, testMatch: "host-mode.spec.ts", use: { ...model.use, baseURL: `http://${host}:${port}` }, webServer: { ...model.webServer, url: `http://${host}:${port}`, command: `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/host_mode_fixture.py` } });
