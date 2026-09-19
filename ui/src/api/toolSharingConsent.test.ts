import { describe, expect, it, vi } from "vitest";
import { rememberToolSharing, sharesToolResultsAlways, type ToolSharingRuntime } from "./toolSharingConsent";
import type { ApiClient } from "./client";
import type { HarnessProfile, ProviderHealth } from "./types";

const provider = { id: "provider-1", name: "Cloud provider", revision: 3, permitsSensitiveData: true, autoShareToolResults: false } as ProviderHealth;
const harness = { id: "harness-1", name: "Codex", revision: 2, permitsSensitiveData: true, autoShareToolResults: false } as HarnessProfile;

describe("tool sharing consent", () => {
  it("recognizes standing consent on either runtime kind", () => {
    expect(sharesToolResultsAlways({ kind: "provider", profile: provider })).toBe(false);
    expect(sharesToolResultsAlways(undefined)).toBe(false);
    expect(sharesToolResultsAlways({
      kind: "harness",
      profile: { ...harness, autoShareToolResults: true },
    })).toBe(true);
  });

  it("stores consent on the runtime profile only when the box is checked", async () => {
    const saved = { ...provider, revision: 4, autoShareToolResults: true };
    const setProviderToolResultSharing = vi.fn().mockResolvedValue(saved);
    const api = { setProviderToolResultSharing } as unknown as ApiClient;
    const onSaved = vi.fn();
    const remember = rememberToolSharing(api, { kind: "provider", profile: provider }, onSaved);

    remember?.onConfirm(false);
    expect(setProviderToolResultSharing).not.toHaveBeenCalled();

    remember?.onConfirm(true);
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith({ kind: "provider", profile: saved }));
    expect(setProviderToolResultSharing).toHaveBeenCalledWith(provider, true);
  });

  it("stores harness consent through the harness profile", async () => {
    const saved = { ...harness, revision: 3, autoShareToolResults: true };
    const setHarnessToolResultSharing = vi.fn().mockResolvedValue(saved);
    const api = { setHarnessToolResultSharing } as unknown as ApiClient;
    const onSaved = vi.fn();
    const remember = rememberToolSharing(api, { kind: "harness", profile: harness } as ToolSharingRuntime, onSaved);

    remember?.onConfirm(true);
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith({ kind: "harness", profile: saved }));
  });

  it("keeps the turn usable when the preference cannot be stored", async () => {
    const setProviderToolResultSharing = vi.fn().mockRejectedValue(new Error("core offline"));
    const api = { setProviderToolResultSharing } as unknown as ApiClient;
    const onSaved = vi.fn();
    const remember = rememberToolSharing(api, { kind: "provider", profile: provider }, onSaved);

    remember?.onConfirm(true);
    await vi.waitFor(() => expect(setProviderToolResultSharing).toHaveBeenCalled());
    expect(onSaved).not.toHaveBeenCalled();
  });
});
