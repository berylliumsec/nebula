import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { ApiClient } from "../api/client";
import { NebulaEventStream, type StreamState } from "../api/events";
import { resolveApiRuntime, type ApiRuntime } from "../api/runtime";
import type {
  AgentRunSummary,
  ApprovalDecision,
  ApprovalSummary,
  AssetSummary,
  EngagementSummary,
  FindingSummary,
  HealthResponse,
  ProviderCatalogEntry,
  ProviderCreateRequest,
  ProviderHealth,
  RunEvent,
} from "../api/types";
import {
  previewApproval,
  previewAssets,
  previewEvents,
  previewFindings,
  previewProviders,
} from "../data/demo";

type CoreState = "checking" | "online" | "offline";

interface WorkspaceContextValue {
  api?: ApiClient;
  runtime?: ApiRuntime;
  coreState: CoreState;
  health?: HealthResponse;
  engagement?: EngagementSummary;
  run?: AgentRunSummary;
  streamState: StreamState;
  events: RunEvent[];
  approvals: ApprovalSummary[];
  assets: AssetSummary[];
  findings: FindingSummary[];
  providers: ProviderHealth[];
  providerCatalog: ProviderCatalogEntry[];
  previewMode: boolean;
  resolveApproval: (id: string, decision: ApprovalDecision) => Promise<void>;
  refreshProvider: (id: string) => Promise<void>;
  addProvider: (request: ProviderCreateRequest) => Promise<void>;
  reconnect: () => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | undefined>(undefined);

export function WorkspaceProvider({ children }: PropsWithChildren) {
  const [runtime, setRuntime] = useState<ApiRuntime>();
  const [api, setApi] = useState<ApiClient>();
  const [coreState, setCoreState] = useState<CoreState>("checking");
  const [health, setHealth] = useState<HealthResponse>();
  const [engagement, setEngagement] = useState<EngagementSummary>();
  const [run, setRun] = useState<AgentRunSummary>();
  const [streamState, setStreamState] = useState<StreamState>("closed");
  const [events, setEvents] = useState<RunEvent[]>(previewEvents);
  const [approvals, setApprovals] = useState<ApprovalSummary[]>([previewApproval]);
  const [assets, setAssets] = useState<AssetSummary[]>(previewAssets);
  const [findings, setFindings] = useState<FindingSummary[]>(previewFindings);
  const [providers, setProviders] = useState<ProviderHealth[]>(previewProviders);
  const [providerCatalog, setProviderCatalog] = useState<ProviderCatalogEntry[]>([]);
  const [attempt, setAttempt] = useState(0);

  const reconnect = useCallback(() => {
    setCoreState("checking");
    setHealth(undefined);
    setAttempt((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let eventStream: NebulaEventStream | undefined;

    void (async () => {
      const resolved = await resolveApiRuntime();
      if (!active) return;
      setRuntime(resolved);
      if (resolved.state !== "ready") {
        setCoreState("offline");
        return;
      }

      const nextApi = new ApiClient({ baseUrl: resolved.baseUrl, token: resolved.token });
      setApi(nextApi);
      try {
        const nextHealth = await nextApi.health(controller.signal);
        if (!active) return;
        const [engagementPage, providerPage, nextProviderCatalog] = await Promise.all([
          nextApi.listEngagements(controller.signal),
          nextApi.listProviders(controller.signal),
          nextApi.listProviderCatalog(controller.signal),
        ]);
        if (!active) return;
        const nextEngagement = engagementPage.items[0];
        let nextRun: AgentRunSummary | undefined;
        setProviders(providerPage.items);
        setProviderCatalog(nextProviderCatalog);

        if (nextEngagement) {
          const [runPage, approvalPage, assetPage, findingPage] = await Promise.all([
            nextApi.listRuns(nextEngagement.id, controller.signal),
            nextApi.listApprovals(nextEngagement.id, controller.signal),
            nextApi.listAssets(nextEngagement.id, controller.signal),
            nextApi.listFindings(nextEngagement.id, controller.signal),
          ]);
          if (!active) return;
          nextRun = runPage.items[0];
          setApprovals(approvalPage.items);
          setAssets(assetPage.items);
          setFindings(findingPage.items);
        } else {
          setApprovals([]);
          setAssets([]);
          setFindings([]);
        }

        setHealth(nextHealth);
        setEngagement(nextEngagement);
        setRun(nextRun);
        setCoreState("online");
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
            },
          });
          eventStream.connect();
        } else {
          setStreamState("unsupported");
        }
      } catch {
        if (active) {
          setCoreState("offline");
          setStreamState("closed");
        }
      }
    })();

    return () => {
      active = false;
      controller.abort();
      eventStream?.disconnect();
    };
  }, [attempt]);

  const resolveApproval = useCallback(
    async (id: string, decision: ApprovalDecision) => {
      if (coreState === "online" && api) {
        const updated = await api.decideApproval(id, { decision });
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
          ? {
              ...provider,
              state: result.healthy ? "healthy" : "offline",
              modelCount: result.models.length,
              lastCheckedAt: new Date().toISOString(),
              message: result.healthy
                ? result.models.length > 0
                  ? `Serving ${result.models.join(", ")}`
                  : "Provider is healthy but reported no models."
                : result.detail ?? "Provider health check failed.",
            }
          : provider));
      } catch (error) {
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
    },
    [api, coreState],
  );

  const value = useMemo(
    () => ({
      api,
      runtime,
      coreState,
      health,
      engagement,
      run,
      streamState,
      events,
      approvals,
      assets,
      findings,
      providers,
      providerCatalog,
      previewMode: coreState !== "online",
      resolveApproval,
      refreshProvider,
      addProvider,
      reconnect,
    }),
    [
      api,
      approvals,
      assets,
      coreState,
      engagement,
      events,
      findings,
      health,
      providers,
      providerCatalog,
      reconnect,
      addProvider,
      refreshProvider,
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
