import { describe, expect, it } from "vitest";
import { credentialName } from "./SavedCredentialsSettings";

describe("saved password names", () => {
  it("turns a typed label into a placeholder name", () => {
    expect(credentialName("Mac Login")).toBe("mac-login");
    expect(credentialName("  sudo / admin!! ")).toBe("sudo-admin-");
    expect(credentialName("-lead")).toBe("lead");
    expect(credentialName("x".repeat(80))).toHaveLength(64);
  });
});
