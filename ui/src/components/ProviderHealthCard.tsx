import { useState } from "react";
import { Activity, Cloud, Cpu, KeyRound, Laptop, Pencil, Power, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { providerVerificationModel } from "../api/providerCapabilities";
import type { ProviderHealth } from "../api/types";

interface ProviderHealthCardProps {
  provider: ProviderHealth;
  preview?: boolean;
  busy?: boolean;
  onRefresh?: (id: string) => Promise<void>;
  onReverify?: (id: string) => Promise<void>;
  onEdit?: (provider: ProviderHealth) => void;
  onToggle?: (provider: ProviderHealth) => Promise<void>;
  onDelete?: (provider: ProviderHealth) => Promise<void>;
}

export function ProviderHealthCard({ provider, preview = false, busy = false, onRefresh, onReverify, onEdit, onToggle, onDelete }: ProviderHealthCardProps) {
  const [refreshing, setRefreshing] = useState(false);
  const KindIcon = provider.kind === "local" ? Laptop : provider.kind === "gateway" ? Cpu : Cloud;
  const verificationModel = providerVerificationModel(provider);
  const verification = verificationModel ? provider.capabilityVerifications?.[verificationModel] : undefined;
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
          <h3 title={provider.name}>{provider.name}</h3>
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
      <p className="provider-message">
        {verification?.status === "verified"
          ? `Tool calling verified for ${verification.model} · ${new Date(verification.checkedAt).toLocaleString()}`
          : verification?.failureDetail
            ? `Tool verification failed for ${verification.model}: ${verification.failureDetail}`
            : verificationModel
              ? `Tool calling is unverified for ${verificationModel}.`
              : "Configure a model to verify tool calling."}
      </p>
      {provider.message && <p className="provider-message">{provider.message}</p>}
      <footer>
        <span>
          {provider.state === "unconfigured" ? <KeyRound size={14} /> : <Activity size={14} />}
          {provider.state === "unconfigured"
            ? "Configuration required"
            : preview
              ? "Preview profile"
              : "Profile registered"}
        </span>
        <div className="provider-card-actions">
          <button className="icon-button subtle" type="button" title={verificationModel ? `Verify tool calling for ${verificationModel}` : "Discover or configure a model before verification"} aria-label={`Reverify ${provider.name} tool calling`} disabled={!onReverify || preview || busy || !provider.enabled || !verificationModel} onClick={() => void onReverify?.(provider.id)}><ShieldCheck size={14} aria-hidden="true" /></button>
          <button className="icon-button subtle" type="button" aria-label={`Edit ${provider.name}`} disabled={!onEdit || preview || busy} onClick={() => onEdit?.(provider)}><Pencil size={14} aria-hidden="true" /></button>
          <button className="icon-button subtle" type="button" aria-label={`${provider.enabled ? "Disable" : "Enable"} ${provider.name}`} disabled={!onToggle || preview || busy} onClick={() => void onToggle?.(provider)}><Power size={14} aria-hidden="true" /></button>
          <button className="icon-button subtle" type="button" aria-label={`Delete ${provider.name}`} disabled={!onDelete || preview || busy} onClick={() => void onDelete?.(provider)}><Trash2 size={14} aria-hidden="true" /></button>
          <button
            className="icon-button subtle"
            type="button"
            aria-label={`Refresh ${provider.name} health`}
            aria-busy={refreshing}
            disabled={!onRefresh || preview || refreshing || busy || !provider.enabled}
            onClick={() => void refresh()}
          >
            <RefreshCw size={15} aria-hidden="true" />
          </button>
        </div>
      </footer>
    </article>
  );
}
