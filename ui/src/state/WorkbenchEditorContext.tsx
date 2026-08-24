import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

export interface WorkbenchEditorBuffer {
  id: string;
  content: string;
  expectedSha256?: string;
  existing: boolean;
  filePath: string;
  savedContent: string;
}

interface WorkbenchEditorSession {
  activeId?: string;
  buffers: WorkbenchEditorBuffer[];
}

interface WorkbenchEditorContextValue {
  sessionFor(engagementId: string): WorkbenchEditorSession;
  updateSession(
    engagementId: string,
    update: (session: WorkbenchEditorSession) => WorkbenchEditorSession,
  ): void;
}

const EMPTY_SESSION: WorkbenchEditorSession = { buffers: [] };
const WorkbenchEditorContext = createContext<WorkbenchEditorContextValue | undefined>(undefined);

export function newEditorBufferId(prefix = "draft"): string {
  return `${prefix}:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

export function WorkbenchEditorProvider({ children }: PropsWithChildren) {
  const [sessions, setSessions] = useState<Record<string, WorkbenchEditorSession>>({});
  const sessionFor = useCallback(
    (engagementId: string) => sessions[engagementId] ?? EMPTY_SESSION,
    [sessions],
  );
  const updateSession = useCallback((
    engagementId: string,
    update: (session: WorkbenchEditorSession) => WorkbenchEditorSession,
  ) => {
    setSessions((current) => {
      const previous = current[engagementId] ?? EMPTY_SESSION;
      const next = update(previous);
      return next === previous ? current : { ...current, [engagementId]: next };
    });
  }, []);
  const value = useMemo(() => ({ sessionFor, updateSession }), [sessionFor, updateSession]);
  return <WorkbenchEditorContext.Provider value={value}>{children}</WorkbenchEditorContext.Provider>;
}

export function useWorkbenchEditor(engagementId: string) {
  const context = useContext(WorkbenchEditorContext);
  if (!context) throw new Error("useWorkbenchEditor must be used inside WorkbenchEditorProvider.");
  const session = context.sessionFor(engagementId);
  const activeIndex = session.buffers.findIndex((buffer) => buffer.id === session.activeId);
  const buffer = activeIndex >= 0 ? session.buffers[activeIndex] : undefined;

  const setBuffer = (next: WorkbenchEditorBuffer | undefined) => {
    context.updateSession(engagementId, (current) => {
      if (!next) {
        const closingIndex = current.buffers.findIndex((candidate) => candidate.id === current.activeId);
        if (closingIndex < 0) return current;
        const buffers = current.buffers.filter((candidate) => candidate.id !== current.activeId);
        const replacement = buffers[Math.min(closingIndex, buffers.length - 1)];
        return { activeId: replacement?.id, buffers };
      }
      const existingIndex = current.buffers.findIndex((candidate) => candidate.id === next.id);
      if (existingIndex < 0) return { activeId: next.id, buffers: [...current.buffers, next] };
      const buffers = [...current.buffers];
      buffers[existingIndex] = next;
      return { activeId: next.id, buffers };
    });
  };

  const activateBuffer = (id: string) => context.updateSession(engagementId, (current) => (
    current.buffers.some((candidate) => candidate.id === id)
      ? { ...current, activeId: id }
      : current
  ));

  const closeBuffer = (id: string) => context.updateSession(engagementId, (current) => {
    const closingIndex = current.buffers.findIndex((candidate) => candidate.id === id);
    if (closingIndex < 0) return current;
    const buffers = current.buffers.filter((candidate) => candidate.id !== id);
    if (current.activeId !== id) return { ...current, buffers };
    const replacement = buffers[Math.min(closingIndex, buffers.length - 1)];
    return { activeId: replacement?.id, buffers };
  });

  const updateBuffers = (update: (buffers: WorkbenchEditorBuffer[]) => WorkbenchEditorBuffer[]) => {
    context.updateSession(engagementId, (current) => ({ ...current, buffers: update(current.buffers) }));
  };

  return {
    buffer,
    buffers: session.buffers,
    activateBuffer,
    closeBuffer,
    setBuffer,
    updateBuffers,
  };
}
