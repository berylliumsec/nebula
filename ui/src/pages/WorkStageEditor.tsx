import { useMemo, useState, type FormEvent } from "react";
import { ArrowDown, ArrowUp, LoaderCircle, Plus, Trash2, X } from "lucide-react";
import { ApiError } from "../api/client";
import type { EngagementSummary, WorkStage } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { stageKeyFor } from "./workStages";

type Draft = WorkStage & { saved: boolean; rowId: number };

interface WorkStageEditorProps {
  project: EngagementSummary;
  /** Stages this project uses from a parent while it has none of its own. */
  inherited?: { from: string; stages: WorkStage[] };
  /** Open items per stage key, so removing a used stage says what happens to them. */
  itemCounts: Record<string, number>;
  save: (stages: WorkStage[], expectedRevision: number | undefined) => Promise<void>;
  close: () => void;
}

const maxStages = 12;

/** Edit a project's ordered Work stages. Saved keys never change, so items keep their stage. */
export function WorkStageEditor({ project, inherited, itemCounts, save, close }: WorkStageEditorProps) {
  // Inherited stages start the draft with their keys, so items keep their stage when this project saves its own.
  const [rows, setRows] = useState<Draft[]>(() => (project.workStages?.length ? project.workStages : inherited?.stages ?? []).map((stage, index) => ({ ...stage, saved: true, rowId: index })));
  const [nextRow, setNextRow] = useState(rows.length);
  // Save against the revision the editor opened with, so a change made elsewhere is reported, not overwritten.
  const [baseRevision] = useState(project.revision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  // Saved keys stay fixed; a new stage's key comes from its name and skips keys already taken.
  const keys = useMemo(() => {
    const taken = new Set(rows.filter((row) => row.saved).map((row) => row.key));
    return rows.map((row) => {
      if (row.saved) return row.key;
      const key = stageKeyFor(row.label, taken);
      taken.add(key);
      return key;
    });
  }, [rows]);
  const invalid = rows.some((row) => !row.label.trim());

  const update = (rowId: number, change: Partial<Draft>) => setRows((current) => current.map((row) => row.rowId === rowId ? { ...row, ...change } : row));
  const move = (index: number, offset: -1 | 1) => setRows((current) => {
    const next = [...current];
    [next[index], next[index + offset]] = [next[index + offset], next[index]];
    return next;
  });
  const add = () => {
    setRows((current) => [...current, { key: "", label: "", flagWhenEmpty: false, saved: false, rowId: nextRow }]);
    setNextRow((value) => value + 1);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (invalid || busy) return;
    setBusy(true); setError(undefined);
    try {
      await save(rows.map((row, index) => ({ key: keys[index], label: row.label.trim(), flagWhenEmpty: row.flagWhenEmpty })), baseRevision);
      close();
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.stages_failed", "Work stages could not be saved.", failure, "work_page");
      setError(failure instanceof ApiError && failure.status === 409
        ? "Stages changed somewhere else. Close and reopen to edit the latest stages."
        : failure instanceof Error ? failure.message : "Core could not save the stages. Try again.");
    } finally { setBusy(false); }
  };

  return <div className="work-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) close(); }}>
    <form className="work-dialog work-stage-editor" role="dialog" aria-modal="true" aria-labelledby="work-stages-title" onSubmit={(event) => void submit(event)}>
      <header><h2 id="work-stages-title">Work stages</h2><button className="work-dialog-close" type="button" aria-label="Close" title="Close" disabled={busy} onClick={close}><X size={18} aria-hidden="true" /></button></header>
      <p className="work-stage-lede">{inherited
        ? <>{project.name} uses {inherited.from}'s stages. Saving gives {project.name} its own stages; {inherited.from} and its other subprojects keep theirs.</>
        : <>Group {project.name}'s items by stage. Stages are saved with this project, and subprojects without their own stages use them.</>}</p>
      {rows.length ? <ol className="work-stage-rows">{rows.map((row, index) => {
        const used = row.saved ? itemCounts[row.key] ?? 0 : 0;
        return <li key={row.rowId}>
          <div className="work-stage-order">
            <button type="button" className="icon-button" aria-label={`Move ${row.label || "stage"} up`} title="Move up" disabled={busy || index === 0} onClick={() => move(index, -1)}><ArrowUp size={15} aria-hidden="true" /></button>
            <button type="button" className="icon-button" aria-label={`Move ${row.label || "stage"} down`} title="Move down" disabled={busy || index === rows.length - 1} onClick={() => move(index, 1)}><ArrowDown size={15} aria-hidden="true" /></button>
          </div>
          <label className="work-stage-label"><span className="sr-only">Stage {index + 1} name</span><input required maxLength={60} value={row.label} placeholder="Stage name" autoFocus={!row.saved && index === rows.length - 1} onChange={(event) => update(row.rowId, { label: event.target.value })} /></label>
          <label className="work-stage-flag" title="Show this stage as missing on All projects while it has no items"><input type="checkbox" checked={row.flagWhenEmpty} onChange={(event) => update(row.rowId, { flagWhenEmpty: event.target.checked })} /> Flag when empty</label>
          <small className="work-stage-count">{used ? `${used} open` : ""}</small>
          <button type="button" className="icon-button" aria-label={`Remove ${row.label || "stage"}`} title={used ? `Remove; its ${used} open items move to Other stage` : "Remove"} disabled={busy} onClick={() => setRows((current) => current.filter((candidate) => candidate.rowId !== row.rowId))}><Trash2 size={15} aria-hidden="true" /></button>
        </li>;
      })}</ol> : <p className="work-empty-inline">No stages yet. Add the steps this project's work moves through.</p>}
      <button type="button" className="button quiet work-stage-add" disabled={busy || rows.length >= maxStages} onClick={add}><Plus size={16} aria-hidden="true" /> Add stage</button>
      {error && <p className="work-track-error" role="alert">{error}</p>}
      <footer><button type="button" className="button quiet" disabled={busy} onClick={close}>Cancel</button><button type="submit" className="button primary" aria-busy={busy} disabled={busy || invalid}>{busy && <LoaderCircle className="spin" size={16} aria-hidden="true" />} Save stages</button></footer>
    </form>
  </div>;
}
