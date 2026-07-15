import { useMemo, useRef, useState, type FormEvent } from "react";
import { LoaderCircle, Sparkles, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { ProviderHealth, WritingTransformResponse } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

interface AIWritingDialogProps {
  api: ApiClient;
  engagementId: string;
  providers: ProviderHealth[];
  purpose: "note" | "report_summary" | "report_section";
  title: string;
  description: string;
  sourceLabel: string;
  sourceText: string;
  initialInstruction: string;
  onApply: (result: WritingTransformResponse) => void;
  onClose: () => void;
}

function providerModel(provider: ProviderHealth | undefined): string {
  return provider?.effectiveDefaultModel
    ?? provider?.defaultModel
    ?? provider?.models[0]
    ?? "";
}

export function AIWritingDialog({
  api,
  engagementId,
  providers,
  purpose,
  title,
  description,
  sourceLabel,
  sourceText,
  initialInstruction,
  onApply,
  onClose,
}: AIWritingDialogProps) {
  const enabledProviders = useMemo(
    () => providers.filter((provider) => provider.enabled && provider.models.length > 0),
    [providers],
  );
  const [providerId, setProviderId] = useState(enabledProviders[0]?.id ?? "");
  const selectedProvider = enabledProviders.find((provider) => provider.id === providerId);
  const [model, setModel] = useState(() => providerModel(enabledProviders[0]));
  const [instruction, setInstruction] = useState(initialInstruction);
  const [result, setResult] = useState<WritingTransformResponse>();
  const [draft, setDraft] = useState("");
  const [cloudConfirmed, setCloudConfirmed] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const isLocal = selectedProvider?.local === true
    || selectedProvider?.kind === "local"
    || selectedProvider?.privacy === "local_only";
  const cloudBlocked = Boolean(selectedProvider && !isLocal && !selectedProvider.permitsSensitiveData);
  const needsCloudConfirmation = Boolean(selectedProvider && !isLocal && selectedProvider.permitsSensitiveData);

  const selectProvider = (nextId: string) => {
    const provider = enabledProviders.find((item) => item.id === nextId);
    setProviderId(nextId);
    setModel(providerModel(provider));
    setCloudConfirmed(false);
    setResult(undefined);
    setDraft("");
  };

  const generate = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedProvider || !model || !instruction.trim() || cloudBlocked) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setGenerating(true);
    setError(undefined);
    try {
      const response = await api.transformWriting({
        engagementId,
        providerId: selectedProvider.id,
        model,
        purpose,
        instruction: instruction.trim(),
        sourceText,
        cloudConfirmed: needsCloudConfirmation && cloudConfirmed,
      }, controller.signal);
      setResult(response);
      setDraft(response.content);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.ai_writing_dialog.transform_failed", "An AI writing request failed.", caughtError, "ai_writing_dialog");
      if (!controller.signal.aborted) {
        setError(caughtError instanceof Error ? caughtError.message : "Could not generate the writing draft.");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = undefined;
      setGenerating(false);
    }
  };

  const close = () => {
    abortRef.current?.abort();
    onClose();
  };

  return <div className="dialog-backdrop"><form className="provider-dialog resource-dialog ai-writing-dialog" role="dialog" aria-modal="true" aria-labelledby="ai-writing-dialog-title" onSubmit={(event) => void generate(event)}>
    <header><div><small>AI-assisted · operator-reviewed</small><h2 id="ai-writing-dialog-title">{title}</h2></div><button className="icon-button subtle" type="button" aria-label="Close AI writing dialog" onClick={close}><X size={17} /></button></header>
    <p className="provider-dialog-note">{description}</p>
    <div className="ai-writing-source"><strong>{sourceLabel}</strong><span>{sourceText.length.toLocaleString()} characters will be used as bounded source data.</span></div>
    {enabledProviders.length ? <div className="ai-writing-runtime">
      <label>Provider<select aria-label="AI writing provider" value={providerId} disabled={generating} onChange={(event) => selectProvider(event.target.value)}>{enabledProviders.map((provider) => <option value={provider.id} key={provider.id}>{provider.name}</option>)}</select></label>
      <label>Model<select aria-label="AI writing model" value={model} disabled={generating || !selectedProvider} onChange={(event) => { setModel(event.target.value); setResult(undefined); setDraft(""); }}>{selectedProvider?.models.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
    </div> : <DiagnosticErrorNotice error="Configure and enable a model provider before using AI writing." fallback="No AI writing provider is available." compact />}
    <label>Tell Nebula how to transform it<textarea aria-label="AI writing instruction" rows={4} maxLength={4000} value={instruction} disabled={generating} onChange={(event) => { setInstruction(event.target.value); setResult(undefined); setDraft(""); }} /></label>
    {cloudBlocked && <DiagnosticErrorNotice error={`${selectedProvider?.name ?? "This provider"} is configured as text-only and cannot receive project notes or report data.`} fallback="This provider cannot receive project data." compact />}
    {needsCloudConfirmation && <label className="ai-writing-confirm"><input type="checkbox" checked={cloudConfirmed} disabled={generating} onChange={(event) => setCloudConfirmed(event.target.checked)} /><span>Allow this request to send the displayed project content to {selectedProvider?.name}. This approval applies only to this transformation.</span></label>}
    {error && <DiagnosticErrorNotice error={error} fallback="Could not generate the writing draft." compact />}
    {result && <label>Editable AI draft<textarea aria-label="AI writing draft" rows={10} value={draft} onChange={(event) => setDraft(event.target.value)} /><small>{result.usage.totalTokens.toLocaleString()} tokens · {result.provenance.model} · output is not saved until you apply and save it</small></label>}
    <footer><button className="button secondary" type="button" onClick={close}>{generating ? "Cancel" : "Close"}</button>{result ? <button className="button primary" type="button" disabled={!draft.trim()} onClick={() => onApply({ ...result, content: draft })}><Sparkles size={15} /> Apply draft</button> : <button className="button primary" type="submit" disabled={generating || !enabledProviders.length || !model || !instruction.trim() || cloudBlocked || (needsCloudConfirmation && !cloudConfirmed)}>{generating ? <><LoaderCircle className="spin" size={15} /> Drafting…</> : <><Sparkles size={15} /> Generate draft</>}</button>}</footer>
  </form></div>;
}
