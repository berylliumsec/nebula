import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { LoaderCircle, RefreshCw, Trash2 } from "lucide-react";
import type { StructuredResultSummary } from "../api/types";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { StandardEmptyState } from "../components/SurfacePrimitives";
import { IconAction } from "../components/IconAction";
import {
  isDashboardView,
  ResultTimeline,
  shapeLabel,
  StructuredResultDashboard,
  useStructuredResult,
  useStructuredResults,
  whenLabel,
  type DashboardView,
} from "../components/structured-result";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { projectSurface } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";

/**
 * The dedicated home for structured results: everything producers published
 * for this project, and the schema-agnostic explorer for whichever one is
 * open. Which result and which view are in the URL, so a view is shareable
 * and survives a reload.
 */
export function ResultsPage() {
  const { api, coreState, engagement } = useWorkspace();
  const confirm = useConfirmation();
  const navigate = useNavigate();
  const { resourceId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [removing, setRemoving] = useState<string>();
  const [removeError, setRemoveError] = useState<string>();

  const live = searchParams.get("follow") !== "off";
  const { items, loading, error, refresh } = useStructuredResults(api, engagement?.id, { live });
  const detail = useStructuredResult(api, engagement?.id, resourceId);
  const view = isDashboardView(searchParams.get("view")) ? (searchParams.get("view") as DashboardView) : "overview";

  // Following a live stream means opening what just arrived, but never while
  // the operator is reading something they opened themselves.
  const [followLatest, setFollowLatest] = useState(true);
  useEffect(() => {
    if (!live || !followLatest || resourceId || items.length === 0 || !engagement) return;
    navigate(`${projectSurface(engagement.id, "results", items[0].id)}`, { replace: true });
  }, [engagement, followLatest, items, live, navigate, resourceId]);

  const open = (item: StructuredResultSummary) => {
    if (!engagement) return;
    setFollowLatest(false);
    navigate(projectSurface(engagement.id, "results", item.id));
  };

  const changeView = (next: DashboardView) => {
    const params = new URLSearchParams(searchParams);
    params.set("view", next);
    setSearchParams(params, { replace: true });
  };

  const remove = async (item: StructuredResultSummary) => {
    if (!api || !engagement) return;
    if (!await confirm({
      title: `Delete “${item.title}”?`,
      message: "The published result and its presentation hints are removed from this project. Nothing else changes.",
      confirmLabel: "Delete result",
      tone: "danger",
    })) return;
    setRemoving(item.id);
    setRemoveError(undefined);
    try {
      await api.deleteStructuredResult(engagement.id, item.id);
      if (resourceId === item.id) navigate(projectSurface(engagement.id, "results"), { replace: true });
      refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.results_page.delete_failed", "A published result could not be deleted.", caught, "results_page");
      setRemoveError(caught instanceof Error ? caught.message : "That result could not be deleted.");
    } finally {
      setRemoving(undefined);
    }
  };

  const selected = items.find((item) => item.id === resourceId);

  return <div className="page results-page">
    <PageHeader
      eyebrow="Structured results"
      title="Results"
      description="What goals and integrations published for this project, explored without a schema."
      actions={<button type="button" className="button quiet" onClick={refresh} disabled={coreState !== "online"}>
        <RefreshCw size={15} aria-hidden="true" /> Refresh
      </button>}
    />

    {error && <DiagnosticErrorNotice title="Published results could not be read" error={error} />}
    {removeError && <DiagnosticErrorNotice title="The result was not deleted" error={removeError} />}

    <div className="results-layout">
      <section className="results-rail" aria-label="Published results">
        <header className="results-rail-header">
          <h2>Published</h2>
          <label className="results-follow">
            <input
              type="checkbox"
              checked={live}
              onChange={(event) => {
                const params = new URLSearchParams(searchParams);
                if (event.target.checked) params.delete("follow");
                else params.set("follow", "off");
                setSearchParams(params, { replace: true });
              }}
            />
            Watch for new results
          </label>
        </header>
        {loading && items.length === 0
          ? <p role="status"><LoaderCircle className="spin" size={14} aria-hidden="true" /> Reading published results…</p>
          : <ResultTimeline
              items={items}
              selectedId={resourceId}
              onSelect={open}
              emptyMessage="No result has been published for this project yet."
            />}
      </section>

      <section className="results-detail" aria-label="Result explorer">
        {!resourceId && items.length === 0 && <StandardEmptyState
          title="Nothing published yet"
          explanation="A running goal publishes here as it works, and any client can POST to /api/v1/projects/{project}/structured-results. Any JSON-compatible value works — no schema is registered."
        />}
        {!resourceId && items.length > 0 && <StandardEmptyState
          title="Choose a result"
          explanation="Open a published result to explore it as a summary, table, relationships, tree or raw JSON."
        />}
        {resourceId && detail.loading && <p role="status"><LoaderCircle className="spin" size={14} aria-hidden="true" /> Opening the result…</p>}
        {resourceId && detail.error && <DiagnosticErrorNotice title="That result could not be opened" error={detail.error} />}
        {resourceId && !detail.loading && !detail.error && !detail.record && <StandardEmptyState
          title="Result unavailable"
          explanation="This link points to a deleted or unknown result. Nebula did not substitute another one."
        />}
        {detail.record && <>
          <header className="results-detail-header">
            <div>
              <h2>{detail.record.title}</h2>
              <p>
                {whenLabel(detail.record.createdAt)}
                {detail.record.producer ? ` · ${detail.record.producer}` : ""}
                {detail.record.stream ? ` · ${detail.record.stream} step ${detail.record.sequence}` : ""}
                {` · ${shapeLabel(detail.record)}`}
              </p>
              {detail.record.summary && <p>{detail.record.summary}</p>}
            </div>
            {selected && <IconAction
              icon={Trash2}
              label={`Delete ${detail.record.title}`}
              disabled={removing === selected.id}
              onClick={() => void remove(selected)}
            />}
          </header>
          <StructuredResultDashboard
            key={detail.record.id}
            value={detail.record.result}
            hints={detail.record.hints}
            title={detail.record.title}
            view={view}
            onViewChange={changeView}
          />
        </>}
      </section>
    </div>
  </div>;
}
