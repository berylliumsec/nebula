import { describe, expect, it } from "vitest";
import { parseAllowedDomains, parseAllowedPorts } from "./EngagementPolicySettings";

describe("parseAllowedPorts", () => {
  it("expands ranges and combines them with individual ports", () => {
    expect(parseAllowedPorts("0-3, 2, 80\n443")).toEqual([0, 1, 2, 3, 80, 443]);
  });

  it("rejects reversed, malformed, and out-of-bounds ranges", () => {
    expect(parseAllowedPorts("400-0")).toBeUndefined();
    expect(parseAllowedPorts("80-http")).toBeUndefined();
    expect(parseAllowedPorts("65535-65536")).toBeUndefined();
  });
});

describe("parseAllowedDomains", () => {
  it("treats root HTTP(S) URLs and hostnames as the same domain", () => {
    expect(parseAllowedDomains("https://www.Google.com/\nwww.google.com.")).toEqual(["www.google.com"]);
  });

  it("keeps path- and port-specific entries in URL-only scope", () => {
    expect(parseAllowedDomains("https://example.com/admin")).toBeUndefined();
    expect(parseAllowedDomains("https://example.com:8443/")).toBeUndefined();
  });
});
