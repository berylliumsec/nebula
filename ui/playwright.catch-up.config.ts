import { defineConfig } from "@playwright/test";
import recovery from "./playwright.workspace-recovery.config";
export default defineConfig({ ...recovery, testMatch: "catch-up.spec.ts" });
