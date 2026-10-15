import { useCallback, useEffect, useId, useRef, useState } from "react";
import { BookOpen, ChevronDown, RefreshCw } from "lucide-react";
import type { ApiClient } from "../api/client";
import { deleteButlerMemory, readButlerMemory, updateButlerMemory, type ButlerMemoryItem, type ButlerMemoryResult, type ButlerMemoryScope } from "../api/butlerMemory";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./ButlerMemory.module.css";

const scopeLabel: Record<ButlerMemoryScope, string> = {
  global_preference: "Preference",
  global_lesson: "General lesson",
  project_lesson: "Project lesson",
};

function dateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function MemoryRow({ item, onEdit, onDelete, editing, deleting, busy, disabled, draft, setDraft, save, cancel, confirmDelete }: {
  item: ButlerMemoryItem;
  onEdit(): void;
  onDelete(): void;
  editing: boolean;
  deleting: boolean;
  busy: boolean;
  disabled: boolean;
  draft: string;
  setDraft(value: string): void;
  save(): void;
  cancel(): void;
  confirmDelete(): void;
}) {
  const preview = item.content.length > 170 ? `${item.content.slice(0, 170).trimEnd()}…` : item.content;
  return <li className={styles.row}>
    <details>
      <summary><span className={styles.preview}>{preview}</span><small>{scopeLabel[item.scope]} · {item.sourceLabel} · Updated {dateLabel(item.updatedAt)}</small></summary>
      <div className={styles.rowBody}>
        {item.content.length > 170 && <p className={styles.fullContent}>{item.content}</p>}
        {item.sourceRef && <p className={styles.source}>Source reference <code>{item.sourceRef}</code></p>}
        {editing ? <div className={styles.editForm}>
          <label htmlFor={`butler-memory-edit-${item.id}`}>Memory content</label>
          <textarea id={`butler-memory-edit-${item.id}`} value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} maxLength={4000} autoFocus />
          <div className={styles.actions}><button className="button primary" type="button" disabled={busy || disabled || !draft.trim() || draft.trim() === item.content} onClick={save}>{busy ? "Saving…" : "Save changes"}</button><button className="button quiet" type="button" disabled={busy} onClick={cancel}>Cancel</button></div>
        </div> : deleting ? <div className={styles.deleteConfirm}>
          <p>Delete this memory? Butler will no longer use it.</p>
          <div className={styles.actions}><button className="button danger" type="button" disabled={busy || disabled} onClick={confirmDelete}>{busy ? "Deleting…" : "Delete memory"}</button><button className="button quiet" type="button" disabled={busy} onClick={cancel}>Cancel</button></div>
        </div> : <div className={styles.actions}><button className="button quiet" type="button" disabled={busy || disabled} onClick={onEdit}>Edit</button><button className="button quiet" type="button" disabled={busy || disabled} onClick={onDelete}>Delete</button></div>}
      </div>
    </details>
  </li>;
}

function ButlerMemoryForProject({ api, projectId, coreState }: {
  api?: ApiClient;
  projectId: string;
  coreState: "checking" | "online" | "offline";
}) {
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<ButlerMemoryResult>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<{ id: string; draft: string }>();
  const [deletingId, setDeletingId] = useState<string>();
  const [busyId, setBusyId] = useState<string>();
  const request = useRef<AbortController | undefined>(undefined);

  const load = useCallback(async (): Promise<boolean> => {
    if (!api || coreState !== "online") return false;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    try {
      const next = await readButlerMemory(api, projectId, query, controller.signal);
      if (!controller.signal.aborted) setResult(next);
      return !controller.signal.aborted;
    } catch (caught) {
      if (controller.signal.aborted) return false;
      void logCaughtDiagnostic("interface.butler_memory.load_failed", "Butler memory could not be loaded.", caught, "butler_memory");
      setError(caught instanceof Error ? caught.message : "Could not load Butler memory.");
      return false;
    } finally {
      if (!controller.signal.aborted) setLoading(false);
      if (request.current === controller) request.current = undefined;
    }
  }, [api, coreState, projectId, query]);

  useEffect(() => {
    if (open) void load();
    return () => request.current?.abort();
  }, [open, load]);

  const mutate = async (item: ButlerMemoryItem, operation: "edit" | "delete") => {
    if (!api || coreState !== "online") return;
    setBusyId(item.id);
    setActionError("");
    setNotice("");
    try {
      if (operation === "edit") {
        const content = editing?.draft.trim();
        if (!content) return;
        await updateButlerMemory(api, projectId, item, content);
      } else {
        await deleteButlerMemory(api, projectId, item);
      }
      setEditing(undefined);
      setDeletingId(undefined);
      const reloaded = await load();
      if (reloaded) setNotice(operation === "edit" ? "Memory updated." : "Memory deleted.");
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_memory.change_failed", "Butler memory could not be changed.", caught, "butler_memory");
      setActionError(`Could not ${operation === "edit" ? "save" : "delete"} memory. ${caught instanceof Error ? caught.message : "Refresh and retry."}`);
    } finally {
      setBusyId(undefined);
    }
  };

  return <section className={styles.root} aria-label="Butler memory">
    <button className={styles.toggle} type="button" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen((value) => !value)}>
      <BookOpen size={17} aria-hidden="true" /><span>Memory</span><ChevronDown className={open ? styles.chevronOpen : undefined} size={17} aria-hidden="true" />
    </button>
    {open && <div id={panelId} className={styles.panel}>
      <div className={styles.intro}><p>Butler remembers useful preferences and lessons as work happens.</p><button className={styles.refresh} type="button" aria-label="Refresh Butler memory" title="Refresh Butler memory" disabled={!api || coreState !== "online" || loading} onClick={() => void load()}><RefreshCw size={17} aria-hidden="true" /></button></div>
      <form className={styles.search} onSubmit={(event) => { event.preventDefault(); const next = queryDraft.trim(); if (next === query) void load(); else { setResult(undefined); setQuery(next); } }}>
        <input aria-label="Search Butler memory" type="search" placeholder="Search memory" value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} />
        <button className="button quiet" type="submit" disabled={!api || coreState !== "online" || loading}>Search</button>
      </form>
      {query && <button className="button quiet" type="button" disabled={loading} onClick={() => { setResult(undefined); setQueryDraft(""); setQuery(""); }}>Show all memory</button>}
      {coreState !== "online" && <p className={styles.note} role="status">Core is offline. Reconnect to review or change memory.</p>}
      {loading && <p className={styles.note} role="status">Loading memory…</p>}
      {error && <div className={styles.error} role="alert"><span>Memory could not be refreshed. {error}</span><button className="button quiet" type="button" disabled={coreState !== "online"} onClick={() => void load()}>Retry</button></div>}
      {actionError && <p className={styles.error} role="alert">{actionError}</p>}
      {notice && <p className={styles.note} role="status">{notice}</p>}
      {result && <>
        <p className={styles.count}>{query ? `${result.items.length} search result${result.items.length === 1 ? "" : "s"}` : `${result.items.length} saved memor${result.items.length === 1 ? "y" : "ies"}`}{result.items.length === 50 ? " · Showing first 50; search to find more" : ""}</p>
        {result.indexStatus.state === "lexical_fallback" && <p className={styles.note}>Search is using keyword matching while semantic search is unavailable.</p>}
        {result.items.length ? <ul className={styles.list}>{result.items.map((item) => <MemoryRow key={item.id} item={item}
          editing={editing?.id === item.id} deleting={deletingId === item.id} busy={busyId === item.id} disabled={!api || coreState !== "online"}
          draft={editing?.id === item.id ? editing.draft : ""} setDraft={(draft) => setEditing({ id: item.id, draft })}
          onEdit={() => { setActionError(""); setDeletingId(undefined); setEditing({ id: item.id, draft: item.content }); }}
          onDelete={() => { setActionError(""); setEditing(undefined); setDeletingId(item.id); }}
          cancel={() => { setEditing(undefined); setDeletingId(undefined); setActionError(""); }}
          save={() => void mutate(item, "edit")} confirmDelete={() => void mutate(item, "delete")} />)}</ul>
          : !loading && !error && <p className={styles.note}>{query ? "No matching memories. Try a different search." : "No memories saved yet. Butler will add useful preferences and lessons as work happens."}</p>}
      </>}
    </div>}
  </section>;
}

/** Keying by project discards prior-project presentation and in-flight reads immediately. */
export function ButlerMemory(props: { api?: ApiClient; projectId: string; coreState: "checking" | "online" | "offline" }) {
  return <ButlerMemoryForProject key={props.projectId} {...props} />;
}
