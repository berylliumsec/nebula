import { BookOpen, Database, FileText, Globe2, RefreshCw, Search, ShieldAlert, Upload } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

const sources = [
  { name: "Acme rules of engagement.pdf", type: "Document", icon: FileText, chunks: 84, state: "ready", updated: "Today, 08:04" },
  { name: "OWASP Web Security Testing Guide", type: "Approved web source", icon: Globe2, chunks: 1240, state: "ready", updated: "Jul 10" },
  { name: "Imported scanner observations", type: "Structured engagement data", icon: Database, chunks: 386, state: "ready", updated: "6 minutes ago" },
  { name: "Customer architecture bundle", type: "Directory", icon: BookOpen, chunks: 0, state: "indexing", updated: "Started 2 minutes ago" },
];

export function KnowledgePage() {
  const { previewMode } = useWorkspace();
  const visibleSources = previewMode ? sources : [];
  return (
    <div className="page knowledge-page">
      <PageHeader eyebrow="Cited context" title="Knowledge" description="Approved sources are visible, removable, reindexable, and isolated from executable instructions." actions={<button className="button primary" type="button"><Upload size={16} /> Add source</button>} />
      <div className="knowledge-layout">
        <section className="panel data-panel knowledge-sources">
          <header className="data-toolbar"><label className="search-field"><Search size={16} /><span className="sr-only">Search knowledge sources</span><input type="search" placeholder="Search sources…" /></label><button className="button quiet" type="button"><RefreshCw size={15} /> Reindex</button></header>
          <div className="source-list">
            {visibleSources.map(({ name, type, icon: Icon, chunks, state, updated }) => (
              <article key={name}><span className="source-icon"><Icon size={19} /></span><div><h3>{name}</h3><p>{type}</p></div><span><strong>{chunks || "—"}</strong><small>chunks</small></span><span className={`source-state ${state}`}>{state === "indexing" && <RefreshCw className="spin" size={13} />}{state}</span><span className="source-updated">{updated}</span><button className="text-link" type="button">Open</button></article>
            ))}
            {visibleSources.length === 0 && <div className="empty-state compact"><BookOpen size={23} /><strong>No knowledge sources loaded</strong><p>Connect the Core knowledge resource to browse engagement sources.</p></div>}
          </div>
        </section>
        <aside className="panel knowledge-policy">
          <span className="policy-illustration"><ShieldAlert size={28} /></span><h2>Retrieval boundary</h2><p>Knowledge content is treated as untrusted data. Instructions found inside sources cannot grant tools, expand scope, or alter system policy.</p>
          <ul><li>Source identity included with every chunk</li><li>Cloud-transfer preview before retrieval</li><li>Secrets and excluded paths are redacted</li><li>Vector index can be rebuilt from authoritative data</li></ul>
          <button className="button secondary full" type="button">Review knowledge policy</button>
        </aside>
      </div>
    </div>
  );
}
