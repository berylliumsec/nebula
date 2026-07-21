import { beforeEach, describe, expect, it, vi } from "vitest";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));

vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import { checkForUpdate, getReleaseInfo, installAvailableUpdate, restartApplication } from "./updater";

describe("desktop updater boundary", () => {
  beforeEach(() => {
    invoke.mockReset();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("does not expose an updater in a browser client", async () => {
    expect(await getReleaseInfo()).toMatchObject({ distribution: "development", updaterEnabled: false });
    expect(await checkForUpdate()).toBeUndefined();
    expect(await installAvailableUpdate()).toBe(false);
    await expect(restartApplication()).resolves.toBeUndefined();
    expect(invoke).not.toHaveBeenCalled();
  });

  it("uses the signed native updater only inside Tauri", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    invoke
      .mockResolvedValueOnce({ version: "3.0.0", distribution: "direct", updaterEnabled: true })
      .mockResolvedValueOnce({ currentVersion: "3.0.0", version: "3.0.1" })
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(undefined);

    expect(await getReleaseInfo()).toMatchObject({ distribution: "direct", updaterEnabled: true });
    expect(await checkForUpdate()).toMatchObject({ version: "3.0.1" });
    expect(await installAvailableUpdate()).toBe(true);
    await expect(restartApplication()).resolves.toBeUndefined();
    expect(invoke.mock.calls.map(([command]) => command)).toEqual([
      "release_info",
      "check_for_update",
      "install_available_update",
      "restart_application",
    ]);
  });
});
