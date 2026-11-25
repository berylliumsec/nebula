import {useEffect, useState, type FormEvent} from "react";
import type {ApiClient} from "../api/client";
import {BUTLER_INTERVAL_CHANGED_EVENT, getButlerCheckInterval, updateButlerCheckInterval, updateButlerSummaryTarget, type ButlerCheckInterval} from "../api/butlerInstructions";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import {announceSettingsSaved} from "./SettingsSaveFeedback";

export function ButlerCheckIntervalSettings({api, previewMode = false}: {api?: ApiClient; previewMode?: boolean}) {
  const [saved, setSaved] = useState<ButlerCheckInterval>();
  const [draft, setDraft] = useState("5");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [refreshKey, setRefreshKey] = useState(0);
  const [changedElsewhere, setChangedElsewhere] = useState(false);
  const [targetDraft, setTargetDraft] = useState("");
  const [savingTarget, setSavingTarget] = useState(false);

  useEffect(() => {
    if (!api || previewMode) return;
    const controller = new AbortController();
    setLoading(true);
    void getButlerCheckInterval(api, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setSaved(next);
        setDraft(String(next.check_interval_minutes));
        setTargetDraft(targetKey(next));
        setChangedElsewhere(false);
        setError(undefined);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.butler_interval.load_failed", "Butler check interval could not be loaded.", caught, "butler");
        setError(caught instanceof Error ? caught.message : "Could not load Butler check interval.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, previewMode, refreshKey]);

  useEffect(() => {
    if (!api || previewMode) return;
    const refresh = () => {
      if (saved && draft !== String(saved.check_interval_minutes)) {
        setChangedElsewhere(true);
        setError("Butler's check interval changed elsewhere. Your draft is kept; reload the current value before saving.");
        return;
      }
      setNotice(undefined);
      setRefreshKey(current => current + 1);
    };
    window.addEventListener(BUTLER_INTERVAL_CHANGED_EVENT, refresh);
    return () => window.removeEventListener(BUTLER_INTERVAL_CHANGED_EVENT, refresh);
  }, [api, previewMode, saved, draft]);

  const minutes = Number(draft);
  const valid = /^\d+$/.test(draft) && Number.isInteger(minutes) && minutes >= 1 && minutes <= 1440;
  const canSave = !!api && !previewMode && !loading && !saving && !changedElsewhere && !!saved && valid && minutes !== saved.check_interval_minutes;

  const summaryTarget = saved?.summary_target;
  const canSaveTarget = !!api && !previewMode && !loading && !saving && !savingTarget && !!saved && !!summaryTarget && targetDraft !== targetKey(saved);

  const saveTarget = async () => {
    if (!api || !saved || !canSaveTarget || !summaryTarget) return;
    const choice = summaryTarget.options.find((item) => `${item.project_id}/${item.session_id}` === targetDraft);
    const target = choice ? {project_id: choice.project_id, session_id: choice.session_id} : null;
    setSavingTarget(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const updated = await updateButlerSummaryTarget(api, target, saved.revision);
      const observed = await getButlerCheckInterval(api);
      if (targetKey(observed) !== targetDraft || targetKey(updated) !== targetDraft) {
        throw new Error("Core did not confirm where check summaries post. Reload before editing again.");
      }
      setSaved(observed);
      setDraft(String(observed.check_interval_minutes));
      announceSettingsSaved("Butler check summary chat saved on Core.");
      setNotice(choice ? `Saved. Check summaries post to ${choice.title} in ${choice.project_name}.` : "Saved. Check summaries post to Core's default Butler chat.");
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_interval.summary_target_failed", "Butler check summary chat could not be confirmed on Core.", caught, "butler");
      const message = caught instanceof Error ? caught.message : "Could not save the check summary chat.";
      setError(message.includes("changed") || message.includes("409")
        ? "Butler's check settings changed elsewhere. Reload the current value before saving."
        : message);
    } finally {
      setSavingTarget(false);
    }
  };

  const reload = () => {
    setNotice(undefined);
    setRefreshKey(current => current + 1);
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !saved || !canSave) return;
    setSaving(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const updated = await updateButlerCheckInterval(api, minutes, saved.revision);
      const observed = await getButlerCheckInterval(api);
      if (observed.revision !== updated.revision || observed.check_interval_minutes !== minutes) {
        throw new Error("Core did not confirm Butler's check interval. Reload before editing again.");
      }
      setSaved(observed);
      setDraft(String(observed.check_interval_minutes));
      announceSettingsSaved("Butler check interval saved on Core.");
      setNotice(`Saved. Butler checks every ${minutes} minute${minutes === 1 ? "" : "s"}.`);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_interval.save_failed", "Butler check interval could not be confirmed on Core.", caught, "butler");
      const message = caught instanceof Error ? caught.message : "Could not save Butler check interval.";
      setError(message.includes("changed") || message.includes("409")
        ? "Butler's check interval changed elsewhere. Reload the current value before saving."
        : message);
    } finally {
      setSaving(false);
    }
  };

  return <section className="settings-section" id="butler-check-interval-settings" aria-labelledby="butler-check-interval-title">
    <div className="section-heading"><div><h2 id="butler-check-interval-title">Butler check interval</h2><p>Nebula-wide scheduled checks, including idle projects, and where their summaries post</p></div></div>
    <form onSubmit={(event) => void save(event)}>
      <label htmlFor="butler-check-interval-minutes">Check every</label>{" "}
      <input id="butler-check-interval-minutes" type="number" min="1" max="1440" step="1" inputMode="numeric" value={draft} onChange={(event) => setDraft(event.target.value)} disabled={!api || previewMode || loading || saving} style={{width: 90, minHeight: 44}} />{" "}minutes{" "}
      <button className="button primary" type="submit" disabled={!canSave}>{saving ? "Saving…" : "Save interval"}</button>{" "}
      <button className="button quiet" type="button" onClick={() => void reload()} disabled={!api || previewMode || loading || saving}>Reload</button>
    </form>
    <p>Default: 5 minutes. Applies to the next due check; a check already in progress keeps running.</p>
    {previewMode && <p role="status">Connect to Core to change Butler's interval.</p>}
    {loading && <p role="status">Loading Butler's interval…</p>}
    {saved?.next_check_at && <p>Next due: <time dateTime={saved.next_check_at}>{new Date(saved.next_check_at).toLocaleString()}</time></p>}
    {summaryTarget && <div className="butler-summary-target">
      <label htmlFor="butler-check-summary-target">Post check summaries to</label>{" "}
      <select id="butler-check-summary-target" value={targetDraft} onChange={(event) => setTargetDraft(event.target.value)} disabled={!api || previewMode || loading || saving || savingTarget} style={{minHeight: 44}}>
        <option value="">Default: chat of the project hosting Nebula-wide checks</option>
        {summaryTarget.options.map((item) => <option key={`${item.project_id}/${item.session_id}`} value={`${item.project_id}/${item.session_id}`}>{`${item.project_name} · ${item.title}`}</option>)}
      </select>{" "}
      <button className="button secondary" type="button" onClick={() => void saveTarget()} disabled={!canSaveTarget}>{savingTarget ? "Saving…" : "Save summary chat"}</button>
      <p>Each finished Butler check posts one summary with a link to its check chat. Checks whose result is "No change" post nothing.</p>
      {summaryTarget.effective
        ? <p>Summaries now post to <a href={summaryTarget.effective.url}>{summaryTarget.effective.title}</a>{summaryTarget.effective.source === "configured" ? "." : " (default)."}</p>
        : <p>No Butler chat can receive summaries yet; each check posts to its own project's Butler chat.</p>}
      {summaryTarget.unavailable.length > 0 && <p role="status">{`Unavailable: ${summaryTarget.unavailable.map((item) => item.reason).join("; ")}.`}</p>}
      {summaryTarget.last_fallback && <p>Last fallback: <time dateTime={summaryTarget.last_fallback.at}>{new Date(summaryTarget.last_fallback.at).toLocaleString()}</time>{` · ${summaryTarget.last_fallback.fallbacks.map((item) => item.reason).join("; ")}`}</p>}
    </div>}
    {!valid && <p role="alert">Choose a whole number from 1 to 1440 minutes.</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}

function targetKey(value: ButlerCheckInterval): string {
  const configured = value.summary_target?.configured;
  return configured?.session_id ? `${configured.project_id}/${configured.session_id}` : "";
}
