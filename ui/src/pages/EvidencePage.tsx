import { Camera, FileCode2, FileSearch, Image, LockKeyhole, Search, Upload } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

const artifacts = [
  { name: "gateway-service-detection.xml", type: "Nmap XML", icon: FileCode2, size: "84 KB", hash: "7f392a…d861", source: "Network analyst", linked: 2 },
  { name: "jwt-algorithm-response.har", type: "HTTP archive", icon: FileSearch, size: "218 KB", hash: "a821d4…1c09", source: "Web analyst", linked: 1 },
  { name: "admin-login-annotated.png", type: "Redacted derivative", icon: Image, size: "1.4 MB", hash: "0bc122…7d3e", source: "Jordan Diaz", linked: 1 },
  { name: "tls-certificate-chain.pem", type: "Certificate", icon: LockKeyhole, size: "8 KB", hash: "917df1…02af", source: "Recon specialist", linked: 0 },
];

export function EvidencePage() {
  const { previewMode } = useWorkspace();
  const visibleArtifacts = previewMode ? artifacts : [];
  return (
    <div className="page evidence-page">
      <PageHeader
        eyebrow="Immutable provenance"
        title="Evidence"
        description="Content-addressed artifacts preserve source, command, timestamps, and finding links."
        actions={<><button className="button secondary" type="button"><Camera size={16} /> Capture</button><button className="button primary" type="button"><Upload size={16} /> Add evidence</button></>}
      />
      <div className="evidence-callout callout"><LockKeyhole size={18} /><div><strong>Originals are immutable</strong><p>Annotations and redactions create separately hashed derivative artifacts.</p></div><span>SHA-256</span></div>
      <section className="panel data-panel evidence-panel">
        <header className="data-toolbar"><label className="search-field"><Search size={16} /><span className="sr-only">Search evidence</span><input type="search" placeholder="Search artifact, hash, source, finding…" /></label><div><button className="button secondary active" type="button">Grid</button><button className="button quiet" type="button">Table</button></div></header>
        <div className="artifact-grid">
          {visibleArtifacts.map(({ name, type, icon: Icon, size, hash, source, linked }) => (
            <article className="artifact-card" key={name}>
              <div className="artifact-preview"><Icon size={31} strokeWidth={1.4} /><span>{type}</span></div>
              <div className="artifact-body"><h3>{name}</h3><p>{size} · <code>{hash}</code></p><dl><div><dt>Source</dt><dd>{source}</dd></div><div><dt>Findings</dt><dd>{linked || "Not linked"}</dd></div></dl></div>
              <footer><span><LockKeyhole size={13} /> Verified</span><button className="text-link" type="button">Inspect</button></footer>
            </article>
          ))}
          {visibleArtifacts.length === 0 && <div className="empty-state compact"><LockKeyhole size={23} /><strong>No evidence loaded</strong><p>Evidence records will appear after the Core evidence endpoint is connected to this view.</p></div>}
        </div>
        <footer className="table-footer"><span>{visibleArtifacts.length} artifacts in view</span><span>{previewMode ? "Preview hashes" : "Verification state supplied by Core"}</span></footer>
      </section>
    </div>
  );
}
