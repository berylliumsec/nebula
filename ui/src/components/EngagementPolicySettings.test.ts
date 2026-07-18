import { describe, expect, it } from "vitest";
import { parseAllowedPorts } from "./EngagementPolicySettings";

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
