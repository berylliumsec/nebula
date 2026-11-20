import { describe, expect, it } from "vitest";
import type { ChatAssistantConfig, HarnessProfile, ProviderHealth } from "../api/types";
import { butlerQuestionAbout, butlerThreadRequest, butlerThreadReservation, butlerThreadRuntime, butlerThreadTitle, filterBoardItems, filterProjectPicker, projectPickerGroups } from "./butlerThreads";

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

describe("project thread picker", () => {
  it("puts projects with waiting or working chats first and leaves archived out", () => {
    const projects = [
      { id: "quiet", name: "Quiet", status: "active" },
      { id: "busy", name: "Busy", status: "active" },
      { id: "needs", name: "Needs you", status: "active" },
      { id: "old", name: "Old", status: "archived" },
    ];
    const items = [
      { state: "working", project: { id: "busy", name: "Busy" } },
      { state: "working", project: { id: "busy", name: "Busy" } },
      { state: "working", project: { id: "needs", name: "Needs you" } },
      { state: "waiting", project: { id: "needs", name: "Needs you" } },
      { state: "working", project: { id: "old", name: "Old" } },
    ] as Parameters<typeof projectPickerGroups>[1];
    const groups = projectPickerGroups(projects, items);
    expect(groups.active.map(({ project, label }) => [project.id, label])).toEqual([
      ["busy", "2 working"],
      ["needs", "1 need you"],
    ]);
    expect(groups.quiet.map((project) => project.id)).toEqual(["quiet"]);
  });
});

describe("Butler mode search", () => {
  const groups = {
    active: [{ project: { id: "bb", name: "Apple Bug Bounty" }, label: "14 working" }, { project: { id: "sim", name: "Sim Lab" }, label: "2 need you" }],
    quiet: [{ id: "tv", name: "Apple TV fuzzing" }, { id: "notes", name: "Notes" }],
  };

  it("filters the project picker on every word of the name, Active before Quiet", () => {
    expect(filterProjectPicker(groups, "").options.map((option) => [option.project.id, option.group, option.label])).toEqual([
      ["bb", "active", "14 working"], ["sim", "active", "2 need you"], ["tv", "quiet", undefined], ["notes", "quiet", undefined],
    ]);
    expect(filterProjectPicker(groups, "  APPLE ").options.map((option) => option.project.id)).toEqual(["bb", "tv"]);
    expect(filterProjectPicker(groups, "apple fuzz").options.map((option) => option.project.id)).toEqual(["tv"]);
    expect(filterProjectPicker(groups, "missing")).toEqual({ options: [], total: 0 });
  });

  it("renders only the first rows of a long project list and counts the rest", () => {
    const many = { active: [], quiet: Array.from({ length: 1000 }, (_, index) => ({ id: `p${index}`, name: `Project ${index}` })) };
    const result = filterProjectPicker(many, "project");
    expect(result.options).toHaveLength(80);
    expect(result.total).toBe(1000);
    expect(filterProjectPicker(many, "project 999").options.map((option) => option.project.id)).toEqual(["p999"]);
  });

  it("filters board cards by chat title, project name or model", () => {
    const items = [
      { title: "Run exact-build simulation", project: { id: "bb", name: "Apple Bug Bounty" }, model: "deepseek/deepseek-v4.1-flash" },
      { title: "Complete the Recon review", project: { id: "bb", name: "Apple Bug Bounty" }, model: "gpt-5" },
      { title: "Triage crashes", project: { id: "sim", name: "Sim Lab" } },
    ];
    expect(filterBoardItems(items, "")).toEqual(items);
    expect(filterBoardItems(items, "bug bounty").map((item) => item.title)).toEqual(["Run exact-build simulation", "Complete the Recon review"]);
    expect(filterBoardItems(items, "recon bounty").map((item) => item.title)).toEqual(["Complete the Recon review"]);
    expect(filterBoardItems(items, "DEEPSEEK").map((item) => item.title)).toEqual(["Run exact-build simulation"]);
    expect(filterBoardItems(items, "sim lab crash").map((item) => item.title)).toEqual(["Triage crashes"]);
    expect(filterBoardItems(items, "nothing")).toEqual([]);
  });
});
