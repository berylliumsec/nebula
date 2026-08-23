import {
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from "react";
import { FileUp, LoaderCircle, ShieldCheck, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type {
  EngagementScopePolicy,
  HarnessProfile,
  ProviderHealth,
  ScopeImport,
} from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { aiRuntimeLabel, aiRuntimeOptions } from "./aiRuntimes";
import { ModalSurface } from "./DialogSystem";

const MAX_SOURCE_BYTES = 20 * 1024 * 1024;
const ACCEPTED =
  ".txt,.md,.markdown,.rst,.log,.csv,.json,.jsonl,.ndjson,.html,.htm,.pdf,.docx,.xlsx";

interface ScopeImportDialogProps {
  api: ApiClient;
  engagementId: string;
  scope: EngagementScopePolicy;
  providers: ProviderHealth[];
  harnesses?: HarnessProfile[];
  onApplied: (scope: EngagementScopePolicy) => void;
  onClose: () => void;
}

function encodeBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

export function ScopeImportDialog({
  api,
  engagementId,
  scope,
  providers,
  harnesses = [],
  onApplied,
  onClose,
}: ScopeImportDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const runtimes = useMemo(
    () => aiRuntimeOptions(providers, harnesses, { requireStructuredProvider: true }),
    [harnesses, providers],
  );
  const [runtimeKey, setRuntimeKey] = useState(runtimes[0]?.key ?? "");
  const selectedRuntime = runtimes.find(
    (runtime) => runtime.key === runtimeKey,
  );
  const [model, setModel] = useState(() => runtimes[0]?.defaultModel ?? "");
  const [file, setFile] = useState<File>();
  const [cloudConfirmed, setCloudConfirmed] = useState(false);
  const [result, setResult] = useState<ScopeImport>();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState<"extract" | "apply">();
  const [error, setError] = useState<string>();
  const cloudBlocked = Boolean(
    selectedRuntime && !selectedRuntime.local && !selectedRuntime.permitsSensitiveData,
  );
  const needsCloudConfirmation = Boolean(
    selectedRuntime && !selectedRuntime.local && selectedRuntime.permitsSensitiveData,
  );

  const chooseFile = (event: ChangeEvent<HTMLInputElement>) => {
    const next = event.target.files?.[0];
    event.target.value = "";
    if (!next) return;
    if (next.size > MAX_SOURCE_BYTES) {
      setError(`${next.name} is larger than the 20 MB scope-import limit.`);
      return;
    }
    setFile(next);
    setResult(undefined);
    setSelectedIds(new Set());
    setError(undefined);
  };

  const selectRuntime = (nextKey: string) => {
    const next = runtimes.find((runtime) => runtime.key === nextKey);
    setRuntimeKey(nextKey);
    setModel(next?.defaultModel ?? "");
    setCloudConfirmed(false);
    setResult(undefined);
    setSelectedIds(new Set());
  };

  const extract = async (event: FormEvent) => {
    event.preventDefault();
    if (!file || !selectedRuntime || !model || cloudBlocked) return;
    setBusy("extract");
    setError(undefined);
    try {
      const created = await api.createScopeImport({
        engagementId,
        backendKind: selectedRuntime.kind,
        providerId: selectedRuntime.kind === "provider" ? selectedRuntime.id : undefined,
        harnessProfileId: selectedRuntime.kind === "harness" ? selectedRuntime.id : undefined,
        model,
        filename: file.name,
        mediaType: file.type || undefined,
        contentBase64: encodeBase64(await file.arrayBuffer()),
        cloudConfirmed: needsCloudConfirmation && cloudConfirmed,
      });
      setResult(created);
      setSelectedIds(
        new Set(
          created.candidates
            .filter(
              (candidate) =>
                candidate.classification === "allowed" &&
                candidate.normalizedValue,
            )
            .map((candidate) => candidate.id),
        ),
      );
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.scope_import.extract_failed",
        "A scope import failed.",
        caughtError,
        "scope_import_dialog",
      );
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "Could not analyze the scope document.",
      );
    } finally {
      setBusy(undefined);
    }
  };

  const apply = async () => {
    if (!result) return;
    setBusy("apply");
    setError(undefined);
    try {
      const applied = await api.applyScopeImport(
        engagementId,
        result.id,
        [...selectedIds],
        scope.revision,
      );
      onApplied(applied.scope);
      onClose();
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.scope_import.apply_failed",
        "A scope import could not be applied.",
        caughtError,
        "scope_import_dialog",
      );
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "Could not apply the reviewed targets.",
      );
    } finally {
      setBusy(undefined);
    }
  };

  const close = () => {
    if (busy) return;
    if (result?.status === "ready")
      void api
        .discardScopeImport(engagementId, result.id)
        .catch((caughtError) => {
          void logCaughtDiagnostic(
            "interface.scope_import.discard_failed",
            "A scope import draft could not be discarded.",
            caughtError,
            "scope_import_dialog",
          );
        });
    onClose();
  };

  return (
      <ModalSurface
        as="form"
        className="provider-dialog resource-dialog scope-import-dialog"
        labelledBy="scope-import-title"
        onClose={close}
        onSubmit={(event) => void extract(event)}
      >
        <header>
          <div>
            <small>AI-assisted · explicit review</small>
            <h2 id="scope-import-title">Import scope targets</h2>
          </div>
          <button
            className="icon-button subtle"
            type="button"
            aria-label="Close scope import"
            onClick={close}
          >
            <X size={17} />
          </button>
        </header>
        <p className="provider-dialog-note">
          Nebula extracts targets as untrusted proposals. Nothing becomes
          authorized until you review and apply selected entries.
        </p>
        {!result && (
          <>
            <input
              ref={inputRef}
              className="sr-only"
              type="file"
              accept={ACCEPTED}
              aria-label="Choose scope document"
              onChange={chooseFile}
            />
            <button
              className="scope-import-file"
              type="button"
              disabled={Boolean(busy)}
              onClick={() => inputRef.current?.click()}
            >
              <FileUp size={20} />
              <span>
                <strong>{file?.name ?? "Choose a scope document"}</strong>
                <small>
                  PDF, DOCX, XLSX, CSV, text, HTML, or JSON · up to 20 MB
                </small>
              </span>
            </button>
            {runtimes.length ? (
              <div className="ai-writing-runtime">
                <label>
                  Runtime
                  <select
                    aria-label="Scope import runtime"
                    value={runtimeKey}
                    disabled={Boolean(busy)}
                    onChange={(event) => selectRuntime(event.target.value)}
                  >
                    {runtimes.map((runtime) => (
                      <option value={runtime.key} key={runtime.key}>
                        {aiRuntimeLabel(runtime)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Model
                  <select
                    aria-label="Scope import model"
                    value={model}
                    disabled={Boolean(busy)}
                    onChange={(event) => setModel(event.target.value)}
                  >
                    {selectedRuntime?.models.map((item) => (
                      <option value={item} key={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            ) : (
              <DiagnosticErrorNotice
                error="No enabled strict-output provider or Codex harness is available."
                fallback="Configure an AI runtime before importing scope."
                compact
              />
            )}
            {cloudBlocked && (
              <DiagnosticErrorNotice
                error={`${selectedRuntime?.name ?? "This runtime"} cannot receive project data.`}
                fallback="This runtime cannot receive the document."
                compact
              />
            )}
            {needsCloudConfirmation && (
              <label className="ai-writing-confirm">
                <input
                  type="checkbox"
                  checked={cloudConfirmed}
                  disabled={Boolean(busy)}
                  onChange={(event) => setCloudConfirmed(event.target.checked)}
                />
                <span>
                  Allow this request to send the selected scope document text to{" "}
                  {selectedRuntime?.name}. This approval applies only to this
                  import.
                </span>
              </label>
            )}
          </>
        )}
        {result && (
          <div className="scope-import-review">
            <div className="ai-writing-source">
              <strong>{result.filename}</strong>
              <span>
                {result.candidates.length} unique proposals ·{" "}
                {result.usage.totalTokens.toLocaleString()} tokens · base scope
                revision {result.baseScopeRevision}
              </span>
            </div>
            {result.warnings.length > 0 && (
              <details>
                <summary>
                  {result.warnings.length} import warning
                  {result.warnings.length === 1 ? "" : "s"}
                </summary>
                <ul>
                  {result.warnings.map((warning, index) => (
                    <li key={`${warning}-${index}`}>{warning}</li>
                  ))}
                </ul>
              </details>
            )}
            <div
              className="scope-import-candidates"
              role="group"
              aria-label="Proposed scope targets"
            >
              {result.candidates.map((candidate) => {
                const selectable =
                  candidate.classification === "allowed" &&
                  Boolean(candidate.normalizedValue);
                return (
                  <label
                    className={`scope-import-candidate ${candidate.classification}`}
                    key={candidate.id}
                  >
                    <input
                      type="checkbox"
                      disabled={!selectable || busy === "apply"}
                      checked={selectable && selectedIds.has(candidate.id)}
                      onChange={(event) =>
                        setSelectedIds((current) => {
                          const next = new Set(current);
                          if (event.target.checked) next.add(candidate.id);
                          else next.delete(candidate.id);
                          return next;
                        })
                      }
                    />
                    <span>
                      <strong>
                        {candidate.normalizedValue ?? candidate.rawValue}
                      </strong>
                      <small>
                        {candidate.targetType.toUpperCase()} ·{" "}
                        {candidate.classification} · {candidate.sourceLocation}
                      </small>
                      {candidate.sourceExcerpt && (
                        <em>{candidate.sourceExcerpt}</em>
                      )}
                      {candidate.warnings.map((warning) => (
                        <em key={warning}>{warning}</em>
                      ))}
                    </span>
                  </label>
                );
              })}
              {result.candidates.length === 0 && (
                <p>No scope targets were found in this document.</p>
              )}
            </div>
          </div>
        )}
        {error && (
          <DiagnosticErrorNotice
            error={error}
            fallback="The scope import could not be completed."
            compact
          />
        )}
        <footer>
          <button
            className="button secondary"
            type="button"
            disabled={Boolean(busy)}
            onClick={close}
          >
            {result ? "Discard" : "Cancel"}
          </button>
          {result ? (
            <button
              className="button primary"
              type="button"
              disabled={busy === "apply" || selectedIds.size === 0}
              onClick={() => void apply()}
            >
              {busy === "apply" ? (
                <>
                  <LoaderCircle className="spin" size={15} /> Applying…
                </>
              ) : (
                <>
                  <ShieldCheck size={15} /> Apply {selectedIds.size} target
                  {selectedIds.size === 1 ? "" : "s"}
                </>
              )}
            </button>
          ) : (
            <button
              className="button primary"
              type="submit"
              disabled={
                Boolean(busy) ||
                !file ||
                !selectedRuntime ||
                !model ||
                cloudBlocked ||
                (needsCloudConfirmation && !cloudConfirmed)
              }
            >
              {busy === "extract" ? (
                <>
                  <LoaderCircle className="spin" size={15} /> Analyzing…
                </>
              ) : (
                <>
                  <FileUp size={15} /> Analyze document
                </>
              )}
            </button>
          )}
        </footer>
      </ModalSurface>
  );
}
