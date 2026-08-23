import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserAuthorizationRecovery, browserSessionIsAuthorized, browserSessionRequiresRelaunch, resolveApiRuntime } from "./runtime";

describe("browser API runtime", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/workspace");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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

  it("requires relaunch after a production browser reload but not during development", () => {
    expect(browserSessionRequiresRelaunch(undefined, false)).toBe(true);
    expect(browserSessionRequiresRelaunch(undefined, true)).toBe(false);
    expect(browserSessionRequiresRelaunch("one-time-secret", false)).toBe(false);
  });

  it("directs LAN browsers to pairing while reserving relaunch for loopback sessions", () => {
    expect(browserAuthorizationRecovery("192.168.1.155")).toBe("pair");
    expect(browserAuthorizationRecovery("nebula.lan")).toBe("pair");
    expect(browserAuthorizationRecovery("127.0.0.1")).toBe("relaunch");
    expect(browserAuthorizationRecovery("[::1]")).toBe("relaunch");
    expect(browserAuthorizationRecovery("localhost")).toBe("relaunch");
  });

  it("accepts a browser when Core confirms cookie or no-auth access", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const authorized = await browserSessionIsAuthorized("http://nebula.test");

    expect(authorized).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/health$/),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("rejects a browser when Core rejects the session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 401 })));

    const authorized = await browserSessionIsAuthorized("http://nebula.test/api/v1");

    expect(authorized).toBe(false);
  });
});
