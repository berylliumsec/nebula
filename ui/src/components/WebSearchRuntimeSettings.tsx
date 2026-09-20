import { useCallback, useEffect, useState } from "react";
import { Globe } from "lucide-react";
import type { WebSearchEngine, WebSearchRuntime } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { announceSettingsSaved } from "./SettingsSaveFeedback";
import { testedAgo } from "./TypeSafeIntegrationSettings";
import { useConfirmation } from "./DialogSystem";

/** The project opt-in follows runtime changes without a reload. */
export const WEB_SEARCH_CHANGED_EVENT = "nebula:web-search-runtime-changed";

function publish(status: WebSearchRuntime) {
  window.dispatchEvent(new CustomEvent<WebSearchRuntime>(WEB_SEARCH_CHANGED_EVENT, { detail: status }));
}

export const WEB_SEARCH_ENGINES: { id: WebSearchEngine; label: string }[] = [
  { id: "duckduckgo", label: "DuckDuckGo" },
  { id: "brave", label: "Brave" },
  { id: "wikipedia", label: "Wikipedia" },
  { id: "stackexchange", label: "Stack Exchange" },
  { id: "github", label: "GitHub" },
];

type Health = { label: string; tone: "muted" | "good" | "bad" };

export function runtimeHealth(status: WebSearchRuntime | undefined): Health {
  if (!status) return { label: "Checking\u2026", tone: "muted" };
  if (!status.runtimeAvailable) return { label: "No container runtime", tone: "bad" };
  switch (status.containerState) {
    case "ready": return { label: "Ready", tone: "good" };
    case "starting": return { label: "Starting", tone: "muted" };
    case "stopped": return { label: "Stopped", tone: "muted" };
    case "failed": return { label: "Failed", tone: "bad" };
    default: return { label: "Not installed", tone: "muted" };
  }
}

/** Short digest for display; the full value stays in the API payload. */
export function shortDigest(digest: string | undefined): string | undefined {
  if (!digest) return undefined;
  const hex = digest.replace(/^sha256:/, "");
  return hex.length > 16 ? `sha256:${hex.slice(0, 6)}…${hex.slice(-4)}` : digest;
}

type Busy = "install" | "start" | "stop" | "test" | "remove" | "engines";

export function WebSearchRuntimeSettings() {
  const confirm = useConfirmation();
  const { api, coreState, previewMode } = useWorkspace();
  const [status, setStatus] = useState<WebSearchRuntime>();
  const [busy, setBusy] = useState<Busy>();
  const [error, setError] = useState<unknown>();

  const load = useCallback(async () => {
    if (!api || coreState !== "online") return;
    try {
      setStatus(await api.getWebSearchRuntime());
      setError(undefined);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.web_search.load_failed", "The web search runtime status could not be loaded.", loadError, "integrations");
      setError(loadError);
    }
  }, [api, coreState]);

  useEffect(() => { void load(); }, [load]);

  const run = async (kind: Busy, action: () => Promise<WebSearchRuntime>, saved?: string) => {
    setBusy(kind);
    setError(undefined);
    try {
      const next = await action();
      setStatus(next);
      publish(next);
      if (saved) announceSettingsSaved(saved);
      return next;
    } catch (actionError) {
      void logCaughtDiagnostic(`interface.web_search.${kind}_failed`, "The web search runtime operation failed.", actionError, "integrations");
      setError(actionError);
      return undefined;
    } finally {
      setBusy(undefined);
    }
  };

  const toggleEngine = (engine: WebSearchEngine, enabled: boolean) => {
    if (!api || !status) return;
    const next = enabled
      ? [...status.engines, engine]
      : status.engines.filter((item) => item !== engine);
    if (next.length === 0) return;
    void run("engines", () => api.setWebSearchEngines(next), "Search engines saved");
  };

  const remove = async () => {
    if (!api) return;
    const approved = await confirm({
      title: "Remove the search runtime?",
      message: <>The container is deleted from this host. Projects that use web search keep working once it is installed again.{status?.projectsUsing ? ` ${status.projectsUsing} project${status.projectsUsing === 1 ? " uses" : "s use"} it.` : ""}</>,
      confirmLabel: "Remove runtime",
      tone: "danger",
    });
    if (approved) await run("remove", () => api.removeWebSearchRuntime(), "Search runtime removed");
  };

  const health = runtimeHealth(status);
  const disabled = previewMode || !api || coreState !== "online" || Boolean(busy);
  const installed = Boolean(status?.imageDigest);
  const running = status?.containerState === "ready";
  const test = status?.lastTest;
  const digest = shortDigest(status?.imageDigest);

  return <section className="settings-section" id="web-search-runtime-settings">
    <div className="section-heading"><div><h2>Web search</h2><p>A metasearch container Nebula runs on this host so agents can research the public web.</p></div><Globe size={20} /></div>
    {Boolean(error) && <DiagnosticErrorNotice error={error} fallback="The web search runtime operation failed." compact />}
    <div className="panel web-search-panel">
      <header className="panel-header compact web-search-header">
        <div><h3>Search runtime</h3><p>No account and no API key. Each project opts in separately.</p></div>
        <span className={`integration-status ${health.tone}`} role="status"><span aria-hidden="true" />{health.label}</span>
      </header>
      {status?.runtimeAvailable === false
        ? <p className="provider-dialog-note">{status.runtimeDetail || "Web search needs a container runtime. Configure one under Runtimes, then install the search runtime here."}</p>
        : <dl className="integration-facts">
          <div><dt>Image</dt><dd>{digest ? `searxng/searxng · ${digest} · pinned` : "Not installed"}</dd></div>
          {status?.port !== undefined && <div><dt>Address</dt><dd>{`127.0.0.1:${status.port} · loopback only`}</dd></div>}
          {test && <div><dt>Last test</dt><dd>{test.ok
            ? [testedAgo(test.testedAt), test.latencyMs !== undefined ? `${test.latencyMs} ms` : undefined, test.enginesAnswered.join(", ") || undefined].filter(Boolean).join(" · ")
            : `${testedAgo(test.testedAt)} · ${test.error ?? "failed"}`}</dd></div>}
          <div><dt>In use by</dt><dd>{status?.projectsUsing === 1 ? "1 project" : `${status?.projectsUsing ?? 0} projects`}</dd></div>
        </dl>}
      <fieldset className="web-search-engines">
        <legend>Engines</legend>
        {WEB_SEARCH_ENGINES.map(({ id, label }) => {
          const checked = status?.engines.includes(id) ?? false;
          const last = checked && status?.engines.length === 1;
          return <label className="provider-consent" key={id}>
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled || last}
              onChange={(event) => toggleEngine(id, event.target.checked)}
            />
            <span><strong>{label}</strong>{last && <small>At least one engine stays selected.</small>}</span>
          </label>;
        })}
      </fieldset>
      <p className="web-search-disclosure">Queries reach the engines you select, over this machine&rsquo;s own connection. Projects marked local&nbsp;only cannot use web search.</p>
      <footer className="web-search-footer">
        <span>The test sends one fixed sample query, never project data.</span>
        <span className="web-search-actions">
          {status && !installed && <button className="button primary" type="button" disabled={disabled} onClick={() => void run("install", () => api!.installWebSearchRuntime(), "Search runtime installed")}>{busy === "install" ? "Installing…" : "Install"}</button>}
          {installed && !running && <button className="button primary" type="button" disabled={disabled} onClick={() => void run("start", () => api!.startWebSearchRuntime(), "Search runtime started")}>{busy === "start" ? "Starting…" : "Start"}</button>}
          {installed && <button className="button secondary" type="button" disabled={disabled} onClick={() => void run("test", () => api!.testWebSearchRuntime())}>{busy === "test" ? "Testing…" : "Test"}</button>}
          {running && <button className="button secondary" type="button" disabled={disabled} onClick={() => void run("stop", () => api!.stopWebSearchRuntime(), "Search runtime stopped")}>{busy === "stop" ? "Stopping…" : "Stop"}</button>}
          {installed && <button className="button danger-quiet" type="button" disabled={disabled} onClick={() => void remove()}>{busy === "remove" ? "Removing…" : "Remove"}</button>}
        </span>
      </footer>
    </div>
  </section>;
}
