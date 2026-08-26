import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, Braces, GitCompareArrows, LoaderCircle, Pause, Play, RefreshCw, Send, ShieldAlert, Square, Target, Trash2 } from "lucide-react";
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
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function headerPairs(value: string): Array<[string, string]> {
  const parsed = JSON.parse(value || "{}") as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)
    || Object.values(parsed).some((item) => typeof item !== "string")) {
    throw new Error("Headers must be a JSON object with string values.");
  }
  return Object.entries(parsed) as Array<[string, string]>;
}

export function BrowserResearchSuite({ api, desktop, identity, operatorId, projectId, session, view }: Props) {
  const confirm = useConfirmation();
  const [workspace, setWorkspace] = useState<SecurityBrowserResearchWorkspace>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [repeaterName, setRepeaterName] = useState("Repeater");
  const [repeaterMethod, setRepeaterMethod] = useState("GET");
  const [repeaterUrl, setRepeaterUrl] = useState("");
  const [repeaterHeaders, setRepeaterHeaders] = useState("{}");
  const [repeaterBody, setRepeaterBody] = useState("");
  const [selectedRepeaterId, setSelectedRepeaterId] = useState<string>();
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

  const refresh = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    try {
      setWorkspace(await api.getSecurityBrowserResearch(projectId));
      setError(undefined);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.research_suite_load_failed", "Burp-parity browser research state could not be loaded.", caught, "workbench_browser");
      setError(message(caught));
    } finally {
      setLoading(false);
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
    setRepeaterUrl(current);
    setAttackUrl(current ? `${current.replace(/\/$/, "")}/§id§` : "");
    setCrawlUrl(current);
    setSelectedRepeaterId(undefined);
  }, [session?.id]);

  const sessionItems = <T extends { sessionId: string }>(items: T[] | undefined): T[] =>
    items?.filter((item) => item.sessionId === session?.id) ?? [];

  const decideIntercept = async (id: string, decision: "forward" | "drop") => {
    const item = workspace?.intercepts.find((candidate) => candidate.id === id);
    if (!item) return;
    setBusy(true);
    try {
      await api.decideSecurityBrowserIntercept(item, decision, operatorId);
      setNotice(decision === "forward" ? "The paused transaction was released." : "The paused transaction was dropped.");
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.intercept_decision_failed", "The paused browser transaction could not be decided.", caught, "browser_research_suite");
      setError(message(caught));
    } finally {
      setBusy(false);
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

  const createRepeater = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !identity) return;
    setBusy(true);
    setError(undefined);
    try {
      const headers = headerPairs(repeaterHeaders);
      const selected = workspace?.repeaterTabs.find((tab) => tab.id === selectedRepeaterId);
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
      setSelectedRepeaterId(saved.id);
      setNotice(selected ? "Repeater request updated." : "Repeater request saved. Review it, then queue one native send.");
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
    setRepeaterName(tab.name);
    setRepeaterMethod(tab.method);
    setRepeaterUrl(tab.url);
    setRepeaterHeaders(JSON.stringify(Object.fromEntries(tab.headers), null, 2));
    setRepeaterBody(tab.bodyTemplate);
    setError(undefined);
    setNotice(undefined);
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
      if (selectedRepeaterId === tab.id) setSelectedRepeaterId(undefined);
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
      const body = (await blob.text()).slice(0, 1_048_576);
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
  if (error && !workspace) return <div className="browser-research-empty error" role="alert"><strong>Research tools are unavailable</strong><span>{error}</span><button className="button secondary" onClick={() => void refresh()} type="button">Try again</button></div>;

  return <div className="browser-suite" aria-busy={busy}>
    {error && <div className="browser-notice error" role="alert"><AlertTriangle size={14} /><span>{error}</span><button type="button" aria-label="Dismiss research error" onClick={() => setError(undefined)}>×</button></div>}
    {notice && <div className="browser-notice" role="status"><span>{notice}</span><button type="button" aria-label="Dismiss research notice" onClick={() => setNotice(undefined)}>×</button></div>}

    {view === "target" && <section aria-labelledby="browser-target-heading">
      <header className="browser-suite-heading"><div><Target size={16} /><span><h3 id="browser-target-heading">Target map</h3><small>In-scope locations discovered by browsing, proxy capture, HAR, crawl, and automation.</small></span></div><button className="icon-button subtle" aria-label="Refresh target map" type="button" onClick={() => void refresh()}><RefreshCw size={14} /></button></header>
      <form className="browser-suite-form" onSubmit={createCrawl}><label className="browser-suite-wide">Crawl start URL<input required value={crawlUrl} onChange={(event) => setCrawlUrl(event.target.value)} /></label><label>Maximum depth<input type="number" min={0} max={10} value={crawlDepth} onChange={(event) => setCrawlDepth(Number(event.target.value))} /></label><label>Request budget<input type="number" min={1} max={10000} value={crawlRequests} onChange={(event) => setCrawlRequests(Number(event.target.value))} /></label><button className="button primary" disabled={busy || !desktop || !session || !identity || !crawlUrl} type="submit">Create bounded crawl</button>{!desktop && <small className="browser-suite-wide">A paired client can inspect and stop crawls; the desktop owns network execution.</small>}</form>
      {sessionItems(workspace?.crawlJobs).length > 0 && <ol className="browser-suite-list">{[...sessionItems(workspace?.crawlJobs)].reverse().map((crawl) => <li key={crawl.id}><span className={`browser-action-status ${crawl.state}`}>{crawl.state}</span><div><strong>{crawl.startUrl}</strong><small>depth {crawl.maxDepth} · {crawl.requestsCompleted}/{crawl.maxRequests} requests · {crawl.nodesDiscovered} nodes{crawl.error ? ` · ${crawl.error}` : ""}</small><span className="browser-suite-actions">{crawl.state === "draft" && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "queue")}>Queue on desktop</button>}{crawl.state === "queued" && <small>Waiting for the owning desktop…</small>}{crawl.state === "running" && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "pause")}><Pause size={13} /> Pause</button>}{crawl.state === "paused" && <button className="button primary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "resume")}><Play size={13} /> Resume</button>}{["failed", "cancelled"].includes(crawl.state) && <button className="button secondary" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "retry")}>Retry</button>}{["draft", "queued", "running", "paused"].includes(crawl.state) && <button className="button quiet danger" disabled={busy} type="button" onClick={() => void transitionCrawl(crawl, "cancel")}><Square size={13} /> Cancel</button>}{["draft", "complete", "cancelled", "failed"].includes(crawl.state) && <button className="button quiet danger" disabled={busy} aria-label={`Delete crawl ${crawl.startUrl}`} type="button" onClick={() => void deleteCrawl(crawl)}><Trash2 size={13} /> Delete</button>}</span></div></li>)}</ol>}
      {sessionItems(workspace?.siteNodes).length ? <ol className="browser-suite-list">{sessionItems(workspace?.siteNodes).map((node) => <li key={node.id}><span className={`browser-method method-${node.method.toLowerCase()}`}>{node.method}</span><div><strong>{node.url}</strong><small>{node.kind} · {node.discoverySource}{node.statusCode ? ` · ${node.statusCode}` : ""}{node.parameterNames.length ? ` · parameters: ${node.parameterNames.join(", ")}` : ""}</small></div></li>)}</ol> : <div className="browser-research-empty"><Target size={20} /><strong>No mapped targets</strong><span>Browse an authorized page, import a HAR, or start a bounded crawl.</span></div>}
    </section>}

    {view === "intercepts" && <section aria-labelledby="browser-intercept-heading">
      <header className="browser-suite-heading"><div><ShieldAlert size={16} /><span><h3 id="browser-intercept-heading">Intercept queue</h3><small>Paused native requests and responses fail closed on expiry or disconnect.</small></span></div></header>
      {!desktop && <p className="browser-automation-mobile-note">This paired device can decide durable items, but only the desktop owns the live transaction.</p>}
      {sessionItems(workspace?.intercepts).length ? <ol className="browser-suite-list">{[...sessionItems(workspace?.intercepts)].reverse().map((item) => <li key={item.id}><span className={`browser-action-status ${item.state}`}>{item.state}</span><div><strong>{item.phase} · {item.method} {item.url}</strong><small>Expires {new Date(item.expiresAt).toLocaleTimeString()}{item.error ? ` · ${item.error}` : ""}</small>{item.state === "paused" && <span className="browser-suite-actions"><button className="button secondary" disabled={busy} type="button" onClick={() => void decideIntercept(item.id, "drop")}>Drop</button><button className="button primary" disabled={busy} type="button" onClick={() => void decideIntercept(item.id, "forward")}>Forward</button></span>}</div></li>)}</ol> : <div className="browser-research-empty"><ShieldAlert size={20} /><strong>No paused traffic</strong><span>Enable interception in Session. Every in-scope request and response will pause here for an explicit decision.</span></div>}
    </section>}

    {view === "repeater" && <section aria-labelledby="browser-repeater-heading">
      <header className="browser-suite-heading"><div><Send size={16} /><span><h3 id="browser-repeater-heading">Repeater</h3><small>Edit and send one scope-checked request through the selected desktop identity. Cookies remain native.</small></span></div></header>
      {!desktop && <p className="browser-automation-mobile-note">This device can inspect, cancel, retry, and delete durable requests. The paired desktop performs sends.</p>}
      <form className="browser-suite-form" onSubmit={createRepeater}><label>Name<input required value={repeaterName} onChange={(event) => setRepeaterName(event.target.value)} /></label><label>Method<input required maxLength={32} value={repeaterMethod} onChange={(event) => setRepeaterMethod(event.target.value.toUpperCase())} /></label><label className="browser-suite-wide">URL<input required value={repeaterUrl} onChange={(event) => setRepeaterUrl(event.target.value)} /></label><label className="browser-suite-wide">Headers JSON<textarea rows={5} value={repeaterHeaders} onChange={(event) => setRepeaterHeaders(event.target.value)} /></label><label className="browser-suite-wide">Body<textarea rows={6} maxLength={65536} value={repeaterBody} onChange={(event) => setRepeaterBody(event.target.value)} /></label><button className="button primary" disabled={busy || !session || !identity || !repeaterUrl} type="submit">{selectedRepeaterId ? "Save request changes" : "Save Repeater request"}</button></form>
      {sessionItems(workspace?.repeaterTabs).length ? <ol className="browser-suite-list">{[...sessionItems(workspace?.repeaterTabs)].reverse().map((tab) => {
        const results = (workspace?.repeaterResults ?? []).filter((result) => result.tabId === tab.id).sort((left, right) => right.sequence - left.sequence);
        return <li key={tab.id} className={selectedRepeaterId === tab.id ? "selected" : ""}><span className={`browser-method method-${tab.method.toLowerCase()}`}>{tab.method}</span><div><button className="browser-suite-select" type="button" onClick={() => selectRepeater(tab)}><strong>{tab.name}</strong><small>{tab.url} · {results.length} retained result{results.length === 1 ? "" : "s"} · identity isolated</small></button><span className={`browser-action-status ${tab.state}`}>{tab.state}</span>{tab.error && <small className="browser-suite-error">{tab.error}</small>}<span className="browser-suite-actions">{["draft", "ready"].includes(tab.state) && <button className="button primary" disabled={busy || !desktop} title={!desktop ? "The paired desktop owns native sends." : undefined} type="button" onClick={() => void transitionRepeater(tab, "queue")}><Send size={13} /> Send once</button>}{["failed", "cancelled"].includes(tab.state) && <button className="button secondary" disabled={busy || !desktop} type="button" onClick={() => void transitionRepeater(tab, "retry")}>Retry send</button>}{["queued", "running"].includes(tab.state) && <button className="button quiet danger" disabled={busy} type="button" onClick={() => void transitionRepeater(tab, "cancel")}><Square size={13} /> Cancel</button>}{!["queued", "running"].includes(tab.state) && <button className="button quiet danger" disabled={busy} aria-label={`Delete Repeater request ${tab.name}`} type="button" onClick={() => void deleteRepeater(tab)}><Trash2 size={13} /> Delete</button>}</span>{results.length > 0 && <details><summary>Result history ({results.length})</summary><ol className="browser-result-list">{results.map((result) => <li key={result.id}><strong>{result.error ? "Failed" : result.statusCode ?? "No status"}</strong><span>{result.durationMs === undefined ? "—" : `${result.durationMs} ms`} · {result.responseBytes === undefined ? "—" : `${result.responseBytes} bytes`}</span>{result.error && <small>{result.error}</small>}<pre>{JSON.stringify(Object.fromEntries(result.responseHeaders), null, 2)}</pre>{result.responseBodyArtifactId && <div className="browser-result-body"><button className="button secondary" disabled={busy} type="button" onClick={() => void loadRepeaterBody(result.responseBodyArtifactId!)}>Preview redacted body</button>{bodyPreviews[result.responseBodyArtifactId] !== undefined && <pre>{bodyPreviews[result.responseBodyArtifactId]}</pre>}</div>}</li>)}</ol></details>}</div></li>;
      })}</ol> : <div className="browser-research-empty"><Send size={20} /><strong>No Repeater requests</strong><span>Save the current URL or import an in-scope request from Proxy traffic.</span></div>}
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
