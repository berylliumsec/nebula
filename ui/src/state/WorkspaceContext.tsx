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
import { ApiClient } from "../api/client";
import { NebulaEventStream, type StreamState } from "../api/events";
import { providerVerificationModel } from "../api/providerCapabilities";
import { resolveApiRuntime, type ApiRuntime } from "../api/runtime";
import { setDiagnosticsAvailability } from "../diagnostics";
import type {
  AgentRunSummary,
  ApprovalDecisionRequest,
  ApprovalSummary,
  AssetSummary,
  AssetCreateRequest,
  EngagementCreateRequest,
  EngagementSummary,
  EvidenceSummary,
  EvidenceUploadRequest,
  FindingCreateRequest,
  FindingSummary,
  FindingUpdateRequest,
  HealthResponse,
  KnowledgeIngestRequest,
  KnowledgeSource,
  MissionCreateRequest,
  ObservationSummary,
  ObservationCreateRequest,
  ObservationUpdateRequest,
  OperatorProfile,
  OperatorProfileCreateRequest,
  OperatorProfileUpdateRequest,
  ProviderCatalogEntry,
  ProviderCreateRequest,
  ProviderHealth,
  ProviderUpdateRequest,
  RunEvent,
  ReportCreateRequest,
  ReportSummary,
  ReportUpdateRequest,
  RunStopRequest,
  SetupStatus,
} from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

type CoreState = "checking" | "online" | "offline";
export type WorkspaceState = "starting" | "bootstrapping" | "ready" | "degraded" | "failed";
export type ResourceLoadState = "loading" | "ready" | "empty" | "failed";
export type WorkspaceResource = "projects" | "providers" | "providerCatalog" | "operators" | "setup" | "activity" | "approvals" | "assets" | "findings" | "evidence" | "notes" | "sources" | "reports";
export interface ResourceStatus {
  state: ResourceLoadState;
  error?: unknown;
}

const workspaceResources: WorkspaceResource[] = ["projects", "providers", "providerCatalog", "operators", "setup", "activity", "approvals", "assets", "findings", "evidence", "notes", "sources", "reports"];
const initialResourceStatus = Object.fromEntries(workspaceResources.map((key) => [key, { state: "loading" }])) as Record<WorkspaceResource, ResourceStatus>;

export function evolveRunFromEvent(current: AgentRunSummary, event: RunEvent): AgentRunSummary {
  if (event.runId && event.runId !== current.id) return current;
  const base = { ...current, updatedAt: event.occurredAt };
  if (event.kind === "run.queued") return { ...base, status: "queued" };
  if (event.kind === "run.started") return { ...base, status: "planning", startedAt: current.startedAt ?? event.occurredAt };
  if (event.kind === "run.planned") {
    const tasks = Array.isArray(event.payload.tasks) ? event.payload.tasks.length : current.totalTasks;
    return { ...base, status: "running", totalTasks: tasks };
  }
  if (event.kind === "run.waiting_approval") return { ...base, status: "waiting_approval" };
  if (event.kind === "run.stop_requested") return { ...base, status: "cancelling" };
  if (event.kind === "run.completed") {
    return {
      ...base,
      status: "complete",
      completedTasks: Math.max(current.completedTasks, current.totalTasks),
      spentUsd: typeof event.payload.cost_usd === "number" ? event.payload.cost_usd : current.spentUsd,
    };
  }
  if (event.kind === "run.failed") return { ...base, status: "failed" };
  if (event.kind === "run.cancelled") return { ...base, status: "cancelled" };
  if (event.kind === "task.completed") {
    return { ...base, completedTasks: Math.min(current.totalTasks || Number.MAX_SAFE_INTEGER, current.completedTasks + 1) };
  }
  return current;
}

interface WorkspaceContextValue {
  api?: ApiClient;
  runtime?: ApiRuntime;
  coreState: CoreState;
  workspaceState: WorkspaceState;
  coreError?: string;
  health?: HealthResponse;
  setupStatus?: SetupStatus;
  resourceStatus: Record<WorkspaceResource, ResourceStatus>;
  engagements: EngagementSummary[];
  operatorProfiles: OperatorProfile[];
  activeOperator?: OperatorProfile;
  engagement?: EngagementSummary;
  run?: AgentRunSummary;
  streamState: StreamState;
  events: RunEvent[];
  approvals: ApprovalSummary[];
  assets: AssetSummary[];
  findings: FindingSummary[];
  evidence: EvidenceSummary[];
  observations: ObservationSummary[];
  reports: ReportSummary[];
  providers: ProviderHealth[];
  providerCatalog: ProviderCatalogEntry[];
  knowledgeSources: KnowledgeSource[];
  previewMode: boolean;
  resolveApproval: (id: string, request: ApprovalDecisionRequest) => Promise<void>;
  refreshProvider: (id: string) => Promise<void>;
  reverifyProvider: (id: string, model?: string) => Promise<void>;
  addProvider: (request: ProviderCreateRequest) => Promise<void>;
  updateProvider: (id: string, request: ProviderUpdateRequest) => Promise<ProviderHealth>;
  setProviderEnabled: (id: string, enabled: boolean, expectedRevision: number) => Promise<ProviderHealth>;
  deleteProvider: (id: string, expectedRevision: number) => Promise<void>;
  selectEngagement: (id: string) => void;
  createEngagement: (request: EngagementCreateRequest) => Promise<EngagementSummary>;
  addAsset: (request: AssetCreateRequest) => Promise<AssetSummary>;
  createFinding: (request: FindingCreateRequest) => Promise<FindingSummary>;
  updateFinding: (id: string, request: FindingUpdateRequest) => Promise<FindingSummary>;
  uploadEvidence: (request: EvidenceUploadRequest) => Promise<EvidenceSummary>;
  createObservation: (request: ObservationCreateRequest) => Promise<ObservationSummary>;
  updateObservation: (id: string, request: ObservationUpdateRequest) => Promise<ObservationSummary>;
  deleteObservation: (id: string, expectedRevision: number) => Promise<void>;
  startMission: (request: MissionCreateRequest) => Promise<AgentRunSummary>;
  stopMission: (id: string, request?: RunStopRequest) => Promise<AgentRunSummary>;
  deleteMission: (id: string) => Promise<void>;
  createOperatorProfile: (request: OperatorProfileCreateRequest) => Promise<OperatorProfile>;
  updateOperatorProfile: (id: string, request: OperatorProfileUpdateRequest) => Promise<OperatorProfile>;
  activateOperatorProfile: (id: string, expectedRevision?: number) => Promise<OperatorProfile>;
  deleteOperatorProfile: (id: string, expectedRevision?: number) => Promise<void>;
  createReport: (request: ReportCreateRequest) => Promise<ReportSummary>;
  updateReport: (id: string, request: ReportUpdateRequest) => Promise<ReportSummary>;
  signOffReport: (id: string, expectedRevision: number, operatorId: string, attestation?: string) => Promise<ReportSummary>;
  ingestKnowledgeSource: (request: KnowledgeIngestRequest) => Promise<KnowledgeSource>;
  reindexKnowledgeSource: (id: string) => Promise<void>;
  removeKnowledgeSource: (id: string) => Promise<void>;
  refreshSetupRuntime: () => Promise<void>;
  reconnect: () => void;
  retryResource: (resource: WorkspaceResource) => Promise<void>;
}

const WorkspaceContext = createContext<WorkspaceContextValue | undefined>(undefined);

export function WorkspaceProvider({ children }: PropsWithChildren) {
  const [runtime, setRuntime] = useState<ApiRuntime>();
  const [api, setApi] = useState<ApiClient>();
  const [workspaceState, setWorkspaceState] = useState<WorkspaceState>("starting");
  const coreState: CoreState = workspaceState === "starting" ? "checking" : workspaceState === "failed" ? "offline" : "online";
  const [coreError, setCoreError] = useState<string>();
  const [health, setHealth] = useState<HealthResponse>();
  const [setupStatus, setSetupStatus] = useState<SetupStatus>();
  const [resourceStatus, setResourceStatus] = useState<Record<WorkspaceResource, ResourceStatus>>(initialResourceStatus);
  const [engagements, setEngagements] = useState<EngagementSummary[]>([]);
  const [operatorProfiles, setOperatorProfiles] = useState<OperatorProfile[]>([]);
  const [engagement, setEngagement] = useState<EngagementSummary>();
  const [run, setRun] = useState<AgentRunSummary>();
  const [streamState, setStreamState] = useState<StreamState>("closed");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [approvals, setApprovals] = useState<ApprovalSummary[]>([]);
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [findings, setFindings] = useState<FindingSummary[]>([]);
  const [evidence, setEvidence] = useState<EvidenceSummary[]>([]);
  const [observations, setObservations] = useState<ObservationSummary[]>([]);
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [providerCatalog, setProviderCatalog] = useState<ProviderCatalogEntry[]>([]);
  const [knowledgeSources, setKnowledgeSources] = useState<KnowledgeSource[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [selectedEngagementId, setSelectedEngagementId] = useState(() => localStorage.getItem("nebula.engagement") ?? "");
  const runtimeResolution = useRef<Promise<ApiRuntime> | undefined>(undefined);

  const reconnect = useCallback(() => {
    setWorkspaceState("starting");
    setCoreError(undefined);
    setResourceStatus((current) => Object.fromEntries(Object.entries(current).map(([key, value]) => [key, value.state === "failed" ? { state: "loading" } : value])) as Record<WorkspaceResource, ResourceStatus>);
    runtimeResolution.current = undefined;
    setAttempt((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let eventStream: NebulaEventStream | undefined;

    void (async () => {
      runtimeResolution.current ??= resolveApiRuntime();
      const resolved = await runtimeResolution.current;
      if (!active) return;
      setRuntime(resolved);
      if (resolved.state !== "ready") {
        setCoreError(resolved.message ?? "Nebula Core could not be started.");
        setWorkspaceState("failed");
        return;
      }

      const nextApi = new ApiClient({ baseUrl: resolved.baseUrl, token: resolved.token });
      setApi(nextApi);
      try {
        const nextHealth = await nextApi.health(controller.signal);
        if (!active) return;
        setDiagnosticsAvailability(
          nextHealth.diagnosticsDegraded !== true,
          nextHealth.diagnosticsDegraded ? "Nebula Core reported degraded local diagnostics." : undefined,
        );
        setHealth(nextHealth);
        setWorkspaceState("bootstrapping");

        const loadErrors: string[] = [];
        const [engagementResult, providerResult, catalogResult, operatorResult, setupResult] = await Promise.allSettled([
          nextApi.listEngagements(controller.signal),
          nextApi.listProviders(controller.signal),
          nextApi.listProviderCatalog(controller.signal),
          nextApi.listOperatorProfiles(controller.signal),
          nextApi.setupStatus(controller.signal),
        ]);
        if (!active) return;

        const engagementItems = engagementResult.status === "fulfilled" ? engagementResult.value.items : [];
        if (engagementResult.status === "rejected") loadErrors.push("projects");
        setEngagements(engagementItems);
        const rememberedId = selectedEngagementId || localStorage.getItem("nebula.engagement") || "";
        const nextEngagement = engagementItems.find((item) => item.id === rememberedId)
          ?? engagementItems[0];
        if (nextEngagement && nextEngagement.id !== selectedEngagementId) {
          setSelectedEngagementId(nextEngagement.id);
          localStorage.setItem("nebula.engagement", nextEngagement.id);
        }
        if (!nextEngagement) localStorage.removeItem("nebula.engagement");

        if (providerResult.status === "fulfilled") setProviders(providerResult.value.items);
        else {
          setProviders([]);
          loadErrors.push("model providers");
        }
        if (catalogResult.status === "fulfilled") setProviderCatalog(catalogResult.value);
        else {
          setProviderCatalog([]);
          loadErrors.push("provider catalog");
        }
        if (operatorResult.status === "fulfilled") setOperatorProfiles(operatorResult.value);
        else {
          setOperatorProfiles([]);
          loadErrors.push("operator profiles");
        }
        if (setupResult.status === "fulfilled") {
          setSetupStatus(setupResult.value);
          if (setupResult.value.core.status !== "ready") loadErrors.push("setup");
        } else {
          setSetupStatus(undefined);
          loadErrors.push("setup status");
        }
        setResourceStatus((current) => ({
          ...current,
          projects: engagementResult.status === "fulfilled" ? { state: engagementItems.length ? "ready" : "empty" } : { state: "failed", error: engagementResult.reason },
          providers: providerResult.status === "fulfilled" ? { state: providerResult.value.items.length ? "ready" : "empty" } : { state: "failed", error: providerResult.reason },
          providerCatalog: catalogResult.status === "fulfilled" ? { state: catalogResult.value.length ? "ready" : "empty" } : { state: "failed", error: catalogResult.reason },
          operators: operatorResult.status === "fulfilled" ? { state: operatorResult.value.length ? "ready" : "empty" } : { state: "failed", error: operatorResult.reason },
          setup: setupResult.status === "fulfilled" ? { state: "ready" } : { state: "failed", error: setupResult.reason },
        }));

        let nextRun: AgentRunSummary | undefined;
        setApprovals([]);
        setAssets([]);
        setFindings([]);
        setEvidence([]);
        setObservations([]);
        setKnowledgeSources([]);
        setReports([]);

        if (nextEngagement) {
          const detailResults = await Promise.allSettled([
            nextApi.listRuns(nextEngagement.id, controller.signal),
            nextApi.listApprovals(nextEngagement.id, controller.signal),
            nextApi.listAssets(nextEngagement.id, controller.signal),
            nextApi.listFindings(nextEngagement.id, controller.signal),
            nextApi.listEvidence(nextEngagement.id, controller.signal),
            nextApi.listObservations(nextEngagement.id, controller.signal),
            nextApi.listKnowledgeSources(nextEngagement.id, controller.signal),
            nextApi.listReports(nextEngagement.id, controller.signal),
          ]);
          if (!active) return;
          const labels = ["activity", "approvals", "assets", "findings", "evidence", "notes", "sources", "reports"];
          detailResults.forEach((result, index) => {
            if (result.status === "rejected") loadErrors.push(labels[index]);
          });
          setResourceStatus((current) => ({
            ...current,
            ...Object.fromEntries(detailResults.map((result, index) => [labels[index], result.status === "fulfilled" ? { state: result.value.items.length ? "ready" : "empty" } : { state: "failed", error: result.reason }])),
          }));
          const [runResult, approvalResult, assetResult, findingResult, evidenceResult, observationResult, knowledgeResult, reportResult] = detailResults;
          if (runResult.status === "fulfilled") nextRun = runResult.value.items[runResult.value.items.length - 1];
          if (approvalResult.status === "fulfilled") setApprovals(approvalResult.value.items);
          if (assetResult.status === "fulfilled") setAssets(assetResult.value.items);
          if (findingResult.status === "fulfilled") setFindings(findingResult.value.items);
          if (evidenceResult.status === "fulfilled") setEvidence(evidenceResult.value.items);
          if (observationResult.status === "fulfilled") setObservations(observationResult.value.items);
          if (knowledgeResult.status === "fulfilled") setKnowledgeSources(knowledgeResult.value.items);
          if (reportResult.status === "fulfilled") setReports(reportResult.value.items);
        }

        setEngagement(nextEngagement);
        setRun(nextRun);
        const degraded = nextHealth.status === "degraded" || loadErrors.length > 0;
        setWorkspaceState(degraded ? "degraded" : "ready");
        setCoreError(loadErrors.length
          ? undefined
          : nextHealth.status === "degraded" ? "Nebula Core reported limited availability." : undefined);
        setEvents([]);

        if (nextRun) {
          eventStream = new NebulaEventStream({
            apiBaseUrl: nextApi.baseUrl,
            token: nextApi.getToken(),
            cursor: {
              after: 0,
              engagementId: nextEngagement?.id,
              runId: nextRun.id,
            },
            onStateChange: setStreamState,
            onEvent: (event) => {
              setEvents((current) => [event, ...current].slice(0, 100));
              setRun((current) => current ? evolveRunFromEvent(current, event) : current);
              if (!nextEngagement) return;
              if (event.kind === "approval.requested" || event.kind === "approval.resolved") {
                void nextApi.listApprovals(nextEngagement.id, controller.signal)
                  .then((page) => { if (active) setApprovals(page.items); })
                  .catch((caughtError) => {
                    void logCaughtDiagnostic("interface.workspace_context.caught_failure_01", "A handled interface operation failed.", caughtError, "workspace_context"); /* The event remains visible if the authoritative refresh fails. */ });
              }
              if (event.kind === "finding.created" || event.kind === "finding.updated") {
                void nextApi.listFindings(nextEngagement.id, controller.signal)
                  .then((page) => { if (active) setFindings(page.items); })
                  .catch((caughtError) => {
                    void logCaughtDiagnostic("interface.workspace_context.caught_failure_02", "A handled interface operation failed.", caughtError, "workspace_context"); /* Preserve the last loaded finding list until the next refresh. */ });
              }
              if (event.kind === "evidence.created") {
                void nextApi.listEvidence(nextEngagement.id, controller.signal)
                  .then((page) => { if (active) setEvidence(page.items); })
                  .catch((caughtError) => {
                    void logCaughtDiagnostic("interface.workspace_context.caught_failure_03", "A handled interface operation failed.", caughtError, "workspace_context"); /* Preserve the last loaded evidence list until the next refresh. */ });
              }
            },
          });
          eventStream.connect();
        } else {
          setStreamState("unsupported");
        }
      } catch (error) {
        void logCaughtDiagnostic("interface.workspace_context.caught_failure_04", "A handled interface operation failed.", error, "workspace_context");
        if (active) {
          setCoreError(error instanceof Error ? error.message : "Nebula Core could not be reached.");
          setWorkspaceState("failed");
          setStreamState("closed");
        }
      }
    })();

    return () => {
      active = false;
      controller.abort();
      eventStream?.disconnect();
    };
  }, [attempt, selectedEngagementId]);

  useEffect(() => {
    if (!api || workspaceState === "failed" || !["detecting_runner", "preparing_image"].includes(setupStatus?.terminal.status ?? "")) return;
    let active = true;
    let timer: number | undefined;
    const poll = () => {
      timer = globalThis.setTimeout(() => {
        void api.setupStatus()
          .then((next) => {
            if (!active) return;
            setSetupStatus(next);
            if (["detecting_runner", "preparing_image"].includes(next.terminal.status)) poll();
          })
          .catch((caughtError) => {
            void logCaughtDiagnostic("interface.workspace_context.caught_failure_05", "A handled interface operation failed.", caughtError, "workspace_context");
            if (active) poll();
          });
      }, 1_000);
    };
    poll();
    return () => {
      active = false;
      if (timer !== undefined) globalThis.clearTimeout(timer);
    };
  }, [api, setupStatus?.terminal.status, workspaceState]);

  const refreshSetupRuntime = useCallback(async () => {
    if (!api || workspaceState === "failed") throw new Error("Nebula Core must be available to check Terminal setup.");
    const next = await api.refreshSetupRuntime();
    setSetupStatus(next);
  }, [api, workspaceState]);

  const retryResource = useCallback(async (resource: WorkspaceResource) => {
    if (!api) throw new Error("Nebula Core must be available to retry this resource.");
    setResourceStatus((current) => ({ ...current, [resource]: { state: "loading" } }));
    try {
      let count = 1;
      if (resource === "projects") { const value = await api.listEngagements(); setEngagements(value.items); count = value.items.length; }
      else if (resource === "providers") { const value = await api.listProviders(); setProviders(value.items); count = value.items.length; }
      else if (resource === "providerCatalog") { const value = await api.listProviderCatalog(); setProviderCatalog(value); count = value.length; }
      else if (resource === "operators") { const value = await api.listOperatorProfiles(); setOperatorProfiles(value); count = value.length; }
      else if (resource === "setup") setSetupStatus(await api.setupStatus());
      else {
        if (!engagement) throw new Error("Select a Project before retrying this resource.");
        if (resource === "activity") { const value = await api.listRuns(engagement.id); setRun(value.items[value.items.length - 1]); count = value.items.length; }
        else if (resource === "approvals") { const value = await api.listApprovals(engagement.id); setApprovals(value.items); count = value.items.length; }
        else if (resource === "assets") { const value = await api.listAssets(engagement.id); setAssets(value.items); count = value.items.length; }
        else if (resource === "findings") { const value = await api.listFindings(engagement.id); setFindings(value.items); count = value.items.length; }
        else if (resource === "evidence") { const value = await api.listEvidence(engagement.id); setEvidence(value.items); count = value.items.length; }
        else if (resource === "notes") { const value = await api.listObservations(engagement.id); setObservations(value.items); count = value.items.length; }
        else if (resource === "sources") { const value = await api.listKnowledgeSources(engagement.id); setKnowledgeSources(value.items); count = value.items.length; }
        else if (resource === "reports") { const value = await api.listReports(engagement.id); setReports(value.items); count = value.items.length; }
      }
      setResourceStatus((current) => ({ ...current, [resource]: { state: count ? "ready" : "empty" } }));
    } catch (error) {
      void logCaughtDiagnostic(`interface.workspace.resource_${resource}_retry_failed`, `The ${resource} resource could not be reloaded.`, error, "workspace_context");
      setResourceStatus((current) => ({ ...current, [resource]: { state: "failed", error } }));
      throw error;
    }
  }, [api, engagement]);

  const selectEngagement = useCallback((id: string) => {
    if (!id || id === selectedEngagementId) return;
    localStorage.setItem("nebula.engagement", id);
    setSelectedEngagementId(id);
    setWorkspaceState("starting");
    setCoreError(undefined);
  }, [selectedEngagementId]);

  const createEngagement = useCallback(async (request: EngagementCreateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to create an engagement.");
    }
    const created = await api.createEngagement(request);
    setEngagements((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    localStorage.setItem("nebula.engagement", created.id);
    setSelectedEngagementId(created.id);
    setWorkspaceState("starting");
    return created;
  }, [api, coreState]);

  const resolveApproval = useCallback(
    async (id: string, request: ApprovalDecisionRequest) => {
      if (coreState === "online" && api) {
        const updated = await api.decideApproval(id, request);
        setApprovals((current) =>
          current
            .map((item) => (item.id === id ? updated : item))
            .filter((item) => item.status === "pending"),
        );
        return;
      }
      setApprovals((current) => current.filter((item) => item.id !== id));
    },
    [api, coreState],
  );

  const refreshProvider = useCallback(
    async (id: string) => {
      if (coreState !== "online" || !api) return;
      try {
        const result = await api.refreshProviderHealth(id);
        setProviders((current) => current.map((provider) => provider.id === id
          ? (() => {
              const selectableModels = result.healthy
                ? provider.modelAllowlist.length
                  ? result.models.filter((model) => provider.modelAllowlist.includes(model))
                  : result.models
                : provider.models;
              return {
                ...provider,
                state: result.healthy ? "healthy" : "offline",
                models: selectableModels,
                availableModels: result.healthy ? result.models : provider.availableModels,
                modelCount: selectableModels.length,
                lastCheckedAt: new Date().toISOString(),
                message: result.healthy
                  ? selectableModels.length > 0
                    ? `Serving ${selectableModels.join(", ")}`
                    : provider.modelAllowlist.length
                      ? "Provider is healthy but reported no allowed models."
                      : "Provider is healthy but reported no models."
                  : result.detail ?? "Provider health check failed.",
              };
            })()
          : provider));
      } catch (error) {
        void logCaughtDiagnostic("interface.workspace_context.caught_failure_06", "A handled interface operation failed.", error, "workspace_context");
        setProviders((current) => current.map((provider) => provider.id === id
          ? {
              ...provider,
              state: "degraded",
              lastCheckedAt: new Date().toISOString(),
              message: error instanceof Error ? error.message : "Provider health check failed.",
            }
          : provider));
      }
    },
    [api, coreState],
  );

  const addProvider = useCallback(
    async (request: ProviderCreateRequest) => {
      if (coreState !== "online" || !api) {
        throw new Error("Nebula Core must be online to add a provider.");
      }
      const created = await api.createProvider(request);
      setProviders((current) => [...current, created]);
      void refreshProvider(created.id);
    },
    [api, coreState, refreshProvider],
  );

  const updateProvider = useCallback(async (id: string, request: ProviderUpdateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to update a provider.");
    }
    const updated = await api.updateProvider(id, request);
    setProviders((current) => current.map((provider) => provider.id === id ? updated : provider));
    void refreshProvider(id);
    return updated;
  }, [api, coreState, refreshProvider]);

  const reverifyProvider = useCallback(async (id: string, requestedModel?: string) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to verify a provider.");
    }
    const current = providers.find((provider) => provider.id === id);
    const model = requestedModel?.trim() || providerVerificationModel(current);
    if (!current || !model) throw new Error("Configure an exact model before verification.");
    const updated = await api.verifyProviderCapabilities(id, model, current.revision);
    setProviders((items) => items.map((provider) => provider.id === id ? updated : provider));
  }, [api, coreState, providers]);

  const setProviderEnabled = useCallback(async (id: string, enabled: boolean, expectedRevision: number) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to change a provider.");
    }
    const updated = await api.setProviderEnabled(id, enabled, expectedRevision);
    setProviders((current) => current.map((provider) => provider.id === id ? updated : provider));
    return updated;
  }, [api, coreState]);

  const deleteProvider = useCallback(async (id: string, expectedRevision: number) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to delete a provider.");
    }
    await api.deleteProvider(id, expectedRevision);
    setProviders((current) => current.filter((provider) => provider.id !== id));
  }, [api, coreState]);

  const addAsset = useCallback(async (request: AssetCreateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to add an asset.");
    }
    const created = await api.createAsset(request);
    setAssets((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    return created;
  }, [api, coreState]);

  const createFinding = useCallback(async (request: FindingCreateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to create a finding.");
    }
    const created = await api.createFinding(request);
    setFindings((current) => [created, ...current.filter((finding) => finding.id !== created.id)]);
    return created;
  }, [api, coreState]);

  const updateFinding = useCallback(async (id: string, request: FindingUpdateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to update a finding.");
    }
    const updated = await api.updateFinding(id, request);
    setFindings((current) => current.map((finding) => finding.id === id ? updated : finding));
    return updated;
  }, [api, coreState]);

  const uploadEvidence = useCallback(async (request: EvidenceUploadRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to add evidence.");
    }
    const created = await api.uploadEvidence(request);
    setEvidence((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    if (request.findingId) {
      setFindings((current) => current.map((finding) => {
        if (finding.id !== request.findingId) return finding;
        const evidenceIds = finding.evidenceIds.includes(created.id)
          ? finding.evidenceIds
          : [...finding.evidenceIds, created.id];
        return { ...finding, evidenceIds, evidenceCount: evidenceIds.length };
      }));
    }
    return created;
  }, [api, coreState]);

  const createObservation = useCallback(async (request: ObservationCreateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to create a note.");
    }
    const created = await api.createObservation(request);
    setObservations((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    return created;
  }, [api, coreState]);

  const updateObservation = useCallback(async (id: string, request: ObservationUpdateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to update a note.");
    }
    const updated = await api.updateObservation(id, request);
    setObservations((current) => current.map((item) => item.id === id ? updated : item));
    return updated;
  }, [api, coreState]);

  const deleteObservation = useCallback(async (id: string, expectedRevision: number) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to delete a note.");
    }
    await api.deleteObservation(id, expectedRevision);
    setObservations((current) => current.filter((item) => item.id !== id));
  }, [api, coreState]);

  const startMission = useCallback(async (request: MissionCreateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to start a mission.");
    }
    const created = await api.createMission(request);
    setRun(created);
    setAttempt((value) => value + 1);
    return created;
  }, [api, coreState]);

  const stopMission = useCallback(async (id: string, request: RunStopRequest = {}) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to stop a mission.");
    }
    const updated = await api.stopRun(id, request);
    setRun(updated);
    setAttempt((value) => value + 1);
    return updated;
  }, [api, coreState]);

  const deleteMission = useCallback(async (id: string) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to delete a mission.");
    }
    await api.deleteRun(id);
    setRun((current) => current?.id === id ? undefined : current);
    setEvents([]);
    setApprovals((current) => current.filter((approval) => approval.runId !== id));
    setAttempt((value) => value + 1);
  }, [api, coreState]);

  const createOperatorProfile = useCallback(async (request: OperatorProfileCreateRequest) => {
    if (coreState !== "online" || !api) throw new Error("Nebula Core must be online to create an operator profile.");
    const created = await api.createOperatorProfile(request);
    setOperatorProfiles((current) => [created, ...current.map((item) => created.active ? { ...item, active: false } : item)]);
    return created;
  }, [api, coreState]);

  const updateOperatorProfile = useCallback(async (id: string, request: OperatorProfileUpdateRequest) => {
    if (coreState !== "online" || !api) throw new Error("Nebula Core must be online to update an operator profile.");
    const updated = await api.updateOperatorProfile(id, request);
    setOperatorProfiles((current) => current.map((item) => item.id === id ? updated : item));
    return updated;
  }, [api, coreState]);

  const activateOperatorProfile = useCallback(async (id: string, expectedRevision?: number) => {
    if (coreState !== "online" || !api) throw new Error("Nebula Core must be online to activate an operator profile.");
    const active = await api.activateOperatorProfile(id, expectedRevision);
    const refreshed = await api.listOperatorProfiles();
    setOperatorProfiles(refreshed);
    return active;
  }, [api, coreState]);

  const deleteOperatorProfile = useCallback(async (id: string, expectedRevision?: number) => {
    if (coreState !== "online" || !api) throw new Error("Nebula Core must be online to delete an operator profile.");
    await api.deleteOperatorProfile(id, expectedRevision);
    setOperatorProfiles((current) => current.filter((item) => item.id !== id));
  }, [api, coreState]);

  const createReport = useCallback(async (request: ReportCreateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to create a report.");
    }
    const created = await api.createReport(request);
    setReports((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    return created;
  }, [api, coreState]);

  const updateReport = useCallback(async (id: string, request: ReportUpdateRequest) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to save a report.");
    }
    const updated = await api.updateReport(id, request);
    setReports((current) => current.map((item) => item.id === id ? updated : item));
    return updated;
  }, [api, coreState]);

  const signOffReport = useCallback(async (
    id: string,
    expectedRevision: number,
    operatorId: string,
    attestation?: string,
  ) => {
    if (coreState !== "online" || !api) {
      throw new Error("Nebula Core must be online to sign off a report.");
    }
    const signed = await api.signOffReport(id, expectedRevision, operatorId, attestation);
    setReports((current) => current.map((item) => item.id === id ? signed : item));
    return signed;
  }, [api, coreState]);

  const ingestKnowledgeSource = useCallback(
    async (request: KnowledgeIngestRequest) => {
      if (coreState !== "online" || !api) {
        throw new Error("Nebula Core must be online to add a knowledge source.");
      }
      const created = await api.ingestKnowledgeSource(request);
      setKnowledgeSources((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      return created;
    },
    [api, coreState],
  );

  const reindexKnowledgeSource = useCallback(
    async (id: string) => {
      if (coreState !== "online" || !api) {
        throw new Error("Nebula Core must be online to reindex a knowledge source.");
      }
      const updated = await api.reindexKnowledgeSource(id);
      setKnowledgeSources((current) => current.map((item) => item.id === id ? updated : item));
    },
    [api, coreState],
  );

  const removeKnowledgeSource = useCallback(
    async (id: string) => {
      if (coreState !== "online" || !api) {
        throw new Error("Nebula Core must be online to remove a knowledge source.");
      }
      await api.deleteKnowledgeSource(id);
      setKnowledgeSources((current) => current.filter((item) => item.id !== id));
    },
    [api, coreState],
  );

  const value = useMemo(
    () => ({
      api,
      runtime,
      coreState,
      workspaceState,
      coreError,
      health,
      setupStatus,
      resourceStatus,
      engagements,
      operatorProfiles,
      activeOperator: operatorProfiles.find((profile) => profile.active),
      engagement,
      run,
      streamState,
      events,
      approvals,
      assets,
      findings,
      evidence,
      observations,
      reports,
      providers,
      providerCatalog,
      knowledgeSources,
      previewMode: false,
      resolveApproval,
      refreshProvider,
      reverifyProvider,
      addProvider,
      updateProvider,
      setProviderEnabled,
      deleteProvider,
      selectEngagement,
      createEngagement,
      addAsset,
      createFinding,
      updateFinding,
      uploadEvidence,
      createObservation,
      updateObservation,
      deleteObservation,
      startMission,
      stopMission,
      deleteMission,
      createOperatorProfile,
      updateOperatorProfile,
      activateOperatorProfile,
      deleteOperatorProfile,
      createReport,
      updateReport,
      signOffReport,
      ingestKnowledgeSource,
      reindexKnowledgeSource,
      removeKnowledgeSource,
      refreshSetupRuntime,
      reconnect,
      retryResource,
    }),
    [
      api,
      approvals,
      assets,
      coreState,
      workspaceState,
      coreError,
      engagement,
      engagements,
      operatorProfiles,
      events,
      findings,
      evidence,
      observations,
      reports,
      health,
      setupStatus,
      resourceStatus,
      providers,
      providerCatalog,
      knowledgeSources,
      reconnect,
      addProvider,
      updateProvider,
      setProviderEnabled,
      deleteProvider,
      selectEngagement,
      createEngagement,
      addAsset,
      createFinding,
      updateFinding,
      uploadEvidence,
      createObservation,
      updateObservation,
      deleteObservation,
      startMission,
      stopMission,
      deleteMission,
      createOperatorProfile,
      updateOperatorProfile,
      activateOperatorProfile,
      deleteOperatorProfile,
      createReport,
      updateReport,
      retryResource,
      signOffReport,
      ingestKnowledgeSource,
      reindexKnowledgeSource,
      removeKnowledgeSource,
      refreshSetupRuntime,
      refreshProvider,
      reverifyProvider,
      resolveApproval,
      run,
      runtime,
      streamState,
    ],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const context = useContext(WorkspaceContext);
  if (!context) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return context;
}
