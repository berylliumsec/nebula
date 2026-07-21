import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

export interface WorkbenchEditorBuffer {
  content: string;
  expectedSha256?: string;
  existing: boolean;
  filePath: string;
  savedContent: string;
}

interface WorkbenchEditorContextValue {
  bufferFor(engagementId: string): WorkbenchEditorBuffer | undefined;
  setBuffer(engagementId: string, buffer: WorkbenchEditorBuffer | undefined): void;
}

const WorkbenchEditorContext = createContext<WorkbenchEditorContextValue | undefined>(undefined);

export function WorkbenchEditorProvider({ children }: PropsWithChildren) {
  const [buffers, setBuffers] = useState<Record<string, WorkbenchEditorBuffer>>({});
  const bufferFor = useCallback((engagementId: string) => buffers[engagementId], [buffers]);
  const setBuffer = useCallback((engagementId: string, buffer: WorkbenchEditorBuffer | undefined) => {
    setBuffers((current) => {
      if (!buffer) {
        if (!(engagementId in current)) return current;
        const next = { ...current };
        delete next[engagementId];
        return next;
      }
      return { ...current, [engagementId]: buffer };
    });
  }, []);
  const value = useMemo(() => ({ bufferFor, setBuffer }), [bufferFor, setBuffer]);
  return <WorkbenchEditorContext.Provider value={value}>{children}</WorkbenchEditorContext.Provider>;
}

export function useWorkbenchEditor(engagementId: string) {
  const context = useContext(WorkbenchEditorContext);
  if (!context) throw new Error("useWorkbenchEditor must be used inside WorkbenchEditorProvider.");
  return {
    buffer: context.bufferFor(engagementId),
    setBuffer: (buffer: WorkbenchEditorBuffer | undefined) => context.setBuffer(engagementId, buffer),
  };
}
