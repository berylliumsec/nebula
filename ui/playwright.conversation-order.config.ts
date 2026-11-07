import {defineConfig} from "@playwright/test";
import {networkInterfaces} from "node:os";
import model from "./playwright.application-model.config";

const host = process.env.NEBULA_MODEL_TEST_HOST ?? Object.values(networkInterfaces()).flat()
  .find(address => address?.family === "IPv4" && !address.internal)?.address;
if (!host) throw new Error("Conversation ordering acceptance requires a non-loopback LAN address");
const port = process.env.NEBULA_MODEL_TEST_PORT ?? "19489";
export default defineConfig({
  ...model, testMatch: "conversation-order.spec.ts",
  use: {...model.use, baseURL: `http://${host}:${port}`, serviceWorkers: "block"},
  projects: model.projects?.map(project => ({...project, use: {
    ...project.use,
    ...(project.use?.browserName !== "webkit" && process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE
      ? {launchOptions: {executablePath: process.env.NEBULA_TEST_CHROMIUM_EXECUTABLE}} : {}),
  }})),
  webServer: {
    command: `${process.env.NEBULA_TEST_PYTHON ?? "python"} ../tests/v3/conversation_order_fixture.py`,
    url: `http://127.0.0.1:${port}/web-build.json`, timeout: 60000,
  },
});
