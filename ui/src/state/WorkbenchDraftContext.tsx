import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { ResourceKind, ResourceRef } from "../api/types";
import { desktopDeviceId } from "../api/runtime";
import { logCaughtDiagnostic } from "../diagnostics";
import { projectSurface } from "../resourceRoutes";
import { sha256Hex } from "../sha256";
import { useWorkspace } from "./WorkspaceContext";
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
  activeHandoffIds: string[];
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

const handoffResourceKinds: Partial<Record<string, ResourceKind>> = {
  project: "project",
  conversation: "conversation",
  workbench: "conversation",
  note: "note",
  source: "source",
  knowledge: "source",
  workspace_file: "workspace_file",
  asset: "asset",
  evidence: "evidence",
  finding: "finding",
  report: "report",
  terminal: "terminal_session",
  terminal_command: "terminal_command",
  browser_session: "browser_session",
  browser_tab: "browser_tab",
  browser_exchange: "browser_exchange",
  mission: "mission",
  execution: "execution",
};

interface AssistantDraftMerge {
  drafts: SelectionActionDraft[];
  notice?: string;
}

export interface SelectionHandoffMetadata {
  sourceRefs: ResourceRef[];
  sourceHashes: Record<string, string>;
  sourceLabels: Record<string, string>;
}

/** Keeps the canonical conversation selected when a handoff starts inside Assistant. */
export function assistantHandoffSessionId(
  projectId: string,
  pathname: string,
  search: string,
): string | undefined {
  if (pathname !== projectSurface(projectId, "workbench")) return undefined;
  return new URLSearchParams(search).get("session") || undefined;
}

function assistantHandoffPath(projectId: string, sessionId?: string, handoffId?: string): string {
  const parameters = new URLSearchParams({ view: "chat" });
  if (sessionId) parameters.set("session", sessionId);
  if (handoffId) parameters.set("handoff", handoffId);
  return `${projectSurface(projectId, "workbench")}?${parameters}`;
}

/** Builds the durable, reference-only portion of a transient selection handoff. */
export function selectionHandoffMetadata(
  projectId: string,
  draft: SelectionActionDraft,
): SelectionHandoffMetadata {
  const kind = handoffResourceKinds[draft.source.kind];
  const sourceRefs: ResourceRef[] = kind && draft.source.id
    ? [{ projectId, kind, id: draft.source.id }]
    : [];
  const sourceKey = sourceRefs[0] ? `${sourceRefs[0].kind}:${sourceRefs[0].id}` : undefined;
  return {
    sourceRefs,
    sourceHashes: sourceKey ? { [sourceKey]: sha256Hex(draft.text) } : {},
    sourceLabels: sourceKey ? { [sourceKey]: draft.source.label } : {},
  };
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
  const { api, engagement } = useWorkspace();
  const location = useLocation();
  const navigate = useNavigate();
  const [assistantContext, setAssistantContext] = useState<AssistantDraftMerge>({ drafts: [] });
  const { drafts: assistantDrafts, notice: assistantDraftNotice } = assistantContext;
  const [noteDraft, setNoteDraft] = useState<SelectionActionDraft>();
  const [executionDraft, setExecutionDraft] = useState<SelectionActionDraft>();
  const [findingDraft, setFindingDraft] = useState<FindingDraftRequest>();
  const [activeHandoffIds, setActiveHandoffIds] = useState<string[]>([]);

  const persistSelectionHandoff = useCallback(async (
    draft: SelectionActionDraft,
    actionId: string,
    view: string,
    assistantSessionId?: string,
  ) => {
    if (!api || !engagement) return;
    const metadata = selectionHandoffMetadata(engagement.id, draft);
    try {
      const envelope = await api.createHandoff({
        projectId: engagement.id,
        sourceRefs: metadata.sourceRefs,
        actionId,
        originDeviceId: await desktopDeviceId(),
        sourceHashes: metadata.sourceHashes,
        sourceLabels: metadata.sourceLabels,
        transient: true,
      });
      setActiveHandoffIds((current) => [...new Set([...current, envelope.id])]);
      if (view === "chat") {
        navigate(assistantHandoffPath(engagement.id, assistantSessionId, envelope.id), { replace: true });
        return;
      }
      const parameters = new URLSearchParams({ view, handoff: envelope.id });
      navigate(`${projectSurface(engagement.id, "workbench")}?${parameters}`, { replace: true });
    } catch (error) {
      void logCaughtDiagnostic(
        "interface.handoff.selection_create_failed",
        "The selection stayed in memory, but its durable handoff reference could not be created.",
        error,
        "handoffs",
      );
    }
  }, [api, engagement, navigate]);

  const requestNebulaDraft = useCallback((request: NebulaDraftRequest) => {
    const next = toSelectionDraft(request);
    if (!next) return;
    const currentSessionId = engagement
      ? assistantHandoffSessionId(engagement.id, location.pathname, location.search)
      : undefined;
    setAssistantContext((current) => mergeAssistantDraft(current.drafts, next));
    navigate(engagement ? assistantHandoffPath(engagement.id, currentSessionId) : "/?view=chat");
    void persistSelectionHandoff(next, "ask_nebula", "chat", currentSessionId);
  }, [engagement, location.pathname, location.search, navigate, persistSelectionHandoff]);

  const requestNoteDraft = useCallback((request: NebulaDraftRequest) => {
    const next = toSelectionDraft(request);
    if (!next) return;
    setNoteDraft(next);
    navigate(engagement ? `${projectSurface(engagement.id, "workbench")}?view=notes` : "/?view=notes");
    void persistSelectionHandoff(next, "take_note", "notes");
  }, [engagement, navigate, persistSelectionHandoff]);

  const requestFindingDraft = useCallback((request: FindingDraftRequest) => {
    setFindingDraft(request);
    navigate(`/projects/${encodeURIComponent(request.engagementId)}/findings`);
    if (!api) return;
    void (async () => {
      try {
        const source: ResourceRef = { projectId: request.engagementId, kind: "evidence", id: request.evidenceId };
        const envelope = await api.createHandoff({
          projectId: request.engagementId,
          sourceRefs: [source],
          actionId: "draft_finding",
          originDeviceId: await desktopDeviceId(),
          sourceLabels: { [`evidence:${request.evidenceId}`]: request.title },
        });
        setActiveHandoffIds((current) => [...new Set([...current, envelope.id])]);
        navigate(`/projects/${encodeURIComponent(request.engagementId)}/findings?handoff=${encodeURIComponent(envelope.id)}`, { replace: true });
      } catch (error) {
        void logCaughtDiagnostic("interface.handoff.finding_create_failed", "The finding draft remained in memory, but its durable source handoff could not be created.", error, "handoffs");
      }
    })();
  }, [api, navigate]);

  const openAssistantSelection = useCallback((draft: SelectionActionDraft) => {
    const currentSessionId = engagement
      ? assistantHandoffSessionId(engagement.id, location.pathname, location.search)
      : undefined;
    setAssistantContext((current) => mergeAssistantDraft(current.drafts, draft));
    navigate(engagement ? assistantHandoffPath(engagement.id, currentSessionId) : "/?view=chat");
    void persistSelectionHandoff(draft, "ask_nebula", "chat", currentSessionId);
  }, [engagement, location.pathname, location.search, navigate, persistSelectionHandoff]);
  const openNoteSelection = useCallback((draft: SelectionActionDraft) => {
    setNoteDraft(draft);
    navigate(engagement ? `${projectSurface(engagement.id, "workbench")}?view=notes` : "/?view=notes");
    void persistSelectionHandoff(draft, "take_note", "notes");
  }, [engagement, navigate, persistSelectionHandoff]);
  const openRunSelection = useCallback((draft: SelectionActionDraft) => {
    setExecutionDraft(draft);
    navigate(engagement ? `${projectSurface(engagement.id, "workbench")}?view=terminal` : "/?view=terminal");
    void persistSelectionHandoff(draft, "run", "terminal");
  }, [engagement, navigate, persistSelectionHandoff]);
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
    activeHandoffIds,
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
    activeHandoffIds,
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
