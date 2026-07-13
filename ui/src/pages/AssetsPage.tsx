import { useMemo, useState, type FormEvent } from "react";
import { Network, Plus, Search, Server, Upload, X } from "lucide-react";
import type { AssetSummary } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

function displayTime(value?: string): string {
  if (!value) return "Never";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

export function AssetsPage() {
  const { addAsset, assets, engagement, findings, previewMode } = useWorkspace();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<"all" | AssetSummary["kind"]>("all");
  const [exposure, setExposure] = useState<"all" | AssetSummary["exposure"]>("all");
  const [selected, setSelected] = useState<AssetSummary>();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [assetKind, setAssetKind] = useState<AssetSummary["kind"]>("host");
  const [address, setAddress] = useState("");
  const [hostname, setHostname] = useState("");
  const [criticality, setCriticality] = useState<AssetSummary["criticality"]>("medium");
  const [assetExposure, setAssetExposure] = useState<AssetSummary["exposure"]>("unknown");
  const [tags, setTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const visibleAssets = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return assets.filter((asset) => {
      if (kind !== "all" && asset.kind !== kind) return false;
      if (exposure !== "all" && asset.exposure !== exposure) return false;
      if (!needle) return true;
      return `${asset.displayName} ${asset.address ?? ""} ${asset.hostname ?? ""} ${asset.tags.join(" ")}`.toLowerCase().includes(needle);
    });
  }, [assets, exposure, kind, query]);
  const knownServiceCounts = assets.filter((asset) => asset.serviceCount !== undefined);
  const serviceCount = knownServiceCounts.reduce((total, asset) => total + (asset.serviceCount ?? 0), 0);
  const observedCount = assets.filter((asset) => asset.lastSeenAt).length;
  const findingCount = (assetId: string) => findings.filter((finding) => finding.assetIds.includes(assetId)).length;
  const withFindings = assets.filter((asset) => findingCount(asset.id) > 0).length;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!engagement) return;
    setSaving(true);
    setError(undefined);
    try {
      await addAsset({
        engagementId: engagement.id,
        name: name.trim(),
        kind: assetKind,
        address: address || undefined,
        hostname: hostname || undefined,
        criticality,
        exposure: assetExposure,
        tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
      });
      setAdding(false);
      setName("");
      setAddress("");
      setHostname("");
      setTags("");
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "Could not add the asset.");
    } finally {
      setSaving(false);
    }
  };

  const activeFilters = Number(kind !== "all") + Number(exposure !== "all");
  return (
    <div className="page assets-page">
      <PageHeader
        eyebrow="Attack surface"
        title="Assets"
        description="Scoped asset records with identity, exposure, criticality, tags, and observation metadata."
        actions={<>
          <button className="button secondary" type="button" disabled title="Scanner inventory normalization is release-gated"><Upload size={16} /> Import inventory</button>
          <button className="button primary" type="button" disabled={previewMode || !engagement} onClick={() => { setError(undefined); setAdding(true); }}><Plus size={16} /> Add asset</button>
        </>}
      />
      <section className="summary-strip" aria-label="Asset totals">
        <div><span className="summary-icon blue"><Network size={18} /></span><span><strong>{assets.length}</strong><small>Loaded assets</small></span></div>
        <div><span className="summary-icon violet"><Server size={18} /></span><span><strong>{knownServiceCounts.length ? serviceCount : "—"}</strong><small>Recorded services</small></span></div>
        <div><span className="summary-icon green"><span className="status-dot healthy" /></span><span><strong>{observedCount}</strong><small>With observation time</small></span></div>
        <div><span className="summary-icon red"><span className="status-dot critical" /></span><span><strong>{withFindings}</strong><small>With findings</small></span></div>
      </section>

      <section className="panel data-panel">
        <header className="data-toolbar">
          <label className="search-field"><Search size={16} /><span className="sr-only">Search assets</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search host, domain, IP, tag…" /></label>
          <div className="toolbar-filters">
            <label><span>Kind</span><select aria-label="Filter assets by kind" value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="all">All kinds</option>{(["host", "domain", "url", "cloud", "repository", "other"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
            <label><span>Exposure</span><select aria-label="Filter assets by exposure" value={exposure} onChange={(event) => setExposure(event.target.value as typeof exposure)}><option value="all">All exposure</option>{(["external", "internal", "unknown"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
            {activeFilters > 0 && <button className="button quiet" type="button" onClick={() => { setKind("all"); setExposure("all"); }}>Clear {activeFilters}</button>}
            <button className="button quiet" type="button" disabled title="Interactive topology is release-gated">Topology</button>
          </div>
        </header>
        <div className="table-scroll">
          <table className="data-table assets-table">
            <thead><tr><th scope="col">Asset</th><th scope="col">Exposure</th><th scope="col">Criticality</th><th scope="col">Services</th><th scope="col">Findings</th><th scope="col">Last observed</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {visibleAssets.map((asset) => { const linkedFindingCount = findingCount(asset.id); return <tr key={asset.id}><td><div className="asset-name"><span><Server size={16} /></span><div><strong>{asset.displayName}</strong><small>{asset.kind} · {engagement?.name ?? "preview engagement"}</small></div></div></td><td><span className={`exposure-badge ${asset.exposure}`}>{asset.exposure}</span></td><td><span className={`severity-label ${asset.criticality}`}><span />{asset.criticality}</span></td><td><strong>{asset.serviceCount ?? "—"}</strong></td><td>{linkedFindingCount > 0 ? <span className="finding-count">{linkedFindingCount}</span> : "—"}</td><td>{displayTime(asset.lastSeenAt)}</td><td><button className="text-link" type="button" onClick={() => setSelected(asset)}>Inspect</button></td></tr>; })}
              {visibleAssets.length === 0 && <tr><td colSpan={7}>{query || activeFilters ? "No assets match the current search and filters." : "No assets have been recorded for this engagement."}</td></tr>}
            </tbody>
          </table>
        </div>
        <footer className="table-footer"><span>Showing {visibleAssets.length} of {assets.length} loaded assets</span></footer>
      </section>

      {adding && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="asset-dialog-title" onSubmit={(event) => void submit(event)}><header><div><small>Engagement asset</small><h2 id="asset-dialog-title">Add asset</h2></div><button className="icon-button subtle" type="button" aria-label="Close asset dialog" onClick={() => setAdding(false)}><X size={17} /></button></header><label>Name<input required autoFocus value={name} onChange={(event) => setName(event.target.value)} /></label><label>Kind<select value={assetKind} onChange={(event) => setAssetKind(event.target.value as AssetSummary["kind"])}>{(["host", "domain", "url", "cloud", "repository", "other"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label><div className="resource-form-grid"><label>Address<input value={address} placeholder="IP, CIDR, or URL" onChange={(event) => setAddress(event.target.value)} /></label><label>Hostname<input value={hostname} onChange={(event) => setHostname(event.target.value)} /></label><label>Criticality<select value={criticality} onChange={(event) => setCriticality(event.target.value as AssetSummary["criticality"])}>{(["critical", "high", "medium", "low", "info"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label><label>Exposure<select value={assetExposure} onChange={(event) => setAssetExposure(event.target.value as AssetSummary["exposure"])}>{(["unknown", "external", "internal"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label></div><label>Tags<input value={tags} placeholder="production, api (comma-separated)" onChange={(event) => setTags(event.target.value)} /></label>{error && <p className="form-error" role="alert">{error}</p>}<footer><button className="button secondary" type="button" onClick={() => setAdding(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || !name.trim()}>{saving ? "Adding…" : "Add asset"}</button></footer></form></div>}

      {selected && <aside className="resource-inspector" role="complementary" aria-labelledby="asset-detail-title"><header><div><small>{selected.kind} · {selected.exposure}</small><h2 id="asset-detail-title">{selected.displayName}</h2></div><button className="icon-button subtle" type="button" aria-label="Close asset details" onClick={() => setSelected(undefined)}><X size={17} /></button></header><dl className="resource-details"><div><dt>Address</dt><dd>{selected.address || "Not recorded"}</dd></div><div><dt>Hostname</dt><dd>{selected.hostname || "Not recorded"}</dd></div><div><dt>Criticality</dt><dd>{selected.criticality}</dd></div><div><dt>Services</dt><dd>{selected.serviceCount ?? "Not recorded"}</dd></div><div><dt>Findings</dt><dd>{findingCount(selected.id)}</dd></div><div><dt>Last observed</dt><dd>{displayTime(selected.lastSeenAt)}</dd></div></dl><div className="scope-chip-list">{selected.tags.length ? selected.tags.map((tag) => <span key={tag}>{tag}</span>) : <span>No tags</span>}</div></aside>}
    </div>
  );
}
