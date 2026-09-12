import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { AlertTriangle, Braces, GitCompareArrows, LoaderCircle, Pause, Play, Plus, RefreshCw, Send, ShieldAlert, Square, Target, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../api/client";
import type {
  SecurityBrowserAttack,
  SecurityBrowserCrawlJob,
  SecurityBrowserIdentity,
  SecurityBrowserResearchWorkspace,
  SecurityBrowserSession,
} from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useConfirmation } from "./DialogSystem";

export type BrowserResearchToolView = "target" | "intercepts" | "repeater" | "intruder" | "utilities";

interface Props {
  api: ApiClient;
  desktop: boolean;
  identity?: SecurityBrowserIdentity;
  operatorId: string;
  projectId: string;
  session?: SecurityBrowserSession;
  view: BrowserResearchToolView;
  draftStore?: RepeaterDraftStore;
  onOpenRepeater?: () => void;
}

type RepeaterDraft = { name: string; method: string; url: string; headers: string; body: string; baseline: string };
export type RepeaterDraftStore = Map<string, { selected?: string; drafts: Record<string, RepeaterDraft> }>;
const draftValue = (draft: Omit<RepeaterDraft, "baseline">) => JSON.stringify([draft.name, draft.method, draft.url, draft.headers, draft.body]);

export function BrowserResearchSuite(props: Props) {
  const localStore = useRef<RepeaterDraftStore>(new Map());
  return <ResearchSuiteSession key={`${props.projectId}:${props.session?.id ?? "none"}`} {...props} draftStore={props.draftStore ?? localStore.current} />;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function headerPairs(value: string): Array<[string, string]> {
  const trimmed = value.trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("{")) {
    const parsed = JSON.parse(trimmed) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)
      || Object.values(parsed).some((item) => typeof item !== "string")) {
      throw new Error("Headers must contain text values.");
    }
    return Object.entries(parsed) as Array<[string, string]>;
  }
  return trimmed.split(/\r?\n/).map((line, index) => {
    const separator = line.indexOf(":");
    if (separator <= 0) throw new Error(`Header line ${index + 1} must use Name: value.`);
    const name = line.slice(0, separator).trim();
    const headerValue = line.slice(separator + 1).trim();
    if (!name) throw new Error(`Header line ${index + 1} needs a name.`);
    return [name, headerValue];
  });
}

const formatHeaders = (headers: Array<[string, string]>) => headers.map(([name, value]) => `${name}: ${value}`).join("\n");
const reusableSecretHeader = /authorization|cookie|csrf|xsrf|api[-_]?key|token/i;

function ResearchSuiteSession({ api, desktop, identity, operatorId, projectId, session, view, draftStore, onOpenRepeater }: Props) {
  const storeKey = `${projectId}:${session?.id ?? "none"}`;
  const initialUrl = session?.tabs.find((tab) => tab.id === session.activeTabId)?.url ?? session?.tabs[0]?.url ?? "";
  const emptyDraft = { name: "Repeater", method: "GET", url: initialUrl, headers: "", body: "" };
  const entry = useRef(draftStore?.get(storeKey) ?? { selected: undefined as string | undefined, drafts: {} as Record<string, RepeaterDraft> }).current;
  const initialDraft = entry.drafts[entry.selected ?? "new"] ?? { ...emptyDraft, baseline: draftValue(emptyDraft) };
  const baseline = useRef(initialDraft.baseline);
  const confirm = useConfirmation();
  const [workspace, setWorkspace] = useState<SecurityBrowserResearchWorkspace>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [repeaterName, setRepeaterName] = useState(initialDraft.name);
  const [repeaterMethod, setRepeaterMethod] = useState(initialDraft.method);
  const [repeaterUrl, setRepeaterUrl] = useState(initialDraft.url);
  const [repeaterHeaders, setRepeaterHeaders] = useState(initialDraft.headers);
  const [repeaterBody, setRepeaterBody] = useState(initialDraft.body);
  const [selectedRepeaterId, setSelectedRepeaterId] = useState<string | undefined>(entry.selected);
  const [selectedResultId, setSelectedResultId] = useState<string>();
  const [fetchError, setFetchError] = useState<string>();
  const [bodyPreviews, setBodyPreviews] = useState<Record<string, string>>({});
  const [attackName, setAttackName] = useState("Identifier boundaries");
  const [attackStrategy, setAttackStrategy] = useState<SecurityBrowserAttack["strategy"]>("sniper");
  const [attackMethod, setAttackMethod] = useState("GET");
  const [attackUrl, setAttackUrl] = useState("");
  const [attackPosition, setAttackPosition] = useState("id");
  const [attackHeaders, setAttackHeaders] = useState("{}");
  const [attackBody, setAttackBody] = useState("");
  const [payloads, setPayloads] = useState("0\n1\n-1");
  const [decoderOperation, setDecoderOperation] = useState("url_encode");
  const [decoderInput, setDecoderInput] = useState("");
  const [decoderOutput, setDecoderOutput] = useState("");
  const [compareMode, setCompareMode] = useState("text");
  const [compareLeft, setCompareLeft] = useState("");
  const [compareRight, setCompareRight] = useState("");
  const [compareOutput, setCompareOutput] = useState("");
  const [tokenSamples, setTokenSamples] = useState("");
  const [crawlUrl, setCrawlUrl] = useState("");
  const [crawlDepth, setCrawlDepth] = useState(2);
  const [crawlRequests, setCrawlRequests] = useState(100);

  const refreshSequence = useRef(0);
  useEffect(() => () => { refreshSequence.current += 1; }, []);
  const refresh = useCallback(async (background = false) => {
    const sequence = ++refreshSequence.current;
    if (!background) setLoading(true);
    try {
      const next = await api.getSecurityBrowserResearch(projectId);
      if (sequence !== refreshSequence.current) return;
      setWorkspace(next);
      setFetchError(undefined);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.research_suite_load_failed", "Burp-parity browser research state could not be loaded.", caught, "workbench_browser");
      if (sequence === refreshSequence.current) setFetchError(message(caught));
    } finally {
      if (sequence === refreshSequence.current) setLoading(false);
    }
  }, [api, projectId]);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh(true);
    }, 1_500);
    const onFocus = () => void refresh(true);
    window.addEventListener("focus", onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [refresh]);

  useEffect(() => {
    const current = session?.tabs.find((tab) => tab.id === session.activeTabId)?.url ?? session?.tabs[0]?.url ?? "";
    setAttackUrl(current ? `${current.replace(/\/$/, "")}/§id§` : "");
    setCrawlUrl(current);
  }, [session?.id]);

  const currentDraft = { name: repeaterName, method: repeaterMethod, url: repeaterUrl, headers: repeaterHeaders, body: repeaterBody, baseline: baseline.current };
  const dirty = draftValue(currentDraft) !== baseline.current;
  useEffect(() => {
    entry.selected = selectedRepeaterId;
    entry.drafts[selectedRepeaterId ?? "new"] = currentDraft;
    draftStore?.set(storeKey, entry);
  });
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if ([...(draftStore?.values() ?? [])].some((item) => Object.values(item.drafts).some((draft) => draftValue(draft) !== draft.baseline))) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [draftStore]);
  const restoreDraft = (draft: RepeaterDraft) => {
    baseline.current = draft.baseline;
    setRepeaterName(draft.name);
    setRepeaterMethod(draft.method);
    setRepeaterUrl(draft.url);
    setRepeaterHeaders(draft.headers);
    setRepeaterBody(draft.body);
    setSelectedResultId(undefined);
  };

  useEffect(() => {
    if (!selectedResultId && selectedRepeaterId) {
      const latest = workspace?.repeaterResults.filter((result) => result.tabId === selectedRepeaterId).sort((a, b) => b.sequence - a.sequence)[0];
      if (latest) setSelectedResultId(latest.id);
    }
  }, [selectedResultId, selectedRepeaterId, workspace]);

  const sessionItems = <T extends { sessionId: string }>(items: T[] | undefined): T[] =>
    items?.filter((item) => item.sessionId === session?.id) ?? [];

  const decideIntercept = async (id: string, decision: "forward" | "drop", edits?: { method: string; url: string; headers: Array<[string, string]> }) => {
    const item = workspace?.intercepts.find((candidate) => candidate.id === id);
    if (!item) return;
    setBusy(true);
    try {
      await api.decideSecurityBrowserIntercept(item, decision, operatorId, edits);
      setNotice(decision === "forward" ? "The paused transaction was released." : "The paused transaction was dropped.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.intercept_decision_failed", "The paused browser transaction could not be decided.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const forwardEditedIntercept = (event: FormEvent<HTMLFormElement>, id: string) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const headers = headerPairs(String(data.get("headers") ?? ""))
        .filter(([name, value]) => !reusableSecretHeader.test(name) && !value.startsWith("<redacted:"));
      void decideIntercept(id, "forward", {
        method: String(data.get("method") ?? "GET").toUpperCase(),
        url: String(data.get("url") ?? ""),
        headers,
      });
    } catch (caught) { // diagnostic-expected: invalid editable header input remains in the form.
      setError(message(caught));
    }
  };

  const createCrawl = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !identity) return;
    setBusy(true);
    try {
      await api.createSecurityBrowserCrawl(projectId, {
        sessionId: session.id,
        identityId: identity.id,
        startUrl: crawlUrl,
        maxDepth: crawlDepth,
        maxRequests: crawlRequests,
        maxConcurrency: 1,
        maxDurationSeconds: 300,
        maxBodyBytes: 1_048_576,
      });
      setNotice("Bounded crawl saved as a draft. Queue and start it explicitly from the owning desktop.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.crawl_create_failed", "The bounded browser crawl could not be created.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const transitionCrawl = async (crawl: SecurityBrowserCrawlJob, action: "queue" | "pause" | "resume" | "retry" | "cancel") => {
    setBusy(true);
    try {
      await api.transitionSecurityBrowserCrawl(crawl, action, operatorId);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.crawl_transition_failed", "The browser crawl state could not be changed.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const deleteCrawl = async (crawl: SecurityBrowserCrawlJob) => {
    if (!await confirm({ title: "Delete this crawl?", message: "The crawl definition and pending frontier will be deleted. Discovered target-map entries remain available.", confirmLabel: "Delete crawl", tone: "danger" })) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.deleteSecurityBrowserCrawl(crawl);
      setNotice("Crawl and its pending frontier were deleted. Discovered target-map entries were retained.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.crawl_delete_failed", "The browser crawl could not be deleted.", caught, "browser_research_suite");
      setError(`${message(caught)} Cancel active work before deleting it.`);
    } finally {
      setBusy(false);
    }
  };

  const saveRepeater = async (sendAfterSave: boolean) => {
    if (!session || !identity) return;
    if (sendAfterSave && !desktop) return;
    setBusy(true);
    setError(undefined);
    try {
      const headers = headerPairs(repeaterHeaders);
      const selected = workspace?.repeaterTabs.find((tab) => tab.id === selectedRepeaterId);
      if (selectedRepeaterId && !selected) throw new Error("This saved request is no longer available. Your draft is preserved; select another request or start a new one.");
      const saved = selected
        ? await api.updateSecurityBrowserRepeaterTab(selected, {
            name: repeaterName,
            method: repeaterMethod,
            url: repeaterUrl,
            headers,
            bodyTemplate: repeaterBody,
          })
        : await api.createSecurityBrowserRepeaterTab(projectId, {
            sessionId: session.id,
            identityId: identity.id,
            name: repeaterName,
            method: repeaterMethod,
            url: repeaterUrl,
            headers,
            bodyTemplate: repeaterBody,
          });
      baseline.current = draftValue(currentDraft);
      if (!selectedRepeaterId) delete entry.drafts.new;
      setSelectedRepeaterId(saved.id);
      if (sendAfterSave) {
        await api.transitionSecurityBrowserRepeaterTab(saved, "queue", operatorId);
        setNotice("The visible request was saved and queued for one native send.");
      } else {
        setNotice(selected ? "Repeater draft saved." : "Repeater draft created.");
      }
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.repeater_create_failed", "The Repeater tab could not be saved.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const selectRepeater = (tab: SecurityBrowserResearchWorkspace["repeaterTabs"][number]) => {
    setSelectedRepeaterId(tab.id);
    const saved = { name: tab.name, method: tab.method, url: tab.url, headers: formatHeaders(tab.headers), body: tab.bodyTemplate };
    restoreDraft(entry.drafts[tab.id] ?? { ...saved, baseline: draftValue(saved) });
    setError(undefined);
    setNotice(undefined);
  };

  const newRepeater = () => {
    setSelectedRepeaterId(undefined);
    restoreDraft(entry.drafts.new ?? { ...emptyDraft, baseline: draftValue(emptyDraft) });
    setError(undefined);
    setNotice(undefined);
  };

  const discardRepeaterChanges = async () => {
    if (!await confirm({ title: "Discard unsaved changes?", message: "The saved request and its responses will remain available.", confirmLabel: "Discard changes", tone: "danger" })) return;
    delete entry.drafts[selectedRepeaterId ?? "new"];
    const saved = workspace?.repeaterTabs.find((tab) => tab.id === selectedRepeaterId);
    if (saved) selectRepeater(saved);
    else newRepeater();
  };

  const copyInterceptToRepeater = async (item: SecurityBrowserResearchWorkspace["intercepts"][number]) => {
    if (!session || !identity) return;
    setBusy(true);
    setError(undefined);
    try {
      const saved = await api.createSecurityBrowserRepeaterTab(projectId, {
        sessionId: session.id,
        identityId: identity.id,
        name: `${item.method} ${new URL(item.url).pathname || "/"}`,
        method: item.method,
        url: item.url,
        headers: item.headers,
        bodyTemplate: "",
      });
      baseline.current = draftValue({ name: saved.name, method: saved.method, url: saved.url, headers: formatHeaders(saved.headers), body: saved.bodyTemplate });
      setSelectedRepeaterId(saved.id);
      setRepeaterName(saved.name);
      setRepeaterMethod(saved.method);
      setRepeaterUrl(saved.url);
      setRepeaterHeaders(formatHeaders(saved.headers));
      setRepeaterBody(saved.bodyTemplate);
      setNotice("Request copied to Repeater without a body. Unavailable or redacted content is not copied; the live intercept is still paused.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.intercept_repeater_copy_failed", "A paused request could not be copied to Repeater.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const transitionRepeater = async (
    tab: SecurityBrowserResearchWorkspace["repeaterTabs"][number],
    action: "queue" | "cancel" | "retry",
  ) => {
    setBusy(true);
    setError(undefined);
    try {
      await api.transitionSecurityBrowserRepeaterTab(tab, action, operatorId);
      setNotice(action === "queue" || action === "retry"
        ? "Request queued for the owning desktop. It remains durable if this panel closes."
        : "Repeater request cancelled.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.repeater_transition_failed", "The Repeater request state could not be changed.", caught, "browser_research_suite");
      setError(`${message(caught)} Refresh the durable request and retry.`);
    } finally {
      setBusy(false);
    }
  };

  const deleteRepeater = async (tab: SecurityBrowserResearchWorkspace["repeaterTabs"][number]) => {
    if (!await confirm({ title: `Delete ${tab.name}?`, message: "The durable request and its retained result history will be deleted.", confirmLabel: "Delete request", tone: "danger" })) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.deleteSecurityBrowserRepeaterTab(tab);
      delete entry.drafts[tab.id];
      if (selectedRepeaterId === tab.id) { setSelectedRepeaterId(undefined); restoreDraft(entry.drafts.new ?? { ...emptyDraft, baseline: draftValue(emptyDraft) }); }
      setNotice("Repeater request and its retained result history were deleted.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.repeater_delete_failed", "The Repeater request could not be deleted.", caught, "browser_research_suite");
      setError(`${message(caught)} Cancel active work before deleting it.`);
    } finally {
      setBusy(false);
    }
  };

  const loadRepeaterBody = async (artifactId: string) => {
    setBusy(true);
    setError(undefined);
    try {
      const blob = await api.getArtifactContent(artifactId);
      const text = await blob.text();
      const body = text.slice(0, 1_048_576) + (text.length > 1_048_576 ? "\n\n[Preview truncated at 1,048,576 characters]" : "");
      setBodyPreviews((current) => ({ ...current, [artifactId]: body }));
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.repeater_body_load_failed", "The retained Repeater response body could not be loaded.", caught, "browser_research_suite");
      setError(`${message(caught)} The response metadata remains available; retry the body preview.`);
    } finally {
      setBusy(false);
    }
  };

  const createAttack = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !identity) return;
    const positions = attackPosition.split(",").map((value) => value.trim()).filter(Boolean);
    const payloadSets = payloads.split(/^---$/m).map((set) => set.split("\n").map((value) => value.trim()).filter(Boolean));
    setBusy(true);
    setError(undefined);
    try {
      const headers = headerPairs(attackHeaders);
      const templates = [attackUrl, attackBody, ...headers.map(([, value]) => value)];
      const missing = positions.filter((position) => !templates.some((value) => value.includes(`§${position}§`)));
      if (missing.length) {
        throw new Error(`Add ${missing.map((position) => `§${position}§`).join(", ")} to the URL, a header value, or the body before saving.`);
      }
      const requiredSets = ["pitchfork", "cluster_bomb"].includes(attackStrategy) ? positions.length : 1;
      if (payloadSets.length !== requiredSets || payloadSets.some((set) => !set.length)) throw new Error(`${attackStrategy.replaceAll("_", " ")} requires ${requiredSets} non-empty payload set${requiredSets === 1 ? "" : "s"}. Separate sets with a line containing only ---.`);
      const plannedRequests = attackStrategy === "sniper"
        ? positions.length * payloadSets[0].length
        : attackStrategy === "battering_ram"
          ? payloadSets[0].length
          : attackStrategy === "pitchfork"
            ? Math.min(...payloadSets.map((set) => set.length))
            : payloadSets.reduce((total, set) => total * set.length, 1);
      await api.createSecurityBrowserAttack(projectId, {
        sessionId: session.id,
        identityId: identity.id,
        name: attackName,
        strategy: attackStrategy,
        method: attackMethod,
        urlTemplate: attackUrl,
        headersTemplate: headers,
        bodyTemplate: attackBody,
        positions,
        payloadSets,
        transforms: ["url_encode"],
        maxRequests: Math.min(1000, Math.max(1, plannedRequests)),
        maxConcurrency: 1,
        requestsPerSecond: 2,
      });
      setNotice("Intruder attack saved as a draft. Queue it when its positions and budgets are correct.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.attack_create_failed", "The bounded Intruder draft could not be saved.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const transitionAttack = async (attack: SecurityBrowserAttack, action: "queue" | "pause" | "resume" | "retry" | "cancel") => {
    setBusy(true);
    try {
      await api.transitionSecurityBrowserAttack(attack, action, operatorId);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.attack_transition_failed", "The Intruder attack state could not be changed.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const deleteAttack = async (attack: SecurityBrowserAttack) => {
    if (!await confirm({ title: `Delete ${attack.name}?`, message: "The bounded attack definition and every retained result will be deleted.", confirmLabel: "Delete attack", tone: "danger" })) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.deleteSecurityBrowserAttack(attack);
      setNotice("Intruder attack and its results were deleted.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.attack_delete_failed", "The Intruder attack could not be deleted.", caught, "browser_research_suite");
      setError(`${message(caught)} Cancel active work before deleting it.`);
    } finally {
      setBusy(false);
    }
  };

  const decode = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.securityBrowserDecode(decoderOperation, decoderInput);
      setDecoderOutput(typeof result.result === "string" ? result.result : JSON.stringify(result.result, null, 2));
      setError(undefined);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.decoder_failed", "The browser Decoder transformation failed.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const compare = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.securityBrowserCompare(compareMode, compareLeft, compareRight);
      setCompareOutput(JSON.stringify(result, null, 2));
      setError(undefined);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.comparer_failed", "The browser comparison failed.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const sequence = async (event: FormEvent) => {
    event.preventDefault();
    if (!session) return;
    const samples = tokenSamples.split("\n").map((value) => value.trim()).filter(Boolean);
    setBusy(true);
    try {
      await api.createSecurityBrowserTokenAnalysis(projectId, { sessionId: session.id, name: "Token analysis", samples });
      setNotice("Token samples were analyzed descriptively; this is not a cryptographic certification.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.sequencer_failed", "The token analysis could not be completed.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  if (loading && !workspace) return <div className="browser-research-empty"><LoaderCircle className="spin" size={18} /> Loading durable research tools…</div>;
  if (fetchError && !workspace) return <div className="browser-research-empty error" role="alert"><strong>Research tools are unavailable</strong><span>{fetchError}</span><button className="button secondary" onClick={() => void refresh()} type="button">Try again</button></div>;

  return <div className="browser-suite" aria-busy={busy}>
    {fetchError && <div className="browser-notice error" role="alert"><span>{fetchError} Saved content remains visible.</span><button type="button" onClick={() => void refresh(true)}>Retry refresh</button></div>}
    {error && <div className="browser-notice error" role="alert"><AlertTriangle size={14} /><span>{error}</span><button type="button" aria-label="Dismiss research error" onClick={() => setError(undefined)}>×</button></div>}
    {notice && <div className="browser-notice" role="status"><span>{notice}</span>{view === "intercepts" && notice.startsWith("Request copied") && onOpenRepeater && <button type="button" onClick={onOpenRepeater}>Open in Repeater</button>}<button type="button" aria-label="Dismiss research notice" onClick={() => setNotice(undefined)}>×</button></div>}

    {view === "target" && <section aria-labelledby="browser-target-heading">
      <header className="browser-suite-heading"><div><Target size={16} /><span><h3 id="browser-target-heading">Target map</h3><small>In-scope locations discovered by browsing, proxy capture, HAR, crawl, and automation.</small></span></div><button className="icon-button subtle" aria-label="Refresh target map" type="button" onClick={() => void refresh()}><RefreshCw size={14} /></button></header>
      <form className="browser-suite-form" onSubmit={createCrawl}><label className="browser-suite-wide">Crawl start URL<input required value={crawlUrl} onChange={(event) => setCrawlUrl(event.target.value)} /></label><label>Maximum depth<input type="number" min={0} max={10} value={crawlDepth} onChange={(event) => setCrawlDepth(Number(event.target.value))} /></label><label>Request budget<input type="number" min={1} max={10000} value={crawlRequests} onChange={(event) => setCrawlRequests(Number(event.target.value))} /></label><button className="button primary" disabled={busy || !desktop || !session || !identity || !crawlUrl} type="submit">Create bounded crawl</button>{!desktop && <small className="browser-suite-wide">A paired client can inspect and stop crawls; the desktop owns network execution.</small>}</form>
      {sessionItems(workspace?.crawlJobs).length > 0 && <ol className="browser-suite-list">{[...sessionItems(workspace?.crawlJobs)].reverse().map((crawl) => <li key={crawl.id}><span className={`browser-action-status ${crawl.state}`}>{crawl.state}</span><div><strong>{crawl.startUrl}</strong><small>depth {crawl.maxDepth} · {crawl.requestsCompleted}/{crawl.maxRequests} requests · {crawl.nodesDiscovered} nodes{crawl.error ? ` · ${crawl.error}` : ""}</small><span className="browser-suite-actions">{crawl.state === "draft" && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "queue")}>Queue on desktop</button>}{crawl.state === "queued" && <small>Waiting for the owning desktop…</small>}{crawl.state === "running" && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "pause")}><Pause size={13} /> Pause</button>}{crawl.state === "paused" && <button className="button primary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "resume")}><Play size={13} /> Resume</button>}{["failed", "cancelled"].includes(crawl.state) && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "retry")}>Retry</button>}{["draft", "queued", "running", "paused"].includes(crawl.state) && <button className="button quiet danger" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "cancel")}><Square size={13} /> Cancel</button>}{["draft", "complete", "cancelled", "failed"].includes(crawl.state) && <button className="button quiet danger" disabled={busy} aria-label={`Delete crawl ${crawl.startUrl}`} type="button" onClick={() => void deleteCrawl(crawl)}><Trash2 size={13} /> Delete</button>}</span></div></li>)}</ol>}
      {sessionItems(workspace?.siteNodes).length ? <ol className="browser-suite-list">{sessionItems(workspace?.siteNodes).map((node) => <li key={node.id}><span className={`browser-method method-${node.method.toLowerCase()}`}>{node.method}</span><div><strong>{node.url}</strong><small>{node.kind} · {node.discoverySource}{node.statusCode ? ` · ${node.statusCode}` : ""}{node.parameterNames.length ? ` · parameters: ${node.parameterNames.join(", ")}` : ""}</small></div></li>)}</ol> : <div className="browser-research-empty"><Target size={20} /><strong>No mapped targets</strong><span>Browse an authorized page, import a HAR, or start a bounded crawl.</span></div>}
    </section>}

    {view === "intercepts" && <section aria-labelledby="browser-intercept-heading">
      <header className="browser-suite-heading"><div><ShieldAlert size={16} /><span><h3 id="browser-intercept-heading">Intercept queue</h3><small>Paused native requests and responses fail closed on expiry or disconnect.</small></span></div></header>
      {!desktop && <p className="browser-automation-mobile-note">This paired device can decide durable items, but only the desktop owns the live transaction.</p>}
      {sessionItems(workspace?.intercepts).length ? [true, false].map((pending) => { const items = sessionItems(workspace?.intercepts).filter((item) => (item.state === "paused") === pending); const list = <ol className="browser-suite-list">{[...items].reverse().map((item) => <li key={item.id}><span className={`browser-action-status ${item.state}`}>{item.state}</span><div><strong>{item.phase} · {item.method} {item.url}</strong><small>{item.statusCode ? `${item.statusCode} · ` : ""}{item.state === "paused" ? `Expires ${new Date(item.expiresAt).toLocaleTimeString()} · ${Math.max(0, Math.ceil((new Date(item.expiresAt).getTime() - Date.now()) / 1000))}s remaining` : `Expired or decided · ${new Date(item.expiresAt).toLocaleTimeString()}`}{item.error ? ` · ${item.error}` : ""}</small><details><summary>Headers ({item.headers.length})</summary><pre>{formatHeaders(item.headers) || "No retained headers"}</pre></details>{item.state === "paused" && item.phase === "request" && <details><summary>Edit request before forwarding</summary><form className="browser-suite-form" onSubmit={(event) => forwardEditedIntercept(event, item.id)}><label>Method<input name="method" required defaultValue={item.method} /></label><label className="browser-suite-wide">URL<input name="url" required defaultValue={item.url} /></label><label className="browser-suite-wide">Headers<textarea aria-label="Intercept headers" name="headers" rows={5} defaultValue={formatHeaders(item.headers)} /><small>Edited values replace matching headers. Redacted secrets remain unchanged.</small></label><button className="button primary" disabled={busy} type="submit">Forward edited request</button></form></details>}{item.state === "paused" && <span className="browser-suite-actions"><button className="button secondary" disabled={busy} type="button" onClick={() => void copyInterceptToRepeater(item)}>Copy to Repeater</button><button className="button quiet danger" disabled={busy} type="button" onClick={() => void decideIntercept(item.id, "drop")}>Drop</button><button className="button primary" disabled={busy} type="button" onClick={() => void decideIntercept(item.id, "forward")}>Forward {item.phase}</button></span>}</div></li>)}</ol>; return pending ? <section key="pending"><h4>Paused requests ({items.length})</h4>{items.length ? list : <p>No requests waiting for a decision.</p>}</section> : items.length > 0 ? <details key="history"><summary>Completed history ({items.length})</summary>{list}</details> : null; }) : <div className="browser-research-empty"><ShieldAlert size={20} /><strong>No paused traffic</strong><span>Enable interception in Session. Every in-scope request and response will pause here for an explicit decision.</span></div>}
    </section>}

    {view === "repeater" && <section className="repeater-workspace" aria-labelledby="browser-repeater-heading">
      <header className="browser-suite-heading"><div><Send size={16} /><span><h3 id="browser-repeater-heading">Repeater</h3><small>{identity?.name ?? "No identity selected"} · {desktop ? "Desktop connected" : "Sends require the paired desktop"}</small></span></div><button className="icon-button subtle" aria-label="New Repeater request" title="New Repeater request" type="button" disabled={busy} onClick={newRepeater}><Plus size={14} /></button></header>
      <nav className="repeater-requests" aria-label="Saved requests">
        <button type="button" aria-pressed={!selectedRepeaterId} disabled={busy} onClick={newRepeater}>New request</button>
        {[...sessionItems(workspace?.repeaterTabs)].reverse().map((tab) => <button key={tab.id} type="button" disabled={busy} aria-pressed={selectedRepeaterId === tab.id} aria-label={`${tab.method} ${tab.name}`} onClick={() => selectRepeater(tab)} title={tab.url}><span className="browser-method">{tab.method}</span><span>{tab.name}</span>{entry.drafts[tab.id] && draftValue(entry.drafts[tab.id]) !== entry.drafts[tab.id].baseline && <span aria-label="Unsaved changes"> •</span>}</button>)}
      </nav>
      <div className="repeater-message-grid">
      <form className="browser-suite-form repeater-editor" onSubmit={(event) => { event.preventDefault(); void saveRepeater(desktop); }}><fieldset disabled={busy}><legend>Request {dirty ? "· Unsaved changes" : ""}</legend><span className="browser-suite-actions browser-suite-wide"><button className="button secondary" disabled={busy || !session || !identity || !repeaterUrl} type="button" onClick={() => void saveRepeater(false)}>Save draft</button><button className="button primary" disabled={busy || !desktop || !session || !identity || !repeaterUrl} title={!desktop ? "The paired desktop owns native sends." : undefined} type="submit"><Send size={13} /> Send</button>{dirty && <button className="button quiet" type="button" onClick={() => void discardRepeaterChanges()}>Discard changes</button>}</span><label>Name<input required value={repeaterName} onChange={(event) => setRepeaterName(event.target.value)} /></label><label>Method<input required maxLength={32} value={repeaterMethod} onChange={(event) => setRepeaterMethod(event.target.value.toUpperCase())} /></label><label className="browser-suite-wide">URL<input required value={repeaterUrl} onChange={(event) => setRepeaterUrl(event.target.value)} /></label><label className="browser-suite-wide">Headers<textarea aria-label="Headers" rows={5} placeholder={'Accept: application/json\nX-Request-ID: test-1'} value={repeaterHeaders} onChange={(event) => setRepeaterHeaders(event.target.value)} /><small>One <code>Name: value</code> per line. Existing JSON header objects are also accepted.</small></label><label className="browser-suite-wide">Body<textarea aria-label="Body" rows={6} maxLength={65536} value={repeaterBody} onChange={(event) => setRepeaterBody(event.target.value)} /></label></fieldset></form>
      <section className="repeater-response" aria-labelledby="repeater-response-heading">
        <h4 id="repeater-response-heading">Response</h4>
        {selectedRepeaterId ? (() => {
          const results = (workspace?.repeaterResults ?? []).filter((result) => result.tabId === selectedRepeaterId).sort((a, b) => b.sequence - a.sequence);
          const result = results.find((item) => item.id === selectedResultId) ?? results[0];
          const tab = workspace?.repeaterTabs.find((item) => item.id === selectedRepeaterId);
          return <>
            {tab && <div className="repeater-result-context"><span className={`browser-action-status ${tab.state}`}>{tab.state}</span>{tab.error && <p role="alert">{tab.error}</p>}<span className="browser-suite-actions">{["failed", "cancelled"].includes(tab.state) && <button className="button secondary" disabled={busy || !desktop} type="button" onClick={() => void transitionRepeater(tab, "retry")}>Retry unchanged request</button>}{["queued", "running"].includes(tab.state) && <button className="button quiet danger" disabled={busy} type="button" onClick={() => void transitionRepeater(tab, "cancel")}><Square size={13} /> Cancel</button>}{!["queued", "running"].includes(tab.state) && <button className="icon-button subtle" disabled={busy} title={`Delete ${tab.name}`} aria-label={`Delete Repeater request ${tab.name}`} type="button" onClick={() => void deleteRepeater(tab)}><Trash2 size={14} aria-hidden="true" /></button>}</span></div>}
            {result ? <>
              <label>Result history ({results.length})<select aria-label="Response history" value={result.id} onChange={(event) => setSelectedResultId(event.target.value)}>{results.map((item) => <option key={item.id} value={item.id}>#{item.sequence + 1} · {item.error ? "Failed" : item.statusCode ?? "No status"} · {new Date(item.createdAt).toLocaleString()}</option>)}</select></label>
              <div className="repeater-response-meta"><strong>{result.error ? "Failed" : result.statusCode ?? "No status"}</strong><span>{result.durationMs ?? "—"} ms</span><span>{result.responseBytes ?? "—"} bytes</span></div>
              <small>Retained result #{result.sequence + 1}. This response does not represent unsaved edits. The submitted request revision is unavailable.</small>
              {result.error && <p role="alert">{result.error}</p>}
              <h5>Headers</h5><pre>{formatHeaders(result.responseHeaders) || "No retained response headers"}</pre>
              <h5>Body</h5>{result.responseBodyArtifactId ? <div className="browser-result-body"><button className="button secondary" disabled={busy} type="button" onClick={() => void loadRepeaterBody(result.responseBodyArtifactId!)}>Preview redacted body</button>{bodyPreviews[result.responseBodyArtifactId] !== undefined && <pre>{bodyPreviews[result.responseBodyArtifactId] || "Empty response body"}</pre>}</div> : <p>No retained response body.</p>}
            </> : <div className="browser-research-empty"><strong>No response yet</strong><span>Retained results for this request appear here.</span></div>}
          </>;
        })() : <div className="browser-research-empty"><strong>No request selected</strong><span>Select a saved request to inspect its response history.</span></div>}
      </section></div>
    </section>}

    {view === "intruder" && <section aria-labelledby="browser-intruder-heading">
      <header className="browser-suite-heading"><div><ShieldAlert size={16} /><span><h3 id="browser-intruder-heading">Intruder</h3><small>Curated or inert custom payloads with explicit rate, concurrency, and request budgets.</small></span></div></header>
      {!desktop && <p className="browser-automation-mobile-note">This device can monitor, pause, cancel, and retry. Only the paired desktop executes payload requests.</p>}
      <form className="browser-suite-form" onSubmit={createAttack}><label>Name<input required value={attackName} onChange={(event) => setAttackName(event.target.value)} /></label><label>Strategy<select value={attackStrategy} onChange={(event) => setAttackStrategy(event.target.value as SecurityBrowserAttack["strategy"])}><option value="sniper">Sniper</option><option value="battering_ram">Battering ram</option><option value="pitchfork">Pitchfork</option><option value="cluster_bomb">Cluster bomb</option></select></label><label>Method<input required value={attackMethod} onChange={(event) => setAttackMethod(event.target.value.toUpperCase())} /></label><label>Position names<input required value={attackPosition} onChange={(event) => setAttackPosition(event.target.value)} /><small>Comma-separated, for example <code>id, role</code>.</small></label><label className="browser-suite-wide">URL template<input required value={attackUrl} onChange={(event) => setAttackUrl(event.target.value)} /><small>Put a marker such as <code>§id§</code> in the URL, a header value, or the body for every named position.</small></label><label className="browser-suite-wide">Headers JSON<textarea rows={4} value={attackHeaders} onChange={(event) => setAttackHeaders(event.target.value)} /></label><label className="browser-suite-wide">Body template<textarea rows={5} maxLength={65536} value={attackBody} onChange={(event) => setAttackBody(event.target.value)} /></label><label className="browser-suite-wide">Payload sets<textarea rows={7} value={payloads} onChange={(event) => setPayloads(event.target.value)} /><small>One value per line. Pitchfork and cluster bomb need one set per position, in the same order; separate sets with a line containing only <code>---</code>.</small></label><button className="button primary" disabled={busy || !session || !identity || !attackUrl || !attackPosition.trim() || !payloads.trim()} type="submit">Save attack draft</button></form>
      {sessionItems(workspace?.attacks).length ? <ol className="browser-suite-list">{[...sessionItems(workspace?.attacks)].reverse().map((attack) => {
        const results = (workspace?.attackResults ?? []).filter((result) => result.attackId === attack.id).sort((left, right) => left.sequence - right.sequence);
        return <li key={attack.id}><span className={`browser-action-status ${attack.state}`}>{attack.state}</span><div><strong>{attack.name}</strong><small>{attack.strategy.replaceAll("_", " ")} · {attack.requestCount}/{attack.maxRequests} requests · {attack.errorCount} errors · {attack.requestsPerSecond}/s{attack.error ? ` · ${attack.error}` : ""}</small><span className="browser-suite-actions">{attack.state === "draft" && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionAttack(attack, "queue")}>Queue on desktop</button>}{attack.state === "queued" && <small>Waiting for the owning desktop…</small>}{attack.state === "running" && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionAttack(attack, "pause")}><Pause size={13} /> Pause</button>}{attack.state === "paused" && <button className="button primary" disabled={busy} type="button" onClick={() => void transitionAttack(attack, "resume")}><Play size={13} /> Resume</button>}{["failed", "cancelled"].includes(attack.state) && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionAttack(attack, "retry")}>Retry remaining</button>}{["draft", "queued", "running", "paused"].includes(attack.state) && <button className="button quiet danger" disabled={busy} type="button" onClick={() => void transitionAttack(attack, "cancel")}><Square size={13} /> Cancel</button>}{["draft", "complete", "cancelled", "failed"].includes(attack.state) && <button className="button quiet danger" disabled={busy} aria-label={`Delete Intruder attack ${attack.name}`} type="button" onClick={() => void deleteAttack(attack)}><Trash2 size={13} /> Delete</button>}</span>{results.length > 0 && <details><summary>Results ({results.length})</summary><ol className="browser-result-list">{results.map((result) => <li key={result.id}><code>#{result.sequence + 1}</code><strong>{result.error ? "ERR" : result.statusCode ?? "—"}</strong><span>{result.payloads.join(", ")} · {result.responseBytes ?? "—"} bytes · {result.durationMs ?? "—"} ms</span>{result.error && <small>{result.error}</small>}</li>)}</ol></details>}</div></li>;
      })}</ol> : <div className="browser-research-empty"><ShieldAlert size={20} /><strong>No attacks</strong><span>Create a bounded attack draft; requests run only after you queue it.</span></div>}
    </section>}

    {view === "utilities" && <section aria-labelledby="browser-utilities-heading">
      <header className="browser-suite-heading"><div><Braces size={16} /><span><h3 id="browser-utilities-heading">Decoder · Comparer · Sequencer</h3><small>Deterministic bounded utilities. Token analysis is descriptive, not a cryptographic certification.</small></span></div></header>
      <div className="browser-utility-grid"><form onSubmit={decode}><h4>Decoder</h4><label>Operation<select value={decoderOperation} onChange={(event) => setDecoderOperation(event.target.value)}><option value="url_encode">URL encode</option><option value="url_decode">URL decode</option><option value="html_encode">HTML encode</option><option value="html_decode">HTML decode</option><option value="base64_encode">Base64 encode</option><option value="base64_decode">Base64 decode</option><option value="hex_encode">Hex encode</option><option value="hex_decode">Hex decode</option><option value="gzip_compress">Gzip + Base64</option><option value="gzip_decompress">Base64 + gunzip</option><option value="jwt_inspect">Inspect JWT</option><option value="sha256">SHA-256</option></select></label><textarea aria-label="Decoder input" rows={5} value={decoderInput} onChange={(event) => setDecoderInput(event.target.value)} /><button className="button secondary" disabled={busy} type="submit">Transform</button><textarea aria-label="Decoder output" readOnly rows={5} value={decoderOutput} /></form><form onSubmit={compare}><h4><GitCompareArrows size={14} /> Comparer</h4><label>Mode<select value={compareMode} onChange={(event) => setCompareMode(event.target.value)}><option value="text">Text</option><option value="json">JSON</option><option value="http">HTTP message</option><option value="bytes">Base64 bytes</option></select></label><textarea aria-label="Compare left" rows={4} value={compareLeft} onChange={(event) => setCompareLeft(event.target.value)} /><textarea aria-label="Compare right" rows={4} value={compareRight} onChange={(event) => setCompareRight(event.target.value)} /><button className="button secondary" disabled={busy} type="submit">Compare</button><pre>{compareOutput}</pre></form><form onSubmit={sequence}><h4>Sequencer</h4><label>One token per line<textarea rows={7} value={tokenSamples} onChange={(event) => setTokenSamples(event.target.value)} /></label><button className="button secondary" disabled={busy || !session || !tokenSamples.trim()} type="submit">Analyze samples</button>{sessionItems(workspace?.tokenAnalyses).map((analysis) => <dl key={analysis.id}><div><dt>Samples</dt><dd>{analysis.sampleCount}</dd></div><div><dt>Unique</dt><dd>{analysis.uniqueCount}</dd></div><div><dt>Collisions</dt><dd>{analysis.collisionCount}</dd></div><div><dt>Entropy/char</dt><dd>{analysis.shannonBitsPerCharacter.toFixed(3)} bits</dd></div></dl>)}</form></div>
      <footer className="browser-suite-footer"><span>Promote verified request/response evidence through the existing finding lifecycle.</span><Link className="button secondary" to="/findings">Open Findings</Link></footer>
    </section>}
  </div>;
}
