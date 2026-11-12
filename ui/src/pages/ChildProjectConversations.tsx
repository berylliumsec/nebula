import { useEffect, useMemo, useState } from "react";
import { ChevronDown, FolderOpen, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../api/client";
import type { ChatSessionActivity, ChatSessionSummary, EngagementSummary } from "../api/types";
import { resourcePath } from "../resourceRoutes";
import { conversationMatchesSearch } from "./chatBulkActions";

const INITIAL_VISIBLE = 8;
const MORE_VISIBLE = 20;

/** A child chat stays owned by its project; the parent only links to it. */
export function ChildProjectConversations({
  api, project, query, onOpen,
}: {
  api: ApiClient;
  project: EngagementSummary;
  query: string;
  onOpen?: () => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [activity, setActivity] = useState<Record<string, ChatSessionActivity["state"]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [reload, setReload] = useState(0);
  const [visibleCount, setVisibleCount] = useState(INITIAL_VISIBLE);

  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    void api.listChatSessions(project.id, controller.signal, true)
      .then(page => {
        if (!controller.signal.aborted) setSessions(page.items.filter(session => session.engagementId === project.id && !session.isSideChat));
      })
      .catch(caught => {
        if (!controller.signal.aborted) setError(`Could not load chats: ${caught instanceof Error ? caught.message : "Try again."}`);
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    void api.listChatSessionActivity(project.id, controller.signal)
      .then(items => {
        if (!controller.signal.aborted) setActivity(Object.fromEntries(items.map(item => [item.sessionId, item.state])));
      })
      .catch(() => { /* Chat links remain available when activity is unavailable. */ });
    return () => controller.abort();
  }, [api, project.id, expanded, reload]);

  useEffect(() => setVisibleCount(INITIAL_VISIBLE), [project.id, query]);

  const matches = useMemo(() => sessions.filter(session => conversationMatchesSearch(session, query)), [sessions, query]);
  const shown = matches.slice(0, visibleCount);
  return <section className="session-list-group child-project-conversations" aria-label={`${project.name} conversations`}>
    <h3 className="child-project-heading"><button className="session-list-group-toggle" type="button" aria-expanded={expanded} onClick={() => setExpanded(current => !current)}>
        <ChevronDown size={12} className={expanded ? undefined : "collapsed"} aria-hidden="true" />
        <FolderOpen size={12} aria-hidden="true" />
        <span className="child-project-name" title={project.name}>{project.name}</span>
        {!loading && !error && <span>{sessions.length}</span>}
      </button><button className="child-project-refresh" type="button" aria-label={`Refresh ${project.name} chats`} title={`Refresh ${project.name} chats`} onClick={() => { setExpanded(true); setReload(value => value + 1); }}><RefreshCw size={14} aria-hidden="true" /></button></h3>
    {expanded && <>
      {error && <div className="child-project-status" role="alert">{error} <button type="button" onClick={() => setReload(value => value + 1)}>Retry</button></div>}
      {loading && <p className="child-project-status" role="status">Loading chats…</p>}
      {!loading && !error && shown.map(session => {
        const state = activity[session.id] ?? "idle";
        return <div className="session-list-item child-project-chat" key={session.id}>
          <Link className="session-select" to={resourcePath(project.id, "conversation", session.id)} onClick={onOpen} aria-label={`Open ${session.title} in ${project.name}`}>
            <span className={`conversation-activity-marker ${state}`} aria-hidden="true" />
            <span><strong title={session.title}><span className="conversation-title-text">{session.title}</span></strong><small>{session.archivedAt ? "Archived" : state === "working" ? "Working" : state === "waiting" ? "Waiting for you" : session.model || "Saved conversation"}</small></span>
          </Link>
        </div>;
      })}
      {!loading && !error && !matches.length && <p className="child-project-status">{query ? "No matching chats" : "No saved chats"}</p>}
      {!loading && !error && shown.length < matches.length && <button className="child-project-more" type="button" onClick={() => setVisibleCount(value => value + MORE_VISIBLE)}>Show more chats ({matches.length - shown.length})</button>}
    </>}
  </section>;
}
