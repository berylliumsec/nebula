import {useMemo, useRef, useState} from "react";

type Edit<T, D> = {base: T; draft: D; busy?: boolean; error?: string};

/** Transient edits retain their original revision and never follow another URL. */
export function useRevisionedResourceDraft<T extends {id: string}, D>(
  projectId: string | undefined,
  item: T | undefined,
  createDraft: (item: T) => D,
) {
  const [edits, setEdits] = useState<Record<string, Edit<T, D>>>({});
  const locks = useRef(new Set<string>());
  const key = projectId && item ? JSON.stringify([projectId, item.id]) : undefined;
  const edit = useMemo(() => key && item
    ? edits[key] ?? {base: item, draft: createDraft(item)}
    : undefined, [createDraft, edits, item, key]);

  return {
    snapshot: edit?.base,
    draft: edit?.draft,
    saving: Boolean(edit?.busy),
    error: edit?.error,
    update: (change: (draft: D) => D) => {
      if (!key || !edit || locks.current.has(key)) return;
      setEdits(current => ({...current, [key]: {...(current[key] ?? edit), draft: change((current[key] ?? edit).draft), error: undefined}}));
    },
    discard: () => {
      if (!key || locks.current.has(key)) return;
      setEdits(current => {
        const next = {...current};
        delete next[key];
        return next;
      });
    },
    save: async (persist: (base: T, draft: D) => Promise<T>) => {
      if (!key || !edit || locks.current.has(key)) return;
      locks.current.add(key);
      setEdits(current => ({...current, [key]: {...edit, busy: true, error: undefined}}));
      try {
        const saved = await persist(edit.base, edit.draft);
        if (saved.id !== edit.base.id) throw new Error("The saved response belongs to another record.");
        setEdits(current => ({...current, [key]: {base: saved, draft: createDraft(saved)}}));
        return saved;
      } catch (error) {
        // diagnostic-expected: retain the draft and expose this save failure on its owning record.
        setEdits(current => ({...current, [key]: {...edit, busy: false, error: error instanceof Error ? error.message : "The record could not be saved."}}));
      } finally {
        locks.current.delete(key);
      }
    },
  };
}
