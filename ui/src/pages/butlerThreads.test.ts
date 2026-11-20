import { describe, expect, it } from "vitest";
import type { ChatAssistantConfig, HarnessProfile, ProviderHealth } from "../api/types";
import { butlerQuestionAbout, butlerThreadRequest, butlerThreadReservation, butlerThreadRuntime, butlerThreadTitle } from "./butlerThreads";

const config: ChatAssistantConfig = {
  backend: "provider",
  profileId: "cloud",
  model: "model-a",
  reasoningEffort: "high",
  mcpServerIds: ["mcp-1"],
  hookIds: [],
  includeKnowledge: true,
  allowCloudKnowledge: false,
  toolsEnabled: true,
  planningEnabled: false,
  allowSubagents: true,
  allowAgentMessaging: false,
  maxActiveSubagents: 3,
};

const provider = (overrides: Partial<ProviderHealth>) => ({ id: "cloud", name: "Cloud", kind: "commercial", local: false, permitsSensitiveData: true, ...overrides }) as ProviderHealth;

describe("Butler mode threads", () => {
  it("reuses the project's saved Butler settings for a new thread", () => {
    const runtime = butlerThreadRuntime(config, [provider({})], []);
    expect(runtime).toEqual({ ok: true, config, allowCloudToolResults: true });
    if (!runtime.ok) return;
    expect(butlerThreadReservation("p1", runtime, "Which chats need me?")).toMatchObject({
      engagementId: "p1", backend: "provider", providerId: "cloud", harnessProfileId: undefined,
      model: "model-a", mcpServerIds: ["mcp-1"], thread: true, title: "Which chats need me?", allowCloudToolResults: true,
    });
    expect(butlerThreadRequest("p1", "thread-1", runtime, [], "Hello")).toMatchObject({
      controlAssistant: true, engagementId: "p1", sessionId: "thread-1", backend: "provider", providerId: "cloud",
      model: "model-a", reasoningEffort: "high", toolsEnabled: true, allowSubagents: true, maxActiveSubagents: 3,
      messages: [{ role: "user", content: "Hello" }], allowCloudToolResults: true,
    });
  });

  it("explains what to fix when Butler cannot run a thread", () => {
    expect(butlerThreadRuntime(undefined, [], [])).toMatchObject({ ok: false, error: expect.stringContaining("Open the Butler panel once") });
    expect(butlerThreadRuntime(config, [], [])).toMatchObject({ ok: false, error: expect.stringContaining("no longer available") });
    expect(butlerThreadRuntime(config, [provider({ permitsSensitiveData: false })], [])).toMatchObject({ ok: false, error: expect.stringContaining("text only") });
    expect(butlerThreadRuntime(config, [provider({ local: true, permitsSensitiveData: false })], [])).toMatchObject({ ok: true, allowCloudToolResults: false });
  });

  it("sends harness options only to a harness Butler", () => {
    const harness = { id: "codex", name: "Codex", localOnly: true, permitsSensitiveData: false, models: ["gpt"], modelOptions: [] } as unknown as HarnessProfile;
    const runtime = butlerThreadRuntime({ ...config, backend: "harness", profileId: "codex", model: "gpt", harnessReasoningEffort: "medium" }, [], [harness]);
    expect(runtime.ok).toBe(true);
    if (!runtime.ok) return;
    expect(butlerThreadReservation("p1", runtime, "t")).toMatchObject({ backend: "harness", providerId: undefined, harnessProfileId: "codex", harnessReasoningEffort: "medium" });
    expect(butlerThreadRequest("p1", "s", runtime, [harness], "x")).toMatchObject({ backend: "harness", harnessProfileId: "codex", reasoningEffort: undefined, harnessReasoningEffort: "medium" });
  });

  it("titles threads from the question and names the chat it asks about", () => {
    expect(butlerThreadTitle("  Which   chats\nneed me?  ")).toBe("Which chats need me?");
    expect(butlerThreadTitle("x".repeat(120))).toHaveLength(80);
    expect(butlerThreadTitle("   ")).toBe("Butler thread");
    const about = butlerQuestionAbout({ sessionId: "s9", title: "Kernel fuzz", now: "Approve run_command: make install", project: { id: "p", name: "R" }, state: "waiting", backend: "provider", isSubagent: false, updatedAt: "" }, "Is this safe?");
    expect(about).toContain('About the chat "Kernel fuzz" (conversation s9 in this project). Current status: Approve run_command: make install.');
    expect(about.endsWith("Is this safe?")).toBe(true);
    expect(butlerQuestionAbout(undefined, "Plain")).toBe("Plain");
  });
});
