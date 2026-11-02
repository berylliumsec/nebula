import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {CredentialStatus, ProviderHealth} from "../api/types";
import {attachButlerProviderCredential} from "./butlerProviderCredentialAction";

const providerId = "b96e898d-207f-4943-846e-c00f4415fc26";
const reference = `session:${"a".repeat(32)}`;
const provider = {id: providerId, name: "Fixture model", providerType: "vllm", revision: 3,
  credentialRef: undefined, openrouterManagementSecretRef: undefined} as ProviderHealth;
const action = {kind: "provider_attach_credential" as const, projectId: "project", providerId,
  name: provider.name, expectedRevision: provider.revision, reference, slot: "inference" as const};
const status: CredentialStatus = {reference, persistence: "session", available: true, state: "available"};

describe("Butler provider credential handoff", () => {
  it("cancels before any provider mutation", async () => {
    const api = {getProvider: vi.fn().mockResolvedValue(provider), credentialStatus: vi.fn().mockResolvedValue(status),
      attachProviderCredential: vi.fn()} as unknown as ApiClient;
    expect(await attachButlerProviderCredential(api, action, () => "project", async () => false))
      .toContain("No credential was attached");
    expect(api.attachProviderCredential).not.toHaveBeenCalled();
  });

  it("refuses stale provider identity or unavailable credentials", async () => {
    const api = {getProvider: vi.fn().mockResolvedValue({...provider, revision: 4}),
      credentialStatus: vi.fn().mockResolvedValue(status), attachProviderCredential: vi.fn()} as unknown as ApiClient;
    await expect(attachButlerProviderCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("provider changed");
    expect(api.credentialStatus).not.toHaveBeenCalled();
    expect(api.attachProviderCredential).not.toHaveBeenCalled();
    vi.mocked(api.getProvider).mockResolvedValue({...provider, id: "other-provider"});
    await expect(attachButlerProviderCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("provider changed");
    vi.mocked(api.getProvider).mockResolvedValue(provider);
    vi.mocked(api.credentialStatus).mockResolvedValue({...status, available: false, state: "missing"});
    await expect(attachButlerProviderCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("credential is unavailable");
    expect(api.attachProviderCredential).not.toHaveBeenCalled();
  });

  it("rechecks after confirmation and reports Core's saved reference", async () => {
    const saved = {...provider, revision: 4, credentialRef: reference};
    const api = {getProvider: vi.fn().mockResolvedValueOnce(provider).mockResolvedValueOnce(provider).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), attachProviderCredential: vi.fn().mockResolvedValue(saved)} as unknown as ApiClient;
    const message = await attachButlerProviderCredential(api, action, () => "project", async () => true);
    expect(api.attachProviderCredential).toHaveBeenCalledWith(providerId, 3, reference, "inference");
    expect(api.credentialStatus).toHaveBeenCalledTimes(3);
    expect(message).toContain("Core confirmed the reference and credential availability");
  });

  it("reads the exact provider after a lost write response instead of replaying", async () => {
    const saved = {...provider, revision: 4, credentialRef: reference};
    const api = {getProvider: vi.fn().mockResolvedValueOnce(provider).mockResolvedValueOnce(provider).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), attachProviderCredential: vi.fn().mockRejectedValue(new Error("lost response"))} as unknown as ApiClient;
    expect(await attachButlerProviderCredential(api, action, () => "project", async () => true))
      .toContain("Core confirmed the reference");
    expect(api.attachProviderCredential).toHaveBeenCalledTimes(1);
  });

  it("attaches an OpenRouter management reference without replacing inference", async () => {
    const openrouter = {...provider, providerType: "openrouter", credentialRef: "env:INFERENCE_KEY"};
    const saved = {...openrouter, revision: 4, openrouterManagementSecretRef: reference};
    const api = {getProvider: vi.fn().mockResolvedValueOnce(openrouter).mockResolvedValueOnce(openrouter).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), attachProviderCredential: vi.fn().mockResolvedValue(saved)} as unknown as ApiClient;
    const message = await attachButlerProviderCredential(api, {...action, slot: "management"}, () => "project", async () => true);
    expect(api.attachProviderCredential).toHaveBeenCalledWith(providerId, 3, reference, "management");
    expect(message).toContain("OpenRouter management");
    expect(saved.credentialRef).toBe("env:INFERENCE_KEY");
  });

  it("refuses to reuse an inference reference as a management key", async () => {
    const openrouter = {...provider, providerType: "openrouter", credentialRef: reference};
    const api = {getProvider: vi.fn().mockResolvedValue(openrouter), credentialStatus: vi.fn(),
      attachProviderCredential: vi.fn()} as unknown as ApiClient;
    await expect(attachButlerProviderCredential(api, {...action, slot: "management"}, () => "project", async () => true))
      .rejects.toThrow("separate references");
    expect(api.attachProviderCredential).not.toHaveBeenCalled();
  });
});
