import { defineConfig, devices } from "@playwright/test";

const testPort = process.env.NEBULA_UI_TEST_PORT ?? "1420";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.015,
    },
  },
  use: {
    baseURL: `http://127.0.0.1:${testPort}`,
    colorScheme: "dark",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      testIgnore: "**/real-core.spec.ts",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "compact",
      testIgnore: "**/real-core.spec.ts",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 700 } },
    },
    {
      name: "narrow",
      testIgnore: "**/real-core.spec.ts",
      use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "mobile-chromium",
      testMatch: "**/interface.spec.ts",
      grep: /host folder picker|project scope normalizes|mission workflow|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell/,
      use: { ...devices["Pixel 5"] },
    },
    {
      name: "mobile-chromium-ledger-390",
      testMatch: "**/interface.spec.ts",
      grep: /activity ledger groups repeated work/,
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "mobile-chromium-small",
      testMatch: "**/interface.spec.ts",
      grep: /host folder picker|project scope normalizes|mission workflow|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell/,
      use: { ...devices["Pixel 5"], viewport: { width: 320, height: 700 } },
    },
    {
      name: "mobile-chromium-wide",
      testMatch: "**/interface.spec.ts",
      grep: /activity ledger groups repeated work|browser research tools expose durable workflows|completed harness output/,
      use: { ...devices["Pixel 5"], viewport: { width: 430, height: 932 } },
    },
    {
      name: "mobile-webkit-small",
      testMatch: "**/interface.spec.ts",
      grep: /activity ledger groups repeated work|browser research tools expose durable workflows|completed harness output/,
      use: { ...devices["iPhone 13"], viewport: { width: 320, height: 700 } },
    },
    {
      name: "mobile-webkit",
      testMatch: "**/interface.spec.ts",
      grep: /host folder picker|project scope normalizes|mission workflow|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell/,
      use: { ...devices["iPhone 13"] },
    },
    {
      name: "mobile-webkit-wide",
      testMatch: "**/interface.spec.ts",
      grep: /host folder picker|project scope normalizes|mission workflow|completed harness output|activity ledger groups repeated work|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell/,
      use: { ...devices["iPhone 13"], viewport: { width: 430, height: 932 } },
    },
    {
      name: "real-core",
      testMatch: "**/real-core.spec.ts",
      dependencies: ["desktop", "compact", "narrow"],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${testPort}`,
    url: `http://127.0.0.1:${testPort}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
