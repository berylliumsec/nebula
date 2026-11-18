import {request as unkeyedRequest} from "@playwright/test";
import {test, expect} from "./fixtures/journey-test";
import {keyedCoreRequests} from "./fixtures/core-harness";
import {startRealCore, stopRealCore, localNetworkIpv4} from "./fixtures/real-core";
import {startLocalModelStub, stopLocalModelStub} from "./fixtures/model-stub";
import {pairRealCoreBrowser, openProjectSwitcherFromShell, clickAssistantSettings, openRuntimeSettings} from "./fixtures/operator-ui";
const playwrightRequest = keyedCoreRequests(unkeyedRequest);

// Every case has an independent database, port and browser context.
test.describe.configure({mode: "parallel"});

test("assistant upgrade first-time user configures a provider uploads a document and analyses it", async ({page, resources}, testInfo) => {
  test.setTimeout(120_000);
  page.setDefaultTimeout(15_000);
  const prompt = "Analyse the attached review: report its reference and total affected hosts.";
  const document = "Review reference: FIRST-REVIEW-742\nOffice: 12 hosts\nLab: 24 hosts\nRemote: 30 hosts\n";
  const answer = "FIRST-REVIEW-742: 66 affected hosts across 3 locations.";
  const analysisRequests: Record<string, unknown>[] = [];
  // Only the external model is synthetic. Its answer is conditional on receiving
  // the uploaded bytes, so a canned reply cannot hide a broken attachment path.
  const stub = await startLocalModelStub({beforeResponse: async body => {
    const messages = JSON.stringify(body.messages);
    if (messages.includes(prompt)) analysisRequests.push(body);
    stub.responseContent = messages.includes(prompt) && messages.includes("FIRST-REVIEW-742")
      && ["Office: 12 hosts", "Lab: 24 hosts", "Remote: 30 hosts"].every(row => messages.includes(row))
      ? answer : "The uploaded review is missing.";
  }});
  resources.own("model service", stub, stopLocalModelStub);
  const core = await startRealCore({bindHost: "0.0.0.0", browserHost: localNetworkIpv4()});
  resources.own("Core", core, stopRealCore, value => value.logs?.() ?? "Core logs unavailable.");
  const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
  resources.own("API context", api, value => value.dispose());
  const read = async (route: string) => {
    let response = await api.get(route);
    // The UI's view lease admits a cold project asynchronously. Retry only
    // its explicit retryable transition, never authorization or other failures.
    await expect.poll(async () => {
      const body = response.status() === 503 ? await response.json() : undefined;
      if (body?.code !== "project_runtime_unavailable" || body.retryable !== true) return true;
      response = await api.get(route);
      return response.ok();
    }, {timeout: 30_000, intervals: [250, 500, 1000]}).toBe(true);
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  {
    await test.step("Start without providers, uploads or conversations and pair the browser", async () => {
      expect(await read("providers")).toEqual([]);
      expect(await read("chat-sessions")).toEqual([]);
      await pairRealCoreBrowser(page, core, "First-time operator");
    });

    const providerName = "My first provider";
    let providerId = "";
    await test.step("Configure and discover the first provider through Settings", async () => {
      await openRuntimeSettings(page, "provider");
      const addProvider = page.getByRole("button", {name: "Add provider", exact: true});
      await addProvider.scrollIntoViewIfNeeded();
      await expect(addProvider).toBeInViewport({ratio: 1});
      await addProvider.click();
      const dialog = page.getByRole("dialog", {name: /^(Add model provider|Edit My first provider)$/});
      await dialog.getByRole("combobox", {name: "Provider type", exact: true}).selectOption("vllm");
      await dialog.getByRole("textbox", {name: "Endpoint", exact: true}).fill(`${stub.origin}/v1`);
      await dialog.getByRole("textbox", {name: "Profile name", exact: true}).fill(providerName);
      // vLLM discovery supplies IDs, not limits. Give this synthetic model
      // enough capacity for real Core instructions, tools and the upload.
      await dialog.getByText("Advanced provider options", {exact: true}).click();
      await dialog.getByRole("spinbutton", {name: "Context window (tokens)", exact: true}).fill("131072");
      await dialog.getByText("Advanced provider options", {exact: true}).click();
      await dialog.getByRole("button", {name: "Add provider", exact: true}).click();
      await expect(dialog).toBeHidden();
      const card = page.locator("article.provider-card").filter({has: page.getByRole("heading", {name: providerName, exact: true})});
      await expect(card.locator(".health-label")).toHaveClass(/healthy/, {timeout: 20_000});
      await card.getByRole("button", {name: `Edit ${providerName}`, exact: true}).click();
      await dialog.getByRole("combobox", {name: "Default model", exact: true}).selectOption("security-model");
      await dialog.getByRole("button", {name: "Save provider", exact: true}).click();
      await expect(dialog).toBeHidden();
      const providers = await read("providers");
      expect(providers).toHaveLength(1);
      expect(providers[0].metadata.default_model).toBe("security-model");
      expect(providers[0].metadata.options.context_window).toBe(131072);
      providerId = providers[0].id;
      const close = page.getByRole("button", {name: "Close setting", exact: true});
      if (await close.isVisible()) await close.click();
    });

    let projectId = "";
    await test.step("Create a project and its first chat through the UI", async () => {
      const switcher = await openProjectSwitcherFromShell(page);
      await switcher.getByRole("button", {name: "New project", exact: true}).click();
      await switcher.getByLabel("Name", {exact: true}).fill("My first analysis");
      await switcher.getByRole("button", {name: "Create in Host mode", exact: true}).click();
      await expect(switcher).toBeHidden();
      const project = (await read("engagements")).find((item: {name: string}) => item.name === "My first analysis");
      expect(project).toBeTruthy();
      projectId = project.id;
      await expect(page).toHaveURL(`${core.origin}/projects/${projectId}/workbench?view=chat`);
      expect(await read(`chat-sessions?engagement_id=${projectId}`)).toEqual([]);
      await page.getByRole("button", {name: "Start new chat", exact: true}).click();
      await clickAssistantSettings(page);
      const settings = page.getByRole("dialog", {name: "Assistant settings"});
      await settings.getByRole("combobox", {name: "Chat runtime"}).selectOption("provider");
      await settings.getByRole("combobox", {name: "Chat provider"}).selectOption(providerId);
      await expect(settings.getByRole("combobox", {name: "Chat model"})).toHaveValue("security-model");
      await settings.getByRole("button", {name: "Close assistant settings"}).click();
      await expect(page.locator(".chat-message.operator, .chat-message.assistant")).toHaveCount(0);
    });

    let sourceId = "";
    await test.step("Upload the first document and attach its extracted content", async () => {
      // Knowledge belongs to a project; a global read has no execution owner.
      expect(await read(`knowledge?engagement_id=${projectId}`)).toEqual([]);
      await page.getByRole("button", {name: "Attach image or file", exact: true}).click();
      const attachment = page.locator(".assistant-attachment-dialog");
      await attachment.getByRole("button", {name: "Choose knowledge source", exact: true}).click();
      await expect(attachment).toContainText("No sources yet. Upload a document above.");
      await attachment.locator("input[type=file]").setInputFiles({name: "first-review.txt", mimeType: "text/plain", buffer: Buffer.from(document)});
      await expect(attachment.locator("pre")).toContainText("FIRST-REVIEW-742");
      await attachment.getByRole("button", {name: "Attach this excerpt", exact: true}).click();
      await expect(page.getByRole("region", {name: "Selected context pack"})).toContainText("first-review.txt");
      const sources = await read(`knowledge?engagement_id=${projectId}`);
      expect(sources).toHaveLength(1);
      expect(sources[0].name).toBe("first-review.txt");
      sourceId = sources[0].id;
    });

    await test.step("Analyse the uploaded content and restore the saved result after reload", async () => {
      await page.getByRole("textbox", {name: "Message the analyst assistant", exact: true}).fill(prompt);
      await page.getByRole("button", {name: "Send message", exact: true}).click();
      const output = page.locator(".chat-message.assistant .assistant-markdown");
      await expect(output).toContainText(answer, {timeout: 30_000});
      expect(analysisRequests.length).toBeGreaterThan(0);
      expect(analysisRequests[0].model).toBe("security-model");
      const session = new URL(page.url()).searchParams.get("session");
      expect(session).toBeTruthy();
      const savedUrl = page.url();
      await page.reload();
      await expect(page).toHaveURL(savedUrl);
      await expect(page.locator(".chat-message.operator")).toContainText(prompt);
      await expect(output).toContainText(answer);
      const messages = await read(`chat/sessions/${session}/messages`);
      expect(messages.filter((message: {role: string}) => message.role === "user")).toHaveLength(1);
      expect(messages.filter((message: {role: string; content: string}) => message.role === "assistant" && message.content.includes(answer))).toHaveLength(1);
      expect((await read(`knowledge?engagement_id=${projectId}`)).map((source: {id: string}) => source.id)).toEqual([sourceId]);
      const build = await (await page.request.get(`${core.origin}/web-build.json`)).json();
      await testInfo.attach("first-user-journey", {body: JSON.stringify({origin: core.origin, build, projectId, session, sourceId, providerId}), contentType: "application/json"});
      await testInfo.attach("first-analysis-restored", {body: await page.screenshot(), contentType: "image/png"});
    });
  }
});
