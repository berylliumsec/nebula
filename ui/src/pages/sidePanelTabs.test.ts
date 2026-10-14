import {describe, expect, it} from "vitest";
import {appendSideToolTab, removeSideToolTab, sideToolLabel, sideToolTabs} from "./sidePanelTabs";

describe("side panel tabs", () => {
  it("adds another tab for each menu click and retains stable labels", () => {
    const params = new URLSearchParams("view=chat&sideChat=side-one");
    appendSideToolTab(params, "terminal", "term-one");
    appendSideToolTab(params, "terminal", "term-two");
    appendSideToolTab(params, "code", "code-one");
    expect(sideToolTabs(params).map(sideToolLabel)).toEqual(["Terminal 1", "Terminal 2", "Code 1"]);
    expect(params.get("panelTab")).toBe("code-one");
    removeSideToolTab(params, "term-one");
    expect(sideToolTabs(params).map(sideToolLabel)).toEqual(["Terminal 2", "Code 1"]);
    expect(params.get("sideChat")).toBe("side-one");
  });

  it("selects a survivor and clears per-tab file state on close", () => {
    const params = new URLSearchParams();
    appendSideToolTab(params, "code", "code-one");
    params.set("panelFile.code-one", "notes/review.md");
    appendSideToolTab(params, "browser", "browser-one");
    removeSideToolTab(params, "browser-one");
    expect(params.get("panelTab")).toBe("code-one");
    removeSideToolTab(params, "code-one");
    expect(params.has("panelTab")).toBe(false);
    expect(params.has("panelFile.code-one")).toBe(false);
  });

  it("ignores unknown, duplicate and malformed URL records", () => {
    const params = new URLSearchParams("panel=terminal:1:one&panel=terminal:2:one&panel=unknown:1:two&panel=code:0:three&panel=code:1:four%3Aextra");
    expect(sideToolTabs(params)).toEqual([{kind: "terminal", number: 1, id: "one"}]);
  });
});
