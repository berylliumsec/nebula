import { useEffect, useMemo, useRef, useState } from "react";
import { Play, RefreshCw, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type {
  ExecutionCapabilities,
  ExecutionNetworkRequest,
  ExecutionPreflight,
  ExecutionRequest,
  OperatorExecution,
} from "../api/types";
import { ModalSurface } from "./DialogSystem";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { visibleSource } from "./assistantCode";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

interface ExecutionReviewDialogProps {
  api: ApiClient;
  engagementId: string;
  candidate: FencedRunCandidate;
  capabilities?: ExecutionCapabilities;
  onClose: () => void;
  onStarted: (execution: OperatorExecution) => void;
}

function parsePorts(value: string): number[] | undefined {
  const fields = value.split(",").map((item) => item.trim()).filter(Boolean);
  if (!fields.length) return undefined;
  const ports = fields.map(Number);
  if (ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65_535)) return undefined;
  return [...new Set(ports)].sort((left, right) => left - right);
}

export function ExecutionReviewDialog({
  api,
  engagementId,
  candidate,
  capabilities,
  onClose,
  onStarted,
}: ExecutionReviewDialogProps) {
  const [mode, setMode] = useState<ExecutionNetworkRequest["mode"]>("none");
  const [target, setTarget] = useState("");
  const [portText, setPortText] = useState("443");
  const [preview, setPreview] = useState<ExecutionPreflight>();
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string>();
  const idempotencyKey = useRef(globalThis.crypto.randomUUID());
  const runtimeCapability = capabilities?.runtimes.find((item) => item.language === candidate.language);
  const ports = parsePorts(portText);
  const network: ExecutionNetworkRequest = mode === "none"
    ? { mode: "none", ports: [] }
    : { mode: "scoped", target: target.trim() || undefined, ports: ports ?? [] };
  const request = useMemo<ExecutionRequest>(() => ({
    engagementId,
    language: candidate.declaredLanguage,
    source: candidate.source,
    origin: candidate.origin,
    network,
  }), [candidate, engagementId, mode, network.target, portText]);

  const review = async (signal?: AbortSignal) => {
    if (mode === "scoped" && (!target.trim() || !ports?.length)) {
      setError("Scoped network requires one explicit target and at least one valid port.");
      return;
    }
    setLoading(true);
    setError(undefined);
    setPreview(undefined);
    try {
      const result = await api.preflightExecution(request, signal);
      setPreview(result);
      if (!result.allowed) setError(result.detail);
    } catch (reviewError) {
      void logCaughtDiagnostic("interface.execution_review_dialog.caught_failure_01", "A handled interface operation failed.", reviewError, "execution_review_dialog");
      if (!signal?.aborted) setError(reviewError instanceof Error ? reviewError.message : "Execution review failed.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    void review(controller.signal);
    return () => controller.abort();
    // A newly mounted dialog always begins with an offline review.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate, engagementId]);

  useEffect(() => {
    setPreview(undefined);
    setError(undefined);
  }, [mode, target, portText]);

  const start = async () => {
    if (!preview?.allowed || !preview.previewToken || !preview.previewFingerprint || starting) return;
    setStarting(true);
    setError(undefined);
    try {
      const execution = await api.startExecution(request, preview, idempotencyKey.current);
      onStarted(execution);
      onClose();
    } catch (startError) {
      void logCaughtDiagnostic("interface.execution_review_dialog.caught_failure_02", "A handled interface operation failed.", startError, "execution_review_dialog");
      setError(startError instanceof Error ? startError.message : "Execution could not be started.");
      setPreview(undefined);
    } finally {
      setStarting(false);
    }
  };

  return (
    <ModalSurface labelledBy="execution-review-title" className="execution-review-dialog" onClose={onClose}>
      <header>
        <div>
          <span className="eyebrow">Mandatory review</span>
          <h2 id="execution-review-title">Review exact code execution</h2>
          <p>A fresh disposable container will receive this source with no interactive stdin.</p>
        </div>
        <button className="icon-button subtle" type="button" aria-label="Close execution review" onClick={onClose}><X size={17} /></button>
      </header>
      <div className="execution-review-body">
        <section>
          <h3>Exact source</h3>
          <pre className="execution-source-review">{visibleSource(candidate.source)}</pre>
        </section>
        <section className="execution-network-review">
          <h3>Network</h3>
          <div className="segmented-control" role="radiogroup" aria-label="Execution network mode">
            <label><input type="radio" name="execution-network" checked={mode === "none"} disabled={runtimeCapability?.offline === false} onChange={() => setMode("none")} /> Offline</label>
            <label><input type="radio" name="execution-network" checked={mode === "scoped"} disabled={runtimeCapability?.scopedNetwork !== true} onChange={() => setMode("scoped")} /> One scoped target</label>
          </div>
          {mode === "scoped" && <div className="execution-network-fields"><label>Approved target<input value={target} placeholder="host.example or 192.0.2.10" onChange={(event) => setTarget(event.target.value)} /></label><label>Ports<input value={portText} placeholder="443, 8443" onChange={(event) => setPortText(event.target.value)} /></label></div>}
        </section>
        {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
        {preview?.allowed && preview.runtime && preview.network && (
          <section className="execution-preview-facts" aria-label="Validated execution preview">

            <dl>
              <div><dt>Interpreter</dt><dd><code>{[preview.runtime.interpreter, ...preview.runtime.arguments].join(" ")}</code></dd></div>
              <div><dt>Container</dt><dd>Fresh and disposable · stdin closed</dd></div>
              <div><dt>Image</dt><dd><code>{preview.runtime.image}</code></dd></div>
              <div><dt>Runtime digest</dt><dd><code>{preview.runtime.runtimeDigest}</code></dd></div>
              <div><dt>Runner</dt><dd>{preview.runtime.runnerRuntime} · {preview.runtime.runnerIsolation} · {preview.runtime.runnerProfileId} r{preview.runtime.runnerProfileRevision}<br /><code>{preview.runtime.runnerExecutable}</code> · {preview.runtime.runnerPlatform}{preview.runtime.runnerContext ? ` · context ${preview.runtime.runnerContext}` : ""}{preview.runtime.runnerSocket ? ` · ${preview.runtime.runnerSocket}` : ""}</dd></div>
              <div><dt>Workspace</dt><dd><code>{preview.workspace}</code> · engagement-persistent</dd></div>
              <div><dt>Limits</dt><dd>{preview.limits.cpuCount} CPU · {preview.limits.memoryMb} MiB · {preview.limits.pids} PIDs · {preview.limits.timeoutSeconds}s · {preview.limits.outputBytesPerStream.toLocaleString()} bytes/stream</dd></div>
              <div><dt>Network</dt><dd>{preview.network.mode === "none" ? "Offline" : `${preview.network.target} · ports ${preview.network.ports.join(", ")} · ${preview.network.resolvedAddresses.join(", ")}`}</dd></div>
              <div><dt>Source SHA-256</dt><dd><code>{preview.sourceSha256}</code></dd></div>
            </dl>
          </section>
        )}
      </div>
      <footer>
        <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
        {!preview?.allowed ? <button className="button primary" type="button" disabled={loading || starting} onClick={() => void review()}><RefreshCw className={loading ? "spin" : undefined} size={14} /> {loading ? "Checking…" : "Review request"}</button> : <button className="button danger" type="button" data-autofocus disabled={starting} onClick={() => void start()}><Play size={14} /> {starting ? "Starting…" : "Run"}</button>}
      </footer>
    </ModalSurface>
  );
}
