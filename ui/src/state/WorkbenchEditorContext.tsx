import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { logCaughtDiagnostic } from "../diagnostics";
import { loadEditorSessions, saveEditorSessions, type PersistedEditorSessions } from "./editorSessionPersistence";

export interface WorkbenchEditorBuffer {
  id: string;
  content: string;
  expectedSha256?: string;
  existing: boolean;
  filePath: string;
  restoreFromCore?: boolean;
  savedContent: string;
}

interface WorkbenchEditorSession {
  activeId?: string;
  buffers: WorkbenchEditorBuffer[];
  primaryId?: string;
  secondaryId?: string;
}

type PersistenceState = "loading" | "ready" | "failed";

interface WorkbenchEditorContextValue {
  persistenceError?: string;
  persistenceState: PersistenceState;
  retryPersistence(): void;
  sessionFor(engagementId: string): WorkbenchEditorSession;
  updateSession(engagementId: string, update: (session: WorkbenchEditorSession) => WorkbenchEditorSession): void;
}

const EMPTY_SESSION: WorkbenchEditorSession = { buffers: [] };
const WorkbenchEditorContext = createContext<WorkbenchEditorContextValue | undefined>(undefined);

export function newEditorBufferId(prefix = "draft"): string {
  return `${prefix}:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

export function WorkbenchEditorProvider({ children }: PropsWithChildren) {
  const [sessions, setSessions] = useState<Record<string, WorkbenchEditorSession>>({});
  const sessionsRef = useRef(sessions);
  const hydratedRef = useRef(false);
  const [persistenceState, setPersistenceState] = useState<PersistenceState>("loading");
  const [persistenceError, setPersistenceError] = useState<string>();
  sessionsRef.current = sessions;

  useEffect(() => {
    let active = true;
    void loadEditorSessions().then((restored) => {
      if (!active) return;
      setSessions((current) => ({ ...restored, ...current }));
      hydratedRef.current = true;
      setPersistenceState("ready");
    }).catch((error) => {
      if (!active) return;
      void logCaughtDiagnostic("interface.editor_session.restore_failed", "Editor hot-exit state could not be restored from this device.", error, "code_editor");
      hydratedRef.current = true;
      setPersistenceState("failed");
      setPersistenceError(error instanceof Error ? error.message : "IndexedDB is unavailable.");
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!hydratedRef.current) return;
    const timer = globalThis.setTimeout(() => {
      void saveEditorSessions(sessions as PersistedEditorSessions).then(() => {
        setPersistenceState("ready");
        setPersistenceError(undefined);
      }).catch((error) => {
        void logCaughtDiagnostic("interface.editor_session.persist_failed", "Editor hot-exit state could not be saved on this device.", error, "code_editor");
        setPersistenceState("failed");
        setPersistenceError(error instanceof Error ? error.message : "IndexedDB rejected the editor draft.");
      });
    }, 250);
    return () => globalThis.clearTimeout(timer);
  }, [sessions]);

  useEffect(() => {
    const persistNow = () => {
      if (!hydratedRef.current) return;
      void saveEditorSessions(sessionsRef.current as PersistedEditorSessions).catch((error) => {
        void logCaughtDiagnostic("interface.editor_session.pagehide_persist_failed", "Editor hot-exit state could not be flushed while leaving the page.", error, "code_editor");
        setPersistenceState("failed");
        setPersistenceError(error instanceof Error ? error.message : "IndexedDB rejected the editor draft.");
      });
    };
    const persistWhenHidden = () => { if (document.visibilityState === "hidden") persistNow(); };
    globalThis.addEventListener("pagehide", persistNow);
    document.addEventListener("visibilitychange", persistWhenHidden);
    return () => {
      globalThis.removeEventListener("pagehide", persistNow);
      document.removeEventListener("visibilitychange", persistWhenHidden);
    };
  }, []);

  const retryPersistence = useCallback(() => {
    setPersistenceState("loading");
    void saveEditorSessions(sessionsRef.current as PersistedEditorSessions).then(() => {
      setPersistenceState("ready");
      setPersistenceError(undefined);
    }).catch((error) => {
      void logCaughtDiagnostic("interface.editor_session.retry_failed", "The editor hot-exit persistence retry failed.", error, "code_editor");
      setPersistenceState("failed");
      setPersistenceError(error instanceof Error ? error.message : "IndexedDB rejected the editor draft.");
    });
  }, []);

  const sessionFor = useCallback((engagementId: string) => sessions[engagementId] ?? EMPTY_SESSION, [sessions]);
  const updateSession = useCallback((engagementId: string, update: (session: WorkbenchEditorSession) => WorkbenchEditorSession) => {
    setSessions((current) => {
      const previous = current[engagementId] ?? EMPTY_SESSION;
      const next = update(previous);
      return next === previous ? current : { ...current, [engagementId]: next };
    });
  }, []);
  const value = useMemo(() => ({ persistenceError, persistenceState, retryPersistence, sessionFor, updateSession }), [persistenceError, persistenceState, retryPersistence, sessionFor, updateSession]);
  return <WorkbenchEditorContext.Provider value={value}>{children}</WorkbenchEditorContext.Provider>;
}

export function useWorkbenchEditor(engagementId: string) {
  const context = useContext(WorkbenchEditorContext);
  if (!context) throw new Error("useWorkbenchEditor must be used inside WorkbenchEditorProvider.");
  const session = context.sessionFor(engagementId);
  const byId = (id?: string) => session.buffers.find((candidate) => candidate.id === id);
  const buffer = byId(session.activeId);
  const primaryBuffer = byId(session.primaryId ?? session.activeId);
  const secondaryBuffer = byId(session.secondaryId);

  const setBuffer = (next: WorkbenchEditorBuffer | undefined) => {
    context.updateSession(engagementId, (current) => {
      if (!next) return current.activeId ? closeBufferInSession(current, current.activeId) : current;
      const existingIndex = current.buffers.findIndex((candidate) => candidate.id === next.id);
      const buffers = existingIndex < 0 ? [...current.buffers, next] : current.buffers.map((candidate) => candidate.id === next.id ? next : candidate);
      if (!current.primaryId) return { activeId: next.id, buffers, primaryId: next.id };
      if (current.activeId === current.secondaryId) return { ...current, activeId: next.id, buffers, secondaryId: next.id };
      return { ...current, activeId: next.id, buffers, primaryId: next.id };
    });
  };

  const updateBuffer = (id: string, changes: Partial<WorkbenchEditorBuffer>) => {
    context.updateSession(engagementId, (current) => ({
      ...current,
      buffers: current.buffers.map((candidate) => candidate.id === id ? { ...candidate, ...changes } : candidate),
    }));
  };

  const activateBuffer = (id: string) => context.updateSession(engagementId, (current) => {
    if (!current.buffers.some((candidate) => candidate.id === id)) return current;
    if (id === current.primaryId || id === current.secondaryId) return { ...current, activeId: id };
    if (current.activeId === current.secondaryId) return { ...current, activeId: id, secondaryId: id };
    return { ...current, activeId: id, primaryId: id };
  });

  const closeBuffer = (id: string) => context.updateSession(engagementId, (current) => closeBufferInSession(current, id));
  const updateBuffers = (update: (buffers: WorkbenchEditorBuffer[]) => WorkbenchEditorBuffer[]) => context.updateSession(engagementId, (current) => ({ ...current, buffers: update(current.buffers) }));

  const splitEditor = () => context.updateSession(engagementId, (current) => {
    const primaryId = current.primaryId ?? current.activeId;
    if (!primaryId || current.secondaryId) return current;
    const secondary = current.buffers.find((candidate) => candidate.id !== primaryId);
    return secondary ? { ...current, activeId: secondary.id, primaryId, secondaryId: secondary.id } : current;
  });
  const closeSplit = () => context.updateSession(engagementId, (current) => current.secondaryId ? { ...current, activeId: current.primaryId, secondaryId: undefined } : current);
  const focusPane = (id: string) => context.updateSession(engagementId, (current) => id !== current.activeId && (id === current.primaryId || id === current.secondaryId) ? { ...current, activeId: id } : current);

  return {
    activateBuffer,
    buffer,
    buffers: session.buffers,
    closeBuffer,
    closeSplit,
    focusPane,
    persistenceError: context.persistenceError,
    persistenceState: context.persistenceState,
    primaryBuffer,
    retryPersistence: context.retryPersistence,
    secondaryBuffer,
    setBuffer,
    splitEditor,
    updateBuffer,
    updateBuffers,
  };
}

function closeBufferInSession(session: WorkbenchEditorSession, id: string): WorkbenchEditorSession {
  const closingIndex = session.buffers.findIndex((candidate) => candidate.id === id);
  if (closingIndex < 0) return session;
  const buffers = session.buffers.filter((candidate) => candidate.id !== id);
  let primaryId = session.primaryId;
  let secondaryId = session.secondaryId;
  if (primaryId === id) {
    primaryId = secondaryId ?? buffers[Math.min(closingIndex, buffers.length - 1)]?.id;
    secondaryId = undefined;
  } else if (secondaryId === id) {
    secondaryId = undefined;
  }
  const activeId = session.activeId === id ? primaryId : session.activeId;
  return { activeId, buffers, primaryId, secondaryId };
}
