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

export interface FindingDraftRequest {
  engagementId: string;
  title: string;
  description: string;
  evidenceId: string;
}

interface WorkbenchDraftContextValue {
  assistantDrafts: SelectionActionDraft[];
  assistantDraftNotice?: string;
  noteDraft?: SelectionActionDraft;
  executionDraft?: SelectionActionDraft;
  findingDraft?: FindingDraftRequest;
  requestNebulaDraft(request: NebulaDraftRequest): void;
  requestNoteDraft(request: NebulaDraftRequest): void;
  requestFindingDraft(request: FindingDraftRequest): void;
  removeAssistantDraft(index: number): void;
  clearAssistantDrafts(): void;
  clearAssistantDraftNotice(): void;
  clearNoteDraft(): void;
  clearExecutionDraft(): void;
  clearFindingDraft(): void;
}

const WorkbenchDraftContext = createContext<WorkbenchDraftContextValue | undefined>(undefined);

export const ASSISTANT_CONTEXT_CHARACTER_LIMIT = 20_000;
export const ASSISTANT_CONTEXT_ITEM_LIMIT = 20;

interface AssistantDraftMerge {
  drafts: SelectionActionDraft[];
  notice?: string;
}

function sameAssistantDraft(left: SelectionActionDraft, right: SelectionActionDraft): boolean {
  return left.source.kind === right.source.kind
    && left.source.id === right.source.id
    && left.source.label === right.source.label
    && left.text === right.text;
}

/** Keeps the browser pack inside Core's exact 20-item / 20,000-character contract. */
export function mergeAssistantDraft(
  current: SelectionActionDraft[],
  next: SelectionActionDraft,
): AssistantDraftMerge {
  if (current.some((item) => sameAssistantDraft(item, next))) {
    return { drafts: current, notice: `${next.source.label} is already in the context pack.` };
  }
  if (current.length >= ASSISTANT_CONTEXT_ITEM_LIMIT) {
    return { drafts: current, notice: `A context pack can contain up to ${ASSISTANT_CONTEXT_ITEM_LIMIT} selections.` };
  }
  const used = current.reduce((total, item) => total + item.text.length, 0);
  let remaining = ASSISTANT_CONTEXT_CHARACTER_LIMIT - used;
  if (remaining <= 0) {
    return { drafts: current, notice: "The 20,000-character context pack is full. Remove a selection before adding another." };
  }
  if (next.text.length <= remaining) return { drafts: [...current, next] };
  if (
    remaining > 0
    && next.text.charCodeAt(remaining - 1) >= 0xd800
    && next.text.charCodeAt(remaining - 1) <= 0xdbff
    && next.text.charCodeAt(remaining) >= 0xdc00
    && next.text.charCodeAt(remaining) <= 0xdfff
  ) remaining -= 1;
  if (remaining <= 0) {
    return { drafts: current, notice: "The 20,000-character context pack is full. Remove a selection before adding another." };
  }
  return {
    drafts: [...current, {
      ...next,
      text: next.text.slice(0, remaining),
      truncated: true,
    }],
    notice: `${next.source.label} was bounded to the remaining ${remaining.toLocaleString()} context characters.`,
  };
}

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
  const canonicalProject = /^\/projects\/[^/]+(?:\/([^/]+))?/.exec(pathname);
  if (canonicalProject) {
    const surface = canonicalProject[1];
    if (!surface) return { kind: "project", label: "Project selection" };
    if (surface === "workbench") return { kind: "workbench", label: "Workbench selection" };
    const singular = surface === "evidence" ? "evidence" : surface.replace(/s$/, "");
    return { kind: singular, label: `${singular[0]?.toUpperCase() ?? ""}${singular.slice(1)} selection` };
  }
  const kind = pathname.replace(/^\/+|\/+$/g, "").replace(/[^a-z0-9._-]+/g, "-") || "document";
  return { kind, label: `${kind[0]?.toUpperCase() ?? ""}${kind.slice(1)} selection` };
}

export function WorkbenchDraftProvider({ children }: PropsWithChildren) {
  const location = useLocation();
  const navigate = useNavigate();
  const [assistantContext, setAssistantContext] = useState<AssistantDraftMerge>({ drafts: [] });
  const { drafts: assistantDrafts, notice: assistantDraftNotice } = assistantContext;
  const [noteDraft, setNoteDraft] = useState<SelectionActionDraft>();
  const [executionDraft, setExecutionDraft] = useState<SelectionActionDraft>();
  const [findingDraft, setFindingDraft] = useState<FindingDraftRequest>();

  const requestNebulaDraft = useCallback((request: NebulaDraftRequest) => {
    const next = toSelectionDraft(request);
    if (!next) return;
    setAssistantContext((current) => mergeAssistantDraft(current.drafts, next));
    navigate("/?view=chat");
  }, [navigate]);

  const requestNoteDraft = useCallback((request: NebulaDraftRequest) => {
    const next = toSelectionDraft(request);
    if (!next) return;
    setNoteDraft(next);
    navigate("/?view=notes");
  }, [navigate]);

  const requestFindingDraft = useCallback((request: FindingDraftRequest) => {
    setFindingDraft(request);
    navigate("/findings");
  }, [navigate]);

  const openAssistantSelection = useCallback((draft: SelectionActionDraft) => {
    setAssistantContext((current) => mergeAssistantDraft(current.drafts, draft));
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
  const removeAssistantDraft = useCallback((index: number) => {
    setAssistantContext((current) => ({
      drafts: current.drafts.filter((_, currentIndex) => currentIndex !== index),
    }));
  }, []);
  const clearAssistantDrafts = useCallback(() => {
    setAssistantContext({ drafts: [] });
  }, []);
  const clearAssistantDraftNotice = useCallback(() => {
    setAssistantContext((current) => ({ drafts: current.drafts }));
  }, []);
  const clearNoteDraft = useCallback(() => setNoteDraft(undefined), []);
  const clearExecutionDraft = useCallback(() => setExecutionDraft(undefined), []);
  const clearFindingDraft = useCallback(() => setFindingDraft(undefined), []);
  const resolveSource = useCallback(
    (element: Element | null) => sourceForRoute(location.pathname, element),
    [location.pathname],
  );

  const value = useMemo<WorkbenchDraftContextValue>(() => ({
    assistantDrafts,
    assistantDraftNotice,
    noteDraft,
    executionDraft,
    findingDraft,
    requestNebulaDraft,
    requestNoteDraft,
    requestFindingDraft,
    removeAssistantDraft,
    clearAssistantDrafts,
    clearAssistantDraftNotice,
    clearNoteDraft,
    clearExecutionDraft,
    clearFindingDraft,
  }), [
    assistantDrafts,
    assistantDraftNotice,
    clearAssistantDraftNotice,
    clearAssistantDrafts,
    clearNoteDraft,
    clearExecutionDraft,
    clearFindingDraft,
    executionDraft,
    findingDraft,
    noteDraft,
    removeAssistantDraft,
    requestNebulaDraft,
    requestNoteDraft,
    requestFindingDraft,
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
