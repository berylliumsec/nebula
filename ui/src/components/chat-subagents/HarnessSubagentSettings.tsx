import type { ProviderHealth } from "../../api/types";
import { providerModelVerification } from "../../api/providerCapabilities";
import { providerDefaultModel } from "../../api/runtimeDefaults";
import { SubagentLimitField, subagentLimitLabel } from "./SubagentLimitField";

export interface HarnessSubagentChoice {
  enabled: boolean;
  providerId: string;
  model: string;
  /** How many subagents may run at once; absent means no limit. */
  limit?: number;
}

interface HarnessSubagentSettingsProps {
  /** Enabled model providers; only those that may receive project data are offered. */
  providers: ProviderHealth[];
  choice: HarnessSubagentChoice;
  /** The harness the operator is talking to, e.g. "Codex". */
  harnessName: string;
  disabled?: boolean;
  onChange: (choice: HarnessSubagentChoice) => void;
}

/** Providers a harness chat can hand project work to. */
export function subagentProviders(providers: ProviderHealth[]): ProviderHealth[] {
  return providers.filter((provider) => provider.enabled && (provider.local || provider.permitsSensitiveData));
}

function providerModels(provider: ProviderHealth | undefined, current: string): string[] {
  if (!provider) return [];
  const listed = provider.modelAllowlist.length ? provider.modelAllowlist : provider.models;
  return current && !listed.includes(current) ? [current, ...listed] : listed;
}

/** The first usable provider and its default model, preferring a checked one. */
export function defaultSubagentChoice(providers: ProviderHealth[]): Pick<HarnessSubagentChoice, "providerId" | "model"> {
  const eligible = subagentProviders(providers);
  const verified = eligible.find((provider) => {
    const model = providerDefaultModel(provider);
    return model && providerModelVerification(provider, model)?.status === "verified";
  });
  const provider = verified ?? eligible[0];
  return { providerId: provider?.id ?? "", model: provider ? providerDefaultModel(provider) : "" };
}

/**
 * Assistant settings for a harness chat: let the harness delegate independent
 * tasks to one provider model. Turning it on is the operator's consent to send
 * those tasks' tool outputs to that provider.
 */
export function HarnessSubagentSettings({ providers, choice, harnessName, disabled = false, onChange }: HarnessSubagentSettingsProps) {
  const eligible = subagentProviders(providers);
  const provider = eligible.find((item) => item.id === choice.providerId);
  const models = providerModels(provider, choice.model);
  const verification = provider && choice.model ? providerModelVerification(provider, choice.model) : undefined;
  const status = !choice.enabled
    ? undefined
    : !eligible.length
      ? { tone: "error", text: "No enabled provider may receive project data. Add or allow one in Model providers." }
      : !provider || !choice.model
        ? { tone: "error", text: "Choose the provider model subagents run on." }
        : verification?.status === "verified"
          ? { tone: "ok", text: provider.local ? "Tools verified · Subagent tool outputs stay on this machine." : `Tools verified · Subagent tool outputs are sent to ${provider.name}.` }
          : verification?.status === "failed"
            ? { tone: "error", text: `${choice.model} failed the tool check, so it cannot run subagents. Pick another model.` }
            : { tone: "pending", text: "Checking tool support for this model…" };

  return <div className="chat-harness-mcp chat-harness-subagents" data-guide="subagents">
    <span>Subagents</span>
    <small>{harnessName} can hand independent tasks to a provider model and wait for the reports.</small>
    <label className="chat-knowledge-toggle">
      <input
        type="checkbox"
        checked={choice.enabled}
        disabled={disabled}
        onChange={(event) => {
          const fallback = choice.providerId ? {} : defaultSubagentChoice(providers);
          onChange({ ...choice, ...fallback, enabled: event.target.checked });
        }}
      />
      <span><strong>Provider subagents</strong><small>Delegate to the model below · {subagentLimitLabel(choice.limit)}</small></span>
    </label>
    {choice.enabled && <div className="chat-settings-fields">
      <label>
        <span>Subagent provider</span>
        <select
          aria-label="Subagent provider"
          value={choice.providerId}
          disabled={disabled || !eligible.length}
          onChange={(event) => {
            const next = eligible.find((item) => item.id === event.target.value);
            onChange({ ...choice, providerId: event.target.value, model: next ? providerDefaultModel(next) : "" });
          }}
        >
          {!provider && <option value="">Choose a provider</option>}
          {eligible.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <label>
        <span>Subagent model</span>
        <select
          aria-label="Subagent model"
          value={choice.model}
          disabled={disabled || !provider}
          onChange={(event) => onChange({ ...choice, model: event.target.value })}
        >
          {!choice.model && <option value="">Choose a model</option>}
          {models.map((model) => <option key={model} value={model}>{model}</option>)}
        </select>
      </label>
    </div>}
    {choice.enabled && <SubagentLimitField
      limit={choice.limit}
      delegator={harnessName}
      disabled={disabled}
      onChange={(limit) => onChange({ ...choice, limit })}
    />}
    {status && <small className="chat-harness-subagent-status" data-tone={status.tone} role={status.tone === "error" ? "alert" : "status"}>
      {status.tone === "ok" && <span aria-hidden="true">✓ </span>}{status.text}
    </small>}
  </div>;
}
