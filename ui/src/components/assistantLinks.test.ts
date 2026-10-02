import { describe, expect, it } from "vitest";
import { workspaceLink } from "./assistantLinks";

describe("assistant workspace links", () => {
  const root = "/home/agent/nebula";

  it("resolves project paths and line references", () => {
    expect(workspaceLink("ui/src/App.tsx#L42", root)).toEqual({ path: "ui/src/App.tsx", line: 42 });
    expect(workspaceLink("/home/agent/nebula/ui/src/App.tsx:12", root)).toEqual({ path: "ui/src/App.tsx", line: 12 });
    expect(workspaceLink("/workspace/ui/src/App.tsx", root)).toEqual({ path: "ui/src/App.tsx" });
  });

  it("rejects links outside the workspace and ambiguous paths", () => {
    for (const href of ["/home/agent/other/file.ts", "../other/file.ts", "/workspace/../secret", "javascript:alert(1)", "https://example.test/file.ts", "//example.test/file", "a/%2e%2e/secret", "a%2fb%252fsecret", "a?download=1", "a#other"]) {
      expect(workspaceLink(href, root), href).toBeUndefined();
    }
  });
});
