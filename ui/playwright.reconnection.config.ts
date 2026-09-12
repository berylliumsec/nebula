import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19448";
const host = process.env.NEBULA_MODEL_TEST_HOST ?? "127.0.0.1";
export default defineConfig({...model, testMatch: "chat-reconnection.spec.ts", use: {...model.use, baseURL: `http://${host}:${port}`}, webServer: {...model.webServer, url: `http://${host}:${port}`, command: `NEBULA_MODEL_TEST_PORT=${port} ${process.env.NEBULA_TEST_PYTHON ?? "poetry run python"} ../tests/v3/chat_reconnection_fixture.py`}});
