import { useState } from "react";
import { Activity, Cloud, Cpu, KeyRound, Laptop, RefreshCw } from "lucide-react";
import type { ProviderHealth } from "../api/types";

interface ProviderHealthCardProps {
  provider: ProviderHealth;
  preview?: boolean;
  onRefresh?: (id: string) => Promise<void>;
}

export function ProviderHealthCard({ provider, preview = false, onRefresh }: ProviderHealthCardProps) {
  const [refreshing, setRefreshing] = useState(false);
  const KindIcon = provider.kind === "local" ? Laptop : provider.kind === "gateway" ? Cpu : Cloud;
  const refresh = async () => {
    if (!onRefresh || refreshing) return;
    setRefreshing(true);
    try {
      await onRefresh(provider.id);
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <article className="provider-card">
      <div className="provider-heading">
        <span className="provider-icon">
          <KindIcon size={19} aria-hidden="true" />
        </span>
        <div>
          <h3>{provider.name}</h3>
          <p>{provider.kind === "local" ? "Local inference" : provider.kind}</p>
        </div>
        <span className={`health-label ${provider.state}`}>
          <span aria-hidden="true" /> {provider.state}
        </span>
      </div>
      <div className="provider-metrics">
        <span>
          <strong>{provider.modelCount}</strong>
          <small>models</small>
        </span>
        <span>
          <strong>{provider.latencyMs ? `${provider.latencyMs} ms` : "—"}</strong>
          <small>health latency</small>
        </span>
        <span>
          <strong>{provider.privacy.replace("_", " ")}</strong>
          <small>data boundary</small>
        </span>
      </div>
      <div className="capability-list" aria-label={`${provider.name} capabilities`}>
        {provider.capabilities.map((capability) => <span key={capability}>{capability}</span>)}
      </div>
      {provider.message && <p className="provider-message">{provider.message}</p>}
      <footer>
        <span>
          {provider.state === "unconfigured" ? <KeyRound size={14} /> : <Activity size={14} />}
          {provider.state === "unconfigured"
            ? "Credentials required"
            : preview
              ? "Preview profile"
              : "Profile registered"}
        </span>
        <button
          className="icon-button subtle"
          type="button"
          aria-label={`Refresh ${provider.name} health`}
          aria-busy={refreshing}
          disabled={!onRefresh || preview || refreshing}
          onClick={() => void refresh()}
        >
          <RefreshCw size={15} aria-hidden="true" />
        </button>
      </footer>
    </article>
  );
}
