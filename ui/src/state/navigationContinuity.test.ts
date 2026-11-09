import { beforeEach, expect, it, vi } from "vitest";
import { forgetNavigation, isApplicationEntry, NAVIGATION_LIMITS, NAVIGATION_STORAGE_KEY, rememberChat, rememberedChatPath, rememberedNavigation, rememberProject } from "./navigationContinuity";

const scope = {key: "core-a:operator", persistent: true};
beforeEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

it("remembers exact project/chat identifiers only in the authenticated Core/principal scope", () => {
  rememberChat(scope, "project / one", "older-chat");
  expect(rememberedChatPath(scope)).toBe("/projects/project%20%2F%20one/workbench?view=chat&session=older-chat");
  for (const key of ["core-b:operator", "core-a:device:other"]) expect(rememberedNavigation({...scope, key})).toBeUndefined();
  expect(JSON.parse(localStorage.getItem(NAVIGATION_STORAGE_KEY)!)[0]).toEqual({scope: scope.key, projectId: "project / one", sessionId: "older-chat", updatedAt: expect.any(Number)});
});

it("keeps a project's chat while refreshing its selection and clears it when the project changes", () => {
  rememberChat(scope, "one", "chat-one"); rememberProject(scope, "one");
  expect(rememberedNavigation(scope)?.sessionId).toBe("chat-one");
  rememberProject(scope, "two");
  expect(rememberedNavigation(scope)?.projectId).toBe("two");
  expect(rememberedChatPath(scope)).toBeUndefined();
  forgetNavigation(scope); expect(rememberedNavigation(scope)).toBeUndefined();
});

it("does not migrate legacy unscoped choices or persist an unidentified Core", () => {
  localStorage.setItem("nebula.engagement", "old-core-project");
  expect(rememberedNavigation(scope)).toBeUndefined();
  expect(rememberChat({key: "window:ephemeral", persistent: false}, "project", "chat")).toBe(true);
  expect(localStorage.getItem(NAVIGATION_STORAGE_KEY)).toBeNull();
});

it("expires old navigation and rejects future timestamps", () => {
  for (const updatedAt of [Date.now() - NAVIGATION_LIMITS.ageMs - 1, Date.now() + 60_000]) {
    localStorage.setItem(NAVIGATION_STORAGE_KEY, JSON.stringify([{scope: scope.key, projectId: "project", sessionId: "chat", updatedAt}]));
    expect(rememberedNavigation(scope)).toBeUndefined();
  }
});

it("bounds retained scope count and total bytes", () => {
  for (let index = 0; index < 70; index++) rememberChat({key: `core-${index}:operator`, persistent: true}, "p".repeat(500), "s".repeat(500));
  const raw = localStorage.getItem(NAVIGATION_STORAGE_KEY)!;
  expect(JSON.parse(raw).length).toBeLessThanOrEqual(NAVIGATION_LIMITS.scopes);
  expect(new TextEncoder().encode(raw).byteLength).toBeLessThanOrEqual(NAVIGATION_LIMITS.bytes);
  expect(rememberedNavigation({key: "core-69:operator", persistent: true})?.sessionId).toHaveLength(500);
});

it("keeps navigation usable when browser storage is blocked", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
  expect(rememberedNavigation(scope)).toBeUndefined();
  expect(rememberChat(scope, "project", "chat")).toBe(false);
});

it("ignores malformed or oversized metadata and rejects unsafe identifiers", () => {
  for (const value of ["{", "[]".repeat(NAVIGATION_LIMITS.bytes)]) {
    localStorage.setItem(NAVIGATION_STORAGE_KEY, value); expect(rememberedNavigation(scope)).toBeUndefined();
  }
  localStorage.removeItem(NAVIGATION_STORAGE_KEY); rememberChat(scope, "project", "chat\nsecret");
  expect(localStorage.getItem(NAVIGATION_STORAGE_KEY)).toBeNull();
});

it("recognizes only the pristine native entry, preserving explicit deep links", () => {
  expect(isApplicationEntry({pathname: "/", search: "", hash: ""})).toBe(true);
  for (const location of [{pathname: "/settings", search: "", hash: ""}, {pathname: "/", search: "?view=chat", hash: ""}, {pathname: "/", search: "", hash: "#pair=secret"}]) expect(isApplicationEntry(location)).toBe(false);
});
