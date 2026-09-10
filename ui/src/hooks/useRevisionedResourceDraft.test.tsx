import {act, renderHook} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import {useRevisionedResourceDraft} from "./useRevisionedResourceDraft";

const first = {id: "a", title: "First", revision: 1};
const second = {id: "b", title: "Second", revision: 1};
const draft = (item: typeof first) => ({title: item.title});
function setup() {
  return renderHook(({project, item}: {project: string; item?: typeof first}) => useRevisionedResourceDraft(project, item, draft), {initialProps: {project: "project", item: first} as {project: string; item?: typeof first}});
}

describe("revisioned resource drafts", () => {
  it("retains each record's draft across list and history navigation", () => {
    const {result, rerender} = setup();
    act(() => result.current.update(() => ({title: "First unsaved"})));
    rerender({project: "project", item: undefined});
    expect(result.current.draft).toBeUndefined();
    rerender({project: "project", item: second});
    expect(result.current.draft?.title).toBe("Second");
    act(() => result.current.update(() => ({title: "Second unsaved"})));
    rerender({project: "project", item: first});
    expect(result.current.draft?.title).toBe("First unsaved");
  });
  it("does not mix identical record IDs across projects", () => {
    const {result, rerender} = setup();
    act(() => result.current.update(() => ({title: "Private draft"})));
    rerender({project: "other", item: first});
    expect(result.current.draft?.title).toBe("First");
  });
  it("preserves the original revision and input after a conflict", async () => {
    const {result, rerender} = setup();
    act(() => result.current.update(() => ({title: "Unsaved"})));
    rerender({project: "project", item: {...first, revision: 2, title: "Concurrent"}});
    const persist = vi.fn().mockRejectedValue(new Error("Revision conflict"));
    await act(async () => {await result.current.save(persist);});
    expect(persist).toHaveBeenCalledWith(first, {title: "Unsaved"});
    expect(result.current.error).toBe("Revision conflict");
    expect(result.current.draft?.title).toBe("Unsaved");
    expect(result.current.saving).toBe(false);
  });
  it("deduplicates submissions and confines late success to its owner", async () => {
    const {result, rerender} = setup();
    let resolve!: (value: typeof first) => void;
    const persist = vi.fn(() => new Promise<typeof first>(done => {resolve = done;}));
    let pending!: Promise<typeof first | undefined>;
    act(() => {pending = result.current.save(persist); void result.current.save(persist);});
    expect(persist).toHaveBeenCalledTimes(1);
    rerender({project: "project", item: second});
    expect(result.current.saving).toBe(false);
    await act(async () => {resolve({...first, title: "Saved", revision: 2}); await pending;});
    expect(result.current.snapshot?.id).toBe("b");
    expect(result.current.draft?.title).toBe("Second");
    rerender({project: "project", item: first});
    expect(result.current.snapshot?.revision).toBe(2);
    expect(result.current.draft?.title).toBe("Saved");
  });
  it("does not expose a previous record's late failure", async () => {
    const {result, rerender} = setup();
    let reject!: (error: Error) => void;
    let pending!: Promise<typeof first | undefined>;
    act(() => {pending = result.current.save(() => new Promise((_, fail) => {reject = fail;}));});
    rerender({project: "project", item: second});
    await act(async () => {reject(new Error("First failed")); await pending;});
    expect(result.current.error).toBeUndefined();
    rerender({project: "project", item: first});
    expect(result.current.error).toBe("First failed");
  });
  it("discards only the selected draft and accepts the latest saved snapshot", () => {
    const {result, rerender} = setup();
    act(() => result.current.update(() => ({title: "Unsaved"})));
    rerender({project: "project", item: {...first, title: "Concurrent", revision: 2}});
    act(() => result.current.discard());
    expect(result.current.draft?.title).toBe("Concurrent");
    expect(result.current.snapshot?.revision).toBe(2);
  });
});
