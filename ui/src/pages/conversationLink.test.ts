import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import type { EngagementSummary } from "../api/types";
import { conversationLinkFailure, conversationLinkTarget } from "./conversationLink";

function project(id: string, status: EngagementSummary["status"] = "active"): EngagementSummary {
  return {
    id,
    name: `Project ${id}`,
    description: "",
    status,
    tags: [],
    createdAt: "2026-09-26T10:00:00Z",
    updatedAt: "2026-09-26T10:00:00Z",
    scopeAssetCount: 0,
  } as EngagementSummary;
}

describe("conversationLinkTarget", () => {
  const active = [project("a"), project("b")];
  const archived = [project("old", "archived")];

  it("keeps a conversation of the open project in place", () => {
    expect(conversationLinkTarget({ id: "chat", engagementId: "a" }, "a", active, archived)).toEqual({ kind: "current" });
  });

  it("names the active project that owns the conversation", () => {
    expect(conversationLinkTarget({ id: "chat", engagementId: "b" }, "a", active, archived)).toEqual({ kind: "project", projectId: "b" });
  });

  it("does not switch into an archived project", () => {
    expect(conversationLinkTarget({ id: "chat", engagementId: "old" }, "a", active, archived)).toEqual({
      kind: "unavailable",
      state: { sessionId: "chat", status: "archived", project: archived[0] },
    });
  });

  it("reports a project this Core does not list", () => {
    expect(conversationLinkTarget({ id: "chat", engagementId: "gone" }, "a", active, archived)).toEqual({
      kind: "unavailable",
      state: { sessionId: "chat", status: "unknown_project" },
    });
  });
});

describe("conversationLinkFailure", () => {
  it("separates a deleted conversation from a refused or failed read", () => {
    expect(conversationLinkFailure("chat", new ApiError("not found", 404))).toEqual({ sessionId: "chat", status: "missing" });
    expect(conversationLinkFailure("chat", new ApiError("forbidden", 403))).toEqual({ sessionId: "chat", status: "forbidden" });
    expect(conversationLinkFailure("chat", new ApiError("unauthorized", 401))).toEqual({ sessionId: "chat", status: "forbidden" });
    expect(conversationLinkFailure("chat", new ApiError("Core is restarting.", 503))).toEqual({ sessionId: "chat", status: "failed", message: "Core is restarting." });
    expect(conversationLinkFailure("chat", "offline")).toEqual({ sessionId: "chat", status: "failed", message: "Nebula Core did not answer." });
  });
});
