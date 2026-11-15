import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { FolderKanban, MessageSquare, RefreshCw } from "lucide-react";
import { buildProjectSnapshots, relativeAge, type ProjectSnapshot } from "../api/projectSnapshots";
import type { ChatAttentionItem, ChatSessionActivity, ChatSessionSummary } from "../api/types";
import { sameJson, startVisiblePoll, type VisiblePoll } from "../api/visiblePoll";
import { logCaughtDiagnostic } from "../diagnostics";
import { rememberedProjectId, useWorkspace } from "../state/WorkspaceContext";
import { NebulaAssistantLauncherContext } from "./NebulaAssistantLauncher";
import styles from "./ShellProjects.module.css";

/** Path of Butler for one project inside the iPhone app. */
export function shellButlerPath(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/butler`;
}

/** The iPhone app's only two destinations. */
export function ShellTabBar({ docked = false }: { docked?: boolean }) {
  const { continuityScope, engagement, engagements } = useWorkspace();
  const project = engagement ?? engagements.find(item => item.id === rememberedProjectId(continuityScope)) ?? engagements[0];
  return <nav className={`${styles.tabBar}${docked ? ` ${styles.docked}` : ""}`} aria-label="Nebula app">
    <NavLink className={styles.tab} to={project ? shellButlerPath(project.id) : "/"}><MessageSquare size={20} aria-hidden="true" /><span>Butler</span></NavLink>
    <NavLink className={styles.tab} to="/projects" end><FolderKanban size={20} aria-hidden="true" /><span>Projects</span></NavLink>
  </nav>;
}

interface SnapshotReads {
  sessions: Map<string, ChatSessionSummary[]>;
  activity: Map<string, ChatSessionActivity[]>;
  attention: ChatAttentionItem[];
  failed: number;
}

/** Read-only snapshot of active projects that hold at least one chat. Core owns every value shown. */
export function ShellProjectsPage() {
  const { api, engagements, workspaceState } = useWorkspace();
  const navigate = useNavigate();
  const [reads, setReads] = useState<SnapshotReads>();
  const [error, setError] = useState<string>();
  const [now, setNow] = useState(() => Date.now());
  const pollRef = useRef<VisiblePoll | undefined>(undefined);
  // A failed read keeps the project's last Core answer instead of dropping its card.
  const lastGood = useRef<SnapshotReads | undefined>(undefined);
  const connected = Boolean(api && (workspaceState === "ready" || workspaceState === "degraded"));
  const activeIds = useMemo(() => engagements.filter(item => item.status === "active").map(item => item.id), [engagements]);
  const activeKey = activeIds.join(",");

  useEffect(() => {
    if (!api || !connected) return;
    const controller = new AbortController();
    pollRef.current = startVisiblePoll({
      signal: controller.signal,
      intervalMs: 30_000,
      read: async signal => {
        const ids = activeKey ? activeKey.split(",") : [];
        const attention = api.listChatAttention(signal).catch((caught: unknown) => {
          if (!signal.aborted) void logCaughtDiagnostic("interface.shell_projects.attention_failed", "Pending chat actions could not be loaded for the Projects snapshot.", caught, "shell-projects");
          return undefined;
        });
        const results = await Promise.allSettled(ids.map(async id => {
          const [page, activity] = await Promise.all([api.listChatSessionsPage(id, 0, signal), api.listChatSessionActivity(id, signal)]);
          return { id, sessions: page.items, activity };
        }));
        const attentionItems = await attention;
        if (signal.aborted) return "stop";
        const previous = lastGood.current;
        const next: SnapshotReads = { sessions: new Map(), activity: new Map(), attention: attentionItems ?? previous?.attention ?? [], failed: 0 };
        results.forEach((result, index) => {
          const id = ids[index];
          if (result.status === "fulfilled") {
            next.sessions.set(id, result.value.sessions);
            next.activity.set(id, result.value.activity);
          } else {
            next.failed += 1;
            const sessions = previous?.sessions.get(id);
            const activity = previous?.activity.get(id);
            if (sessions) next.sessions.set(id, sessions);
            if (activity) next.activity.set(id, activity);
            void logCaughtDiagnostic("interface.shell_projects.read_failed", "A project snapshot could not be loaded.", result.reason, "shell-projects");
          }
        });
        lastGood.current = next;
        setNow(Date.now());
        setReads(current => current && sameJson([...current.sessions], [...next.sessions]) && sameJson([...current.activity], [...next.activity]) && sameJson(current.attention, next.attention) && current.failed === next.failed ? current : next);
        setError(attentionItems === undefined ? "Pending actions could not be loaded." : undefined);
        return next.failed || attentionItems === undefined ? "active" : "idle";
      },
    });
    return () => { controller.abort(); pollRef.current = undefined; };
  }, [api, connected, activeKey]);

  const snapshots = useMemo(() => reads ? buildProjectSnapshots(engagements, reads.sessions, reads.activity, reads.attention) : [], [engagements, reads]);

  return <section className={styles.page} aria-labelledby="shell-projects-title">
    <header className={styles.header}>
      <h1 id="shell-projects-title">Projects</h1>
      <button className="icon-button subtle" type="button" aria-label="Refresh projects" title="Refresh projects" disabled={!connected} onClick={() => pollRef.current?.poke()}><RefreshCw size={18} aria-hidden="true" /></button>
    </header>
    {!connected && <p className={styles.notice} role="status">Connect to Core to see project snapshots.</p>}
    {connected && !reads && <p className={styles.notice} role="status">Loading projects…</p>}
    {reads && reads.failed > 0 && <p className={styles.notice} role="alert">{reads.failed} {reads.failed === 1 ? "project" : "projects"} could not be read. Retrying.</p>}
    {error && <p className={styles.notice} role="alert">{error}</p>}
    {reads && !snapshots.length && reads.failed === 0 && <div className={styles.empty}><strong>No active projects with chats</strong><p>Active projects appear here once they have a conversation. Ask Butler to start one.</p></div>}
    <ul className={styles.list}>
      {snapshots.map(item => <li key={item.id}><ProjectCard item={item} now={now} onAskButler={() => navigate(shellButlerPath(item.id))} /></li>)}
    </ul>
    <ShellTabBar />
  </section>;
}

function ProjectCard({ item, now, onAskButler }: { item: ProjectSnapshot; now: number; onAskButler: () => void }) {
  return <details className={styles.card}>
    <summary className={styles.summary}>
      <span className={styles.avatar} aria-hidden="true">{(item.name.trim()[0] ?? "N").toUpperCase()}</span>
      <span className={styles.identity}>
        <strong>{item.name}</strong>
        <small>{item.chatCount} {item.chatCount === 1 ? "chat" : "chats"} · {relativeAge(item.lastActivityAt, now)}</small>
      </span>
      <span className={styles.badges}>
        {item.needsYou > 0 && <span className={styles.badge} data-tone="attention">{item.needsYou} need you</span>}
        {item.working > 0 && <span className={styles.badge} data-tone="working">{item.working} running</span>}
        {item.waiting > 0 && <span className={styles.badge} data-tone="waiting">{item.waiting} waiting</span>}
      </span>
    </summary>
    <div className={styles.detail}>
      {item.description && <p className={styles.description}>{item.description}</p>}
      <ul className={styles.chats} aria-label={`Recent chats in ${item.name}`}>
        {item.chats.map(chat => <li key={chat.id}>
          <span className={styles.dot} data-state={chat.state} aria-hidden="true" />
          <span className={styles.chatTitle}>{chat.title}</span>
          <small>{chat.state === "idle" ? relativeAge(chat.updatedAt, now) : chat.state === "working" ? "Running" : "Waiting"}</small>
        </li>)}
      </ul>
      <button className="button primary" type="button" onClick={onAskButler}>Ask Butler about {item.name}</button>
    </div>
  </details>;
}

/** Shown under docked Butler; visible only when Butler hid itself (for example, to show a setting). */
export function ShellButlerRoute() {
  const launcher = useContext(NebulaAssistantLauncherContext);
  return <section className={styles.page} aria-label="Butler">
    <div className={styles.empty}><strong>Butler is hidden</strong><p>Butler stepped aside to show something else.</p>
      {launcher && !launcher.visible && <button className="button primary" type="button" onClick={launcher.toggle}>Show Butler</button>}
    </div>
    <ShellTabBar />
  </section>;
}
