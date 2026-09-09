import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("dense model retains selection, filters, focus and readable mobile landscape", async ({
  page,
  request,
}, info) => {
  const headers = { Authorization: "Bearer model-test-token" };
  const project = await (
    await request.post("/api/v1/engagements", {
      headers,
      data: { name: "Dense model" },
    })
  ).json();
  const objects = Array.from({ length: 30 }, (_, i) => ({
    op: "put_object",
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
    type: "links_to",
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
  ).toBeVisible();
  await page.goto(
    `/projects/${project.id}/application-model?object=page-1&depth=2`,
  );
  const outline = page.getByRole("complementary", { name: "Object outline" });
  const search = page.getByRole("textbox", { name: "Search objects" });
  await search.fill("Page 1");
  await expect(page).toHaveURL(/q=Page\+1/);
  await expect(outline.getByRole("button")).toHaveCount(11);
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
  await page.screenshot({
    path: info.outputPath("dense-model.png"),
    fullPage: true,
  });
});
