import type { ApiClient } from "./client";
import type { ButlerBoardState } from "./butlerBoard";

/** The latest chat change to one workspace file. */
export interface FileAttribution {
  path: string;
  sessionId: string;
  sessionTitle: string;
  turnId?: string;
  changedAt: string;
  state: ButlerBoardState;
}

export interface FileChangeHunk { start: number; lines: number; kind: "added" | "changed" }

export interface FileChangeEvent {
  sessionId: string;
  sessionTitle: string;
  turnId?: string;
  turnIndex?: number;
  at: string;
  kind: "created" | "modified" | "deleted";
  added?: number;
  removed?: number;
  /** Line-level changes in the file as it is now, when Core has a diff. */
  hunks?: FileChangeHunk[];
  attribution: "owned" | "uncertain";
}

type Wire = Record<string, any>;
const pick = (value: Wire, snake: string, camel: string): any => value[camel] ?? value[snake];
const optional = <T,>(value: T | null | undefined): T | undefined => value ?? undefined;

function projectPath(projectId: string): string {
  return `projects/${encodeURIComponent(projectId)}/files/attribution`;
}

/** Files chats changed in the last week, newest first; undefined on 304. */
export async function getFileAttributionIfChanged(api: ApiClient, projectId: string, etag: string | undefined, signal?: AbortSignal): Promise<{ files: FileAttribution[]; truncated: boolean; etag?: string } | undefined> {
  const answer = await api.requestIfChanged<Wire>(projectPath(projectId), etag, { signal });
  if (!answer) return undefined;
  const files = ((answer.value.files ?? []) as Wire[]).map((file) => ({
    path: file.path,
    sessionId: pick(file, "session_id", "sessionId"),
    sessionTitle: pick(file, "session_title", "sessionTitle") ?? "Chat",
    turnId: optional(pick(file, "turn_id", "turnId")),
    changedAt: pick(file, "changed_at", "changedAt"),
    state: file.state,
  }));
  return { files, truncated: Boolean(answer.value.truncated), etag: answer.etag };
}

/** Every recorded chat change to one file, newest first. */
export async function getFileHistory(api: ApiClient, projectId: string, path: string, signal?: AbortSignal): Promise<FileChangeEvent[]> {
  const value = await api.request<Wire>(`${projectPath(projectId)}/history?path=${encodeURIComponent(path)}`, { signal });
  return ((value.events ?? []) as Wire[]).map((event) => ({
    sessionId: pick(event, "session_id", "sessionId"),
    sessionTitle: pick(event, "session_title", "sessionTitle") ?? "Chat",
    turnId: optional(pick(event, "turn_id", "turnId")),
    turnIndex: optional(pick(event, "turn_index", "turnIndex")),
    at: event.at,
    kind: event.kind,
    added: optional(event.added),
    removed: optional(event.removed),
    hunks: optional(event.hunks),
    attribution: event.attribution === "uncertain" ? "uncertain" : "owned",
  }));
}

/** A file-tree mark for each changed path and every folder above it; the most urgent state wins. */
export function fileMarks(files: FileAttribution[]): Record<string, ButlerBoardState> {
  const rank: Record<ButlerBoardState, number> = { waiting: 3, working: 2, failed: 1, idle: 0 };
  const marks: Record<string, ButlerBoardState> = {};
  for (const file of files) {
    const parts = file.path.split("/");
    for (let depth = 1; depth <= parts.length; depth += 1) {
      const key = parts.slice(0, depth).join("/");
      if (marks[key] === undefined || rank[file.state] > rank[marks[key]]) marks[key] = file.state;
    }
  }
  return marks;
}
