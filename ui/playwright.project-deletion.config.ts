import { defineConfig } from "@playwright/test";
import model from "./playwright.application-model.config";
export default defineConfig({ ...model, testMatch: "project-deletion.spec.ts" });
