import applicationModel from "./playwright.application-model.config";
import catchUp from "./playwright.catch-up.config";
import hostMode from "./playwright.host-mode.config";
import projectDeletion from "./playwright.project-deletion.config";
import workspaceQuota from "./playwright.workspace-quota.config";
import workspaceRecovery from "./playwright.workspace-recovery.config";
import reconnection from "./playwright.reconnection.config";
import thinking from "./playwright.thinking.config";
import { defineConfig, devices } from "@playwright/test";
import { assertPlaywrightInvocation } from "../scripts/test_scope_guard.mjs";

// Protect direct npx invocations as well as guarded npm scripts.
assertPlaywrightInvocation(process.argv.slice(2));

const testPort = process.env.NEBULA_UI_TEST_PORT ?? "1420";
const testHost = process.env.NEBULA_UI_TEST_HOST ?? "127.0.0.1";
const testCommand = process.env.NEBULA_UI_TEST_COMMAND
  ?? `npm run dev -- --host ${testHost} --port ${testPort}`;
// Selecting real-Core must never implicitly schedule unrelated browser projects.
const isolatedMatrixEntry = true;

// Workers inherit the validated parent selection rather than its CLI arguments.
const selectedArgs: string[] = JSON.parse(process.env.NEBULA_PLAYWRIGHT_FOCUSED_ARGS ?? "[]");
// One invocation owns one fixture server; reject incompatible selections early.
const fixtureConfigs = {
  "tests/assistant-browser-viewer.spec.ts": applicationModel,
  "tests/working-knowledge.spec.ts": applicationModel,
  "tests/catch-up.spec.ts": catchUp,
  "tests/host-mode.spec.ts": hostMode,
  "tests/project-deletion.spec.ts": projectDeletion,
  "tests/workspace-quota.spec.ts": workspaceQuota,
  "tests/workspace-recovery.spec.ts": workspaceRecovery,
  "tests/chat-reconnection.spec.ts": reconnection,
  "tests/thinking.spec.ts": thinking,
};
const selectedFiles = selectedArgs.filter(arg => /^tests\/.*\.spec\.tsx?$/.test(arg));
const selectedFixtures = selectedFiles.map(file => fixtureConfigs[file as keyof typeof fixtureConfigs]);
if (selectedFixtures.some(Boolean) && new Set(selectedFixtures).size > 1) {
  throw new Error("Select one fixture configuration per Playwright invocation.");
}
export default selectedFixtures.find(Boolean) ?? defineConfig({
  testDir: "./tests",
  // Usage-video scripts mutate a narrated fixture and have their own config.
  // They are not product acceptance tests and must not enter deployment E2E.
  testIgnore: "**/usage/**",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.NEBULA_RELEASE_CORE_SMOKE === "1" ? 0 : process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.015,
    },
  },
  use: {
    baseURL: `http://${testHost}:${testPort}`,
    colorScheme: "dark",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
  },
  projects: [
    ...[
      {name: "assistant-real-desktop", use: {...devices["Desktop Chrome"], viewport: {width: 1440, height: 900}}},
      {name: "assistant-real-compact", use: {...devices["Desktop Chrome"], viewport: {width: 1024, height: 768}}},
      ...[320, 390, 430].flatMap(width => [
        {name: `assistant-real-chromium-${width}`, use: {...devices["Pixel 5"], viewport: {width, height: 844}}},
        {name: `assistant-real-webkit-${width}`, use: {...devices["iPhone 13"], viewport: {width, height: 844}}},
      ]),
    ].map(project => ({...project, testMatch: ["**/real-core.spec.ts", "**/work-import.spec.ts", "**/mcp-project-scope.spec.ts"], grep: /assistant upgrade|assistant planning|project removal|project execution mode|mcp import|project MCP catalog|ssh environments|work hub|runtime usage|notebook real Core/})),
    ...[
      { name: "browser-chromium-landscape", device: "Pixel 5" },
      { name: "browser-webkit-landscape", device: "iPhone 13" },
    ].map(({ name, device }) => ({
      name, testMatch: "**/interface.spec.ts", grep: /browser Assistant /,
      use: { ...devices[device], viewport: { width: 844, height: 390 } },
    })),
    {
      name: "desktop",
      testIgnore: ["**/real-core.spec.ts", "**/usage/**"],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "compact",
      testIgnore: ["**/real-core.spec.ts", "**/usage/**"],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 700 } },
    },
    {
      name: "narrow",
      testIgnore: ["**/real-core.spec.ts", "**/usage/**"],
      use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "mobile-chromium",
      testMatch: "**/interface.spec.ts",
      grep: /provider options explain the working ceiling|OpenRouter profile policy offers ZDR and allowed upstreams|knowledge model banner|working context panel|phone shell |tool suggestions |assistant popup|stabilization Nebula Voice|assistant settings expose provider metadata|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|universal search opens|VPN settings keep|theme picker|host folder picker|project scope normalizes|mission workflow|mission result actions|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant live guidance|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|terminal VPN boundary|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell|editing a sent message|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["Pixel 5"] },
    },
    {
      name: "mobile-chromium-ledger-390",
      testMatch: "**/interface.spec.ts",
      grep: /assistant popup|stabilization Nebula Voice|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|activity ledger groups repeated work|assistant follow-up queue|conversation switching|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "mobile-chromium-small",
      testMatch: "**/interface.spec.ts",
      grep: /phone shell |advanced session binding|assistant popup|stabilization Nebula Voice|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|universal search opens|VPN settings keep|theme picker|host folder picker|project scope normalizes|mission workflow|mission result actions|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant live guidance|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|terminal VPN boundary|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell|editing a sent message|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["Pixel 5"], viewport: { width: 320, height: 700 } },
    },
    {
      name: "mobile-chromium-wide",
      testMatch: "**/interface.spec.ts",
      grep: /phone shell |assistant popup|stabilization Nebula Voice|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|universal search opens|VPN settings keep|theme picker|host folder picker|mission result actions|activity ledger groups repeated work|assistant follow-up queue|browser research tools expose durable workflows|completed harness output|terminal VPN boundary|conversation switching|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["Pixel 5"], viewport: { width: 430, height: 932 } },
    },
    {
      name: "mobile-webkit-small",
      testMatch: "**/interface.spec.ts",
      grep: /phone shell |advanced session binding|assistant popup|stabilization Nebula Voice|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|universal search opens|VPN settings keep|theme picker|host folder picker|mission result actions|activity ledger groups repeated work|assistant follow-up queue|browser research tools expose durable workflows|completed harness output|terminal VPN boundary|conversation switching|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["iPhone 13"], viewport: { width: 320, height: 700 } },
    },
    {
      name: "mobile-webkit",
      testMatch: "**/interface.spec.ts",
      grep: /provider options explain the working ceiling|OpenRouter profile policy offers ZDR and allowed upstreams|knowledge model banner|working context panel|phone shell |tool suggestions |assistant popup|stabilization Nebula Voice|assistant settings expose provider metadata|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|universal search opens|VPN settings keep|theme picker|host folder picker|project scope normalizes|mission workflow|mission result actions|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant live guidance|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|terminal VPN boundary|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["iPhone 13"] },
    },
    {
      name: "mobile-webkit-wide",
      testMatch: "**/interface.spec.ts",
      grep: /phone shell |assistant popup|stabilization Nebula Voice|Terminal opens Assistant beside the live shell|Assistant opens the live terminal beside the chat|browser Assistant |assistant upgrade|universal search opens|VPN settings keep|theme picker|host folder picker|project scope normalizes|mission workflow|mission result actions|completed harness output|activity ledger groups repeated work|assistant follow-up queue|assistant context pack|conversation switching|harness model controls|AI writing submits the visible supported model|New chat detaches|oversized harness activity|audit every primary workspace view|audit primary mutation dialogs|paired-device settings|mobile Workbench navigation|browser research tools expose durable workflows|product typography and touch contracts|shared actions keep sleek geometry|terminal screenshot capture|terminal VPN boundary|code editor keeps its caret|terminal and notes keep a visible focused caret|Zero keeps one navigable panoramic shell|Phosphor icon pack|chat companions reveal Code, Browser, and Notes from the conversation|notebook Workbench tab and chat companion keep the same Python file/,

      use: { ...devices["iPhone 13"], viewport: { width: 430, height: 932 } },
    },
    {
      name: "real-core",
      testMatch: "**/real-core.spec.ts",
      dependencies: isolatedMatrixEntry ? [] : ["desktop", "compact", "narrow"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        // The release DEB bundles full Chromium and deliberately omits its headless shell.
        ...(process.env.NEBULA_RELEASE_CORE_SMOKE === "1" ? { channel: "chromium" } : {}),
      },
    },
  ].map(project => ({
    ...project,
    // Stabilization journeys must not silently disappear behind older per-screen
    // filters. File/project boundaries still apply, including isolated real Core.
    ...("grep" in project ? {grep: new RegExp(`stabilization|automation callback|iPhone assistant shell|${project.name.startsWith("mobile-") ? "provider and harness usage|" : ""}${project.grep.source}`)} : {}),
  })),
  webServer: process.env.NEBULA_RELEASE_CORE_SMOKE === "1" ? undefined : {
    command: testCommand,
    url: `http://${testHost}:${testPort}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
