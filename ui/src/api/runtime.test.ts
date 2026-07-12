import { beforeEach, describe, expect, it } from "vitest";
import { resolveApiRuntime } from "./runtime";

describe("browser API runtime", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/workspace");
  });

  it("consumes a fragment token into memory and removes it from the URL", async () => {
    window.history.replaceState({}, "", "/workspace?mode=local#token=one-time-secret&view=overview");

    const runtime = await resolveApiRuntime();

    expect(runtime).toMatchObject({ mode: "browser", state: "ready", token: "one-time-secret" });
    expect(window.location.pathname).toBe("/workspace");
    expect(window.location.search).toBe("?mode=local");
    expect(window.location.hash).toBe("#view=overview");
    expect(window.location.href).not.toContain("one-time-secret");
  });

  it("keeps the consumed token only in module memory across repeated resolution", async () => {
    window.history.replaceState({}, "", "/workspace#token=one-time-secret");
    await resolveApiRuntime();

    const runtime = await resolveApiRuntime();

    expect(runtime.token).toBe("one-time-secret");
    expect(window.location.hash).toBe("");
    expect(localStorage.getItem("nebula.api.token")).toBeNull();
    expect(sessionStorage.getItem("nebula.api.token")).toBeNull();
  });
});
