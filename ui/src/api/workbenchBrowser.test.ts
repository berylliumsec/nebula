import { describe, expect, it } from "vitest";
import { normalizeBrowserInput } from "./workbenchBrowser";

describe("Workbench browser address normalization", () => {
  it("keeps explicit HTTP and HTTPS addresses", () => {
    expect(normalizeBrowserInput("https://example.test/path")).toBe("https://example.test/path");
    expect(normalizeBrowserInput("http://127.0.0.1:8080/")).toBe("http://127.0.0.1:8080/");
  });

  it("promotes hostnames to HTTPS", () => {
    expect(normalizeBrowserInput("example.test/login")).toBe("https://example.test/login");
    expect(normalizeBrowserInput("localhost:3000")).toBe("https://localhost:3000/");
  });

  it("uses DuckDuckGo for search terms and rejects blanks", () => {
    expect(normalizeBrowserInput("nebula security workbench")).toBe("https://duckduckgo.com/?q=nebula%20security%20workbench");
    expect(() => normalizeBrowserInput("   ")).toThrow("Enter an address or search terms.");
    expect(() => normalizeBrowserInput("file:///etc/passwd")).toThrow("only HTTP and HTTPS");
  });
});
