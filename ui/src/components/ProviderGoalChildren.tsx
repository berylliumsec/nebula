import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ChatGoal } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

// Goal children only matter while a goal may delegate or has delegated, so the
// section renders nothing otherwise.
export function ProviderGoalChildren({
  api, sessionId, goal, onOpenChild,
}: {
  api: ApiClient;
  sessionId: string;
  goal?: ChatGoal;
  onOpenChild?(sessionId: string): void;
}) {
  const [children, setChildren] = useState<ChatGoal[]>([]);
  const [childObjective, setChildObjective] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    setChildren(await api.listGoalChildren(sessionId).catch(() => {
      return []; // diagnostic-expected: a session without a goal has no children
    }));
  };

  useEffect(() => {
    void reload();
  }, [api, sessionId, goal?.revision]);

  const canStart = goal?.status === "running" && goal.childBudget !== undefined;
  if (!canStart && !children.length && !error) return null;

  const start = async () => {
    setBusy(true); setError(undefined);
    try {
      const child = await api.startGoalChild(sessionId, { objective: childObjective.trim(), completionCriteria: ["Child work completes without sharing parent approvals"] });
      setChildObjective("");
      await reload();
      onOpenChild?.(child.sessionId);
    } catch (caught) {
      void logCaughtDiagnostic("interface.session_advanced.action_failed", "A goal child could not start.", caught, "session_advanced");
      setError(caught instanceof Error ? caught.message : "The goal child could not start.");
    } finally { setBusy(false); }
  };

  return <section className="chat-goal-panel" aria-label="Goal children">
    <header><strong>Goal children</strong><small>Independent goals · no copied approvals</small></header>
    {canStart && <div className="chat-goal-form">
      <label>Child objective<input value={childObjective} onChange={event => setChildObjective(event.target.value)} /></label>
      <button className="button quiet" type="button" disabled={busy || !childObjective.trim()} onClick={() => void start()}>Start child</button>
    </div>}
    {children.length > 0 && <ul aria-label="Delegated children">{children.map(child => <li key={child.id}><strong>{child.objective}</strong> <small>{child.status.replaceAll("_", " ")}</small>{onOpenChild && <button className="button quiet" type="button" onClick={() => onOpenChild(child.sessionId)}>Open</button>}</li>)}</ul>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
