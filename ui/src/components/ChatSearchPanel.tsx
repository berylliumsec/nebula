import { ArrowRight, Bookmark, LoaderCircle, MessageSquare, Search } from "lucide-react";
import { logCaughtDiagnostic } from "../diagnostics";
import { useRef, useState } from "react";
import type { ChatSearchHit, ChatSearchPage } from "../pages/useChatNavigation";

export function ChatSearchPanel({search, onSelect}: {search: (q: string, bookmarked: boolean, currentOnly: boolean, offset?: number) => Promise<ChatSearchPage>; onSelect: (hit: ChatSearchHit) => void}) {
  const [query, setQuery] = useState("");
  const [bookmarked, setBookmarked] = useState(false);
  const [currentOnly, setCurrentOnly] = useState(true);
  const [page, setPage] = useState<ChatSearchPage>();
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const generation = useRef(0);
  const run = async (offset = 0) => {
    const request = ++generation.current;
    setBusy(true); setError(undefined);
    try { const result = await search(query, bookmarked, currentOnly, offset); if (generation.current === request) {setPage(result); setIndex(0);} }
    catch (e) { void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat"); if (generation.current === request) setError(e instanceof Error ? e.message : "Search failed. Try again."); }
    finally { if (generation.current === request) setBusy(false); }
  };
  const jump = (next: number) => { if (page?.items[next]) { setIndex(next); onSelect(page.items[next]); } };
  return <details className="assistant-search"><summary aria-label="Search messages and bookmarks" title="Search messages and bookmarks"><Search size={18} aria-hidden="true" /></summary><form onSubmit={event => {event.preventDefault(); void run();}}>
    <div className="assistant-search-field">
      <label className="assistant-search-input"><span className="sr-only">Search transcript</span><input type="search" placeholder="Find a message…" value={query} maxLength={512} onChange={event => setQuery(event.target.value)} /></label>
      <button className="icon-button subtle" disabled={busy} type="submit" aria-label={busy ? "Searching…" : "Search messages"} title={busy ? "Searching…" : "Search messages"}>
        {busy ? <LoaderCircle className="spin" size={17} aria-hidden="true" /> : <ArrowRight size={17} aria-hidden="true" />}
      </button>
    </div>
    <div className="assistant-search-filters" role="group" aria-label="Search filters">
      <label className="assistant-search-filter"><input type="checkbox" checked={currentOnly} onChange={event => setCurrentOnly(event.target.checked)} /><MessageSquare size={14} aria-hidden="true" /><span>This conversation</span></label>
      <label className="assistant-search-filter"><input type="checkbox" checked={bookmarked} onChange={event => setBookmarked(event.target.checked)} /><Bookmark size={14} aria-hidden="true" /><span>Bookmarks only</span></label>
    </div>
  </form>{error && <p role="alert">{error}</p>}{page && <div className="assistant-search-results">{page.items.length > 0 && <div><button className="button quiet" disabled={!page.items.length || index === 0} onClick={() => jump(index - 1)}>Previous match</button><button className="button quiet" disabled={index >= page.items.length - 1} onClick={() => jump(index + 1)}>Next match</button></div>}{!page.items.length && <p role="status">No matching messages.</p>}<ol>{page.items.map((hit, i) => <li key={hit.message_id}><button className="button quiet" aria-current={i === index ? "true" : undefined} onClick={() => jump(i)}><span><strong>{hit.title} · {hit.role}</strong><span>{hit.excerpt}</span></span></button></li>)}</ol>{page.next_offset !== null && <button className="button quiet" onClick={() => void run(page.next_offset!)}>More matches</button>}</div>}</details>;
}
