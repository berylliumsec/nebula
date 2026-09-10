import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("dense model retains selection, filters, focus and readable mobile landscape", async ({
  page,
  request,
}, info) => {
  const graphTransfers: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith("/application-model/graph"))
      graphTransfers.push(request.url());
  });
  const headers = { Authorization: "Bearer model-test-token" };
  const project = await (
    await request.post("/api/v1/engagements", {
      headers,
      data: { name: "Dense model" },
    })
  ).json();
  const objects = Array.from({ length: 30 }, (_, i) => ({
    op: "put_object",
    properties: { purpose: { value: "Identify this step's responsibility in the application workflow." } },
    id: `page-${i}`,
    label:
      i === 1
        ? "Page 1 — " + "long application context ".repeat(10)
        : `Page ${i}`,
    classification: {
      value: "Page",
      status: i % 3 === 0 ? "disputed" : "hypothesized",
    },
    authentication_context: "anonymous",
  }));
  const relationships = Array.from({ length: 29 }, (_, i) => ({
    op: "put_relationship",
    id: `edge-${i}`,
    type: "contains",
    source: `page-${i}`,
    target: `page-${i + 1}`,
    claim: { value: true, status: i % 3 === 0 ? "disputed" : "hypothesized" },
  }));
  const result = await request.post(
    `/api/v1/engagements/${project.id}/application-model/transactions`,
    {
      headers,
      data: {
        expected_revision: 0,
        idempotency_key: "dense",
        operations: [...objects, ...relationships],
      },
    },
  );
  expect(result.ok(), await result.text()).toBeTruthy();
  const bounded = await (
    await request.get(
      `/api/v1/engagements/${project.id}/application-model/view?category=Structure&offset=20`,
      { headers },
    )
  ).json();
  expect(bounded.outline_objects).toHaveLength(10);
  expect(bounded.object_total).toBe(30);
  const origin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pairing = await (
    await request.post(`${origin}/api/v1/auth/pairings`, {
      headers,
      data: { name: "Dense test" },
    })
  ).json();
  await page.goto(
    `/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`,
  );
  await page.getByLabel("Device name").fill("Dense test");
  await page.getByRole("button", { name: "Pair device", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /Nebula Core (ready|degraded)/ }),
  ).toBeVisible({ timeout: 20_000 });
  await page.goto(`/projects/${project.id}/application-model`);
  await expect(
    page.getByRole("heading", { name: "Explore the model" }),
  ).toBeVisible();
  await expect(page.locator(".am-outline .am-object")).toHaveCount(0);
  await page.locator(".am-category summary").click();
  await expect(page.locator(".am-outline .am-object")).toHaveCount(20);
  await page.getByRole("button", { name: "Next Structure objects" }).click();
  await expect(page.locator(".am-outline .am-object")).toHaveCount(10);
  await page.getByRole("button", { name: "Link objects", exact: true }).click();
  const endpointSearch = page.getByLabel("Search objects to link");
  await endpointSearch.fill("Page 0");
  await expect(
    page.locator('select option[value="page-0"]').first(),
  ).toBeAttached();
  await page
    .getByRole("combobox", { name: "From", exact: true })
    .selectOption("page-0");
  await endpointSearch.fill("Page 29");
  await expect(
    page.locator('select option[value="page-29"]').first(),
  ).toBeAttached();
  await page
    .getByRole("combobox", { name: "To", exact: true })
    .selectOption("page-29");
  await page
    .getByRole("combobox", { name: "Relationship", exact: true })
    .selectOption("contains");
  await page
    .getByRole("button", { name: "Save relationship", exact: true })
    .click();
  await expect(page.getByText("Revision 2", { exact: true })).toBeVisible();
  await page.goto(
    `/projects/${project.id}/application-model?object=page-1&depth=2`,
  );
  const toggle = page.getByRole("button", { name: "Show map", exact: true });
  await expect(page.locator(".am-map")).toBeVisible();
  if (page.viewportSize()!.width <= 700) {
    await expect(toggle).toBeVisible();
    await toggle.click();
  }
  const edges = page.locator(".am-edges line");
  await expect(page.locator(".am-edges")).toBeVisible();
  const expand = page.getByRole("button", {
    name: "Expand relationships",
    exact: true,
  });
  await expand.click();
  const fullscreen = page.getByRole("dialog", {
    name: "Project relationships",
  });
  await expect(fullscreen).toBeVisible();
  const restore = page.getByRole("button", {
    name: "Restore relationships",
    exact: true,
  });
  await expect(restore).toBeFocused();
  const fullscreenBox = await fullscreen.boundingBox();
  expect(fullscreenBox!.x).toBe(0);
  expect(fullscreenBox!.y).toBe(0);
  expect(fullscreenBox!.width).toBe(page.viewportSize()!.width);
  expect(
    Math.abs(fullscreenBox!.height - page.viewportSize()!.height),
  ).toBeLessThan(1);
  const restoreBox = await restore.boundingBox();
  expect(restoreBox!.width).toBeGreaterThanOrEqual(44);
  expect(restoreBox!.height).toBeGreaterThanOrEqual(44);
  // The dialog filling the viewport is insufficient: the graph's actual cards
  // must reflow into that space, including an ultrawide viewport like the report.
  const originalViewport = page.viewportSize()!;
  const assertGraphFillsWidth = async () => {
    await expect
      .poll(async () =>
        fullscreen.locator(".am-graph-scroll").evaluate((el) => {
          const box = el.getBoundingClientRect();
          const cards = [...el.querySelectorAll(".am-node")].map((n) =>
            n.getBoundingClientRect(),
          );
          return (
            (Math.max(...cards.map((n) => n.right)) -
              Math.min(...cards.map((n) => n.left))) /
            box.width
          );
        }),
      )
      .toBeGreaterThan(0.7);
  };
  if (originalViewport.width >= 1024) {
    await assertGraphFillsWidth();
    await page.setViewportSize({ width: 2800, height: 1400 });
    await assertGraphFillsWidth();
    await expect
      .poll(async () =>
        fullscreen
          .locator(".am-node")
          .evaluateAll(
            (nodes) =>
              new Set(
                nodes.map((n) => Math.round(n.getBoundingClientRect().left)),
              ).size,
          ),
      )
      .toBeGreaterThan(3);
    await expect
      .poll(async () =>
        fullscreen.locator(".am-graph-scroll").evaluate((el) => {
          const box = el.getBoundingClientRect();
          const cards = [...el.querySelectorAll(".am-node")].map((n) =>
            n.getBoundingClientRect(),
          );
          return (
            (Math.max(...cards.map((n) => n.bottom)) - box.top) / box.height
          );
        }),
      )
      .toBeGreaterThan(0.7);
    await page.screenshot({
      path: info.outputPath("relationships-ultrawide.png"),
    });
    await page.setViewportSize(originalViewport);
    await assertGraphFillsWidth();
  }
  expect(
    (await new AxeBuilder({ page }).include(".am-fullscreen").analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: info.outputPath("relationships-fullscreen.png"),
  });
  await restore.press("Shift+Tab");
  expect(
    await fullscreen.evaluate((el) => el.contains(document.activeElement)),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(fullscreen).toHaveCount(0);
  await expect(expand).toBeFocused();
  await page
    .getByRole("button", { name: "Expand objects", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Object outline" }),
  ).toBeVisible();
  await page.locator(".am-outline .am-object.selected").click();
  await expect(
    page.getByRole("dialog", { name: "Model inspector" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Restore model inspector", exact: true })
    .click();
  await expect(edges.first()).toBeAttached();
  await expect(edges.first()).toHaveCSS("stroke-width", "1.5px");
  await expect
    .poll(async () =>
      page.locator(".am-graph-canvas").evaluate((canvas) => {
        const root = canvas.getBoundingClientRect();
        return [...canvas.querySelectorAll("line[data-target]")].every(
          (line) => {
            const node = canvas.querySelector(
              `[data-node-id="${line.getAttribute("data-target")}"]`,
            )!;
            const box = node.getBoundingClientRect();
            const x = root.left + Number(line.getAttribute("x2")),
              y = root.top + Number(line.getAttribute("y2"));
            return (
              x < box.left || x > box.right || y < box.top || y > box.bottom
            );
          },
        );
      }),
    )
    .toBe(true);
  await page.screenshot({
    path: info.outputPath("visible-arrows.png"),
    fullPage: true,
  });
  const outline = page.getByRole("complementary", { name: "Object outline" });
  const search = page.getByRole("textbox", { name: "Search objects" });
  await search.fill("Page 1");
  await expect(page).toHaveURL(/q=Page\+1/);
  await expect(outline.locator(".am-object")).toHaveCount(11);
  await page.reload();
  await expect(search).toHaveValue("Page 1");
  await page.getByRole("button", { name: "Edit object", exact: true }).click();
  const label = page.getByRole("textbox", {
    name: "Object label",
    exact: true,
  });
  await expect(label).toBeFocused();
  await label.press("End");
  await expect(label).toHaveValue(objects[1].label);
  await label.fill("Draft retained across full screen");
  await page
    .getByRole("button", { name: "Expand model inspector", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Model inspector" }),
  ).toBeVisible();
  await expect(label).toHaveValue("Draft retained across full screen");
  await page
    .getByRole("button", { name: "Restore model inspector", exact: true })
    .click();
  await expect(label).toHaveValue("Draft retained across full screen");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "Model inspector" }),
  ).toContainText(objects[1].label);
  const violations = (
    await new AxeBuilder({ page }).include(".application-model-page").analyze()
  ).violations;
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  if (info.project.use.isMobile) {
    await page.setViewportSize({ width: 844, height: 390 });
    await search.scrollIntoViewIfNeeded();
    await search.focus();
    const bounds = await search.boundingBox();
    expect(bounds!.width).toBeGreaterThan(44);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
    ).toBe(true);
  }
  expect(graphTransfers).toEqual([]);
  await page.screenshot({
    path: info.outputPath("dense-model.png"),
    fullPage: true,
  });
});
