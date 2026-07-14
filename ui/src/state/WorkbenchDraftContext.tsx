import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  createSelectionDraft,
  SelectionActionsProvider,
  type SelectionActionDraft,
  type SelectionSource,
} from "../components/selection";

export interface NebulaDraftRequest {
  text: string;
  sourceKind: string;
  sourceId?: string;
  sourceLabel: string;
  truncated?: boolean;
}

interface WorkbenchDraftContextValue {
  assistantDraft?: SelectionActionDraft;
  noteDraft?: SelectionActionDraft;
  executionDraft?: SelectionActionDraft;
  requestNebulaDraft(request: NebulaDraftRequest): void;
  requestNoteDraft(request: NebulaDraftRequest): void;
  clearAssistantDraft(): void;
  clearNoteDraft(): void;
  clearExecutionDraft(): void;
}

const WorkbenchDraftContext = createContext<WorkbenchDraftContextValue | undefined>(undefined);

function toSelectionDraft(request: NebulaDraftRequest): SelectionActionDraft | undefined {
  const draft = createSelectionDraft({
    text: request.text,
    source: {
      kind: request.sourceKind,
      id: request.sourceId,
      label: request.sourceLabel,
    },
  });
  return draft && request.truncated ? { ...draft, truncated: true } : draft;
}

function sourceForRoute(pathname: string, element: Element | null): SelectionSource {
  const sourceElement = element?.closest<HTMLElement>("[data-selection-source-kind]");
  if (sourceElement) {
    return {
      kind: sourceElement.dataset.selectionSourceKind || "document",
      id: sourceElement.dataset.selectionSourceId || undefined,
      label: sourceElement.dataset.selectionSourceLabel || "Selected text",
    };
  }
  if (pathname === "/") return { kind: "workbench", label: "Workbench" };
  const kind = pathname.replace(/^\/+|\/+$/g, "").replace(/[^a-z0-9._-]+/g, "-") || "document";
  return { kind, label: `${kind[0]?.toUpperCase() ?? ""}${kind.slice(1)} selection` };
}

export function WorkbenchDraftProvider({ children }: PropsWithChildren) {
  const location = useLocation();
  const navigate = useNavigate();
  const [assistantDraft, setAssistantDraft] = useState<SelectionActionDraft>();
  const [noteDraft, setNoteDraft] = useState<SelectionActionDraft>();
  const [executionDraft, setExecutionDraft] = useState<SelectionActionDraft>();

  const requestNebulaDraft = useCallback((request: NebulaDraftRequest) => {
    const next = toSelectionDraft(request);
    if (!next) return;
    setAssistantDraft(next);
    navigate("/?view=chat");
  }, [navigate]);

  const requestNoteDraft = useCallback((request: NebulaDraftRequest) => {
    const next = toSelectionDraft(request);
    if (!next) return;
    setNoteDraft(next);
    navigate("/?view=notes");
  }, [navigate]);

  const openAssistantSelection = useCallback((draft: SelectionActionDraft) => {
    setAssistantDraft(draft);
    navigate("/?view=chat");
  }, [navigate]);
  const openNoteSelection = useCallback((draft: SelectionActionDraft) => {
    setNoteDraft(draft);
    navigate("/?view=notes");
  }, [navigate]);
  const openRunSelection = useCallback((draft: SelectionActionDraft) => {
    setExecutionDraft(draft);
    navigate("/?view=terminal");
  }, [navigate]);
  const clearAssistantDraft = useCallback(() => setAssistantDraft(undefined), []);
  const clearNoteDraft = useCallback(() => setNoteDraft(undefined), []);
  const clearExecutionDraft = useCallback(() => setExecutionDraft(undefined), []);
  const resolveSource = useCallback(
    (element: Element | null) => sourceForRoute(location.pathname, element),
    [location.pathname],
  );

  const value = useMemo<WorkbenchDraftContextValue>(() => ({
    assistantDraft,
    noteDraft,
    executionDraft,
    requestNebulaDraft,
    requestNoteDraft,
    clearAssistantDraft,
    clearNoteDraft,
    clearExecutionDraft,
  }), [
    assistantDraft,
    clearAssistantDraft,
    clearNoteDraft,
    clearExecutionDraft,
    executionDraft,
    noteDraft,
    requestNebulaDraft,
    requestNoteDraft,
  ]);

  return <WorkbenchDraftContext.Provider value={value}>
    <SelectionActionsProvider
      onAsk={openAssistantSelection}
      onAddNote={openNoteSelection}
      onRun={openRunSelection}
      resolveSource={resolveSource}
    >
      {children}
    </SelectionActionsProvider>
  </WorkbenchDraftContext.Provider>;
}

export function useWorkbenchDrafts(): WorkbenchDraftContextValue {
  const value = useContext(WorkbenchDraftContext);
  if (!value) throw new Error("useWorkbenchDrafts must be used inside WorkbenchDraftProvider.");
  return value;
}
