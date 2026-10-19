import { useEffect, useMemo, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ButlerRecoveryCase, ButlerRecoveryGoal } from "../api/butlerOverview";
import type { HarnessActivityEvent, HarnessTurnDetail } from "../api/types";
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

/** Read Core's durable model turn only when an operator opens its check. */
export function ButlerScheduledTranscript({ item, api, coreState, onNavigate }: {
  item: ButlerRecoveryCase;
  api?: ApiClient;
  coreState: "checking" | "online" | "offline";
  onNavigate?(path: string): void;
}) {
  const [open, setOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [events, setEvents] = useState<HarnessActivityEvent[]>([]);
  const [turn, setTurn] = useState<HarnessTurnDetail>();
  const [nextSequence, setNextSequence] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!open || !api || !item.modelTurnId || item.modelBackend !== "harness" || coreState !== "online") return;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    Promise.all([
      api.getHarnessTurnEvents(item.modelTurnId, 0, controller.signal, PAGE_SIZE),
      api.getHarnessTurn(item.modelTurnId, controller.signal),
    ]).then(([page, detail]) => {
      if (controller.signal.aborted) return;
      setEvents(page.events);
      setNextSequence(page.nextSequence);
      setHasMore(page.events.length === PAGE_SIZE);
      setTurn(detail);
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : "The saved activity could not be loaded.");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, coreState, item.modelBackend, item.modelTurnId, open, reload]);

  const activity = useMemo(() => events.reduce(
    (items, event) => isTimelineActivity(event) ? reduceHarnessActivity(items, event, item.id) : items,
    [] as HarnessActivityItem[],
  ), [events, item.id]);
  const ledger = useMemo(
    () => activityLedgerFromHarness("Butler check activity", turn?.status ?? (item.status === "investigating" ? "running" : "complete"), activity),
    [activity, item.status, turn?.status],
  );
  const loadMore = async () => {
    if (!api || !item.modelTurnId || loading || coreState !== "online") return;
    setLoading(true);
    setError(undefined);
    try {
      const page = await api.getHarnessTurnEvents(item.modelTurnId, nextSequence, undefined, PAGE_SIZE);
      setEvents(current => [...current, ...page.events]);
      setNextSequence(page.nextSequence);
      setHasMore(page.events.length === PAGE_SIZE && page.nextSequence > nextSequence);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "More activity could not be loaded.");
    } finally {
      setLoading(false);
    }
  };

  return <details className={styles.transcript} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Check transcript <span>{item.modelTurnId ? "Saved in Core" : "No model turn"}</span></summary>
    <div className={styles.transcriptBody}>
      {item.modelBackend === "provider" && <p className={styles.note}>This provider check saved its actions and outcome above. A full model event stream was not recorded.</p>}
      {!item.modelTurnId && <p className={styles.note}>Butler has not started a model turn for this check.</p>}
      {item.modelBackend === "harness" && item.modelTurnId && <>
        {coreState !== "online" && <p role="status" className={styles.note}>Reconnect to Core to load the saved transcript.</p>}
        {loading && <p role="status" className={styles.note}>Loading saved activity…</p>}
        {error && <div role="alert" className={styles.error}>Could not load the saved activity. {error} <button className="button quiet" type="button" disabled={coreState !== "online"} onClick={() => setReload(value => value + 1)}>Retry</button></div>}
        {turn?.response && <div className={styles.transcriptAnswer}><strong>Butler’s report</strong><p className={styles.transcriptProse}>{turn.response}</p></div>}
        {turn?.error && <p role="alert" className={styles.error}>{turn.error}</p>}
        {events.length > 0 && <ActivityLedger model={ledger} emptyState={<p>No tool activity was recorded.</p>} />}
        {hasMore && <button className="button quiet" type="button" disabled={loading || coreState !== "online"} onClick={() => void loadMore()}>{loading ? "Loading…" : "Show more activity"}</button>}
        {!loading && !error && turn && !turn.response && events.length === 0 && <p className={styles.note}>No model activity has been recorded yet.</p>}
      </>}
      {item.recoveryGoals.length > 0 && <section aria-label="Recovery goal reports" className={styles.recoveryReports}>
        <h4>Recovery goals</h4>
        <ul className={styles.list}>{item.recoveryGoals.map(goal => <RecoveryGoalReport key={goal.id} goal={goal} onNavigate={onNavigate} />)}</ul>
      </section>}
    </div>
  </details>;
}
