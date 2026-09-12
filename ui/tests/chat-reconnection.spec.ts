import {expect, test} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const vendor of ["grok_acp", "codex_app_server"]) {
  test(`${vendor} chat reconnects without resubmitting work`, async ({page, request}, info) => {
    const origin = String(info.project.use.baseURL);
    const local = `http://127.0.0.1:${new URL(origin).port}`;
    const headers = {Authorization: "Bearer reconnect-test-token"};
    // Drop only the viewer's initial response after actual Core text arrives.
    // Core's producer and every replay response remain real.
    await page.addInitScript(() => {
      const original = globalThis.fetch;
      let dropped = false;
      globalThis.fetch = async (...args) => {
        const response = await original(...args);
        if (dropped || !response.url.includes("/chat/completions") || !response.body) return response;
        const reader = response.body.getReader();
        let seen = "";
        return new Response(new ReadableStream({async pull(controller) {
          if (seen.includes("Before disconnect.")) {
            dropped = true;
            void reader.cancel();
            controller.error(new TypeError("Injected viewer disconnect"));
            return;
          }
          const {value, done} = await reader.read();
          if (done) { controller.close(); return; }
          seen += new TextDecoder().decode(value);
          controller.enqueue(value);
        }, cancel() { return reader.cancel(); }}), {status: response.status, headers: response.headers});
      };
    });
    const replayRequests: string[] = [];
    page.on("request", value => { if (value.method() === "GET" && /chat\/turns\/.*\/events\?after=/.test(value.url())) replayRequests.push(value.url()); });
    const sockets: {url: string; disconnect: () => void}[] = [];
    await page.routeWebSocket("**/harness-turns/*/events/ws*", socket => {
      const server = socket.connectToServer();
      sockets.push({url: socket.url(), disconnect: () => {socket.close({code: 1012, reason: "Injected viewer disconnect"}); server.close();}});
    });
    const created = await request.post(`${local}/fixture/chat/${vendor}`, {headers});
    expect(created.ok(), await created.text()).toBe(true);
    const {id} = await created.json();
    const pairing = await (await request.post(`${local}/api/v1/auth/pairings`, {headers, data: {name: "Reconnect test"}})).json();
    await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Reconnect test");
    await page.getByRole("button", {name: "Pair device", exact: true}).click();
    await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20_000});
    await page.goto(`/projects/reconnect-project/workbench?view=chat&session=${id}`);
    const composer = page.locator(".chat-composer textarea");
    await composer.fill("Run the connection check once.");
    await page.getByRole("button", {name: "Send message", exact: true}).click();
    await expect(page.locator(".chat-message.assistant")).toContainText("Before disconnect.", {timeout: 20_000});
    await expect.poll(() => replayRequests.length).toBeGreaterThan(0);
    // Reload attaches the active turn's real WebSocket follower. Close that
    // transport once; the production client must advance its replay cursor.
    await page.reload();
    await expect(page.locator(".chat-message.assistant")).toContainText("Before disconnect.");
    await expect.poll(() => sockets.length).toBe(1);
    sockets[0].disconnect();
    await expect(page.getByText("Connection lost. Reconnecting to the existing turn…", {exact: true})).toBeVisible();
    await page.context().setOffline(true);
    await page.context().setOffline(false);
    await page.evaluate(() => globalThis.dispatchEvent(new Event("online")));
    await expect.poll(() => sockets.length).toBeGreaterThan(1);
    expect(Number(new URL(sockets[1].url).searchParams.get("after"))).toBeGreaterThan(0);
    await request.post(`${local}/fixture/release/${id}`, {headers});
    await expect(page.locator(".chat-message.assistant .assistant-markdown")).toHaveText("Before disconnect. After reconnect.", {timeout: 55_000});
    expect(await (await request.get(`${local}/fixture/executions/${id}`, {headers})).json()).toEqual({executions: 1});
    await page.reload();
    await expect(page.locator(".chat-message.assistant .assistant-markdown")).toHaveText("Before disconnect. After reconnect.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({page}).include(".chat-message.assistant").analyze()).violations).toEqual([]);
    await page.screenshot({path: info.outputPath(`${vendor}-reconnected.png`)});
  });
}
