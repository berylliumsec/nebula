import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ChatGoal } from "../api/types";
import { ApiError } from "../api/client";
import { logCaughtDiagnostic } from "../diagnostics";

export function ProviderSessionAdvanced({
  api, sessionId, goal, onOpenChild,
}: {
  api: ApiClient;
  sessionId: string;
  goal?: ChatGoal;
  onOpenChild?(sessionId: string): void;
}) {
  const [paths, setPaths] = useState("");
  const [checkpoints, setCheckpoints] = useState<Array<{ id: string; label: string; files: Array<{ path: string }> }>>([]);
  const [preview, setPreview] = useState<Array<{ path: string; status: string }>>();
  const [children, setChildren] = useState<ChatGoal[]>([]);
  const [childObjective, setChildObjective] = useState("");
  const [schedule, setSchedule] = useState<{ revision: number; enabled: boolean; interval_seconds: number; next_run_at: string; skip_reason?: string | null; last_status?: string | null }>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    const [saved, kids] = await Promise.all([
      api.listChatCheckpoints(sessionId),
      api.listGoalChildren(sessionId).catch(() => {
        return []; // diagnostic-expected: a session without a goal has no children
      }),
    ]);
    setCheckpoints(saved);
    setChildren(kids);
    try {
      setSchedule(await api.getChatSchedule(sessionId));
    } catch (caught) {
      if (!(caught instanceof ApiError && caught.status === 404)) throw caught;
      setSchedule(undefined);
    }
  };

  useEffect(() => {
    void reload().catch((caught) => {
      void logCaughtDiagnostic("interface.session_advanced.load_failed", "Advanced session controls could not load.", caught, "session_advanced");
      setError(caught instanceof Error ? caught.message : "Advanced session controls could not load.");
    });
  }, [api, sessionId, goal?.revision]);

  const act = async (work: () => Promise<void>) => {
    setBusy(true); setError(undefined);
    try { await work(); await reload(); }
    catch (caught) {
      void logCaughtDiagnostic("interface.session_advanced.action_failed", "An advanced session action failed.", caught, "session_advanced");
      setError(caught instanceof Error ? caught.message : "The session action failed.");
    }
    finally { setBusy(false); }
  };

  return <section className="chat-goal-panel" aria-label="Workspace controls" data-guide="workspace-controls">
    <header><strong>Workspace controls</strong><small>Shared files · independent goals · no copied approvals</small></header>
    <label>Checkpoint files<textarea value={paths} placeholder="One relative path per line" onChange={event => setPaths(event.target.value)} /></label>
    <div className="chat-goal-actions">
      <button className="button secondary" type="button" disabled={busy || !paths.trim()} onClick={() => void act(async () => {
        await api.captureChatCheckpoint(sessionId, { label: "Operator checkpoint", paths: paths.split("\n").map(item => item.trim()).filter(Boolean) });
      })}>Save checkpoint</button>
      {schedule ? <button className="button quiet" type="button" disabled={busy} onClick={() => void act(async () => {
        await api.writeChatSchedule(sessionId, { expectedRevision: schedule.revision, enabled: !schedule.enabled });
      })}>{schedule.enabled ? "Disable schedule" : "Enable schedule"}</button> : <button className="button quiet" type="button" disabled={busy} onClick={() => void act(async () => {
        await api.createChatSchedule(sessionId, 3600);
      })}>Schedule hourly</button>}
    </div>
    {schedule && <p role="status">{schedule.enabled ? "Scheduled" : "Paused"} · next {new Date(schedule.next_run_at).toLocaleString()}{schedule.skip_reason ? ` · ${schedule.skip_reason}` : schedule.last_status ? ` · last ${schedule.last_status}` : ""}</p>}
    {checkpoints.length > 0 && <ul>{checkpoints.map(item => <li key={item.id}><strong>{item.label}</strong> <small>{item.files.length} files</small> <button className="button quiet" type="button" disabled={busy} onClick={() => void act(async () => setPreview(await api.previewChatCheckpoint(item.id)))}>Preview</button> <button className="button quiet" type="button" disabled={busy} onClick={() => void act(async () => { await api.restoreChatCheckpoint(item.id); })}>Restore</button></li>)}</ul>}
    {preview && <p>{preview.map(item => `${item.path}: ${item.status}`).join(" · ")}</p>}
    {goal?.status === "running" && goal.childBudget !== undefined && <div className="chat-goal-form">
      <label>Child objective<input value={childObjective} onChange={event => setChildObjective(event.target.value)} /></label>
      <button className="button quiet" type="button" disabled={busy || !childObjective.trim()} onClick={() => void act(async () => {
        const child = await api.startGoalChild(sessionId, { objective: childObjective.trim(), completionCriteria: ["Child work completes without sharing parent approvals"] });
        setChildObjective("");
        onOpenChild?.(child.sessionId);
      })}>Start child</button>
    </div>}
    {children.length > 0 && <ul aria-label="Delegated children">{children.map(child => <li key={child.id}><strong>{child.objective}</strong> <small>{child.status.replaceAll("_", " ")}</small>{onOpenChild && <button className="button quiet" type="button" onClick={() => onOpenChild(child.sessionId)}>Open</button>}</li>)}</ul>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
