import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("project graph edits, evidence, custom schema and recovery persist through real Core", async ({
  page,
  request,
  context,
}, info) => {
  const headers = { Authorization: "Bearer model-test-token" };
  const project = await (
    await request.post("/api/v1/engagements", {
      headers,
      data: { name: `Model ${info.project.name}` },
    })
  ).json();
  const base = `/api/v1/engagements/${project.id}/application-model`;
  const browser = (
    await (
      await request.get(`/api/v1/engagements/${project.id}/browser-workspace`, {
        headers,
      })
    ).json()
  ).sessions[0];
  const site = await context.newPage();
  const siteResponse = await site.goto("/fixture-site/protected");
  expect(siteResponse!.status()).toBe(403);
  await expect(site.getByRole("heading", { name: "Site A" })).toBeVisible();
  const siteUrl = site.url();
  await site.close();
  const scopeResponse = await request.put(
    `/api/v1/engagements/${project.id}/scope`,
    {
      headers,
      data: {
        allowed_urls: [siteUrl],
        allowed_ports: [Number(new URL(siteUrl).port)],
      },
    },
  );
  expect(scopeResponse.ok(), await scopeResponse.text()).toBeTruthy();
  const sync = await request.put(
    `/api/v1/browser-sessions/${browser.id}/tabs`,
    {
      headers,
      data: {
        expected_revision: browser.revision,
        tabs: [{ id: "fixture", url: siteUrl, title: "Fixture", position: 0 }],
        active_tab_id: "fixture",
        device_owner: "fixture",
      },
    },
  );
  expect(sync.ok()).toBeTruthy();
  const capture = await request.post(
    `/api/v1/browser-sessions/${browser.id}/traffic`,
    {
      headers,
      data: {
        tab_id: "fixture",
        method: "GET",
        url: siteUrl,
        status_code: siteResponse!.status(),
      },
    },
  );
  expect(capture.ok(), await capture.text()).toBeTruthy();
  const evidence = (
    await (await request.get(base + "/evidence", { headers })).json()
  ).evidence[0];
  expect(
    (await (await request.get(base + "/graph", { headers })).json()).objects,
  ).toEqual([]);
  const pairingOrigin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pairing = await (
    await request.post(`${pairingOrigin}/api/v1/auth/pairings`, {
      headers,
      data: { name: "Model acceptance" },
    })
  ).json();
  await page.goto(
    `/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`,
  );
  await page.getByLabel("Device name").fill("Model acceptance");
  await page.getByRole("button", { name: "Pair device", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /Nebula Core (ready|degraded)/ }),
  ).toBeVisible();
  await page.goto(
    `/projects/${project.id}/application-model?collection=old&state=retired`,
  );
  await expect(page.getByText(/older link/)).toBeVisible();
  await page.getByRole("button", { name: "Add object", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Object type", exact: true })
    .selectOption("Operation");
  await page
    .getByLabel("Object label", { exact: true })
    .fill("Login operation");
  await page
    .getByLabel("Authentication context", { exact: true })
    .fill("anonymous");
  await page.getByRole("button", { name: "Save object", exact: true }).click();
  await expect(page.getByText("Saved project revision 1.")).toBeVisible();
  const operationId = new URL(page.url()).searchParams.get("object");
  expect(operationId).toBeTruthy();
  await page.getByRole("button", { name: "Add object", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Object type", exact: true })
    .selectOption("Firewall");
  await page
    .getByLabel("Object label", { exact: true })
    .fill("Possible firewall");
  await page
    .getByText("Supporting / conflicting evidence (0)", { exact: true })
    .click();
  await page
    .getByRole("combobox", {
      name: `Evidence role ${evidence.id}`,
      exact: true,
    })
    .selectOption("supporting");
  await page
    .getByLabel("Reason", { exact: true })
    .fill("403 is consistent with multiple explanations.");
  await page.getByRole("button", { name: "Save object", exact: true }).click();
  await expect(page.getByText("Saved project revision 2.")).toBeVisible();
  const firewallId = new URL(page.url()).searchParams.get("object");
  await page.getByRole("button", { name: "Link objects", exact: true }).click();
  await page
    .getByRole("combobox", { name: "From", exact: true })
    .selectOption(operationId!);
  await page
    .getByRole("combobox", { name: "To", exact: true })
    .selectOption(firewallId!);
  await page
    .getByRole("combobox", { name: "Relationship", exact: true })
    .selectOption("protected_by");
  await page
    .getByLabel("Reason", { exact: true })
    .fill("Hypothesis; status code alone is not proof.");
  await page
    .getByRole("button", { name: "Save relationship", exact: true })
    .click();
  await expect(page.getByText("Saved project revision 3.")).toBeVisible();
  await page.reload();
  await expect(
    page
      .getByLabel("Model inspector")
      .getByRole("heading", { name: "protected_by", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Edit relationship", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Operator review", exact: true })
    .selectOption("accepted");
  // Another writer wins while the operator draft stays open.
  const competing = await request.post(base + "/transactions", {
    headers,
    data: {
      expected_revision: 3,
      idempotency_key: "other-writer",
      operations: [
        {
          op: "put_object",
          id: "competing",
          label: "Competing object",
          classification: { value: "Page" },
          authentication_context: "anonymous",
        },
      ],
    },
  });
  expect(competing.ok()).toBeTruthy();
  await page
    .getByRole("button", { name: "Save relationship", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("draft is retained");
  await page
    .getByRole("button", {
      name: "Apply reviewed draft to revision 4",
      exact: true,
    })
    .click();
  await expect(page.getByText("Saved project revision 5.")).toBeVisible();
  expect(
    (await (await request.get(base + "/graph", { headers })).json())
      .relationships[0].claim.status,
  ).toBe("hypothesized");
  await page
    .getByRole("button", { name: "Define project schema", exact: true })
    .click();
  await page.getByLabel("Name", { exact: true }).fill("TenantBoundary");
  await page
    .getByLabel("Definition", { exact: true })
    .fill("Declared tenant boundary");
  await page
    .getByLabel("Evidence example", { exact: true })
    .fill("Recorded policy declaration");
  await page
    .getByRole("button", { name: "Save definition", exact: true })
    .click();
  await expect(page.getByText("Saved project revision 6.")).toBeVisible();
  await page.getByRole("button", { name: "Add object", exact: true }).click();
  await expect(
    page
      .getByRole("combobox", { name: "Object type", exact: true })
      .locator('option[value="custom.TenantBoundary"]'),
  ).toHaveCount(1);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await context.setOffline(true);
  await context.setOffline(false);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Application model", exact: true }),
  ).toBeVisible();
  await page.goto(
    `/projects/${project.id}/application-model?object=${firewallId}`,
  );
  await page
    .getByLabel("Model inspector")
    .getByText("Evidence (1) and provenance", { exact: true })
    .click();
  await page
    .getByRole("button", { name: "supporting · browser traffic", exact: true })
    .click();
  await expect(page.locator(".am-evidence")).toContainText("403");
  const a11y = await new AxeBuilder({ page })
    .include(".application-model-page")
    .analyze();
  expect(a11y.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("application-model.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "Open source browser" }).click();
  await expect(page).toHaveURL(/browserExchange=/);
  await page.goBack();
  await expect(
    page.getByRole("button", { name: "Dismiss selection", exact: true }),
  ).toBeVisible();
  page.once("dialog", (d) => d.accept("Insufficient evidence"));
  await page
    .getByRole("button", { name: "Dismiss selection", exact: true })
    .click();
  await expect(page.getByText("Saved project revision 7.")).toBeVisible();
  expect(
    (await (await request.get(base + "/graph", { headers })).json())
      .relationships,
  ).toEqual([]);
  expect(
    (await (await request.get(base + "/evidence", { headers })).json()).evidence
      .length,
  ).toBeGreaterThan(0);
  // The workspace maintenance action must not consume file inspection space.
  await page.goto(`/projects/${project.id}/workbench?view=files`);
  await page.getByLabel("Choose workspace file").setInputFiles({
    name: "inspection.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(
      "File inspection remains visible while maintenance controls are collapsed.",
    ),
  });
  await page.getByRole("button", { name: /inspection.txt/ }).click();
  await expect(page.locator(".workspace-file-preview pre")).toContainText(
    "File inspection remains visible",
  );
  const summary = page.getByText("Reset scratch workspace…", { exact: true });
  await expect(summary).toBeVisible();
  await expect(summary.locator("..")).not.toHaveAttribute("open");
  await expect(
    page.getByRole("button", { name: "Reset workspace", exact: true }),
  ).not.toBeVisible();
  const panel = page.locator(".workspace-browser");
  const disclosure = page.locator(".workspace-reset-disclosure");
  const rect = await panel.boundingBox(),
    reset = await disclosure.boundingBox();
  expect(reset!.height).toBeLessThan(rect!.height * 0.2);
  if (info.project.use.hasTouch) await summary.tap();
  else {
    await summary.focus();
    await summary.press("Enter");
  }
  await expect(
    page.getByRole("button", { name: "Reset workspace", exact: true }),
  ).toBeVisible();
  await summary.click();
  await page.screenshot({
    path: info.outputPath("workspace-reset-compact.png"),
    fullPage: true,
  });
  await page.goto("/projects/linked-model-regression/workbench?view=files");
  await expect(page.locator(".workspace-reset-summary")).toContainText(
    "Linked folder",
  );
  await expect(page.locator(".workspace-reset-disclosure")).toHaveCount(0);
  await page.getByRole("button", { name: /linked-inspection.txt/ }).click();
  await expect(page.locator(".workspace-file-preview pre")).toContainText(
    "Linked folder inspection remains visible.",
  );
  await page.screenshot({
    path: info.outputPath("workspace-linked-compact.png"),
    fullPage: true,
  });
});
