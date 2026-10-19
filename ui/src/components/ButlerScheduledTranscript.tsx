import { useEffect, useMemo, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ButlerRecoveryCase, ButlerRecoveryGoal } from "../api/butlerOverview";
import type { HarnessActivityEvent, HarnessTurnDetail } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { activityLedgerFromHarness } from "./activityLedgerModel";
import { ActivityLedger } from "./ActivityLedger";
import { isTimelineActivity, reduceHarnessActivity, type HarnessActivityItem } from "../pages/harnessActivity";
import styles from "./ButlerOversight.module.css";

const PAGE_SIZE = 1_000;

function RecoveryGoalReport({ goal, onNavigate }: { goal: ButlerRecoveryGoal; onNavigate?(path: string): void }) {
  const href = `/projects/${encodeURIComponent(goal.projectId)}/workbench?view=chat&session=${encodeURIComponent(goal.sessionId)}`;
  return <li className={styles.recoveryGoal}>
    <div className={styles.rowHead}><strong>{goal.objective}</strong><span>{goal.status.replaceAll("_", " ")}</span></div>
    {goal.report && <p className={styles.transcriptProse}>{goal.report}</p>}
    <small>{goal.currentStep} step{goal.currentStep === 1 ? "" : "s"} · Updated {new Date(goal.updatedAt).toLocaleString()}</small>
    <a className={styles.workLink} href={href} onClick={(event) => {
      if (!onNavigate) return;
      event.preventDefault();
      onNavigate(href);
    }}>Open recovery goal</a>
  </li>;
}

/** Each attempt keeps its own paged event stream and retry control. */
function SavedTurn({ caseId, turnId, label, api, coreState }: {
  caseId: string;
  turnId: string;
  label?: string;
  api?: ApiClient;
  coreState: "checking" | "online" | "offline";
}) {
  const [reload, setReload] = useState(0);
  const [events, setEvents] = useState<HarnessActivityEvent[]>([]);
  const [turn, setTurn] = useState<HarnessTurnDetail>();
  const [nextSequence, setNextSequence] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!api || coreState !== "online") return;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    Promise.all([
      api.getHarnessTurnEvents(turnId, 0, controller.signal, PAGE_SIZE),
      api.getHarnessTurn(turnId, controller.signal),
    ]).then(([page, detail]) => {
      if (controller.signal.aborted) return;
      setEvents(page.events);
      setNextSequence(page.nextSequence);
      setHasMore(page.events.length === PAGE_SIZE);
      setTurn(detail);
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) {
        void logCaughtDiagnostic("interface.butler_oversight.transcript_load_failed", "Butler check transcript could not be loaded.", caught, "butler_oversight");
        setError(caught instanceof Error ? caught.message : "The saved activity could not be loaded.");
      }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, coreState, turnId, reload]);

  const activity = useMemo(() => events.reduce(
    (items, event) => isTimelineActivity(event) ? reduceHarnessActivity(items, event, caseId) : items,
    [] as HarnessActivityItem[],
  ), [events, caseId]);
  const ledger = useMemo(
    () => activityLedgerFromHarness(label ?? "Butler check activity", turn?.status ?? "running", activity),
    [activity, label, turn?.status],
  );
  const loadMore = async () => {
    if (!api || loading || coreState !== "online") return;
    setLoading(true);
    setError(undefined);
    try {
      const page = await api.getHarnessTurnEvents(turnId, nextSequence, undefined, PAGE_SIZE);
      setEvents(current => [...current, ...page.events]);
      setNextSequence(page.nextSequence);
      setHasMore(page.events.length === PAGE_SIZE && page.nextSequence > nextSequence);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_oversight.transcript_more_failed", "More Butler check activity could not be loaded.", caught, "butler_oversight");
      setError(caught instanceof Error ? caught.message : "More activity could not be loaded.");
    } finally {
      setLoading(false);
    }
  };

  return <section aria-label={label ?? "Saved check turn"}>
        {label && <h4>{label}</h4>}
        {coreState !== "online" && <p role="status" className={styles.note}>Reconnect to Core to load the saved transcript.</p>}
        {loading && <p role="status" className={styles.note}>Loading saved activity…</p>}
        {error && <div role="alert" className={styles.error}>Could not load the saved activity. {error} <button className="button quiet" type="button" disabled={coreState !== "online"} onClick={() => setReload(value => value + 1)}>Retry</button></div>}
        {turn?.response && <div className={styles.transcriptAnswer}><strong>Butler’s report</strong><p className={styles.transcriptProse}>{turn.response}</p></div>}
        {turn?.error && <p role="alert" className={styles.error}>{turn.error}</p>}
        {events.length > 0 && <ActivityLedger model={ledger} emptyState={<p>No tool activity was recorded.</p>} />}
        {hasMore && <button className="button quiet" type="button" disabled={loading || coreState !== "online"} onClick={() => void loadMore()}>{loading ? "Loading…" : "Show more activity"}</button>}
        {!loading && !error && turn && !turn.response && events.length === 0 && <p className={styles.note}>No model activity has been recorded yet.</p>}
  </section>;
}

/** Read Core's durable model turns only when an operator opens the check. */
export function ButlerScheduledTranscript({ item, api, coreState, onNavigate }: {
  item: ButlerRecoveryCase;
  api?: ApiClient;
  coreState: "checking" | "online" | "offline";
  onNavigate?(path: string): void;
}) {
  const [open, setOpen] = useState(false);
  const turnIds = item.modelTurnIds?.length ? item.modelTurnIds : item.modelTurnId ? [item.modelTurnId] : [];
  return <details className={styles.transcript} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Check transcript <span>{turnIds.length ? "Saved in Core" : "No model turn"}</span></summary>
    <div className={styles.transcriptBody}>
      {item.modelBackend === "provider" && <p className={styles.note}>This provider check saved its actions and outcome above. A full model event stream was not recorded.</p>}
      {!turnIds.length && <p className={styles.note}>Butler has not started a model turn for this check.</p>}
      {open && item.modelBackend === "harness" && turnIds.map((turnId, index) => <SavedTurn
        key={turnId}
        caseId={item.id}
        turnId={turnId}
        label={turnIds.length > 1 ? `Attempt ${index + 1}` : undefined}
        api={api}
        coreState={coreState}
      />)}
      {item.recoveryGoals.length > 0 && <section aria-label="Recovery goal reports" className={styles.recoveryReports}>
        <h4>Recovery goals</h4>
        <ul className={styles.list}>{item.recoveryGoals.map(goal => <RecoveryGoalReport key={goal.id} goal={goal} onNavigate={onNavigate} />)}</ul>
      </section>}
    </div>
  </details>;
}
