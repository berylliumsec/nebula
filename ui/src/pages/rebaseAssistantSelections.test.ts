import {describe, expect, it} from "vitest";
import {ApiError} from "../api/client";
import {isAssistantSelectionRevisionConflict, rebaseAssistantSelections} from "./rebaseAssistantSelections";

const before = {
  mcpServerIds: ["notes", "tracker"], hookIds: ["audit"],
  excludedMcpServerIds: ["retired"], mcpContextMode: "always_loaded" as const,
};

describe("rebasing next-turn Assistant selections", () => {
  it.each([
    [new ApiError("Changed", 422, "request", {code: "chat.value_error", operator_detail: "conversation changed; read its Assistant settings and try again"}), true],
    [new ApiError("Changed", 422, "request", {code: "api.request_validation", operator_detail: "conversation changed; read its Assistant settings and try again"}), false],
    [new ApiError("Invalid", 422, "request", {code: "chat.value_error", operator_detail: "an MCP server cannot be selected and excluded"}), false],
    [new ApiError("conversation changed; read its Assistant settings and try again", 422, "request", {code: "chat.value_error"}), false],
    [new ApiError("Changed", 409, "request", {operator_detail: "revision conflict: expected 7, found 8"}), true],
    [new ApiError("Conflict", 409, "request", {operator_detail: "conversation already has an active response"}), false],
    [new ApiError("Conflict", 409, "request"), false],
    [new ApiError("Failed", 500, "request", {code: "chat.value_error", operator_detail: "conversation changed; read its Assistant settings and try again"}), false],
    [new TypeError("Failed to fetch"), false],
  ])("recognizes only positively identified revision conflicts: %s", (error, expected) => {
    expect(isAssistantSelectionRevisionConflict(error)).toBe(expected);
  });

  it("preserves concurrent additions, hooks and mode when unchecking an MCP source", () => {
    expect(rebaseAssistantSelections(before, {...before, mcpServerIds: ["tracker"], mcpContextMode: undefined}, {
      mcpServerIds: ["notes", "tracker", "new"], hookIds: ["audit", "new-hook"],
      excludedMcpServerIds: ["retired", "other"], mcpContextMode: "on_demand",
    })).toEqual({
      mcpServerIds: ["tracker", "new"], excludedMcpServerIds: ["retired", "other", "notes"],
    });
  });

  it("does not restore a different server concurrently removed by another writer", () => {
    expect(rebaseAssistantSelections(before, {...before, mcpServerIds: ["notes", "tracker", "new"]}, {
      ...before, mcpServerIds: ["notes"], excludedMcpServerIds: ["retired", "tracker", "new"],
    })).toEqual({
      mcpServerIds: ["notes", "new"], excludedMcpServerIds: ["retired", "tracker"],
    });
  });

  it("excludes a deselected source even if it was concurrently deselected already", () => {
    expect(rebaseAssistantSelections(before, {...before, mcpServerIds: ["tracker"]}, {
      ...before, mcpServerIds: ["tracker"],
    })).toEqual({mcpServerIds: ["tracker"], excludedMcpServerIds: ["retired", "notes"]});
  });

  it("an explicit exclusion removes a source concurrently selected elsewhere", () => {
    const untouched = {...before, mcpServerIds: []};
    expect(rebaseAssistantSelections(untouched, {...untouched, excludedMcpServerIds: ["retired", "notes"]}, before)).toEqual({
      mcpServerIds: ["tracker"], excludedMcpServerIds: ["retired", "notes"],
    });
  });

  it("restores a source without clearing unrelated new exclusions", () => {
    const excluded = {...before, mcpServerIds: [], excludedMcpServerIds: ["notes"]};
    expect(rebaseAssistantSelections(excluded, {...excluded, mcpServerIds: ["notes"]}, {
      ...excluded, excludedMcpServerIds: ["notes", "other"],
    })).toEqual({mcpServerIds: ["notes"], excludedMcpServerIds: ["other"]});
  });

  it("rebases hook deltas without sending MCP selections, exclusions or mode", () => {
    expect(rebaseAssistantSelections(before, {...before, hookIds: ["new-hook"]}, {
      ...before, hookIds: ["audit", "concurrent"], mcpContextMode: "on_demand",
    })).toEqual({hookIds: ["concurrent", "new-hook"]});
  });

  it("sends only an explicitly changed context mode", () => {
    expect(rebaseAssistantSelections(before, {...before, mcpContextMode: "on_demand"}, {
      ...before, mcpServerIds: ["concurrent"], hookIds: [], excludedMcpServerIds: [],
    })).toEqual({mcpContextMode: "on_demand"});
  });

  it("does not replay unchanged or unspecified mode or other settings", () => {
    const current = {...before, mcpContextMode: "on_demand" as const, hookIds: ["concurrent"]};
    expect(rebaseAssistantSelections(before, before, current)).toEqual({});
    expect(rebaseAssistantSelections(before, {...before, mcpContextMode: undefined}, current)).toEqual({});
  });
});
