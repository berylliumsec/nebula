import { Filter, Network, Plus, Search, Server, Upload } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

function displayTime(value?: string): string {
  if (!value) return "Never";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

export function AssetsPage() {
  const { assets, engagement } = useWorkspace();
  const serviceCount = assets.reduce((total, asset) => total + asset.serviceCount, 0);
  const observedCount = assets.filter((asset) => asset.lastSeenAt).length;
  const withFindings = assets.filter((asset) => asset.findingCount > 0).length;
  return (
    <div className="page assets-page">
      <PageHeader
        eyebrow="Attack surface"
        title="Assets"
        description="Normalized systems, services, software, identities, and their observed relationships."
        actions={
          <><button className="button secondary" type="button"><Upload size={16} /> Import inventory</button><button className="button primary" type="button"><Plus size={16} /> Add asset</button></>
        }
      />
      <section className="summary-strip" aria-label="Asset totals">
        <div><span className="summary-icon blue"><Network size={18} /></span><span><strong>{assets.length}</strong><small>Loaded assets</small></span></div>
        <div><span className="summary-icon violet"><Server size={18} /></span><span><strong>{serviceCount}</strong><small>Linked services</small></span></div>
        <div><span className="summary-icon green"><span className="status-dot healthy" /></span><span><strong>{observedCount}</strong><small>With observation time</small></span></div>
        <div><span className="summary-icon red"><span className="status-dot critical" /></span><span><strong>{withFindings}</strong><small>With findings</small></span></div>
      </section>

      <section className="panel data-panel">
        <header className="data-toolbar">
          <label className="search-field"><Search size={16} /><span className="sr-only">Search assets</span><input type="search" placeholder="Search host, domain, IP, tag…" /></label>
          <div><button className="button quiet" type="button"><Filter size={15} /> Filters <span className="count-badge">2</span></button><button className="button quiet" type="button">Topology</button><button className="button secondary active" type="button">Table</button></div>
        </header>
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th scope="col"><input type="checkbox" aria-label="Select all assets" /></th><th scope="col">Asset</th><th scope="col">Exposure</th><th scope="col">Services</th><th scope="col">Findings</th><th scope="col">Last observed</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {assets.map((asset) => (
                <tr key={asset.id}>
                  <td><input type="checkbox" aria-label={`Select ${asset.displayName}`} /></td>
                  <td><div className="asset-name"><span><Server size={16} /></span><div><strong>{asset.displayName}</strong><small>{asset.kind} · {engagement?.name ?? "preview engagement"}</small></div></div></td>
                  <td><span className={`exposure-badge ${asset.exposure}`}>{asset.exposure}</span></td>
                  <td><strong>{asset.serviceCount}</strong></td>
                  <td>{asset.findingCount > 0 ? <span className="finding-count">{asset.findingCount}</span> : "—"}</td>
                  <td>{displayTime(asset.lastSeenAt)}</td>
                  <td><button className="text-link" type="button">Inspect</button></td>
                </tr>
              ))}
              {assets.length === 0 && <tr><td colSpan={7}>No assets have been recorded for this engagement.</td></tr>}
            </tbody>
          </table>
        </div>
        <footer className="table-footer"><span>Showing {assets.length} loaded assets</span><div><button disabled type="button">Previous</button><button disabled type="button">Next</button></div></footer>
      </section>
    </div>
  );
}
