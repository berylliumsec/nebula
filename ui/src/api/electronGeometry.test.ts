import { describe, expect, it } from "vitest";
// @ts-expect-error Electron's main-process module is intentionally plain JavaScript.
import { pageBounds } from "../../electron/geometry.mjs";

const layout = {
  viewport: { x: 0, y: 0, width: 1440, height: 800 },
  panel: { x: 80, y: 120, width: 900, height: 620 },
  controls: { x: 80, y: 120, width: 900, height: 145 },
  page: { x: 80, y: 265, width: 900, height: 475 },
  dialogOpen: false,
};

describe("Electron browser placement", () => {
  it("keeps the page below controls and within the Browser panel", () => {
    expect(pageBounds(layout, { x: 0, y: 0, width: 1440, height: 800 })).toEqual({
      x: 80, y: 265, width: 900, height: 475,
    });
  });

  it("maps 90% renderer zoom to content bounds", () => {
    expect(pageBounds(layout, { x: 0, y: 0, width: 1296, height: 720 })).toEqual({
      x: 72, y: 239, width: 810, height: 427,
    });
  });

  it("hides a page that reaches the URL controls", () => {
    expect(pageBounds({ ...layout, page: { ...layout.page, y: 250 } },
      { x: 0, y: 0, width: 1440, height: 800 })).toBeNull();
  });

  it("hides the native page while a dialog or invalid layout is present", () => {
    expect(pageBounds({ ...layout, dialogOpen: true },
      { x: 0, y: 0, width: 1440, height: 800 })).toBeNull();
    expect(pageBounds({ ...layout, panel: { ...layout.panel, x: -10 } },
      { x: 0, y: 0, width: 1440, height: 800 })).toBeNull();
  });
});
