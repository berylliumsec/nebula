import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, Braces, GitCompareArrows, LoaderCircle, Pause, Play, RefreshCw, Send, ShieldAlert, Square, Target } from "lucide-react";
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

export function BrowserResearchSuite({ api, desktop, identity, operatorId, projectId, session, view }: Props) {
  const [workspace, setWorkspace] = useState<SecurityBrowserResearchWorkspace>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [repeaterName, setRepeaterName] = useState("Repeater");
  const [repeaterMethod, setRepeaterMethod] = useState("GET");
  const [repeaterUrl, setRepeaterUrl] = useState("");
  const [attackName, setAttackName] = useState("Identifier boundaries");
  const [attackStrategy, setAttackStrategy] = useState<SecurityBrowserAttack["strategy"]>("sniper");
  const [attackMethod, setAttackMethod] = useState("GET");
  const [attackUrl, setAttackUrl] = useState("");
  const [attackPosition, setAttackPosition] = useState("id");
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

  const refresh = useCallback(async () => {
    setLoading(true);
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
    const current = session?.tabs.find((tab) => tab.id === session.activeTabId)?.url ?? session?.tabs[0]?.url ?? "";
    if (!repeaterUrl) setRepeaterUrl(current);
    if (!attackUrl) setAttackUrl(current ? `${current.replace(/\/$/, "")}/§id§` : "");
    if (!crawlUrl) setCrawlUrl(current);
  }, [attackUrl, crawlUrl, repeaterUrl, session]);

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
        maxConcurrency: 2,
        maxDurationSeconds: 300,
        maxBodyBytes: 1_048_576,
      });
      setNotice("Bounded crawl saved as a draft. Queue and start it explicitly from the owning desktop.");
      await refresh();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const transitionCrawl = async (crawl: SecurityBrowserCrawlJob, action: "queue" | "start" | "pause" | "resume" | "cancel") => {
    setBusy(true);
    try {
      await api.transitionSecurityBrowserCrawl(crawl, action, operatorId);
      await refresh();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const createRepeater = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !identity) return;
    setBusy(true);
    try {
      await api.createSecurityBrowserRepeaterTab(projectId, {
        sessionId: session.id,
        identityId: identity.id,
        name: repeaterName,
        method: repeaterMethod,
        url: repeaterUrl,
      });
      setNotice("Repeater tab saved. Sending remains an explicit native action.");
      await refresh();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const createAttack = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !identity) return;
    const values = payloads.split("\n").map((value) => value.trim()).filter(Boolean);
    setBusy(true);
    try {
      await api.createSecurityBrowserAttack(projectId, {
        sessionId: session.id,
        identityId: identity.id,
        name: attackName,
        strategy: attackStrategy,
        method: attackMethod,
        urlTemplate: attackUrl,
        positions: [attackPosition],
        payloadValues: values,
        transforms: ["url_encode"],
        maxRequests: Math.min(1000, Math.max(1, values.length)),
        maxConcurrency: 1,
        requestsPerSecond: 2,
      });
      setNotice("Intruder attack saved as a draft. Queue it when its positions and budgets are correct.");
      await refresh();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  const transitionAttack = async (attack: SecurityBrowserAttack, action: "queue" | "start" | "pause" | "resume" | "cancel") => {
    setBusy(true);
    try {
      await api.transitionSecurityBrowserAttack(attack, action, operatorId);
      await refresh();
    } catch (caught) {
      setError(message(caught));
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
      {sessionItems(workspace?.crawlJobs).length > 0 && <ol className="browser-suite-list">{[...sessionItems(workspace?.crawlJobs)].reverse().map((crawl) => <li key={crawl.id}><span className={`browser-action-status ${crawl.state}`}>{crawl.state}</span><div><strong>{crawl.startUrl}</strong><small>depth {crawl.maxDepth} · {crawl.requestsCompleted}/{crawl.maxRequests} requests · {crawl.nodesDiscovered} nodes</small><span className="browser-suite-actions">{crawl.state === "draft" && <button className="button secondary" type="button" onClick={() => void transitionCrawl(crawl, "queue")}>Queue</button>}{crawl.state === "queued" && desktop && <button className="button primary" type="button" onClick={() => void transitionCrawl(crawl, "start")}><Play size={13} /> Start</button>}{crawl.state === "running" && <button className="button secondary" type="button" onClick={() => void transitionCrawl(crawl, "pause")}><Pause size={13} /> Pause</button>}{crawl.state === "paused" && desktop && <button className="button primary" type="button" onClick={() => void transitionCrawl(crawl, "resume")}><Play size={13} /> Resume</button>}{["draft", "queued", "running", "paused"].includes(crawl.state) && <button className="button quiet danger" type="button" onClick={() => void transitionCrawl(crawl, "cancel")}><Square size={13} /> Cancel</button>}</span></div></li>)}</ol>}
      {sessionItems(workspace?.siteNodes).length ? <ol className="browser-suite-list">{sessionItems(workspace?.siteNodes).map((node) => <li key={node.id}><span className={`browser-method method-${node.method.toLowerCase()}`}>{node.method}</span><div><strong>{node.url}</strong><small>{node.kind} · {node.discoverySource}{node.statusCode ? ` · ${node.statusCode}` : ""}{node.parameterNames.length ? ` · parameters: ${node.parameterNames.join(", ")}` : ""}</small></div></li>)}</ol> : <div className="browser-research-empty"><Target size={20} /><strong>No mapped targets</strong><span>Browse an authorized page, import a HAR, or start a bounded crawl.</span></div>}
    </section>}

    {view === "intercepts" && <section aria-labelledby="browser-intercept-heading">
      <header className="browser-suite-heading"><div><ShieldAlert size={16} /><span><h3 id="browser-intercept-heading">Intercept queue</h3><small>Paused native requests and responses fail closed on expiry or disconnect.</small></span></div></header>
      {!desktop && <p className="browser-automation-mobile-note">This paired device can decide durable items, but only the desktop owns the live transaction.</p>}
      {sessionItems(workspace?.intercepts).length ? <ol className="browser-suite-list">{[...sessionItems(workspace?.intercepts)].reverse().map((item) => <li key={item.id}><span className={`browser-action-status ${item.state}`}>{item.state}</span><div><strong>{item.phase} · {item.method} {item.url}</strong><small>Expires {new Date(item.expiresAt).toLocaleTimeString()}{item.error ? ` · ${item.error}` : ""}</small>{item.state === "paused" && <span className="browser-suite-actions"><button className="button secondary" disabled={busy} type="button" onClick={() => void decideIntercept(item.id, "drop")}>Drop</button><button className="button primary" disabled={busy} type="button" onClick={() => void decideIntercept(item.id, "forward")}>Forward</button></span>}</div></li>)}</ol> : <div className="browser-research-empty"><ShieldAlert size={20} /><strong>No paused traffic</strong><span>Enable interception in Session, then configure a breakpoint in the native proxy.</span></div>}
    </section>}

    {view === "repeater" && <section aria-labelledby="browser-repeater-heading">
      <header className="browser-suite-heading"><div><Send size={16} /><span><h3 id="browser-repeater-heading">Repeater</h3><small>Save protocol-aware request tabs without copying identity cookies.</small></span></div></header>
      <form className="browser-suite-form" onSubmit={createRepeater}><label>Name<input value={repeaterName} onChange={(event) => setRepeaterName(event.target.value)} /></label><label>Method<input value={repeaterMethod} onChange={(event) => setRepeaterMethod(event.target.value.toUpperCase())} /></label><label>URL<input required value={repeaterUrl} onChange={(event) => setRepeaterUrl(event.target.value)} /></label><button className="button primary" disabled={busy || !session || !identity || !repeaterUrl} type="submit">Save Repeater tab</button></form>
      {sessionItems(workspace?.repeaterTabs).length ? <ol className="browser-suite-list">{sessionItems(workspace?.repeaterTabs).map((tab) => <li key={tab.id}><span className={`browser-method method-${tab.method.toLowerCase()}`}>{tab.method}</span><div><strong>{tab.name}</strong><small>{tab.url} · {tab.historyExchangeIds.length} responses · identity isolated</small></div></li>)}</ol> : <div className="browser-research-empty"><Send size={20} /><strong>No Repeater tabs</strong><span>Save the current request or create a new in-scope request above.</span></div>}
    </section>}

    {view === "intruder" && <section aria-labelledby="browser-intruder-heading">
      <header className="browser-suite-heading"><div><ShieldAlert size={16} /><span><h3 id="browser-intruder-heading">Intruder</h3><small>Curated or inert custom payloads with explicit rate, concurrency, and request budgets.</small></span></div></header>
      <form className="browser-suite-form" onSubmit={createAttack}><label>Name<input value={attackName} onChange={(event) => setAttackName(event.target.value)} /></label><label>Strategy<select value={attackStrategy} onChange={(event) => setAttackStrategy(event.target.value as SecurityBrowserAttack["strategy"])}><option value="sniper">Sniper</option><option value="battering_ram">Battering ram</option><option value="pitchfork">Pitchfork</option><option value="cluster_bomb">Cluster bomb</option></select></label><label>Method<input value={attackMethod} onChange={(event) => setAttackMethod(event.target.value.toUpperCase())} /></label><label>Position name<input value={attackPosition} onChange={(event) => setAttackPosition(event.target.value)} /></label><label className="browser-suite-wide">URL template<input required value={attackUrl} onChange={(event) => setAttackUrl(event.target.value)} /><small>Mark a position as <code>§{attackPosition || "id"}§</code>.</small></label><label className="browser-suite-wide">Payloads<textarea rows={5} value={payloads} onChange={(event) => setPayloads(event.target.value)} /></label><button className="button primary" disabled={busy || !session || !identity || !attackUrl || !payloads.trim()} type="submit">Save attack draft</button></form>
      {sessionItems(workspace?.attacks).length ? <ol className="browser-suite-list">{sessionItems(workspace?.attacks).map((attack) => <li key={attack.id}><span className={`browser-action-status ${attack.state}`}>{attack.state}</span><div><strong>{attack.name}</strong><small>{attack.strategy.replace("_", " ")} · {attack.requestCount}/{attack.maxRequests} requests · {attack.requestsPerSecond}/s</small><span className="browser-suite-actions">{attack.state === "draft" && <button className="button secondary" type="button" onClick={() => void transitionAttack(attack, "queue")}>Queue</button>}{attack.state === "queued" && <button className="button primary" type="button" onClick={() => void transitionAttack(attack, "start")}><Play size={13} /> Start</button>}{attack.state === "running" && <button className="button secondary" type="button" onClick={() => void transitionAttack(attack, "pause")}><Pause size={13} /> Pause</button>}{attack.state === "paused" && <button className="button primary" type="button" onClick={() => void transitionAttack(attack, "resume")}><Play size={13} /> Resume</button>}{["draft", "queued", "running", "paused"].includes(attack.state) && <button className="button quiet danger" type="button" onClick={() => void transitionAttack(attack, "cancel")}><Square size={13} /> Cancel</button>}</span></div></li>)}</ol> : <div className="browser-research-empty"><ShieldAlert size={20} /><strong>No attacks</strong><span>Create a bounded attack draft; it sends nothing until explicitly started.</span></div>}
    </section>}

    {view === "utilities" && <section aria-labelledby="browser-utilities-heading">
      <header className="browser-suite-heading"><div><Braces size={16} /><span><h3 id="browser-utilities-heading">Decoder · Comparer · Sequencer</h3><small>Deterministic bounded utilities. Token analysis is descriptive, not a cryptographic certification.</small></span></div></header>
      <div className="browser-utility-grid"><form onSubmit={decode}><h4>Decoder</h4><label>Operation<select value={decoderOperation} onChange={(event) => setDecoderOperation(event.target.value)}><option value="url_encode">URL encode</option><option value="url_decode">URL decode</option><option value="html_encode">HTML encode</option><option value="html_decode">HTML decode</option><option value="base64_encode">Base64 encode</option><option value="base64_decode">Base64 decode</option><option value="hex_encode">Hex encode</option><option value="hex_decode">Hex decode</option><option value="gzip_compress">Gzip + Base64</option><option value="gzip_decompress">Base64 + gunzip</option><option value="jwt_inspect">Inspect JWT</option><option value="sha256">SHA-256</option></select></label><textarea aria-label="Decoder input" rows={5} value={decoderInput} onChange={(event) => setDecoderInput(event.target.value)} /><button className="button secondary" disabled={busy} type="submit">Transform</button><textarea aria-label="Decoder output" readOnly rows={5} value={decoderOutput} /></form><form onSubmit={compare}><h4><GitCompareArrows size={14} /> Comparer</h4><label>Mode<select value={compareMode} onChange={(event) => setCompareMode(event.target.value)}><option value="text">Text</option><option value="json">JSON</option><option value="http">HTTP message</option><option value="bytes">Base64 bytes</option></select></label><textarea aria-label="Compare left" rows={4} value={compareLeft} onChange={(event) => setCompareLeft(event.target.value)} /><textarea aria-label="Compare right" rows={4} value={compareRight} onChange={(event) => setCompareRight(event.target.value)} /><button className="button secondary" disabled={busy} type="submit">Compare</button><pre>{compareOutput}</pre></form><form onSubmit={sequence}><h4>Sequencer</h4><label>One token per line<textarea rows={7} value={tokenSamples} onChange={(event) => setTokenSamples(event.target.value)} /></label><button className="button secondary" disabled={busy || !session || !tokenSamples.trim()} type="submit">Analyze samples</button>{sessionItems(workspace?.tokenAnalyses).map((analysis) => <dl key={analysis.id}><div><dt>Samples</dt><dd>{analysis.sampleCount}</dd></div><div><dt>Unique</dt><dd>{analysis.uniqueCount}</dd></div><div><dt>Collisions</dt><dd>{analysis.collisionCount}</dd></div><div><dt>Entropy/char</dt><dd>{analysis.shannonBitsPerCharacter.toFixed(3)} bits</dd></div></dl>)}</form></div>
      <footer className="browser-suite-footer"><span>Promote verified request/response evidence through the existing finding lifecycle.</span><Link className="button secondary" to="/findings">Open Findings</Link></footer>
    </section>}
  </div>;
}
