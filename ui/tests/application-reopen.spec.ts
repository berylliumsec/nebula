import {expect, test} from "@playwright/test";

test("a fresh application document restores the exact older chat and draft without submitting", async ({context, page, baseURL}, info) => {
  const entity = {created_at: "2026-11-09T10:00:00Z", updated_at: "2026-11-09T10:00:00Z", revision: 1};
  const project = {...entity, id: "reopen-project", name: "Reopen fixture project", description: "", status: "active", tags: [], metadata: {}};
  const provider = {...entity, id: "reopen-provider", name: "Inert synthetic provider", provider_type: "vllm", enabled: true,
    is_local: true, model_allowlist: ["probe-model"], capabilities: {streaming: true},
    privacy: {local_only: true, residency: [], permits_sensitive_data: false}, metadata: {default_model: "probe-model"}};
  const sessions = [
    {...entity, id: "chat-newer", engagement_id: project.id, title: "Newer recent chat", provider_profile_id: provider.id, model: "probe-model", metadata: {message_count: 1}, updated_at: "2026-11-09T11:00:00Z"},
    {...entity, id: "chat-older", engagement_id: project.id, title: "Older selected chat", provider_profile_id: provider.id, model: "probe-model", metadata: {message_count: 1}},
  ];
  let identity = {core_id: "reopen-core", principal_id: "operator"};
  const requests: {method: string; path: string}[] = [];
  const errors: string[] = [];
  context.on("page", value => value.on("pageerror", error => errors.push(String(error))));
  page.on("pageerror", error => errors.push(String(error)));
  await context.addInitScript(({origin}) => {
    (window as any).nebulaDesktop = {kind: "electron", resolveBackendConnection: async () => ({endpoint: `${origin}/api/v1`, token: "inert-reopen-token", source: "local", protocol: "nebula-desktop-runtime-v1"}),
      deviceId: async () => "desktop-reopen-fixture", setZoom: async (value: number) => value, browser: {layout: async () => {}, onState: () => () => {}}};
  }, {origin: baseURL!});
  await context.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url()); const name = url.pathname.slice("/api/v1/".length);
    requests.push({method: route.request().method(), path: url.pathname});
    const respond = (value: unknown, status = 200) => route.fulfill({status, contentType: "application/json", body: JSON.stringify(value)});
    if (name === "health") return respond({status: "ok", version: "3.0.0", mode: "local", database: "ok", runner: "unavailable", human_pty: "unavailable", api_ready: true});
    if (name === "runtime/identity") return respond(identity);
    if (name === "engagements") return respond([project]);
    if (name === `engagements/${project.id}`) return respond(project);
    if (name === "providers") return respond([provider]);
    if (name === "setup/status") return respond({application_stage: "ready", core: {status: "ready"}, terminal: {status: "unavailable"}, assistant: {status: "ready", provider_profile_id: provider.id}});
    if (name === "chat-sessions") return respond(sessions);
    if (name.startsWith("chat-sessions/")) {
      const id = name.split("/")[1]; const session = sessions.find(value => value.id === id);
      if (name.endsWith("/assistant-config")) return respond({session_id: id, revision: 1, settings: {backend: "provider", profile_id: provider.id, model: "probe-model"}});
      if (session && name === `chat-sessions/${id}`) return respond(session);
    }
    const message = /^chat\/sessions\/([^/]+)\/messages$/.exec(name);
    if (message) return respond([{...entity, id: `${message[1]}-reply`, engagement_id: project.id, session_id: message[1], sequence: 1,
      role: "assistant", content: `${message[1]} saved reply`, citations: [], usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2}}]);
    const state = /^chat\/sessions\/([^/]+)\/state$/.exec(name);
    if (state) return respond({schema: "nebula.session-state/v1", session_id: state[1], revision: 1, turn_id: null, harness_turn_id: null,
      execution: "idle", busy: false, detail: "", connection: "connected", actions: ["check_status"], pending: [], decisions: []});
    if (name.endsWith("/pending-turn")) return respond(null);
    if (name.endsWith("/task-plans")) return respond({plans: [], directives: []});
    if (name.endsWith("/goal")) return respond({detail: "Goal not found"}, 404);
    return respond([]);
  });

  const draft = "Unsent exact older-chat draft from before application close.";
  await page.goto("/projects/reopen-project/workbench?view=chat&session=chat-older");
  await expect(page.locator(".chat-composer textarea")).toBeEnabled();
  await page.locator(".chat-composer textarea").fill(draft);
  await expect.poll(() => page.evaluate(() => Object.values(localStorage).some(value => value.includes("Unsent exact older-chat draft")))).toBe(true);
  const build = await page.request.get(baseURL + "/web-build.json");
  const buildIdentity = await build.json();
  await page.close();
  const reopened = await context.newPage();
  await reopened.goto("/");
  await expect(reopened).toHaveURL(/\/projects\/reopen-project\/workbench\?view=chat&session=chat-older$/);
  await expect(reopened.locator(".chat-composer textarea")).toHaveValue(draft);
  await expect(reopened.locator(".chat-message.assistant").filter({hasText: "chat-older saved reply"})).toBeVisible();
  await reopened.screenshot({path: info.outputPath("restored-older-chat.png"), fullPage: true});
  await reopened.close();
  const linked = await context.newPage();
  await linked.goto("/projects/reopen-project/workbench?view=chat&session=chat-newer#explicit");
  await expect(linked).toHaveURL(/session=chat-newer#explicit$/);
  await expect(linked.locator(".chat-composer textarea")).toHaveValue("");
  await linked.close();
  identity = {core_id: "reopen-core", principal_id: "device:other"};
  const otherPrincipal = await context.newPage();
  await otherPrincipal.goto("/");
  await expect(otherPrincipal.locator(".session-layout.terminal")).toBeVisible();
  expect(new URL(otherPrincipal.url()).searchParams.get("session")).toBeNull();
  expect(requests.filter(value => value.method === "POST" && value.path.endsWith("/chat/completions"))).toEqual([]);
  expect(errors).toEqual([]);
  await info.attach("application-reopen", {body: JSON.stringify({build: buildIdentity, scope: "production UI with synthetic API; new document in the same browser context", requests, errors}), contentType: "application/json"});
});
