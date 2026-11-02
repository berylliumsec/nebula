import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {CredentialStatus, HarnessProfile} from "../api/types";
import {attachButlerHarnessCredential} from "./butlerHarnessCredentialAction";

const harnessId = "b96e898d-207f-4943-846e-c00f4415fc26";
const reference = `session:${"b".repeat(32)}`;
const harness = {id: harnessId, name: "Fixture harness", kind: "codex_app_server",
  connectionMode: "spawn", authMode: "existing_session", revision: 3,
  nativeCapabilities: {shell: false}} as HarnessProfile;
const action = {kind: "harness_attach_credential" as const, projectId: "project", harnessId,
  name: harness.name, expectedRevision: harness.revision, reference, authMode: "secret_ref" as const};
const status: CredentialStatus = {reference, persistence: "session", available: true, state: "available"};

describe("Butler harness credential handoff", () => {
  it("cancels before any harness mutation", async () => {
    const api = {getHarness: vi.fn().mockResolvedValue(harness), credentialStatus: vi.fn().mockResolvedValue(status),
      updateHarness: vi.fn()} as unknown as ApiClient;
    expect(await attachButlerHarnessCredential(api, action, () => "project", async () => false))
      .toContain("No credential was attached");
    expect(api.updateHarness).not.toHaveBeenCalled();
  });

  it("refuses a stale harness or unavailable credential", async () => {
    const api = {getHarness: vi.fn().mockResolvedValue({...harness, revision: 4}),
      credentialStatus: vi.fn().mockResolvedValue(status), updateHarness: vi.fn()} as unknown as ApiClient;
    await expect(attachButlerHarnessCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("harness changed");
    expect(api.updateHarness).not.toHaveBeenCalled();
    vi.mocked(api.getHarness).mockResolvedValue(harness);
    vi.mocked(api.credentialStatus).mockResolvedValue({...status, available: false, state: "missing"});
    await expect(attachButlerHarnessCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("credential is unavailable");
    expect(api.updateHarness).not.toHaveBeenCalled();
  });

  it("patches only authentication fields and reads the saved harness back", async () => {
    const saved = {...harness, revision: 4, authMode: "secret_ref", secretRef: reference};
    const api = {getHarness: vi.fn().mockResolvedValueOnce(harness).mockResolvedValueOnce(harness).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), updateHarness: vi.fn().mockResolvedValue(saved)} as unknown as ApiClient;
    const message = await attachButlerHarnessCredential(api, action, () => "project", async () => true);
    expect(api.updateHarness).toHaveBeenCalledWith(harnessId, {auth_mode: "secret_ref", secret_ref: reference}, 3);
    expect(api.credentialStatus).toHaveBeenCalledTimes(3);
    expect(message).toContain("Core confirmed the harness authentication mode and credential availability");
  });

  it("does not replay a write after its response is lost", async () => {
    const saved = {...harness, revision: 4, authMode: "secret_ref", secretRef: reference};
    const api = {getHarness: vi.fn().mockResolvedValueOnce(harness).mockResolvedValueOnce(harness).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), updateHarness: vi.fn().mockRejectedValue(new Error("lost response"))} as unknown as ApiClient;
    expect(await attachButlerHarnessCredential(api, action, () => "project", async () => true))
      .toContain("Core confirmed the harness authentication mode");
    expect(api.updateHarness).toHaveBeenCalledTimes(1);
  });

  it("rejects Grok cached login and a mismatched endpoint mode", async () => {
    const api = {getHarness: vi.fn().mockResolvedValue({...harness, kind: "grok_acp"}),
      credentialStatus: vi.fn(), updateHarness: vi.fn()} as unknown as ApiClient;
    await expect(attachButlerHarnessCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("existing vendor login");
    vi.mocked(api.getHarness).mockResolvedValue({...harness, connectionMode: "endpoint"});
    await expect(attachButlerHarnessCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("authentication mode changed");
    expect(api.updateHarness).not.toHaveBeenCalled();
  });
});
