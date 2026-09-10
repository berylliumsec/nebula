import { expect, it } from "vitest";
import { buildAlignment } from "./buildIdentity";

it("compares full and abbreviated build identities", () => {
  expect(buildAlignment(["abcdef1", "abcdef123456"])).toBe("matching");
  expect(buildAlignment(["abcdef1", "bbbbbbb", "abcdef123456"])).toBe("mismatch");
});
it("does not call dirty or missing builds verified", () => {
  expect(buildAlignment(["abcdef1", "abcdef1"], true)).toBe("unverified");
  expect(buildAlignment(["abcdef1", "unknown"])).toBe("unverified");
  expect(buildAlignment(["development", undefined])).toBe("unverified");
});
