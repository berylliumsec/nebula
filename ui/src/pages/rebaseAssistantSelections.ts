import type {ChatAssistantConfig} from "../api/types";
import {ApiError} from "../api/client";

type SelectionState = Pick<ChatAssistantConfig, "mcpServerIds" | "hookIds" | "excludedMcpServerIds" | "mcpContextMode">;

export function isAssistantSelectionRevisionConflict(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return (error.status === 422 && error.code === "chat.value_error"
    && error.operatorDetail === "conversation changed; read its Assistant settings and try again")
    || (error.status === 409
      && /^revision conflict: expected \d+, found \d+$/.test(error.operatorDetail ?? ""));
}

function delta(before: readonly string[], after: readonly string[]) {
  return {
    added: after.filter(id => !before.includes(id)),
    removed: before.filter(id => !after.includes(id)),
  };
}

function applyDelta(current: readonly string[], added: readonly string[], removed: readonly string[]) {
  return [...new Set([...current.filter(id => !removed.includes(id)), ...added])];
}

/** Rebase operator intent, not the stale settings that happened to surround it. */
export function rebaseAssistantSelections(
  before: SelectionState,
  desired: SelectionState,
  current: SelectionState,
): Partial<SelectionState> {
  const mcp = delta(before.mcpServerIds, desired.mcpServerIds);
  const hooks = delta(before.hookIds, desired.hookIds);
  const exclusions = delta(
    before.excludedMcpServerIds ?? [],
    desired.excludedMcpServerIds ?? before.excludedMcpServerIds ?? [],
  );
  const patch: Partial<SelectionState> = {};
  const removeMcp = [...mcp.removed, ...exclusions.added];
  if (mcp.added.length || removeMcp.length) {
    patch.mcpServerIds = applyDelta(current.mcpServerIds, mcp.added, removeMcp);
  }
  if (hooks.added.length || hooks.removed.length) {
    patch.hookIds = applyDelta(current.hookIds, hooks.added, hooks.removed);
  }
  const exclude = [...exclusions.added, ...mcp.removed];
  const restore = [...exclusions.removed, ...mcp.added];
  if (exclude.length || restore.length) {
    patch.excludedMcpServerIds = applyDelta(current.excludedMcpServerIds ?? [], exclude, restore);
  }
  if (desired.mcpContextMode !== undefined && desired.mcpContextMode !== before.mcpContextMode) {
    patch.mcpContextMode = desired.mcpContextMode;
  }
  return patch;
}
