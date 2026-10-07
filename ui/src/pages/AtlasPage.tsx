import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowRight, CircleAlert, FileText, RefreshCw, X } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { ModalSurface } from "../components/DialogSystem";
import { projectSurface } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";
import "./AtlasPage.css";

type AtlasView = "projects" | "maps" | "simulations";
type AtlasProject = {
  name: string;
  path: string;
  missing: boolean;
  roles: string[];
  surfaces: string[];
  lastWorkedAt: string | null;
  workspace: { linked: boolean; engagementId: string | null };
  workflow: { status: string; summary: string; nextStep: string | null; updatedAt: string | null; source: "nebula-work" | "unknown" };
  migration: { status: "linked" | "unlinked" | "missing"; label: string; detail: string };
  v2Migration: { status: string; label: string; detail: string; validation: string; auditedAt: string | null; auditDetail: string; remainingGate: string | null };
  binaries: { count: number | null; totalBytes: number | null; files: unknown[] };
  lanes: { indexed: boolean; total: number | null; open: number | null; needsSimulation: number | null; readyForRuntime: number | null; stale: number | null };
};
type AtlasDocument = {
  id: string; title: string; kind: string; featured: boolean; project: string;
  path: string; excerpt: string; headings: string[]; updatedAt: string | null;
};
type AtlasDocumentDetail = AtlasDocument & { content: string; truncated: boolean };
type AtlasSimulation = {
  runKey: string; project: string; status: string; feasibility: string;
  decisionImpact: string; summary: string; updatedAt: string | null;
};
type AtlasIndex = {
  generatedAt: string;
  source: { available: boolean; mapPath: string; mapSnapshot: string | null; statusAuthority: string; migrationAuthority: string };
  counts: { grandThreatProjects: number; openLanes: number | null; needsSimulation: number | null; documents: number; simulations: number };
  grandThreatProjects: AtlasProject[];
  documents: AtlasDocument[];
  simulations: AtlasSimulation[];
};

const views: { id: AtlasView; label: string }[] = [
  { id: "projects", label: "Priority projects" },
  { id: "maps", label: "Maps & inventories" },
  { id: "simulations", label: "Simulation records" },
];

function date(value: string | null | undefined): string {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "Unknown" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

function bytes(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value <= 0) return "Unknown";
  const units = ["B", "KB", "MB", "GB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

function count(value: number | null): string { return value === null ? "Unknown" : value.toLocaleString(); }
function errorText(caught: unknown): string { return caught instanceof Error ? caught.message : "Core could not load Intel Atlas."; }
function matches(query: string, values: Array<string | string[] | null | undefined>): boolean {
  const terms = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const haystack = values.flatMap((value) => value ?? []).join(" ").toLocaleLowerCase();
  return terms.every((term) => haystack.includes(term));
}

export function AtlasPage() {
  const { api, reconnect } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const requested = params.get("view");
  const view: AtlasView = views.some((item) => item.id === requested) ? requested as AtlasView : "projects";
  const query = params.get("q") ?? "";
  const role = params.get("role") === "focused" || params.get("role") === "supporting" ? params.get("role")! : "all";
  const documentId = params.get("document");
  const [atlas, setAtlas] = useState<AtlasIndex>();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string>();
  const [document, setDocument] = useState<AtlasDocumentDetail>();
  const [documentError, setDocumentError] = useState<string>();
  const [documentAttempt, setDocumentAttempt] = useState(0);

  const setParam = useCallback((key: string, value: string | null, replace = true) => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    }, { replace });
  }, [setParams]);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!api) { setLoading(false); return; }
    setRefreshing(true);
    try {
      const next = await api.request<AtlasIndex>("atlas", { signal });
      if (signal?.aborted) return;
      setAtlas(next);
      setError(undefined);
    } catch (caught) {
      if (!signal?.aborted) setError(errorText(caught));
    } finally {
      if (!signal?.aborted) { setLoading(false); setRefreshing(false); }
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!api || !documentId) return;
    const controller = new AbortController();
    setDocument(undefined);
    setDocumentError(undefined);
    void api.request<AtlasDocumentDetail>(`atlas/documents/${encodeURIComponent(documentId)}`, { signal: controller.signal })
      .then((record) => { if (!controller.signal.aborted) setDocument(record); })
      .catch((caught: unknown) => { if (!controller.signal.aborted) setDocumentError(errorText(caught)); });
    return () => controller.abort();
  }, [api, documentId, documentAttempt]);

  const projects = useMemo(() => (atlas?.grandThreatProjects ?? []).filter((project) =>
    (role === "all" || project.roles.includes(role)) && matches(query, [project.name, project.path, project.roles, project.surfaces, project.workflow.status, project.workflow.summary, project.v2Migration.label])),
  [atlas, query, role]);
  const documents = useMemo(() => (atlas?.documents ?? []).filter((record) =>
    matches(query, [record.title, record.project, record.kind, record.path, record.excerpt, record.headings])), [atlas, query]);
  const simulations = useMemo(() => (atlas?.simulations ?? []).filter((record) =>
    matches(query, [record.runKey, record.project, record.status, record.feasibility, record.decisionImpact, record.summary])), [atlas, query]);
  const resultCount = view === "projects" ? projects.length : view === "maps" ? documents.length : simulations.length;
  const selectedDocument = atlas?.documents.find((record) => record.id === documentId);
  const closeDocument = () => setParam("document", null, false);

  return <div className="page atlas-page">
    <header className="atlas-head">
      <div><p className="atlas-eyebrow">Research intelligence</p><h1>Intel Atlas</h1><p>Grand Threat projects, research maps, and simulation records.</p></div>
      <button className="button quiet atlas-refresh" type="button" aria-label="Refresh Intel Atlas" title="Refresh Intel Atlas" disabled={!api || refreshing} onClick={() => void load()}><RefreshCw size={17} aria-hidden="true" className={refreshing ? "spin" : undefined} /></button>
    </header>

    {atlas?.source.available && <p className="atlas-source">Map: {atlas.source.mapPath}{atlas.source.mapSnapshot ? ` · snapshot ${atlas.source.mapSnapshot}` : ""} · Read {date(atlas.generatedAt)}. Workflow status comes from Nebula Work when linked.</p>}
    {error && <div className="atlas-error" role="alert"><CircleAlert size={17} aria-hidden="true" /><span>Intel Atlas could not refresh. {error} {atlas ? "The last loaded view is still shown." : ""}</span><button className="button quiet" type="button" onClick={() => api ? void load() : reconnect()}>{api ? "Retry" : "Reconnect"}</button></div>}
    {loading && <div className="atlas-loading" role="status">Loading Intel Atlas…</div>}
    {!loading && !atlas && !error && <div className="atlas-loading" role="status">Connect to Nebula Core to load Intel Atlas.</div>}
    {atlas && !atlas.source.available && <section className="atlas-unavailable" role="status"><h2>Research map unavailable</h2><p>Nebula could not find the Grand Threat project map from linked research projects. Link an Apple research project folder, then refresh Intel Atlas.</p><Link to="/projects">Open Projects <ArrowRight size={15} aria-hidden="true" /></Link></section>}
    {atlas?.source.available && <>
      <section className="atlas-metrics" aria-label="Atlas summary">
        {([
          [atlas.counts.grandThreatProjects, "Grand Threat projects"],
          [atlas.counts.openLanes, "Open lanes"],
          [atlas.counts.needsSimulation, "Need simulation"],
          [atlas.counts.documents, "Maps & inventories"],
          [atlas.counts.simulations, "Simulation records"],
        ] as const).map(([value, label]) => <div key={label}><strong>{count(value)}</strong><span>{label}</span></div>)}
      </section>
      <nav className="atlas-views" aria-label="Atlas views">{views.map((item) => {
        const next = new URLSearchParams(params);
        if (item.id === "projects") next.delete("view"); else next.set("view", item.id);
        next.delete("document");
        return <Link key={item.id} to={`/atlas${next.size ? `?${next}` : ""}`} aria-current={view === item.id ? "page" : undefined} className={view === item.id ? "active" : undefined}>{item.label}</Link>;
      })}</nav>
      <div className="atlas-filters">
        <label>Search<input type="search" value={query} onChange={(event) => setParam("q", event.target.value)} placeholder="Search this view" /></label>
        {view === "projects" && <label>Map role<select value={role} onChange={(event) => setParam("role", event.target.value === "all" ? null : event.target.value)}><option value="all">Focused + supporting</option><option value="focused">Focused only</option><option value="supporting">Supporting only</option></select></label>}
        <span role="status">{resultCount} {view === "projects" ? "projects" : view === "maps" ? "documents" : "records"}</span>
      </div>
      {view === "projects" && <section className="atlas-projects" aria-label="Priority projects">
        {projects.map((project) => <article className="atlas-project" key={project.path}>
          <div className="atlas-project-title"><div><h2>{project.name}</h2><code>{project.path}</code>{project.missing && <span className="atlas-missing">Project scaffold missing</span>}</div>{project.workspace.engagementId && <Link className="atlas-open-work" to={projectSurface(project.workspace.engagementId, "work")}>Open Work <ArrowRight size={15} aria-hidden="true" /></Link>}</div>
          <div className="atlas-chips">{project.roles.map((item) => <span key={`role-${item}`}>{item}</span>)}{project.surfaces.map((item) => <span key={`surface-${item}`}>{item}</span>)}</div>
          <dl className="atlas-project-facts">
            <div><dt>Workflow</dt><dd><strong>{project.workflow.source === "nebula-work" ? project.workflow.status.replaceAll("_", " ") : "Unknown"}</strong><small>{project.workflow.source === "nebula-work" ? project.workflow.nextStep || project.workflow.summary || "No next step recorded" : project.workspace.linked ? "No Research status item in Work" : "No linked Nebula project"}</small>{project.workflow.source === "nebula-work" && <small>{project.workflow.updatedAt ? `Work check-in ${date(project.workflow.updatedAt)}` : "Work item has no check-in yet"}</small>}</dd></div>
            <div><dt>V2 migration audit</dt><dd><strong>{project.v2Migration.label || "Unknown"}</strong><small>{project.v2Migration.auditedAt ? `Audited ${date(project.v2Migration.auditedAt)}` : "No dated audit"}{project.v2Migration.auditDetail ? ` · ${project.v2Migration.auditDetail}` : ""}</small></dd></div>
            <div><dt>Target binaries</dt><dd><strong>{bytes(project.binaries.totalBytes)}</strong><small>{project.binaries.count === null ? "No size data" : `${project.binaries.count} targets`}</small></dd></div>
            <div><dt>Last Work check-in</dt><dd><strong>{date(project.lastWorkedAt)}</strong></dd></div>
            <div><dt>Open lanes</dt><dd><strong>{project.lanes.indexed ? count(project.lanes.open) : "Unknown"}</strong><small>{project.lanes.indexed ? `${count(project.lanes.total)} compiled total` : "No compiled lane index"}</small></dd></div>
            <div><dt>Need simulation</dt><dd><strong>{project.lanes.indexed ? count(project.lanes.needsSimulation) : "Unknown"}</strong><small>{project.lanes.indexed ? `${count(project.lanes.readyForRuntime)} ready for runtime · ${count(project.lanes.stale)} stale` : "Lane state unknown"}</small></dd></div>
          </dl>
        </article>)}
        {!projects.length && <p className="atlas-empty">No priority projects match these filters.</p>}
      </section>}
      {view === "maps" && <section className="atlas-documents" aria-label="Maps and inventories">
        {documents.map((record) => <button className="atlas-document" type="button" key={record.id} onClick={() => setParam("document", record.id, false)}><span>{record.kind}{record.featured ? " · Featured" : ""}</span><strong>{record.title}</strong><p>{record.excerpt}</p><small>{record.project} · {date(record.updatedAt)}</small></button>)}
        {!documents.length && <p className="atlas-empty">No maps or inventories match this search.</p>}
      </section>}
      {view === "simulations" && <section className="atlas-simulations" aria-label="Simulation records">
        {simulations.map((record, index) => <article className="atlas-simulation" key={`${record.runKey}-${index}`}><div><h2>{record.runKey}</h2><span>{record.project}</span></div><p>{record.summary || "No summary recorded"}</p><dl><div><dt>Status</dt><dd>{record.status || "Unknown"}</dd></div><div><dt>Feasibility</dt><dd>{record.feasibility || "Unknown"}</dd></div><div><dt>Decision impact</dt><dd>{record.decisionImpact || "Unknown"}</dd></div><div><dt>Indexed</dt><dd>{date(record.updatedAt)}</dd></div></dl></article>)}
        {!simulations.length && <p className="atlas-empty">No simulation records match this search.</p>}
      </section>}
    </>}
    {documentId && <ModalSurface className="atlas-document-dialog" labelledBy="atlas-document-title" onClose={closeDocument}><header><div><small>{selectedDocument?.kind ?? document?.kind ?? "Map or inventory"}</small><h2 id="atlas-document-title">{selectedDocument?.title ?? document?.title ?? "Document"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close document" title="Close document" onClick={closeDocument}><X size={18} aria-hidden="true" /></button></header>{documentError ? <div role="alert" className="atlas-document-error"><CircleAlert size={17} aria-hidden="true" /><span>Document could not load. {documentError}</span><button className="button quiet" type="button" onClick={() => setDocumentAttempt((value) => value + 1)}>Retry</button></div> : !document || document.id !== documentId ? <p role="status">Loading document…</p> : <><p className="atlas-document-path"><FileText size={15} aria-hidden="true" /> {document.path}</p><pre>{document.content}</pre>{document.truncated && <p className="atlas-truncated">Preview ends at the server read limit. Open the source file for the rest.</p>}</>}</ModalSurface>}
  </div>;
}
