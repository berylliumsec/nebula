import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { LoaderCircle, Sparkles, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, ProviderHealth, WritingTransformResponse } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { aiRuntimeLabel, aiRuntimeOptions } from "./aiRuntimes";

interface AIWritingDialogProps {
  api: ApiClient;
  engagementId: string;
  providers: ProviderHealth[];
  harnesses?: HarnessProfile[];
  purpose: "note" | "report_summary" | "report_section";
  title: string;
  description: string;
  sourceLabel: string;
  sourceText: string;
  initialInstruction: string;
  onApply: (result: WritingTransformResponse) => void;
  onClose: () => void;
}

export function AIWritingDialog({
  api,
  engagementId,
  providers,
  harnesses = [],
  purpose,
  title,
  description,
  sourceLabel,
  sourceText,
  initialInstruction,
  onApply,
  onClose,
}: AIWritingDialogProps) {
  const runtimes = useMemo(
    () => aiRuntimeOptions(providers, harnesses),
    [harnesses, providers],
  );
  const [runtimeKey, setRuntimeKey] = useState(runtimes[0]?.key ?? "");
  const selectedRuntime = runtimes.find((runtime) => runtime.key === runtimeKey);
  const [model, setModel] = useState(() => runtimes[0]?.defaultModel ?? "");
  const [instruction, setInstruction] = useState(initialInstruction);
  const [result, setResult] = useState<WritingTransformResponse>();
  const [draft, setDraft] = useState("");
  const [cloudConfirmed, setCloudConfirmed] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const cloudBlocked = Boolean(selectedRuntime && !selectedRuntime.local && !selectedRuntime.permitsSensitiveData);
  const needsCloudConfirmation = Boolean(selectedRuntime && !selectedRuntime.local && selectedRuntime.permitsSensitiveData);

  useEffect(() => {
    if (selectedRuntime) return;
    const next = runtimes[0];
    setRuntimeKey(next?.key ?? "");
    setModel(next?.defaultModel ?? "");
  }, [runtimes, selectedRuntime]);

  const selectRuntime = (nextKey: string) => {
    const runtime = runtimes.find((item) => item.key === nextKey);
    setRuntimeKey(nextKey);
    setModel(runtime?.defaultModel ?? "");
    setCloudConfirmed(false);
    setResult(undefined);
    setDraft("");
  };

  const generate = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedRuntime || !model || !instruction.trim() || cloudBlocked) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setGenerating(true);
    setError(undefined);
    try {
      const response = await api.transformWriting({
        engagementId,
        backendKind: selectedRuntime.kind,
        providerId: selectedRuntime.kind === "provider" ? selectedRuntime.id : undefined,
        harnessProfileId: selectedRuntime.kind === "harness" ? selectedRuntime.id : undefined,
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
    {runtimes.length ? <div className="ai-writing-runtime">
      <label>Runtime<select aria-label="AI writing runtime" value={runtimeKey} disabled={generating} onChange={(event) => selectRuntime(event.target.value)}>{runtimes.map((runtime) => <option value={runtime.key} key={runtime.key}>{aiRuntimeLabel(runtime)}</option>)}</select></label>
      <label>Model<select aria-label="AI writing model" value={model} disabled={generating || !selectedRuntime} onChange={(event) => { setModel(event.target.value); setResult(undefined); setDraft(""); }}>{selectedRuntime?.models.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
    </div> : <DiagnosticErrorNotice error="Configure and enable a model provider or Codex harness before using AI writing." fallback="No AI writing runtime is available." compact />}
    <label>Tell Nebula how to transform it<textarea aria-label="AI writing instruction" rows={4} maxLength={4000} value={instruction} disabled={generating} onChange={(event) => { setInstruction(event.target.value); setResult(undefined); setDraft(""); }} /></label>
    {cloudBlocked && <DiagnosticErrorNotice error={`${selectedRuntime?.name ?? "This runtime"} is configured as text-only and cannot receive project notes or report data.`} fallback="This runtime cannot receive project data." compact />}
    {needsCloudConfirmation && <label className="ai-writing-confirm"><input type="checkbox" checked={cloudConfirmed} disabled={generating} onChange={(event) => setCloudConfirmed(event.target.checked)} /><span>Allow this request to send the displayed project content to {selectedRuntime?.name}. This approval applies only to this transformation.</span></label>}
    {error && <DiagnosticErrorNotice error={error} fallback="Could not generate the writing draft." compact />}
    {result && <label>Editable AI draft<textarea aria-label="AI writing draft" rows={10} value={draft} onChange={(event) => setDraft(event.target.value)} /><small>{result.usage.totalTokens.toLocaleString()} tokens · {result.provenance.model} · output is not saved until you apply and save it</small></label>}
    <footer><button className="button secondary" type="button" onClick={close}>{generating ? "Cancel" : "Close"}</button>{result ? <button className="button primary" type="button" disabled={!draft.trim()} onClick={() => onApply({ ...result, content: draft })}><Sparkles size={15} /> Apply draft</button> : <button className="button primary" type="submit" disabled={generating || !runtimes.length || !model || !instruction.trim() || cloudBlocked || (needsCloudConfirmation && !cloudConfirmed)}>{generating ? <><LoaderCircle className="spin" size={15} /> Drafting…</> : <><Sparkles size={15} /> Generate draft</>}</button>}</footer>
  </form></div>;
}
