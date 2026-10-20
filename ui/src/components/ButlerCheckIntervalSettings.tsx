import {useEffect, useState, type FormEvent} from "react";
import type {ApiClient} from "../api/client";
import {getButlerCheckInterval, updateButlerCheckInterval, type ButlerCheckInterval} from "../api/butlerInstructions";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import {announceSettingsSaved} from "./SettingsSaveFeedback";

export function ButlerCheckIntervalSettings({api, previewMode = false}: {api?: ApiClient; previewMode?: boolean}) {
  const [saved, setSaved] = useState<ButlerCheckInterval>();
  const [draft, setDraft] = useState("5");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();

  useEffect(() => {
    if (!api || previewMode) return;
    const controller = new AbortController();
    setLoading(true);
    void getButlerCheckInterval(api, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setSaved(next);
        setDraft(String(next.check_interval_minutes));
        setError(undefined);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.butler_interval.load_failed", "Butler check interval could not be loaded.", caught, "butler");
        setError(caught instanceof Error ? caught.message : "Could not load Butler check interval.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, previewMode]);

  const minutes = Number(draft);
  const valid = /^\d+$/.test(draft) && Number.isInteger(minutes) && minutes >= 1 && minutes <= 1440;
  const canSave = !!api && !previewMode && !loading && !saving && !!saved && valid && minutes !== saved.check_interval_minutes;

  const reload = async () => {
    if (!api) return;
    setLoading(true);
    setError(undefined);
    try {
      const next = await getButlerCheckInterval(api);
      setSaved(next);
      setDraft(String(next.check_interval_minutes));
      setNotice(undefined);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_interval.reload_failed", "Butler check interval could not be reloaded.", caught, "butler");
      setError(caught instanceof Error ? caught.message : "Could not reload Butler check interval.");
    } finally {
      setLoading(false);
    }
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
    <div className="section-heading"><div><h2 id="butler-check-interval-title">Butler check interval</h2><p>Nebula-wide scheduled checks, including idle projects</p></div></div>
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
    {!valid && <p role="alert">Choose a whole number from 1 to 1440 minutes.</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
