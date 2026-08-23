import { useEffect, useMemo, useState, type FormEvent } from "react";
import { LoaderCircle, MessageSquare, NotebookPen, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type {
  GeneratedDraft,
  GeneratedDraftContent,
  HarnessProfile,
  OperatorExecution,
  ProviderHealth,
} from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { aiRuntimeLabel, aiRuntimeOptions } from "./aiRuntimes";
import { ModalSurface } from "./DialogSystem";

type InsightAction = "draft" | "chat";

interface ExecutionInsightDialogProps {
  action: InsightAction;
  api: ApiClient;
  execution: OperatorExecution;
  providers: ProviderHealth[];
  harnesses?: HarnessProfile[];
  onClose: () => void;
  onChatAttached: (sessionId: string) => void | Promise<void>;
}

const CATEGORIES = [
  "Engagement and execution identifiers",
  "Runtime language, outcome, and exit code",
  "Up to 32 KiB of bounded redacted source",
  "Up to 64 KiB of bounded redacted interleaved output",
  "Linked execution and evidence identifiers",
];

export function ExecutionInsightDialog({
  action,
  api,
  execution,
  providers,
  harnesses = [],
  onClose,
  onChatAttached,
}: ExecutionInsightDialogProps) {
  const runtimes = useMemo(
    () => aiRuntimeOptions(providers, harnesses, { requireStructuredProvider: action === "draft" }),
    [action, harnesses, providers],
  );
  const [runtimeKey, setRuntimeKey] = useState(runtimes[0]?.key ?? "");
  const runtime = runtimes.find((item) => item.key === runtimeKey);
  const [model, setModel] = useState(runtime?.defaultModel ?? "");
  const [cloudConfirmed, setCloudConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [draft, setDraft] = useState<GeneratedDraft>();
  const [content, setContent] = useState<GeneratedDraftContent>();

  useEffect(() => {
    if (runtime) return;
    const next = runtimes[0];
    setRuntimeKey(next?.key ?? "");
    setModel(next?.defaultModel ?? "");
  }, [runtime, runtimes]);

  const selectRuntime = (key: string) => {
    const selected = runtimes.find((item) => item.key === key);
    setRuntimeKey(key);
    setModel(selected?.defaultModel ?? "");
    setCloudConfirmed(false);
  };

  const cloud = Boolean(runtime && !runtime.local);
  const cloudBlocked = Boolean(runtime && !runtime.local && !runtime.permitsSensitiveData);
  const modelOptions = [...new Set([...(runtime?.models ?? []), ...(model ? [model] : [])])];
  const canSubmit = Boolean(runtime && model.trim() && !cloudBlocked && (!cloud || cloudConfirmed) && !busy);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!runtime || !canSubmit) return;
    setBusy(true);
    setError(undefined);
    try {
      if (action === "chat") {
        const attachment = runtime.kind === "harness"
          ? await api.attachExecutionToChat(
            execution.id,
            undefined,
            model.trim(),
            cloudConfirmed,
            { backendKind: "harness", harnessProfileId: runtime.id },
          )
          : await api.attachExecutionToChat(
            execution.id,
            runtime.id,
            model.trim(),
            cloudConfirmed,
          );
        await onChatAttached(attachment.sessionId);
        onClose();
        return;
      }
      let generated = runtime.kind === "harness"
        ? await api.generateExecutionDraft(
          execution.id,
          "harness",
          model.trim(),
          cloudConfirmed,
          {
            backendKind: "harness",
            harnessProfileId: runtime.id,
            suggestNextSteps: false,
            takeNotes: true,
            cloudConfirmed,
          },
        )
        : await api.generateExecutionDraft(
          execution.id,
          runtime.id,
          model.trim(),
          cloudConfirmed,
        );
      for (let attempt = 0; attempt < 300 && generated.status === "generating"; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        generated = await api.getGeneratedDraft(generated.id);
      }
      setDraft(generated);
      setContent(generated.content);
      if (generated.status !== "ready") {
        throw new Error(generated.errorDetail ?? `Draft ended with status ${generated.status}.`);
      }
    } catch (actionError) {
      void logCaughtDiagnostic("interface.execution_insight_dialog.caught_failure_01", "A handled interface operation failed.", actionError, "execution_insight_dialog");
      setError(actionError instanceof Error ? actionError.message : "The execution AI action failed.");
    } finally {
      setBusy(false);
    }
  };

  const accept = async () => {
    if (!draft || !content) return;
    setBusy(true);
    setError(undefined);
    try {
      const edited = await api.editGeneratedDraft(draft.id, content, draft.revision);
      const accepted = await api.transitionGeneratedDraft(edited.id, "accept", edited.revision);
      setDraft(accepted);
      onClose();
    } catch (acceptError) {
      void logCaughtDiagnostic("interface.execution_insight_dialog.caught_failure_02", "A handled interface operation failed.", acceptError, "execution_insight_dialog");
      setError(acceptError instanceof Error ? acceptError.message : "Could not accept the note.");
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    if (!draft) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.transitionGeneratedDraft(draft.id, "reject", draft.revision);
      onClose();
    } catch (rejectError) {
      void logCaughtDiagnostic("interface.execution_insight_dialog.caught_failure_03", "A handled interface operation failed.", rejectError, "execution_insight_dialog");
      setError(rejectError instanceof Error ? rejectError.message : "Could not reject the note.");
    } finally {
      setBusy(false);
    }
  };

  return (
      <ModalSurface as="form" className="provider-dialog execution-insight-dialog" labelledBy="execution-insight-title" onClose={() => { if (!busy) onClose(); }} onSubmit={(event) => void submit(event)}>
        <header><div><small>Operator-triggered · bounded redacted context</small><h2 id="execution-insight-title">{action === "draft" ? "Draft execution note" : "Discuss execution in chat"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close execution action" disabled={busy} onClick={onClose}><X size={17} /></button></header>
        {!draft?.content ? <>
          <p className="provider-dialog-note">No request is made until you submit this dialog. The selected AI runtime receives only these categories:</p>
          <ul className="execution-context-categories">{CATEGORIES.map((category) => <li key={category}>{category}</li>)}</ul>
          <label>Runtime<select aria-label="Execution AI runtime" value={runtimeKey} disabled={busy} onChange={(event) => selectRuntime(event.target.value)}><option value="">Select runtime</option>{runtimes.map((item) => <option value={item.key} key={item.key}>{aiRuntimeLabel(item)}</option>)}</select></label>
          <label>Model<select required value={model} disabled={busy || !modelOptions.length} onChange={(event) => setModel(event.target.value)}><option value="">{modelOptions.length ? "Select model" : "Check the runtime to discover models"}</option>{modelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
          {action === "draft" && runtimes.length === 0 && <p className="form-error" role="alert">No enabled strict-output provider or Codex harness is available. Configure one before drafting a note.</p>}
          {cloudBlocked && <p className="form-error" role="alert">{runtime?.name} is configured as text-only and cannot receive execution data.</p>}
          {cloud && !cloudBlocked && <label className="provider-consent"><input type="checkbox" checked={cloudConfirmed} onChange={(event) => setCloudConfirmed(event.target.checked)} /><span><strong>Allow this cloud request</strong><small>Send the listed redacted execution categories to {runtime?.name} for this request only. The runtime profile must also permit engagement data.</small></span></label>}
        </> : content && <section className="execution-draft-editor">
          <p className="provider-dialog-note">Review and edit this generated draft. Accepting creates one observation; potential findings remain unverified hypotheses.</p>
          <label>Title<input value={content.title} onChange={(event) => setContent({ ...content, title: event.target.value })} /></label>
          <label>Summary<textarea rows={6} value={content.summary} onChange={(event) => setContent({ ...content, summary: event.target.value })} /></label>
          <label>Observations<textarea rows={5} value={content.observations.join("\n")} onChange={(event) => setContent({ ...content, observations: event.target.value.split("\n").filter(Boolean) })} /><small>One observation per line.</small></label>
          {content.potentialFindings.map((finding, index) => <fieldset key={index}><legend>Unverified hypothesis {index + 1}</legend><label>Title<input value={finding.title} onChange={(event) => setContent({ ...content, potentialFindings: content.potentialFindings.map((item, itemIndex) => itemIndex === index ? { ...item, title: event.target.value } : item) })} /></label><label>Rationale<textarea rows={3} value={finding.rationale} onChange={(event) => setContent({ ...content, potentialFindings: content.potentialFindings.map((item, itemIndex) => itemIndex === index ? { ...item, rationale: event.target.value } : item) })} /></label></fieldset>)}
          <p className="provider-dialog-note">Evidence IDs: {content.evidenceIds.join(", ") || "none"} · Context {draft.contextFingerprint.slice(0, 12)}…</p>
        </section>}
        {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
        <footer>{draft?.content ? <><button className="button danger" type="button" disabled={busy} onClick={() => void reject()}>Reject draft</button><button className="button primary" type="button" disabled={busy || !content?.title.trim()} onClick={() => void accept()}>{busy ? <LoaderCircle className="spin" size={15} /> : <NotebookPen size={15} />} Accept as observation</button></> : <><button className="button secondary" type="button" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={!canSubmit}>{busy ? <LoaderCircle className="spin" size={15} /> : action === "draft" ? <NotebookPen size={15} /> : <MessageSquare size={15} />} {busy ? action === "draft" ? "Generating…" : "Attaching…" : action === "draft" ? "Generate draft" : "Create chat"}</button></>}</footer>
      </ModalSurface>
  );
}
