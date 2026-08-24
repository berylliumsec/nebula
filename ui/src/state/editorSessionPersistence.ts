import { createStore, del, get, set } from "idb-keyval";

export interface PersistedEditorBuffer {
  id: string;
  content: string;
  expectedSha256?: string;
  existing: boolean;
  filePath: string;
  restoreFromCore?: boolean;
  savedContent: string;
}

export interface PersistedEditorSession {
  activeId?: string;
  buffers: PersistedEditorBuffer[];
  primaryId?: string;
  secondaryId?: string;
}

export type PersistedEditorSessions = Record<string, PersistedEditorSession>;

interface EditorSessionEnvelope {
  schema: "nebula.editor-sessions/v1";
  savedAt: string;
  sessions: PersistedEditorSessions;
}

const STORE = createStore("nebula-editor-state", "hot-exit");
const STORAGE_KEY = "sessions/v1";
const MAX_PROJECTS = 50;
const MAX_BUFFERS_PER_PROJECT = 20;
const MAX_BUFFER_BYTES = 1024 * 1024;
const MAX_TOTAL_BYTES = 8 * 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;

function validString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length <= maximum;
}

function encodedBytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function normalizeBuffer(value: unknown): PersistedEditorBuffer | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Partial<PersistedEditorBuffer>;
  if (
    !validString(candidate.id, 200)
    || !validString(candidate.filePath, 4096)
    || !validString(candidate.content, MAX_BUFFER_BYTES)
    || !validString(candidate.savedContent, MAX_BUFFER_BYTES)
    || typeof candidate.existing !== "boolean"
    || encodedBytes(candidate.content) > MAX_BUFFER_BYTES
    || encodedBytes(candidate.savedContent) > MAX_BUFFER_BYTES
    || (candidate.expectedSha256 !== undefined && !SHA256.test(candidate.expectedSha256))
  ) return undefined;
  return {
    id: candidate.id,
    content: candidate.content,
    expectedSha256: candidate.expectedSha256,
    existing: candidate.existing,
    filePath: candidate.filePath,
    restoreFromCore: candidate.restoreFromCore === true,
    savedContent: candidate.savedContent,
  };
}

export function normalizeEditorSessions(value: unknown): PersistedEditorSessions {
  if (!value || typeof value !== "object") return {};
  const envelope = value as Partial<EditorSessionEnvelope>;
  if (envelope.schema !== "nebula.editor-sessions/v1" || !envelope.sessions || typeof envelope.sessions !== "object") return {};
  const sessions: PersistedEditorSessions = {};
  let totalBytes = 0;
  for (const [engagementId, rawSession] of Object.entries(envelope.sessions).slice(0, MAX_PROJECTS)) {
    if (!validString(engagementId, 200) || !rawSession || typeof rawSession !== "object") continue;
    const candidate = rawSession as Partial<PersistedEditorSession>;
    if (!Array.isArray(candidate.buffers)) continue;
    const buffers: PersistedEditorBuffer[] = [];
    for (const rawBuffer of candidate.buffers.slice(0, MAX_BUFFERS_PER_PROJECT)) {
      const buffer = normalizeBuffer(rawBuffer);
      if (!buffer) continue;
      const bytes = encodedBytes(buffer.content) + encodedBytes(buffer.savedContent);
      if (totalBytes + bytes > MAX_TOTAL_BYTES) break;
      totalBytes += bytes;
      buffers.push(buffer);
    }
    const ids = new Set(buffers.map((buffer) => buffer.id));
    if (!buffers.length) continue;
    const primaryId = candidate.primaryId && ids.has(candidate.primaryId)
      ? candidate.primaryId
      : (candidate.activeId && ids.has(candidate.activeId) ? candidate.activeId : buffers[0].id);
    const secondaryId = candidate.secondaryId && ids.has(candidate.secondaryId) && candidate.secondaryId !== primaryId
      ? candidate.secondaryId
      : undefined;
    sessions[engagementId] = {
      activeId: candidate.activeId && [primaryId, secondaryId].includes(candidate.activeId) ? candidate.activeId : primaryId,
      buffers,
      primaryId,
      secondaryId,
    };
  }
  return sessions;
}

function compactSessions(sessions: PersistedEditorSessions): PersistedEditorSessions {
  return Object.fromEntries(Object.entries(sessions).slice(0, MAX_PROJECTS).map(([engagementId, session]) => [engagementId, {
    ...session,
    buffers: session.buffers.slice(0, MAX_BUFFERS_PER_PROJECT).map((buffer) => {
      const dirty = !buffer.existing || buffer.content !== buffer.savedContent;
      return buffer.existing && !dirty
        ? { ...buffer, content: "", savedContent: "", restoreFromCore: true }
        : { ...buffer, restoreFromCore: false };
    }),
  }]));
}

export async function loadEditorSessions(): Promise<PersistedEditorSessions> {
  return normalizeEditorSessions(await get(STORAGE_KEY, STORE));
}

export async function saveEditorSessions(sessions: PersistedEditorSessions): Promise<void> {
  const envelope: EditorSessionEnvelope = {
    schema: "nebula.editor-sessions/v1",
    savedAt: new Date().toISOString(),
    sessions: compactSessions(sessions),
  };
  const normalized = normalizeEditorSessions(envelope);
  await set(STORAGE_KEY, { ...envelope, sessions: normalized }, STORE);
}

export async function clearEditorSessions(): Promise<void> {
  await del(STORAGE_KEY, STORE);
}
