import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import {
  declinedToolSharingMessage,
  notSentMessage,
  providerTurnToolFamilies,
  runWithToolResultConsent,
  textOnlyToolSharingMessage,
  ToolResultSharingDeclinedError,
  type ProviderTurnTools,
} from "./chatToolResultConsent";

// The recovery notice shows one line of at most 180 characters.
const NOTICE_LIMIT = 180;

const plain: ProviderTurnTools = {
  commandRuntime: false,
  mcpServerIds: [],
  sshEnvironmentIds: undefined,
  allowSubagents: false,
  allowAgentMessaging: false,
  subagentConversation: false,
  browserControl: false,
  contextSourceKinds: [],
};

describe("provider turn tool families", () => {
  it("carries no tools for a plain chat, so a cloud provider is not asked", () => {
    expect(providerTurnToolFamilies(plain)).toEqual([]);
    // Letting the model pick an SSH host only matters when commands run.
    expect(providerTurnToolFamilies({ ...plain, sshEnvironmentIds: [] })).toEqual([]);
    expect(providerTurnToolFamilies({ ...plain, contextSourceKinds: ["document", "browser_selection"] })).toEqual([]);
  });

  it("counts Subagents without a command runtime, as Core does", () => {
    expect(providerTurnToolFamilies({ ...plain, allowSubagents: true })).toEqual(["Subagents"]);
  });

  it("names every family the composer can see, in Core's order", () => {
    expect(providerTurnToolFamilies({
      commandRuntime: true,
      mcpServerIds: ["mcp-1"],
      sshEnvironmentIds: ["host-1"],
      allowSubagents: true,
      allowAgentMessaging: true,
      subagentConversation: false,
      browserControl: true,
      contextSourceKinds: ["application_model"],
    })).toEqual([
      "Command runtime",
      "MCP servers",
      "SSH host",
      "Subagents",
      "Agent messaging",
      "Browser control",
      "Project model context",
    ]);
  });

  it("routes a subagent's own conversation to its parent and ignores delegation there", () => {
    expect(providerTurnToolFamilies({
      ...plain,
      allowSubagents: true,
      allowAgentMessaging: true,
      subagentConversation: true,
    })).toEqual(["Parent agent messages"]);
  });
});

describe("refused-send copy", () => {
  it("offers a chat without tools only when every family can be turned off", () => {
    expect(notSentMessage(declinedToolSharingMessage(["Subagents"], "OpenRouter"), true)).toBe(
      "Not sent; your message is kept. Subagents would share tool results with OpenRouter. Send again to allow sharing, or turn it off.",
    );
    expect(declinedToolSharingMessage(["Command runtime", "Subagents"], "OpenRouter")).toBe(
      "Command runtime and Subagents would share tool results with OpenRouter. Send again to allow sharing.",
    );
    expect(textOnlyToolSharingMessage(["Subagents", "Agent messaging"], "OpenRouter")).toBe(
      "OpenRouter is text-only. Permit project/document data in Settings, or turn off Subagents and Agent messaging.",
    );
  });

  it("fits the notice so the next action is not cut off", () => {
    const families = ["Command runtime", "MCP servers", "Subagents", "Agent messaging"] as const;
    const optional = ["MCP servers", "SSH host", "Subagents", "Agent messaging"] as const;
    for (const message of [
      declinedToolSharingMessage(families, "OpenRouter"),
      declinedToolSharingMessage(optional, "OpenRouter"),
      textOnlyToolSharingMessage(optional, "OpenRouter"),
    ]) {
      expect(notSentMessage(message, true).length).toBeLessThanOrEqual(NOTICE_LIMIT);
    }
  });
});

describe("Core-requested tool-result consent", () => {
  const refusal = () => new ApiError("cloud command-result transfer requires explicit confirmation for this turn", 409, undefined, {
    code: "tool_result_consent_required",
    operator_detail: "This turn uses web search, whose tool inputs and results would go to OpenRouter.",
    error_id: "err_consent",
  });

  it("asks once and sends the same request again with consent", async () => {
    const request = vi.fn().mockRejectedValueOnce(refusal()).mockResolvedValueOnce("completed");
    const ask = vi.fn().mockResolvedValue(true);
    const grant = vi.fn();

    await expect(runWithToolResultConsent(request, { canAsk: () => true, ask, grant, runtimeName: "OpenRouter" })).resolves.toBe("completed");
    expect(ask).toHaveBeenCalledTimes(1);
    expect(grant).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("stops without a second send and names what Core would have shared when declined", async () => {
    const request = vi.fn().mockRejectedValue(refusal());
    const grant = vi.fn();

    const declined = runWithToolResultConsent(request, { canAsk: () => true, ask: async () => false, grant, runtimeName: "OpenRouter" });
    await expect(declined).rejects.toBeInstanceOf(ToolResultSharingDeclinedError);
    await expect(declined).rejects.toThrow(
      "This turn uses web search, whose tool inputs and results would go to OpenRouter. Send again to allow sharing.",
    );
    expect(request).toHaveBeenCalledTimes(1);
    expect(grant).not.toHaveBeenCalled();
  });

  it("never asks twice or for other refusals", async () => {
    const ask = vi.fn();
    const consented = vi.fn().mockRejectedValue(refusal());
    await expect(runWithToolResultConsent(consented, { canAsk: () => false, ask, grant: vi.fn(), runtimeName: "OpenRouter" })).rejects.toBeInstanceOf(ApiError);
    const other = vi.fn().mockRejectedValue(new ApiError("provider profile does not permit command-result transfer", 409));
    await expect(runWithToolResultConsent(other, { canAsk: () => true, ask, grant: vi.fn(), runtimeName: "OpenRouter" })).rejects.toThrow("does not permit");
    expect(ask).not.toHaveBeenCalled();
  });
});
