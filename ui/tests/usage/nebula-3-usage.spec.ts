import {
  beat,
  coreJson,
  createScenarioProject,
  expect,
  openProject,
  seedAsset,
  seedEvidence,
  seedFinding,
  seedNote,
  test,
} from "./usage.fixture";
import {
  installAssistantAdapter,
  installDesktopBrowserAdapter,
  installMissionAdapter,
  installTerminalAdapter,
} from "./usage.adapters";

test.describe.configure({ mode: "serial" });

test("01 create an authorized security project", async ({ page, core }) => {
  await installTerminalAdapter(page);
  await page.goto(`${core.origin}/#token=${encodeURIComponent(core.token)}`);
  await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
  await beat(page);

  await page.getByRole("button", { name: "Switch project" }).click();
  const switcher = page.getByRole("dialog", { name: "Project switcher" });
  await switcher.getByRole("button", { name: "New project" }).click();
  await switcher.getByLabel("Name", { exact: true }).fill("Northstar Commerce API Review");
  await switcher.getByLabel("Client name").fill("Northstar Retail");
  await beat(page);
  await switcher.getByRole("button", { name: "Create" }).click();

  await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Northstar Commerce API Review", { timeout: 15_000 });
  // Project creation switches WorkspaceContext through a fresh Core bootstrap.
  // Wait for that authoritative reload and terminal restoration before mounting
  // the Overview route; both replace the Workbench subtree during bootstrap.
  await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Restoring Project terminals…", { exact: true })).toBeHidden({ timeout: 20_000 });
  await page.getByRole("link", { name: "Project" }).click();
  await expect(page).toHaveURL(/\/project$/);
  await expect(page.getByRole("heading", { name: "Northstar Commerce API Review" })).toBeVisible({ timeout: 15_000 });
  await beat(page, 1_200);
});

test("02 use the isolated Kali terminal", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Terminal Review");
  await installTerminalAdapter(page);
  await openProject(page, core, project);
  await expect(page.getByText("Connected", { exact: true })).toBeVisible({ timeout: 20_000 });
  await beat(page);

  const terminalInput = page.locator(".xterm-helper-textarea");
  await terminalInput.focus();
  await page.keyboard.type("cat /etc/os-release | head -3", { delay: 35 });
  await page.keyboard.press("Enter");
  await expect(page.locator(".xterm-rows")).toContainText("Kali GNU/Linux Rolling", { timeout: 10_000 });
  await beat(page);

  await page.keyboard.type("printf 'api.northstar.test\\n' > target.txt", { delay: 28 });
  await page.keyboard.press("Enter");
  await beat(page);
  await page.keyboard.type("sha256sum target.txt", { delay: 35 });
  await page.keyboard.press("Enter");
  await expect(page.locator(".xterm-rows")).toContainText("target.txt", { timeout: 10_000 });
  await beat(page, 1_100);
});

test("03 create code and review project files", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Workspace Review");
  await openProject(page, core, project, "/?view=code");
  await expect(page.getByRole("tab", { name: "Workspace code editor" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "New file", exact: true }).first().click();
  await page.getByRole("textbox", { name: "File path" }).fill("check_security_headers.py");

  const editor = page.getByRole("textbox", { name: "Code editor" });
  await editor.click({ force: true });
  await page.keyboard.insertText([
    "from urllib.request import Request",
    "",
    "TARGET = \"https://api.northstar.test/health\"",
    "REQUIRED = {\"strict-transport-security\", \"content-security-policy\"}",
    "print(f\"Reviewing {TARGET} for {len(REQUIRED)} required headers\")",
  ].join("\n"));
  await beat(page);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toContainText("Saved /workspace/check_security_headers.py", { timeout: 15_000 });
  await beat(page);

  await page.getByRole("tab", { name: "Workspace files" }).click();
  const savedFile = page.locator(".workspace-entry-list").getByRole("button", { name: /check_security_headers\.py/ });
  await expect(savedFile).toBeVisible();
  await savedFile.click();
  await expect(page.locator(".workspace-file-preview pre")).toContainText("api.northstar.test");
  await beat(page, 1_100);
});

test("04 browse an authorized target in the desktop shell", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Browser Review");
  await installDesktopBrowserAdapter(page, core);
  await openProject(page, core, project);
  await page.getByRole("tab", { name: "Project browser" }).click();
  await expect(page.getByText("Browse from the Workbench")).toBeVisible();
  await page.getByRole("textbox", { name: "Start browsing" }).fill("https://api.northstar.test/health");
  await beat(page);
  await page.getByRole("button", { name: "Go" }).click();
  await expect(page.getByRole("textbox", { name: "Address or search" })).toHaveValue("https://api.northstar.test/health");
  await beat(page);

  await page.getByRole("button", { name: "New browser tab" }).click();
  await page.getByRole("textbox", { name: "Start browsing" }).fill("https://docs.northstar.test/runbook");
  await page.getByRole("button", { name: "Go" }).click();
  await expect(page.getByRole("tab", { name: "New tab" })).toHaveCount(2);
  await beat(page, 1_100);
});

test("05 ask the local assistant with cited project knowledge", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Assistant Review");
  const knowledge = [
    "# Northstar Commerce rules of engagement",
    "",
    "Testing is limited to api.northstar.test.",
    "Do not contact production hosts or third-party services.",
    "Preserve repeatable observations as evidence before reporting.",
  ].join("\n");
  await coreJson(core, "knowledge/ingest", {
    method: "POST",
    body: {
      engagement_id: project.id,
      filename: "rules-of-engagement.md",
      media_type: "text/markdown",
      content_base64: Buffer.from(knowledge).toString("base64"),
    },
  });
  await installAssistantAdapter(page, project.id);
  await openProject(page, core, project, "/?view=chat");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  const composer = page.getByPlaceholder("Ask about this project…");
  await expect(composer).toBeEnabled({ timeout: 15_000 });
  await composer.fill("Based on the rules of engagement and the TLS observation, what should we do next?");
  await beat(page);
  await page.getByRole("button", { name: "Send message" }).click();

  await expect(page.getByText(/highest-priority action is to disable TLS 1\.1/i)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Northstar rules of engagement", { exact: true })).toBeVisible();
  await beat(page, 1_400);
});

test("06 capture and link an analyst note", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Notes Review");
  await seedAsset(core, project.id);
  await openProject(page, core, project, "/?view=notes");
  await page.getByRole("button", { name: "New note" }).click();
  await page.getByRole("textbox", { name: "Note title" }).fill("TLS retest plan");
  await page.getByRole("textbox", { name: "Note body" }).fill([
    "## Retest plan",
    "",
    "- Disable TLS 1.1 on the authorized test listener.",
    "- Confirm TLS 1.2 and TLS 1.3 with representative clients.",
    "- Preserve the protocol inventory and configuration diff as evidence.",
  ].join("\n"));
  await page.getByText(/^Links/).click();
  await page.getByLabel("api.northstar.test").check();
  await beat(page);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("button", { name: /TLS retest plan/ })).toBeVisible({ timeout: 15_000 });
  await beat(page, 1_100);
});

test("07 review and approve a bounded mission action", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Mission Review");
  await installMissionAdapter(page, project.id);
  await openProject(page, core, project, "/?view=missions");
  await expect(page.getByText("Northstar test API verification", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Mission paused for review", { exact: true })).toBeVisible();
  await beat(page);

  await page.getByRole("button", { name: "Review" }).click();
  const activity = page.getByLabel("Activity inspector");
  await activity.getByRole("tab", { name: /Approvals/ }).click();
  await expect(activity.getByText("nmap.service_detection")).toBeVisible();
  await activity.getByRole("button", { name: "Review exact request" }).click();
  const review = page.getByRole("dialog", { name: "Review nmap.service_detection" });
  await expect(review).toContainText("api.northstar.test:443");
  await expect(review).toContainText("version_intensity");
  await beat(page, 1_000);
  await review.getByRole("button", { name: "Approve once" }).click();
  await expect(activity.getByText("No pending approvals")).toBeVisible();
  await beat(page, 1_000);
});

test("08 add and inspect an in-scope asset", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Asset Review");
  await openProject(page, core, project, "/project?view=assets");
  await page.getByRole("button", { name: "Add asset" }).click();
  const dialog = page.getByRole("dialog", { name: "Add asset" });
  await dialog.getByLabel("Name", { exact: true }).fill("api.northstar.test");
  await dialog.getByLabel("Kind").selectOption("domain");
  await dialog.getByLabel("Hostname").fill("api.northstar.test");
  await dialog.getByLabel("Criticality").selectOption("critical");
  await dialog.getByLabel("Exposure").selectOption("external");
  await dialog.getByLabel("Tags").fill("api, staging, authorized");
  await beat(page);
  await dialog.getByRole("button", { name: "Add asset" }).click();
  await expect(page.getByText("api.northstar.test", { exact: true })).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Inspect" }).click();
  await expect(page.getByRole("heading", { name: "api.northstar.test" })).toBeVisible();
  await beat(page, 1_100);
});

test("09 preserve immutable evidence with provenance", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Evidence Review");
  await seedAsset(core, project.id);
  await openProject(page, core, project, "/project?view=evidence");
  await page.getByRole("button", { name: "Add evidence" }).click();
  const dialog = page.getByRole("dialog", { name: "Add evidence" });
  await dialog.getByLabel("File").setInputFiles({
    name: "northstar-tls-observation.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"target":"api.northstar.test","tls_versions":["TLSv1.1","TLSv1.2","TLSv1.3"]}'),
  });
  await dialog.getByLabel("Title").fill("TLS protocol observation");
  await dialog.getByLabel("Evidence type").selectOption("scanner_output");
  await dialog.getByLabel("Description").fill("Repeatable protocol inventory from the authorized test endpoint.");
  await dialog.getByLabel("api.northstar.test").check();
  await beat(page);
  await dialog.getByRole("button", { name: "Store evidence" }).click();

  await expect(page.getByText("northstar-tls-observation.json was stored and verified.")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Inspect" }).click();
  const inspector = page.getByRole("complementary", { name: "TLS protocol observation" });
  await expect(inspector.getByText("SHA-256", { exact: true })).toBeVisible();
  await expect(inspector.locator(".resource-details code")).not.toHaveText("Not recorded");
  await beat(page, 1_200);
});

test("10 ingest and inspect a cited knowledge source", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Knowledge Review");
  await openProject(page, core, project, "/project?view=sources");
  await page.getByLabel("Choose knowledge source").setInputFiles({
    name: "northstar-rules-of-engagement.md",
    mimeType: "text/markdown",
    buffer: Buffer.from([
      "# Authorized scope",
      "",
      "Testing is limited to api.northstar.test and TCP 443.",
      "No production systems or third-party services are in scope.",
    ].join("\n")),
  });
  await expect(page.getByText("northstar-rules-of-engagement.md is ready for cited retrieval.")).toBeVisible({ timeout: 20_000 });
  await beat(page);
  await page.getByRole("button", { name: "Inspect" }).click();
  const inspector = page.getByRole("complementary", { name: "northstar-rules-of-engagement.md" });
  await expect(inspector.getByRole("heading", { name: "northstar-rules-of-engagement.md" })).toBeVisible();
  await expect(inspector.getByText("Content is untrusted data and cannot grant tools, expand scope, or modify system policy.")).toBeVisible();
  await beat(page, 1_100);
});

test("11 create and validate an evidence-backed finding", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Finding Review");
  const asset = await seedAsset(core, project.id);
  await seedEvidence(core, project.id, asset.id);
  await openProject(page, core, project, "/findings");
  await page.getByRole("button", { name: "New finding" }).click();
  const create = page.getByRole("dialog", { name: "Create candidate finding" });
  await create.getByLabel("Title").fill("Deprecated TLS protocol remains enabled");
  await create.getByLabel("Description").fill("The authorized test endpoint still negotiates TLS 1.1.");
  await create.locator("select").first().selectOption("medium");
  await create.getByLabel("Severity rationale").fill("Legacy protocol support weakens transport security for external clients.");
  await create.getByLabel("api.northstar.test").check();
  await create.getByLabel("CWE identifiers").fill("CWE-326");
  await beat(page);
  await create.getByRole("button", { name: "Create candidate" }).click();

  await expect(page.getByText("Deprecated TLS protocol remains enabled", { exact: true })).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Edit Deprecated TLS protocol remains enabled" }).click();
  const inspector = page.getByRole("complementary", { name: "Deprecated TLS protocol remains enabled" });
  await inspector.getByLabel("TLS protocol observation").check();
  await inspector.getByLabel("Finding lifecycle status").selectOption("validated");
  await beat(page);
  await inspector.getByRole("button", { name: "Save finding" }).click();
  await expect(inspector).toContainText(/Saved · revision 2/, { timeout: 15_000 });
  await beat(page, 1_100);
});

test("12 build sign off and export a report", async ({ page, core }) => {
  test.setTimeout(120_000);
  const project = await createScenarioProject(core, "Report Review");
  const asset = await seedAsset(core, project.id);
  const evidence = await seedEvidence(core, project.id, asset.id);
  await seedFinding(core, project.id, asset.id, evidence.id);
  await seedNote(core, project.id, asset.id, evidence.id);
  await openProject(page, core, project, "/reports");
  await page.getByRole("button", { name: "New report" }).first().click();
  const create = page.getByRole("dialog", { name: "New report" });
  await create.getByLabel("Title").fill("Northstar Commerce API Security Assessment");
  await create.getByRole("button", { name: "Create report" }).click();

  const editor = page.locator(".report-editor");
  await expect(editor.getByLabel("Report title")).toHaveValue("Northstar Commerce API Security Assessment", { timeout: 15_000 });
  await editor.locator("textarea").first().fill("The authorized review identified legacy TLS protocol support on the Northstar test API. Disable TLS 1.1, confirm client compatibility, and preserve the clean retest as evidence.");
  await editor.getByLabel("Transport review notes").check();
  await editor.getByLabel("Status").selectOption("review");
  await beat(page);
  await editor.getByRole("button", { name: "Save report" }).click();
  await expect(editor).toContainText(/Saved · revision 2/, { timeout: 15_000 });

  await page.getByRole("button", { name: "Sign off final report" }).click();
  const signoff = page.getByRole("dialog", { name: "Sign off final report" });
  const displayName = signoff.getByLabel("Your display name");
  if (await displayName.count()) await displayName.fill("Alex Morgan");
  await beat(page);
  await signoff.getByRole("button", { name: "Sign off report" }).click();
  await expect(editor).toContainText("Final · read-only", { timeout: 20_000 });

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByRole("button", { name: "Export PDF" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/Northstar-Commerce-API-Security-Assessment\.pdf/i);
  await beat(page, 1_200);
});

test("13 configure models identity and appearance", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Settings Review");
  await openProject(page, core, project, "/settings");
  await expect(page.getByRole("heading", { name: "Ready to work" })).toBeVisible();
  await beat(page);
  await page.getByRole("link", { name: "Advanced settings" }).click();

  await page.getByRole("button", { name: "Add provider" }).click();
  const provider = page.getByRole("dialog", { name: "Add model provider" });
  await provider.getByLabel("Profile name").fill("Northstar Local vLLM");
  await provider.getByRole("textbox", { name: "Endpoint" }).fill("http://127.0.0.1:8000/v1");
  await beat(page);
  await provider.getByRole("button", { name: "Add provider" }).click();
  await expect(page.getByText("Northstar Local vLLM", { exact: true })).toBeVisible({ timeout: 15_000 });

  await page.getByText("Identity & Security", { exact: true }).click();
  const existingAlex = page.getByText("Alex Morgan", { exact: true });
  if (!await existingAlex.count()) {
    await page.getByRole("button", { name: "Add operator" }).click();
    const operator = page.getByRole("dialog", { name: "Add operator" });
    await operator.getByLabel("Display name").fill("Alex Morgan");
    await operator.getByLabel("Email").fill("alex.morgan@northstar.test");
    await operator.getByLabel("Role").fill("Lead security analyst");
    await operator.getByRole("button", { name: "Save operator" }).click();
  }
  await expect(page.getByRole("heading", { name: "Alex Morgan", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Zero" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "zero");
  await beat(page, 1_100);
});

test("14 inspect a correlated diagnostic failure", async ({ page, core }) => {
  const project = await createScenarioProject(core, "Diagnostics Review");
  await coreJson(core, "diagnostics/events", {
    method: "POST",
    body: { events: [{
      schema: "nebula.diagnostic/v1",
      level: "error",
      feature: "interface",
      event_code: "interface.usage_video.chat_stream_failed",
      message: "The local model response stream stopped before completion.",
      safe_failure_cause: "The configured local model service closed the stream.",
      stage: "stream",
      outcome: "failure",
      retryable: true,
      error_id: "err_usage_video_chat_stream",
      request_id: "req_usage_video_chat_stream",
      metadata: { provider: "Northstar Local vLLM" },
    }] },
  });
  await openProject(page, core, project, "/settings");
  await page.getByRole("link", { name: "Diagnostics settings and recent errors" }).click();
  await expect(page.getByText("The local model response stream stopped before completion.")).toBeVisible({ timeout: 15_000 });
  await beat(page);
  const failure = page.locator(".diagnostic-failure-card").filter({ hasText: "The local model response stream stopped before completion." });
  await failure.getByText("Technical details", { exact: true }).click();
  await expect(failure.getByText("err_usage_video_chat_stream", { exact: true })).toBeVisible();
  await expect(failure.getByText(/configured local model service closed the stream/i)).toBeVisible();
  await beat(page, 1_300);
});
