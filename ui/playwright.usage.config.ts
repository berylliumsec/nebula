import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/usage",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: "list",
  timeout: 90_000,
  expect: { timeout: 10_000 },
  outputDir: "./test-results/usage-scenarios",
  use: {
    ...devices["Desktop Chrome"],
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
  },
  projects: [{ name: "usage-videos", use: { viewport: { width: 1440, height: 900 } } }],
});
