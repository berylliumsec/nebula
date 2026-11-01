import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {CredentialStatus, McpServerProfile} from "../api/types";
import {attachButlerMcpCredential} from "./butlerMcpCredentialAction";

const serverId = "b96e898d-207f-4943-846e-c00f4415fc26";
const reference = `session:${"b".repeat(32)}`;
const server: McpServerProfile = {id: serverId, name: "remote", transport: "streamable_http", authMode: "none", revision: 3,
  arguments: [], enabled: false, required: false, trustedStdio: false, defaultApproval: "risk_based",
  toolOverrides: {}, tools: [], headerSecretRefs: {"X-Existing": `vault:${"a".repeat(32)}`}, environmentSecretRefs: {}};
const action = {kind: "mcp_attach_credential" as const, projectId: "project", serverId, name: server.name,
  transport: "streamable_http" as const, expectedRevision: 3, reference, slot: "header" as const, key: "X-API-Key"};
const status: CredentialStatus = {reference, persistence: "session", available: true, state: "available"};

describe("Butler MCP credential handoff", () => {
  it("cancels before mutation", async () => {
    const api = {getMcpServer: vi.fn().mockResolvedValue(server), credentialStatus: vi.fn().mockResolvedValue(status),
      updateMcpServer: vi.fn()} as unknown as ApiClient;
    expect(await attachButlerMcpCredential(api, action, () => "project", async () => false))
      .toContain("No credential was attached");
    expect(api.updateMcpServer).not.toHaveBeenCalled();
  });

  it("refuses stale identity, unavailable reference, and a mismatched transport", async () => {
    const api = {getMcpServer: vi.fn().mockResolvedValue({...server, revision: 4}),
      credentialStatus: vi.fn().mockResolvedValue(status), updateMcpServer: vi.fn()} as unknown as ApiClient;
    await expect(attachButlerMcpCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("MCP server changed");
    vi.mocked(api.getMcpServer).mockResolvedValue(server);
    vi.mocked(api.credentialStatus).mockResolvedValue({...status, available: false, state: "missing"});
    await expect(attachButlerMcpCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("credential is unavailable");
    vi.mocked(api.getMcpServer).mockResolvedValue({...server, transport: "stdio"});
    await expect(attachButlerMcpCredential(api, action, () => "project", async () => true))
      .rejects.toThrow("MCP server changed");
    expect(api.updateMcpServer).not.toHaveBeenCalled();
  });

  it("preserves other HTTP headers and reads the exact slot back", async () => {
    const saved = {...server, revision: 4, authMode: "headers" as const,
      headerSecretRefs: {...server.headerSecretRefs, [action.key]: reference}};
    const api = {getMcpServer: vi.fn().mockResolvedValueOnce(server).mockResolvedValueOnce(server).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), updateMcpServer: vi.fn().mockResolvedValue(saved)} as unknown as ApiClient;
    const result = await attachButlerMcpCredential(api, action, () => "project", async () => true);
    expect(api.updateMcpServer).toHaveBeenCalledWith(serverId, {auth_mode: "headers", bearer_secret_ref: null,
      header_secret_refs: {...server.headerSecretRefs, "X-API-Key": reference}}, 3);
    expect(result).toContain("Core confirmed the slot, mode and credential availability");
  });

  it("does not replay a write after its response is lost", async () => {
    const saved = {...server, revision: 4, authMode: "headers" as const,
      headerSecretRefs: {...server.headerSecretRefs, [action.key]: reference}};
    const api = {getMcpServer: vi.fn().mockResolvedValueOnce(server).mockResolvedValueOnce(server).mockResolvedValue(saved),
      credentialStatus: vi.fn().mockResolvedValue(status), updateMcpServer: vi.fn().mockRejectedValue(new Error("lost response"))} as unknown as ApiClient;
    expect(await attachButlerMcpCredential(api, action, () => "project", async () => true))
      .toContain("Core confirmed the slot");
    expect(api.updateMcpServer).toHaveBeenCalledTimes(1);
  });

  it("uses environment refs for stdio and bearer refs for HTTP", async () => {
    const stdio = {...server, transport: "stdio" as const, authMode: "none" as const,
      environmentSecretRefs: {OLDER: `vault:${"a".repeat(32)}`}};
    const stdioAction = {...action, transport: "stdio" as const, slot: "environment" as const, key: "API_TOKEN"};
    const savedStdio = {...stdio, revision: 4,
      environmentSecretRefs: {...stdio.environmentSecretRefs, API_TOKEN: reference}};
    const stdioApi = {getMcpServer: vi.fn().mockResolvedValueOnce(stdio).mockResolvedValueOnce(stdio).mockResolvedValue(savedStdio),
      credentialStatus: vi.fn().mockResolvedValue(status), updateMcpServer: vi.fn().mockResolvedValue(savedStdio)} as unknown as ApiClient;
    expect(await attachButlerMcpCredential(stdioApi, stdioAction, () => "project", async () => true))
      .toContain("Core confirmed the slot");
    expect(stdioApi.updateMcpServer).toHaveBeenCalledWith(serverId,
      {environment_secret_refs: {OLDER: stdio.environmentSecretRefs.OLDER, API_TOKEN: reference}}, 3);

    const bearerAction = {...action, slot: "bearer" as const, key: undefined};
    const savedBearer = {...server, revision: 4, authMode: "bearer" as const,
      bearerSecretRef: reference, headerSecretRefs: {}};
    const bearerApi = {getMcpServer: vi.fn().mockResolvedValueOnce(server).mockResolvedValueOnce(server).mockResolvedValue(savedBearer),
      credentialStatus: vi.fn().mockResolvedValue(status), updateMcpServer: vi.fn().mockResolvedValue(savedBearer)} as unknown as ApiClient;
    expect(await attachButlerMcpCredential(bearerApi, bearerAction, () => "project", async () => true))
      .toContain("Core confirmed the slot");
    expect(bearerApi.updateMcpServer).toHaveBeenCalledWith(serverId,
      {auth_mode: "bearer", bearer_secret_ref: reference, header_secret_refs: {}}, 3);
  });
});
