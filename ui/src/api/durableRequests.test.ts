import "fake-indexeddb/auto";
import { beforeEach, describe, expect, it } from "vitest";
import { createStore, del, get, set } from "idb-keyval";
import { identityScope, MAX_PENDING_RECORD_BYTES, PendingRequestStore, requestIntent } from "./durableRequests";

const database = createStore("nebula-pending-requests", "requests");
const key = "immutable-attempts/v1";
const scope = identityScope({core_id: "core-a", principal_id: "operator"});
const intent = (text = "Hello") => requestIntent("chat/completions", "POST", new Headers({Accept: "text/event-stream", Authorization: "Bearer secret"}), JSON.stringify({engagement_id: "project", messages: [{role: "user", content: text}]}));
beforeEach(async () => { await del(key, database); });

describe("bounded immutable pending requests", () => {
  it("persists exact text and deduplicates an unresolved intent across client reopen", async () => {
    const first = await new PendingRequestStore(scope).prepare(await intent());
    const reopened = await new PendingRequestStore(scope).prepare(await intent());
    expect(reopened.key).toBe(first.key);
    expect(reopened.body).toBe(JSON.stringify({engagement_id: "project", messages: [{role: "user", content: "Hello"}]}));
    expect(JSON.stringify(await get(key, database))).not.toContain("secret");
    expect(first.summary).toBe("Hello");
  });

  it("isolates another Core or principal and keeps the original receipt", async () => {
    const original = new PendingRequestStore(scope);
    const record = await original.prepare(await intent());
    record.commandId = "command"; record.state = "accepted";
    await original.save(record);
    for (const identity of [{core_id: "core-b", principal_id: "operator"}, {core_id: "core-a", principal_id: "device:other"}]) {
      expect(await new PendingRequestStore(identityScope(identity)).list()).toEqual([]);
    }
    expect(await new PendingRequestStore(scope).list()).toEqual([expect.objectContaining({commandId: "command"})]);
  });

  it("refuses changed bytes under the same key and conflicting unconfirmed approval decisions", async () => {
    const store = new PendingRequestStore(scope);
    const first = await store.prepare(await intent());
    await expect(store.prepare(await intent("changed"), first.key)).rejects.toThrow("different contents");
    await store.prepare(await requestIntent("approvals/exact/decision", "POST", new Headers(), '{"decision":"approve"}'));
    await expect(store.prepare(await requestIntent("approvals/exact/decision", "POST", new Headers(), '{"decision":"reject"}'))).rejects.toThrow("earlier decision");
  });

  it("refuses an overfull store without evicting unresolved records", async () => {
    const store = new PendingRequestStore(scope);
    const entry = await intent();
    for (let index = 0; index < 8; index++) await store.prepare({...entry, fingerprint: String(index), body: "x".repeat(MAX_PENDING_RECORD_BYTES - 16_384)});
    await expect(store.prepare({...entry, fingerprint: "too-many", body: "y".repeat(MAX_PENDING_RECORD_BYTES - 16_384)})).rejects.toThrow("storage is full");
    expect(await store.list()).toHaveLength(8);
  });

  it("marks oversized content as non-replayable without retaining it", async () => {
    const entry = await requestIntent("files", "POST", new Headers(), "x".repeat(MAX_PENDING_RECORD_BYTES));
    expect(entry.replayable).toBe(false);
    expect(entry.body).toBeUndefined();
    const store = new PendingRequestStore(scope);
    await store.prepare(entry);
    expect(JSON.stringify(await store.list()).length).toBeLessThan(1024);
  });

  it("preserves unreadable pending data and blocks new requests", async () => {
    await set(key, {unexpected: "keep"}, database);
    await expect(new PendingRequestStore(scope).prepare(await intent())).rejects.toThrow("unreadable");
    expect(await get(key, database)).toEqual({unexpected: "keep"});
  });

  it("keeps credential payloads out of persisted attempts", async () => {
    const entry = await requestIntent("credentials", "POST", new Headers(), '{"secret":"private-value","name":"Provider"}');
    expect(entry.replayable).toBe(false);
    await new PendingRequestStore(scope).prepare(entry);
    expect(JSON.stringify(await get(key, database))).not.toContain("private-value");
  });
});
