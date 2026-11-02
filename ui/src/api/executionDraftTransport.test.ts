import {describe, expect, it, vi} from "vitest";
import {ApiClient} from "./client";
import type {PostToolAssistantConfig} from "./types";

const wireDraft = {id: "draft", engagement_id: "project", execution_id: "execution", provider_profile_id: "runtime",
  model: "fixture-model", prompt_version: "execution-note/v1", context_fingerprint: "a".repeat(64), status: "ready", revision: 2, metadata: {}};
const modes: PostToolAssistantConfig = {backendKind: "harness", harnessProfileId: "harness-1", suggestNextSteps: false,
  takeNotes: true, cloudConfirmed: true};

describe.each(["execution", "mission"] as const)("%s note request", kind => {
  function setup() {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(wireDraft), {status: 202}));
    const api = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
    const generate = kind === "execution" ? api.generateExecutionDraft.bind(api) : api.generateMissionDraft.bind(api);
    return {fetchMock, generate};
  }

  it("keeps a manually selected harness in review mode and retains request consent", async () => {
    const {fetchMock, generate} = setup();
    await generate("target", "harness", "fixture-model", true, modes);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({provider_id: "harness", backend_kind: "harness",
      harness_profile_id: "harness-1", model: "fixture-model", cloud_confirmed: true, suggest_next_steps: false,
      take_notes: true, automatic: false});
  });

  it("keeps manual provider generation in review mode by default", async () => {
    const {fetchMock, generate} = setup();
    await generate("target", "provider-1", "fixture-model", false);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({provider_id: "provider-1",
      backend_kind: "provider", cloud_confirmed: false, take_notes: true, automatic: false});
  });

  it("uses automatic processing only when the caller explicitly requests it", async () => {
    const {fetchMock, generate} = setup();
    await generate("target", "harness", "fixture-model", true, modes, true);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({automatic: true, harness_profile_id: "harness-1"});
  });
});

it("retains Core's current execution revision through exact and paged reads", async () => {
  const wire = {id: "execution", engagement_id: "project", revision: 7, operator_id: "operator", origin: {kind: "selection"},
    language: "python", source_sha256: "a".repeat(64), source_artifact_id: "source", runtime: {arguments: []}, network: {mode: "none"},
    limits: {}, workspace: "/workspace", policy_decision: "approved", status: "completed", queued_at: "2026-11-02T00:00:00Z"};
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(JSON.stringify(wire)))
    .mockResolvedValueOnce(new Response(JSON.stringify([wire])));
  const api = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
  expect((await api.getExecution("execution")).revision).toBe(7);
  expect((await api.listExecutions("project")).items[0].revision).toBe(7);
});
