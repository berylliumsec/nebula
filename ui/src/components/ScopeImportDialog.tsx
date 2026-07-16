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
  ProviderHealth,
  ScopeImport,
} from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

const MAX_SOURCE_BYTES = 20 * 1024 * 1024;
const ACCEPTED =
  ".txt,.md,.markdown,.rst,.log,.csv,.json,.jsonl,.ndjson,.html,.htm,.pdf,.docx,.xlsx";

interface ScopeImportDialogProps {
  api: ApiClient;
  engagementId: string;
  scope: EngagementScopePolicy;
  providers: ProviderHealth[];
  onApplied: (scope: EngagementScopePolicy) => void;
  onClose: () => void;
}

function providerModel(provider: ProviderHealth | undefined): string {
  return (
    provider?.effectiveDefaultModel ??
    provider?.defaultModel ??
    provider?.models[0] ??
    ""
  );
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
  onApplied,
  onClose,
}: ScopeImportDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const availableProviders = useMemo(
    () =>
      providers.filter(
        (provider) =>
          provider.enabled &&
          provider.models.length > 0 &&
          provider.capabilities.some((capability) =>
            capability.toLowerCase().includes("strict structured"),
          ),
      ),
    [providers],
  );
  const [providerId, setProviderId] = useState(availableProviders[0]?.id ?? "");
  const selectedProvider = availableProviders.find(
    (provider) => provider.id === providerId,
  );
  const [model, setModel] = useState(() =>
    providerModel(availableProviders[0]),
  );
  const [file, setFile] = useState<File>();
  const [cloudConfirmed, setCloudConfirmed] = useState(false);
  const [result, setResult] = useState<ScopeImport>();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState<"extract" | "apply">();
  const [error, setError] = useState<string>();
  const isLocal =
    selectedProvider?.local === true ||
    selectedProvider?.kind === "local" ||
    selectedProvider?.privacy === "local_only";
  const cloudBlocked = Boolean(
    selectedProvider && !isLocal && !selectedProvider.permitsSensitiveData,
  );
  const needsCloudConfirmation = Boolean(
    selectedProvider && !isLocal && selectedProvider.permitsSensitiveData,
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

  const selectProvider = (nextId: string) => {
    const next = availableProviders.find((provider) => provider.id === nextId);
    setProviderId(nextId);
    setModel(providerModel(next));
    setCloudConfirmed(false);
    setResult(undefined);
    setSelectedIds(new Set());
  };

  const extract = async (event: FormEvent) => {
    event.preventDefault();
    if (!file || !selectedProvider || !model || cloudBlocked) return;
    setBusy("extract");
    setError(undefined);
    try {
      const created = await api.createScopeImport({
        engagementId,
        providerId: selectedProvider.id,
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
    <div className="dialog-backdrop">
      <form
        className="provider-dialog resource-dialog scope-import-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="scope-import-title"
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
            {availableProviders.length ? (
              <div className="ai-writing-runtime">
                <label>
                  Provider
                  <select
                    aria-label="Scope import provider"
                    value={providerId}
                    disabled={Boolean(busy)}
                    onChange={(event) => selectProvider(event.target.value)}
                  >
                    {availableProviders.map((provider) => (
                      <option value={provider.id} key={provider.id}>
                        {provider.name}
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
                    {selectedProvider?.models.map((item) => (
                      <option value={item} key={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            ) : (
              <DiagnosticErrorNotice
                error="No enabled provider declares strict structured output."
                fallback="Configure a structured-output provider before importing scope."
                compact
              />
            )}
            {cloudBlocked && (
              <DiagnosticErrorNotice
                error={`${selectedProvider?.name ?? "This provider"} cannot receive project data.`}
                fallback="This provider cannot receive the document."
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
                  {selectedProvider?.name}. This approval applies only to this
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
                !selectedProvider ||
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
      </form>
    </div>
  );
}
