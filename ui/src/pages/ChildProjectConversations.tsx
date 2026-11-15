import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../api/client";
import type { EngagementSummary, LinkedProjectChat } from "../api/types";
import { startVisiblePoll, type PollOutcome, type VisiblePoll } from "../api/visiblePoll";
import { logCaughtDiagnostic } from "../diagnostics";
import { resourcePath } from "../resourceRoutes";

const PAGE = 25;
// Core caps one read at 200 chats; a longer list is read in that many pages.
const READ_LIMIT = 200;
const POLL_MS = 5_000;
const IDLE_POLL_MS = 20_000;
const SEARCH_DELAY_MS = 250;

const GROUPS: Array<{ state: LinkedProjectChat["state"]; label: string }> = [
  { state: "waiting", label: "Needs you" },
  { state: "working", label: "Working" },
  { state: "idle", label: "Recent" },
];

function sameChats(left: LinkedProjectChat[], right: LinkedProjectChat[]): boolean {
  return left.length === right.length && left.every((item, index) => {
    const other = right[index];
    return item.session.id === other.session.id && item.state === other.state
      && item.session.revision === other.session.revision && item.project.name === other.project.name;
  });
}

/**
 * Chats owned by the projects linked under ``project``, in one list.
 *
 * Core pages the chats across every linked project, so a project with no
 * chats adds nothing and the list costs one bounded read however many
 * projects are linked. Chats needing the operator come first, then running
 * chats. Each chat still opens in its own project.
 */
export function ChildProjectConversations({
  api, project, query, visible = true, onOpen, onMatchesChange,
}: {
  api: ApiClient;
  project: EngagementSummary;
  query: string;
  visible?: boolean;
  onOpen?: () => void;
  onMatchesChange?: (hasMatches: boolean) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [items, setItems] = useState<LinkedProjectChat[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string>();
  const [windowSize, setWindowSize] = useState(PAGE);
  const [search, setSearch] = useState(query.trim());
  const etagRef = useRef<string | undefined>(undefined);
  const pollRef = useRef<VisiblePoll | undefined>(undefined);

  useEffect(() => {
    const next = query.trim();
    if (next === search) return;
    const timer = setTimeout(() => setSearch(next), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [query, search]);

  // A new project or search starts from the first page with no validator.
  useEffect(() => {
    etagRef.current = undefined;
    setWindowSize(PAGE);
    setItems([]);
    setHasMore(false);
    setLoaded(false);
    setError(undefined);
  }, [project.id, search]);

  const read = useCallback(async (signal: AbortSignal): Promise<PollOutcome> => {
    try {
      const collected: LinkedProjectChat[] = [];
      let offset: number | undefined = 0;
      let more = false;
      while (offset !== undefined && collected.length < windowSize) {
        const limit = Math.min(READ_LIMIT, windowSize - collected.length);
        // Only a single-page read can be answered "unchanged".
        const answer = await api.listLinkedProjectChatsIfChanged(
          project.id, { query: search, offset, limit }, windowSize <= READ_LIMIT ? etagRef.current : undefined, signal,
        );
        if (signal.aborted) return "stop";
        if (!answer) {
          setError(undefined);
          setLoaded(true);
          return "idle";
        }
        if (windowSize <= READ_LIMIT) etagRef.current = answer.etag;
        collected.push(...answer.page.items);
        offset = answer.page.nextOffset;
        more = offset !== undefined;
      }
      setItems(current => sameChats(current, collected) ? current : collected);
      setHasMore(more);
      setError(undefined);
      setLoaded(true);
      return collected.some(item => item.state === "working") ? "active" : "idle";
    } catch (caught) {
      if (signal.aborted) return "stop"; // diagnostic-expected: the list closed or the search changed
      void logCaughtDiagnostic("interface.child_project_conversations.list_failed", "Subproject chats could not be loaded.", caught, "child_project_conversations");
      setError(`Could not load subproject chats: ${caught instanceof Error ? caught.message : "Try again."}`);
      setLoaded(true);
      return "active";
    }
  }, [api, project.id, search, windowSize]);

  // Read only while the conversation list is on screen and this list is open.
  useEffect(() => {
    if (!visible || !expanded) return;
    const controller = new AbortController();
    const poll = startVisiblePoll({ intervalMs: POLL_MS, maxIntervalMs: IDLE_POLL_MS, signal: controller.signal, read });
    pollRef.current = poll;
    window.addEventListener("focus", poll.poke);
    return () => { controller.abort(); pollRef.current = undefined; window.removeEventListener("focus", poll.poke); };
  }, [visible, expanded, read]);

  const hasMatches = items.length > 0;
  useEffect(() => { onMatchesChange?.(hasMatches); }, [hasMatches, onMatchesChange]);
  useEffect(() => () => onMatchesChange?.(false), [onMatchesChange]);

  const groups = useMemo(() => GROUPS
    .map(group => ({ ...group, chats: items.filter(item => item.state === group.state) }))
    .filter(group => group.chats.length), [items]);

  const refresh = () => {
    etagRef.current = undefined;
    setExpanded(true);
    pollRef.current?.poke();
  };

  // Linked projects without chats show nothing: no empty folders.
  if (!loaded || (!items.length && !error)) return null;
  const countLabel = `${items.length}${hasMore ? "+" : ""}`;
  return <section className="session-list-group child-project-conversations" aria-label="Subproject conversations">
    <h3 className="child-project-heading"><button className="session-list-group-toggle" type="button" aria-label="Subproject chats" aria-expanded={expanded} onClick={() => setExpanded(current => !current)}>
        <ChevronDown size={12} className={expanded ? undefined : "collapsed"} aria-hidden="true" />
        <span className="child-project-name">Subprojects</span>
        {items.length > 0 && <span>{countLabel}</span>}
      </button><button className="child-project-refresh" type="button" aria-label="Refresh subproject chats" title="Refresh subproject chats" onClick={refresh}><RefreshCw size={14} aria-hidden="true" /></button></h3>
    {expanded && <>
      {error && <div className="child-project-status" role="alert">{error} <button type="button" onClick={refresh}>Retry</button></div>}
      {groups.map(group => <div className="child-project-group" role="group" aria-label={group.label} key={group.state}>
        {groups.length > 1 && <p className="child-project-group-label" aria-hidden="true">{group.label}</p>}
        {group.chats.map(({ session, project: owner, state }) => <div className="session-list-item child-project-chat" key={session.id}>
          <Link className="session-select" to={resourcePath(owner.id, "conversation", session.id)} onClick={onOpen} aria-label={`Open ${session.title} in ${owner.name}`}>
            <span className={`conversation-activity-marker ${state}`} aria-hidden="true" />
            <span><strong title={session.title}><span className="conversation-title-text">{session.title}</span></strong><small><span className="child-project-owner" title={owner.name}>{owner.name}</span> · {state === "waiting" ? "Waiting for you" : state === "working" ? "Working" : session.archivedAt ? "Archived" : session.model || "Saved conversation"}</small></span>
          </Link>
        </div>)}
      </div>)}
      {hasMore && <button className="child-project-more" type="button" onClick={() => setWindowSize(value => value + PAGE)}>Show more chats</button>}
    </>}
  </section>;
}
