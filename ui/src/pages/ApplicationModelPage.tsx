import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useWorkspace } from "../state/WorkspaceContext";
import { PageHeader } from "../components/PageHeader";
import { Activity, Database, GitBranch, Network, SearchCheck } from "lucide-react";
import { ApplicationModelGraph } from "../components/ApplicationModelGraph";
import "./applicationModel.css";

type Value = { kind: string; type: string; value?: string | number | boolean; reason?: string };
type Collection = { id: string; browser_session_id: string; status: string; processed_count: number; last_source_id?: string; error?: string };
type State = { id: string; created_at: string; branch_key: string; parent_state_ids: string[]; object_version_ids: string[]; observation_ids: string[] };
type Observation = { id: string; source_kind: string; source_id: string; occurred_at: string; causal_status: string; facts: Record<string, Value>; evidence_ids: string[] };
type Version = { id: string; object_id: string; properties: Record<string, Value>; observation_ids: string[] };
type Formula = { op: string; field?: string; value?: string | number | boolean; args?: Formula[]; values?: unknown[] };
type Query = { id: string; state_id: string; status: string; formula: Formula; assertion_ids: string[]; result?: string; base_result?: string; error?: string; assignments: Record<string, unknown>; unsat_core: string[] };
type Workspace = { session: Collection; projection_errors?: string[]; states: State[]; observations: Observation[]; objects: { id: string; label: string }[]; object_versions: Version[]; assertions: { id: string; subject: string; predicate: string; support: string; lifecycle: string; formula?: unknown }[]; queries: Query[] };
type Status = { solver_available: boolean; pending_count: number };

function browserSource(project: string | undefined, browser: string, observation: Observation) {
  const query = new URLSearchParams({ view: "browser", browserSession: browser });
  if (observation.source_kind === "browser_traffic") {
    query.set("tool", "traffic");
    query.set("browserExchange", observation.source_id);
  }
  return `/projects/${project}/workbench?${query}`;
}

function describeCondition(formula: Formula, depth = 0): string {
  if (depth > 20) return "…";
  if (formula.op === "field") return formula.field?.split(".").slice(1).join(".") ?? "field";
  if (formula.op === "literal") return JSON.stringify(formula.value);
  return `${formula.op} (${(formula.args ?? []).map(arg => describeCondition(arg, depth + 1)).join(", ")}${formula.values?.length ? `, ${formula.values.join(", ")}` : ""})`;
}

export function buildCondition(field: string, operator: string, type: string, text: string) {
  let value: string | boolean | number = text;
  if (type === "integer") {
    value = Number(text);
    if (!text.trim() || !Number.isSafeInteger(value)) throw new Error("Enter a whole number within the supported integer range.");
  }
  if (type === "boolean") {
    if (text !== "true" && text !== "false") throw new Error("Choose true or false.");
    value = text === "true";
  }
  return { op: operator, args: [{ op: "field", field }, { op: "literal", value }] };
}

export function ApplicationModelPage() {
  const { api, engagement } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const collection = params.get("collection") ?? "";
  const [status, setStatus] = useState<Status>();
  const [sessions, setSessions] = useState<Collection[]>([]);
  const [browsers, setBrowsers] = useState<{ id: string; name: string }[]>([]);
  const [workspace, setWorkspace] = useState<Workspace>();
  const [browser, setBrowser] = useState("");
  const [history, setHistory] = useState(true);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [field, setField] = useState("");
  const [operator, setOperator] = useState("eq");
  const [literal, setLiteral] = useState("");
  const [assumptions, setAssumptions] = useState<string[]>([]);
  const [compare, setCompare] = useState("");
  const [difference, setDifference] = useState<{ field: string; before: Value | null; after: Value | null }[]>();
  const base = `engagements/${encodeURIComponent(engagement?.id ?? "")}/application-model`;
  const select = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    if (key === "collection") for (const k of ["state", "object", "query"]) next.delete(k);
    if (key === "state") { next.delete("query"); next.delete("object"); }
    if (key === "query") {
      const query = workspace?.queries.find(q => q.id === value);
      if (query) next.set("state", query.state_id);
      else if (state) next.set("state", state.id);
    }
    setParams(next);
  };
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!api || !engagement) return;
    const [s, list, bw] = await Promise.all([
      api.request<Status>(`${base}/status`, { signal }),
      api.request<Collection[]>(`${base}/sessions`, { signal }),
      api.request<{ id: string; name: string }[]>(`${base}/browser-sessions`, { signal }),
    ]);
    const next = collection ? await api.request<Workspace>(`${base}/sessions/${encodeURIComponent(collection)}/workspace`, { signal }) : undefined;
    if (signal?.aborted) return;
    setStatus(s); setSessions(list); setBrowsers(bw); setWorkspace(next); setLoadError("");
  }, [api, engagement, base, collection]);
  useEffect(() => {
    const controller = new AbortController();
    setWorkspace(undefined);
    setAssumptions([]); setCompare(""); setDifference(undefined); setError("");
    const refresh = () => { void load(controller.signal).catch((e: unknown) => { if (!controller.signal.aborted) setLoadError(e instanceof Error ? e.message : "Could not load application model."); }); };
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [load]);
  const mutate = async (path: string, body?: unknown, method = "POST") => {
    if (!api) return;
    setBusy(true); setError("");
    try {
      const result = await api.request<{ id?: string }>(`${base}${path}`, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      if (method !== "DELETE") await load();
      return result;
    } catch (e) { setError(e instanceof Error ? e.message : "Operation failed. Retry from this view."); }
    finally { setBusy(false); }
  };
  const stateId = params.get("state");
  const state = stateId ? workspace?.states.find(s => s.id === stateId) : workspace?.states.at(-1);
  const versions = useMemo(() => workspace?.object_versions.filter(v => state?.object_version_ids.includes(v.id)) ?? [], [workspace, state]);
  const fields = useMemo(() => Object.fromEntries(versions.flatMap(v => Object.entries(v.properties).map(([name, value]) => [`${v.object_id}.${name}`, value]))), [versions]);
  const selectedField = fields[field] ? field : Object.keys(fields)[0] ?? "";
  const selectedQuery = workspace?.queries.find(q => q.id === params.get("query"));
  const selectedObjectId = params.get("object") ?? "";
  const selectedObject = workspace?.objects.find(item => item.id === selectedObjectId);
  const selectedObjectVersion = [...(workspace?.object_versions ?? [])].reverse().find(item => item.object_id === selectedObjectId && state?.object_version_ids.includes(item.id));
  const tab = params.get("modelTab") ?? "map";
  const scope = `/sessions/${encodeURIComponent(collection)}`;
  return <div className="page application-model-page">
    <PageHeader title="Application model" description="Recorded knowledge, evidence, and constraint analysis." />
    {(error || loadError) && <div role="alert"><p>{error || loadError}</p><button className="button" onClick={() => { setError(""); void load().catch(e => setLoadError(String(e))); }}>Retry</button>{collection && <button className="button" onClick={() => select("collection", "")}>Return to collections</button>}</div>}
    {!status && !error && !loadError && <p role="status">Loading application model…</p>}
    {status && <>
      <section className="panel model-section"><h2>Collections</h2>
        <label>Collection<select value={collection} onChange={e => select("collection", e.target.value)}><option value="">Choose a collection</option>{sessions.map(s => <option key={s.id} value={s.id}>{browsers.find(b => b.id === s.browser_session_id)?.name ?? "Browser collection"} · {s.status} · {s.processed_count} records</option>)}</select></label>
        <form onSubmit={async e => { e.preventDefault(); const result = await mutate("/sessions", { browser_session_id: browser || browsers[0]?.id, import_history: history }); if (result?.id) select("collection", result.id); }}>
          <label>Source browser for new model<select value={browser || browsers[0]?.id || ""} onChange={e => setBrowser(e.target.value)}>{browsers.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select></label>
          <p>Shared Chromium creates its collection automatically on the first recorded interaction. Use this form to create or import another browser collection.</p>
          <label className="model-checkbox"><input type="checkbox" checked={history} onChange={e => setHistory(e.target.checked)} />Include recorded history</label>
          <button className="button primary" disabled={busy || !browsers.length}>Create collection</button>
        </form>
        {!browsers.length && <p>Open Browser in the project workbench to create a browser session first.</p>}
      </section>
      {workspace && <>
        <section className="model-overview" aria-label="Application model summary">
          <div><Network aria-hidden="true" /><span>Observations</span><strong>{workspace.observations.length}</strong></div>
          <div><Database aria-hidden="true" /><span>Objects</span><strong>{workspace.objects.length}</strong></div>
          <div><GitBranch aria-hidden="true" /><span>States</span><strong>{workspace.states.length}</strong></div>
          <div><SearchCheck aria-hidden="true" /><span>Queries</span><strong>{workspace.queries.length}</strong></div>
        </section>
        <section className="panel model-section"><p role="status">{workspace.session.status} · {workspace.session.processed_count} records · {status.pending_count} pending</p>
          {workspace.session.last_source_id && <details><summary>Projection checkpoint</summary><code>{workspace.session.last_source_id}</code></details>}
          {workspace.session.error && <p role="alert">{workspace.session.error}</p>}
          {!!workspace.projection_errors?.length && <div role="alert"><p>Some records could not be projected. Browser records remain available.</p><button className="button" disabled={busy} onClick={() => void mutate(`${scope}/retry`)}>Retry projection</button></div>}
          <div className="model-actions"><button className="button" disabled={busy} onClick={() => void mutate(`${scope}/${workspace.session.status === "paused" ? "resume" : "pause"}`)}>{workspace.session.status === "paused" ? "Resume" : "Pause"}</button><button className="button" disabled={busy || workspace.session.status === "paused"} onClick={() => void mutate(`${scope}/import`)}>Import history</button>
          <button className="button" disabled={busy} onClick={async () => { if (window.confirm("Delete this derived collection? Browser records and source evidence are retained.")) { const result = await mutate(scope, undefined, "DELETE"); if (result) select("collection", ""); } }}>Delete collection</button></div>
          <nav className="model-view-tabs" aria-label="Application model views">{[["map", "Model map"], ["states", "Timeline"], ["objects", "Objects and evidence"], ["solver", "Solver"]].map(([id, label]) => <button className={tab === id ? "active" : ""} aria-current={tab === id ? "page" : undefined} key={id} onClick={() => select("modelTab", id)}>{label}</button>)}</nav>
          <label>Knowledge state<select value={state?.id ?? ""} onChange={e => { select("state", e.target.value); setDifference(undefined); }}><option value="">Select a state</option>{workspace.states.map((s, i) => <option key={s.id} value={s.id}>State {i + 1} · context {s.branch_key} · {new Date(s.created_at).toLocaleTimeString()}</option>)}</select></label>
          {stateId && !state && <p role="alert">This state is unavailable in the selected collection.</p>}
          {!workspace.states.length && <p>No recorded states yet. Interact through the existing Browser, or import its recorded history.</p>}
        </section>
        {state && tab === "map" && <section className="model-map-layout">
          <ApplicationModelGraph data={workspace} selectedStateId={state.id} selectedObjectId={selectedObjectId} onSelectState={id => select("state", id)} onSelectObject={id => select("object", id)} />
          <aside className="panel model-inspector" aria-label="Graph selection inspector">
            {selectedObject && selectedObjectVersion ? <>
              <span className="model-eyebrow">Selected object</span><h2>{selectedObject.label}</h2>
              <p>{selectedObjectVersion.id.slice(0, 18)} · {Object.keys(selectedObjectVersion.properties).length} properties</p>
              <dl>{Object.entries(selectedObjectVersion.properties).slice(0, 12).map(([name, value]) => <div key={name}><dt>{name}</dt><dd><span className={`model-value-dot ${value.kind}`} /><span className="model-inspector-value">{value.kind === "concrete" ? String(value.value) : value.reason ?? value.kind}</span></dd></div>)}</dl>
              <button className="button" onClick={() => select("modelTab", "objects")}>Open full history</button>
            </> : <><Activity aria-hidden="true" /><h2>Inspect the model</h2><p>Select an object to see its typed properties, unknowns, source observations, and history.</p><dl><div><dt>Current state</dt><dd>{state.id.slice(0, 16)}</dd></div><div><dt>Branch</dt><dd>{state.branch_key}</dd></div><div><dt>Parent states</dt><dd>{state.parent_state_ids.length}</dd></div></dl></>}
          </aside>
        </section>}
        {state && tab === "states" && <section className="panel model-section"><h2>Recorded observations</h2><p>This snapshot represents knowledge at collection time. It does not restore the remote application.</p>
          {state.parent_state_ids.map(id => <button className="button" key={id} onClick={() => select("state", id)}>Inspect parent state</button>)}
          {workspace.observations.filter(o => state.observation_ids.includes(o.id)).map(o => <article key={o.id}><h3>{o.source_kind}</h3><p>{new Date(o.occurred_at).toLocaleString()} · causality {o.causal_status}</p><dl>{Object.entries(o.facts).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value.kind === "concrete" ? String(value.value) : value.reason ?? value.kind}</dd></div>)}</dl><Link to={browserSource(engagement?.id, workspace.session.browser_session_id, o)}>Open source browser session</Link></article>)}
          <label>Compare with<select value={compare} onChange={e => setCompare(e.target.value)}><option value="">Choose another state</option>{workspace.states.filter(s => s.id !== state.id).map((s, i) => <option value={s.id} key={s.id}>State {i + 1} · {s.branch_key} · {new Date(s.created_at).toLocaleTimeString()}</option>)}</select></label><button className="button" disabled={!compare || busy} onClick={async () => { try { const result = await api?.request<{ changes: NonNullable<typeof difference> }>(`${base}${scope}/diff?left=${encodeURIComponent(compare)}&right=${encodeURIComponent(state.id)}`); setDifference(result?.changes); } catch(e) { setError(String(e)); } }}>Compare states</button>
          {difference && <div><h3>{difference.length} changed properties</h3>{difference.map(change => <p key={change.field}>{change.field.split(".").slice(1).join(".")}: {String(change.before?.value ?? change.before?.reason ?? "absent")} → {String(change.after?.value ?? change.after?.reason ?? "absent")}</p>)}</div>}
        </section>}
        {state && tab === "objects" && <section className="panel model-section"><h2>Objects and evidence</h2>{versions.map(v => <details key={v.id} open={params.get("object") === v.object_id} onToggle={e => { if (e.currentTarget.open && params.get("object") !== v.object_id) select("object", v.object_id); }}>
          <summary>{workspace.objects.find(o => o.id === v.object_id)?.label ?? "Recorded object"}</summary>
          <dl>{Object.entries(v.properties).map(([name, value]) => <div key={name}><dt>{name} · {value.type}</dt><dd>{value.kind === "concrete" ? String(value.value) : `${value.kind}: ${value.reason ?? "unresolved"}`}</dd></div>)}</dl>
          <h3>Source observations</h3>
          {workspace.observations.filter(o => v.observation_ids.includes(o.id)).map(o => <p key={o.id}><Link to={browserSource(engagement?.id, workspace.session.browser_session_id, o)}>Open {o.source_kind}</Link>{o.evidence_ids.map(id => <span key={id}> · <Link to={`/projects/${engagement?.id}/evidence/${encodeURIComponent(id)}`}>Source evidence</Link></span>)}</p>)}
          <details><summary>Assertions and version history</summary>
            {workspace.assertions.filter(a => a.subject === v.object_id).map(a => <p key={a.id}>{a.predicate} · {a.support} · {a.lifecycle}</p>)}
            {workspace.object_versions.filter(old => old.object_id === v.object_id).map((old, index) => {
              const snapshot = workspace.states.find(s => s.object_version_ids.includes(old.id));
              return <p key={old.id}><button className="button" disabled={!snapshot || old.id === v.id} onClick={() => snapshot && select("state", snapshot.id)}>Inspect version {index + 1}{old.id === v.id ? " (selected)" : ""}</button></p>;
            })}
          </details>
        </details>)}</section>}
        {state && tab === "solver" && <section className="panel model-section"><h2>Constraint query</h2><p>Results describe consistency with recorded facts and the assumptions selected below.</p>
          {!status.solver_available && <p role="alert">The Z3 solver is unavailable on Core. Recorded states remain readable.</p>}
          <form onSubmit={async e => { e.preventDefault(); try { const formula = buildCondition(selectedField, operator, fields[selectedField].type, literal); const result = await mutate(`${scope}/queries`, { state_id: state.id, formula, assertion_ids: assumptions }); if (result?.id) select("query", result.id); } catch(e) { setError(e instanceof Error ? e.message : String(e)); } }}>
            <label>Property<select value={selectedField} onChange={e => { setField(e.target.value); setLiteral(""); setOperator("eq"); }}>{Object.entries(fields).map(([name, value]) => <option key={name} value={name}>{name.split(".").slice(1).join(".")} · {value.type} · {name.slice(0, 16)}</option>)}</select></label>
            <label>Condition<select value={operator} onChange={e => setOperator(e.target.value)}><option value="eq">Equals</option><option value="ne">Does not equal</option>{fields[selectedField]?.type === "integer" && <><option value="lt">Less than</option><option value="gt">Greater than</option></>}</select></label>
            <label>Value{fields[selectedField]?.type === "boolean" ? <select value={literal} onChange={e => setLiteral(e.target.value)}><option value="">Choose a value</option><option value="true">true</option><option value="false">false</option></select> : <input value={literal} onChange={e => setLiteral(e.target.value)} />}</label>
            {workspace.assertions.filter(a => a.formula).map(a => <label className="model-checkbox" key={a.id}><input type="checkbox" checked={assumptions.includes(a.id)} onChange={e => setAssumptions(e.target.checked ? [...assumptions, a.id] : assumptions.filter(id => id !== a.id))} />Assume {a.predicate} · {a.support} · {a.lifecycle}</label>)}
            <button className="button primary" disabled={busy || !selectedField || !status.solver_available}>Check consistency</button>
          </form>
          <label>Saved query<select value={selectedQuery?.id ?? ""} onChange={e => select("query", e.target.value)}><option value="">Select a query</option>{workspace.queries.map((q, i) => <option key={q.id} value={q.id}>Query {i + 1} · {q.result ?? q.status}</option>)}</select></label>
          {params.get("query") && !selectedQuery && <p role="alert">This saved query is unavailable in the selected collection.</p>}
          {selectedQuery && <p>Saved condition: {describeCondition(selectedQuery.formula)} · {selectedQuery.assertion_ids.length} explicitly selected assumptions. The form above is a separate draft.</p>}
          {!!selectedQuery?.unsat_core.length && <details><summary>Conflicting facts and assumptions</summary>{selectedQuery.unsat_core.map(id => <p key={id}>{workspace.assertions.find(a => a.id === id)?.predicate ?? id}</p>)}</details>}
          {selectedQuery && <div role="status"><h3>{selectedQuery.result ?? selectedQuery.status}</h3>{selectedQuery.base_result === "UNSAT" && <p>The base assumptions are inconsistent. Review them before interpreting this result.</p>}{selectedQuery.error && <p>{selectedQuery.error}</p>}<dl>{Object.entries(selectedQuery.assignments).map(([name, value]) => <div key={name}><dt>{name.split(".").slice(1).join(".")}</dt><dd>{String(value)}</dd></div>)}</dl>{["queued", "running"].includes(selectedQuery.status) && <button className="button" onClick={() => void mutate(`${scope}/queries/${selectedQuery.id}/cancel`)}>Cancel query</button>}</div>}
        </section>}
      </>}
    </>}
  </div>;
}
