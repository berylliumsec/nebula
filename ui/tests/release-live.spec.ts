import {chmod, copyFile, mkdtemp, readFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import {expect, request, test} from "@playwright/test";

// Explicit local release acceptance only. Never infer a live origin or token.
// Live credentials must not enter a trace; sanitized receipts and screenshots
// are retained instead. All mutations below belong to newly created fixtures.
test.use({trace: "off"});
test("stabilization deployed approval completes once and survives reconnect", async ({page}, info) => {
  test.setTimeout(120_000);
  const origin = process.env.NEBULA_RELEASE_ORIGIN;
  const tokenFile = process.env.NEBULA_RELEASE_TOKEN_FILE;
  const commit = process.env.NEBULA_RELEASE_COMMIT;
  if (!origin || !tokenFile || !commit) throw new Error("Explicit release origin, private token file and commit are required");
  const content = (await readFile(tokenFile, "utf8")).trim();
  const serviceToken = content.match(/^NEBULA_V3_API_TOKEN=(.*)$/m)?.[1].trim().replace(/^(['"])(.*)\1$/, "$2");
  const token = content.startsWith("{") ? JSON.parse(content).token : serviceToken ?? content;
  if (typeof token !== "string" || !token) throw new Error("Release token is missing");
  const api = await request.newContext({baseURL: `${origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${token}`}});
  const fixtureRoot = await mkdtemp(path.join(tmpdir(), "nebula-release-inert-"));
  await chmod(fixtureRoot, 0o700);
  const executable = path.join(fixtureRoot, "stabilization_acp.py");
  await copyFile(path.resolve(import.meta.dirname, "../../scripts/fixtures/stabilization_acp.py"), executable);
  await chmod(executable, 0o700);
  let projectId: string | undefined;
  let harnessId: string | undefined;
  let pairedDeviceId: string | undefined;
  try {
    const manifest = await (await api.get(`${origin}/web-build.json`)).json();
    expect(manifest.commit).toBe(commit);
    expect(manifest.dirty).toBe(false);
    expect((await (await api.get("health")).json()).commit).toBe(commit);
    const projectResponse = await api.post("engagements", {data: {name: `Release check ${commit.slice(0, 7)} ${Date.now()}`}});
    expect(projectResponse.ok()).toBe(true);
    projectId = (await projectResponse.json()).id;
    const harnessResponse = await api.post("harnesses", {data: {
      name: `Inert release check ${Date.now()}`, kind: "grok_acp", executable,
      default_model: "stabilization-fixture", enabled: true,
      privacy: {local_only: true, permits_sensitive_data: true}, native_capabilities: {skills: true},
    }});
    expect(harnessResponse.ok()).toBe(true);
    harnessId = (await harnessResponse.json()).id;
    const health = await api.post(`harnesses/${harnessId}/health`);
    expect(health.ok()).toBe(true);
    expect((await health.json()).healthy).toBe(true);
    // A bearer fragment intentionally lasts only for the current page. Pair
    // through the real UI so reload checks use the supported durable binding.
    const pairing = await api.post(`http://127.0.0.1:${new URL(origin).port}/api/v1/auth/pairings`, {data: {name: "Disposable release browser"}});
    expect(pairing.ok()).toBe(true);
    const invitation = await pairing.json();
    await page.goto(`${origin}/#pair=${encodeURIComponent(invitation.secret)}&code=${encodeURIComponent(invitation.confirmation_code)}`);
    await page.getByLabel("Device name").fill(`Release browser ${projectId}`);
    await page.getByRole("button", {name: "Pair device", exact: true}).click();
    await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20_000});
    pairedDeviceId = (await (await api.get("auth/devices")).json()).find((device: {name: string}) => device.name === `Release browser ${projectId}`)?.id;
    expect(pairedDeviceId).toBeTruthy();
    await page.goto(`${origin}/projects/${projectId}/workbench?view=chat`);
    await page.getByRole("button", {name: "New chat", exact: true}).click();
    await page.getByRole("button", {name: "Assistant settings", exact: true}).click();
    await page.getByRole("combobox", {name: "Chat runtime", exact: true}).selectOption("harness");
    await page.getByRole("combobox", {name: "Chat harness", exact: true}).selectOption(harnessId!);
    await page.getByRole("button", {name: "Close assistant settings", exact: true}).click();
    const composer = page.getByRole("textbox", {name: "Message the analyst assistant", exact: true});
    await composer.fill("Review the fixed inert release fixture. No command executes.");
    await page.getByRole("button", {name: "Send message", exact: true}).click();
    await page.getByRole("button", {name: "Review pending actions", exact: true}).click({timeout: 30_000});
    const approval = page.getByRole("region", {name: "Approval required", exact: true});
    await expect(approval).toBeVisible();
    await page.reload();
    await expect(approval).toBeVisible();
    await approval.getByRole("button", {name: "Approve", exact: true}).click();
    const answer = page.locator(".chat-message.assistant .assistant-markdown");
    await expect(answer).toContainText("NATIVE_APPROVAL_ACCEPTED_ONCE", {timeout: 30_000});
    await expect(page.getByRole("button", {name: "Stop response", exact: true})).toHaveCount(0);
    const session = new URL(page.url()).searchParams.get("session");
    expect(session).toBeTruthy();
    const state = await (await api.get(`chat/sessions/${session}/state`)).json();
    expect(state.execution).toBe("complete");
    expect(state.pending).toEqual([]);
    expect(state.decisions).toHaveLength(1);
    expect(state.decisions[0].continuation).toMatchObject({status: "delivered", adapter_status: "sent", adapter_handoff: "transport_write"});
    expect(state.decisions[0].progress).toBe("observed");
    await composer.fill("Unsent release reconnect draft.");
    await page.context().setOffline(true);
    await expect(page.getByRole("button", {name: "Nebula Core failed. Retry connection", exact: true})).toBeVisible();
    await page.context().setOffline(false);
    await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20_000});
    await expect(composer).toHaveValue("Unsent release reconnect draft.");
    expect(new URL(page.url()).searchParams.get("session")).toBe(session);
    await page.reload();
    await expect(answer).toHaveCount(1);
    await expect(answer).toContainText("NATIVE_APPROVAL_ACCEPTED_ONCE");
    await expect(page.getByRole("button", {name: "Review pending actions", exact: true})).toHaveCount(0);
    const receipts = (await readFile(path.join(fixtureRoot, "stabilization_acp.receipts.jsonl"), "utf8")).trim().split("\n").map(line => JSON.parse(line));
    expect(receipts).toHaveLength(1);
    expect(receipts[0].allowed).toBe(true);
    await info.attach("deployed-approval-receipt", {body: JSON.stringify({origin, commit, projectId, session, state, receipts}), contentType: "application/json"});
    await info.attach("deployed-approval-reconnected", {body: await page.screenshot(), contentType: "image/png"});
  } finally {
    await page.context().setOffline(false);
    // Preserve test evidence but remove these fixtures from normal selection.
    for (const [collection, id, changes] of [["harnesses", harnessId, {enabled: false}], ["engagements", projectId, {status: "archived"}]] as const) {
      if (!id) continue;
      const current = await api.get(`${collection}/${id}`);
      expect(current.ok()).toBe(true);
      const response = await api.patch(`${collection}/${id}`, {data: {changes, expected_revision: (await current.json()).revision}});
      expect(response.ok(), `Could not retire disposable ${collection} fixture`).toBe(true);
    }
    if (pairedDeviceId) expect((await api.delete(`auth/devices/${pairedDeviceId}`)).ok()).toBe(true);
    await api.dispose();
  }
});
