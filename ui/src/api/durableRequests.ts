import { createStore, get, update } from "idb-keyval";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

export interface RuntimeIdentity { core_id: string; principal_id: string }
export interface ContinuityScope { key: string; persistent: boolean; limitation?: string }
export interface RuntimeRequestStatus {
  command_id: string;
  project_id: string;
  state: string;
  turn_id?: string;
  session_id?: string;
  stream_after?: number | null;
  stream_ready?: boolean;
}
export interface PendingRequest {
  key: string;
  scope: string;
  method: string;
  path: string;
  fingerprint: string;
  headers: [string, string][];
  /** Exact text bytes are retained when bounded; binary/large requests are read-only recoverable. */
  body?: string;
  replayable: boolean;
  createdAt: number;
  projectId?: string;
  sessionId?: string;
  summary?: string;
  commandId?: string;
  state: "sending" | "accepted" | "unconfirmed" | "failed" | "uncertain";
  status?: RuntimeRequestStatus;
}

export const MAX_PENDING_BYTES = 8 * 1024 * 1024;
export const MAX_PENDING_RECORD_BYTES = 1024 * 1024;
const MAX_PENDING_RECORDS = 128;
const database = createStore("nebula-pending-requests", "requests");
const recordsKey = "immutable-attempts/v1";
const encoder = new TextEncoder();
export const encodedBytes = (text: string) => encoder.encode(text).byteLength;
export class RequestNotSentError extends Error {
  readonly name = "RequestNotSentError";
}
export class PendingRequestError extends Error {
  readonly name = "PendingRequestError";
  constructor(message: string, readonly request: PendingRequest) { super(message); }
}

export function newRequestKey(): string {
  // randomUUID is absent on ordinary HTTP LAN origins. getRandomValues works there.
  const bytes = new Uint8Array(24);
  globalThis.crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

export function identityScope(identity: RuntimeIdentity): ContinuityScope {
  if (!identity || typeof identity.core_id !== "string" || !identity.core_id.trim()
    || identity.core_id.length > 200 || typeof identity.principal_id !== "string"
    || !identity.principal_id.trim() || identity.principal_id.length > 200) {
    throw new Error("Core returned an invalid storage identity.");
  }
  return { key: `${encodeURIComponent(identity.core_id)}:${encodeURIComponent(identity.principal_id)}`, persistent: true };
}

export function ephemeralScope(): ContinuityScope {
  return { key: `window:${newRequestKey()}`, persistent: false,
    limitation: "This Core does not provide a storage identity. Drafts and pending requests are kept only in this window." };
}

function validRecord(value: unknown): value is PendingRequest {
  if (!value || typeof value !== "object") return false;
  const record = value as PendingRequest;
  return typeof record.key === "string" && typeof record.scope === "string"
    && typeof record.path === "string" && typeof record.fingerprint === "string"
    && Number.isSafeInteger(record.createdAt) && Array.isArray(record.headers)
    && encodedBytes(JSON.stringify(record)) <= MAX_PENDING_RECORD_BYTES;
}

/** An atomic device-wide cap: unresolved attempts are never evicted to make room. */
export class PendingRequestStore {
  private memory: PendingRequest[] = [];
  constructor(readonly scope: ContinuityScope) {}

  async list(): Promise<PendingRequest[]> {
    const records = this.scope.persistent ? await get<unknown>(recordsKey, database) : this.memory;
    if (records !== undefined && (!Array.isArray(records) || !records.every(validRecord))) {
      throw new RequestNotSentError("Pending request storage is unreadable. No new request was sent; recover the saved browser data first.");
    }
    return ((records ?? []) as PendingRequest[]).filter(record => record.scope === this.scope.key);
  }

  private async change(transform: (records: PendingRequest[]) => PendingRequest[]): Promise<void> {
    const apply = (raw: unknown): PendingRequest[] => {
      if (raw !== undefined && (!Array.isArray(raw) || !raw.every(validRecord))) {
        throw new RequestNotSentError("Pending request storage is unreadable. No new request was sent.");
      }
      const records = transform((raw ?? []) as PendingRequest[]);
      if (records.length > MAX_PENDING_RECORDS || encodedBytes(JSON.stringify(records)) > MAX_PENDING_BYTES
        || records.some(record => encodedBytes(JSON.stringify(record)) > MAX_PENDING_RECORD_BYTES)) {
        throw new RequestNotSentError("Pending request storage is full. Check saved requests before sending more work.");
      }
      return records;
    };
    try {
      if (this.scope.persistent) await update(recordsKey, apply, database);
      else this.memory = apply(this.memory);
    } catch (error) {
      if (error instanceof RequestNotSentError) throw error;
      throw new RequestNotSentError("This browser could not save the request. Nothing was sent. Enable browser storage and retry.");
    }
  }

  async prepare(input: Omit<PendingRequest, "key" | "scope" | "createdAt" | "state">, key?: string): Promise<PendingRequest> {
    let prepared!: PendingRequest;
    await this.change(records => {
      const existing = records.find(record => record.scope === this.scope.key
        && (key ? record.key === key : record.fingerprint === input.fingerprint));
      if (existing) {
        if (existing.fingerprint !== input.fingerprint) throw new RequestNotSentError("The saved request has different contents. Check its outcome before changing it.");
        prepared = existing;
        return records;
      }
      if (/^approvals\/[^/]+\/decision(?:\?|$)/.test(input.path)
        && records.some(record => record.scope === this.scope.key && record.path === input.path)) {
        throw new RequestNotSentError("An earlier decision is still unconfirmed. Check saved requests before changing it.");
      }
      prepared = { ...input, key: key ?? newRequestKey(), scope: this.scope.key, createdAt: Date.now(), state: "sending" };
      return [...records, prepared];
    });
    return prepared;
  }

  async save(record: PendingRequest): Promise<void> {
    await this.change(records => records.map(current => current.scope === this.scope.key && current.key === record.key
      ? { ...current, commandId: record.commandId, status: record.status, state: record.state,
          sessionId: record.status?.session_id ?? current.sessionId } : current));
  }
  async remove(key: string): Promise<void> {
    await this.change(records => records.filter(record => record.scope !== this.scope.key || record.key !== key));
  }
}

export async function requestIntent(path: string, method: string, headers: Headers, body?: BodyInit | null) {
  const selected = [...headers.entries()].filter(([name]) => ["accept", "content-type", "if-match", "if-none-match",
    "if-entry-identity", "x-content-sha256", "x-parent-identity", "x-nebula-sensitive-data-acknowledged"].includes(name)).sort();
  const hash = sha256.create();
  let textBody: string | undefined;
  if (body == null || typeof body === "string" || body instanceof URLSearchParams) {
    textBody = body == null ? "" : String(body);
    hash.update(encoder.encode(textBody));
  } else if (body instanceof Blob) {
    if (typeof body.stream === "function") {
      const reader = body.stream().getReader();
      try { while (true) { const chunk = await reader.read(); if (chunk.done) break; hash.update(chunk.value); } }
      finally { reader.releaseLock(); }
    } else {
      for (let offset = 0; offset < body.size; offset += 64 * 1024) {
        const bytes = await new Promise<ArrayBuffer>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as ArrayBuffer);
          reader.onerror = () => reject(new RequestNotSentError("The attachment could not be read. Nothing was sent."));
          reader.readAsArrayBuffer(body.slice(offset, offset + 64 * 1024));
        });
        hash.update(new Uint8Array(bytes));
      }
    }
  } else if (body instanceof ArrayBuffer) hash.update(new Uint8Array(body));
  else if (ArrayBuffer.isView(body)) hash.update(new Uint8Array(body.buffer, body.byteOffset, body.byteLength));
  else throw new RequestNotSentError("This request cannot be saved safely by this browser. Nothing was sent.");
  const fingerprint = bytesToHex(sha256(encoder.encode(JSON.stringify([method, path, selected, bytesToHex(hash.digest())]))));
  let projectId: string | undefined, sessionId: string | undefined, summary: string | undefined;
  try {
    const parsed = textBody ? JSON.parse(textBody) : undefined;
    projectId = typeof parsed?.engagement_id === "string" ? parsed.engagement_id : undefined;
    sessionId = typeof parsed?.session_id === "string" ? parsed.session_id : undefined;
    const messages = parsed?.messages;
    const content = Array.isArray(messages) ? messages.at(-1)?.content : undefined;
    summary = typeof content === "string" ? content.slice(0, 160) : undefined;
  } catch { /* diagnostic-expected: binary and non-JSON mutations have no chat preview. */ }
  // Credential creation/rotation can use the shared transport, but its secrets
  // must never become a browser outbox. Its key/fingerprint still supports GET recovery.
  const secretBody = /"(?:secret|password|api_key|private_key|token|confirmation_token|claim_token)"\s*:/.test(textBody ?? "");
  const replayable = textBody !== undefined && !secretBody && encodedBytes(textBody) <= MAX_PENDING_RECORD_BYTES - 16_384;
  return { path, method, headers: selected as [string, string][], fingerprint,
    body: replayable ? textBody : undefined, replayable, projectId, sessionId, summary };
}

export function receiptId(response: Response, body?: unknown): string | undefined {
  const value = body as {command_id?: unknown; receipt?: {command_id?: unknown}} | undefined;
  const id = response.headers.get("X-Nebula-Command-Id") ?? value?.receipt?.command_id ?? value?.command_id;
  return typeof id === "string" && /^[!-~]{1,200}$/.test(id) ? id : undefined;
}

export async function waitForReceipt(response: Response, signal?: AbortSignal): Promise<void> {
  const seconds = Number(response.headers.get("Retry-After"));
  const delay = Number.isFinite(seconds) && seconds > 0 ? Math.min(10_000, seconds * 1000) : 1000;
  await new Promise<void>((resolve, reject) => {
    const done = () => { signal?.removeEventListener("abort", abort); resolve(); };
    const timer = setTimeout(done, delay);
    const abort = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(new DOMException("Viewer detached", "AbortError")); };
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
}
