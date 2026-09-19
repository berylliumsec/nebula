import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { RefreshCw, RotateCcw, Search, X } from "lucide-react";
import type { GuideProgress } from "../api/types";
import { useDialogPresence } from "../components/DialogSystem";
import { useGuides } from "./GuideProvider";
import { guideCategories, type GuideDefinition } from "./types";

const FOCUSABLE = "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function guideMatches(guide: GuideDefinition, query: string): boolean {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const haystack = `${guide.title} ${guide.summary} ${guide.keywords}`.toLowerCase();
  return terms.every(term => haystack.includes(term));
}

function statusLabel(guide: GuideDefinition, progress?: GuideProgress): { label: string; tone: "start" | "resume" | "done" } {
  if (progress?.status === "completed") return { label: "Done", tone: "done" };
  if (progress?.status === "in_progress") return { label: `${Math.min(progress.stepIndex + 1, guide.steps.length)} / ${guide.steps.length}`, tone: "resume" };
  return { label: "Start", tone: "start" };
}

export function GuidesDrawer({ onClose }: { onClose(): void }) {
  const { guides, page, progress, progressError, progressState, restart, retryProgress, start } = useGuides();
  const [query, setQuery] = useState("");
  const surfaceRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null);

  useDialogPresence(true);

  useEffect(() => {
    searchRef.current?.focus();
    const returnFocus = returnFocusRef.current;
    return () => { if (returnFocus?.isConnected) returnFocus.focus(); };
  }, []);

  const doneCount = guides.filter(guide => progress[guide.id]?.status === "completed").length;
  const sections = useMemo(() => {
    if (query.trim()) return [{ id: "results", label: "Results", items: guides.filter(guide => guideMatches(guide, query)) }];
    const here = guides.filter(guide => guide.pages.includes(page));
    return [
      { id: "page", label: "For this page", items: here },
      ...guideCategories.map(category => ({
        id: category.id,
        label: category.label,
        items: guides.filter(guide => guide.category === category.id && !here.includes(guide)),
      })),
    ].filter(section => section.items.length > 0);
  }, [guides, page, query]);

  const onKeyDown = (event: ReactKeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(surfaceRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };

  return createPortal(<div className="guides-drawer-layer" data-guide-layer>
    <button className="guides-drawer-scrim" type="button" tabIndex={-1} aria-label="Close guides" onClick={onClose} />
    <aside ref={surfaceRef} className="guides-drawer" role="dialog" aria-modal="true" aria-labelledby="guides-drawer-title" onKeyDown={onKeyDown}>
      <header className="guides-drawer-header">
        <h2 id="guides-drawer-title">Guides</h2>
        <span className="guides-drawer-count">{doneCount} of {guides.length} done</span>
        <button className="icon-button subtle" type="button" aria-label="Close guides" title="Close guides" onClick={onClose}><X size={18} aria-hidden="true" /></button>
      </header>
      <label className="guides-search">
        <Search size={15} aria-hidden="true" />
        <input ref={searchRef} type="search" value={query} placeholder="Search guides — try “hooks” or “phone”" aria-label="Search guides" onChange={event => setQuery(event.target.value)} />
      </label>
      {progressError && <div className="guides-progress-error" role="status">
        <span>{progressError}</span>
        {progressState === "failed" && <button className="icon-button subtle" type="button" aria-label="Retry loading guide progress" title="Retry" onClick={retryProgress}><RefreshCw size={15} aria-hidden="true" /></button>}
      </div>}
      <div className="guides-drawer-body">
        {sections.length === 0 && <p className="guides-empty" role="status">No guide matches “{query.trim()}”.</p>}
        {sections.map(section => <section key={section.id} className="guides-section" aria-labelledby={`guides-section-${section.id}`}>
          <h3 id={`guides-section-${section.id}`}>{section.label}</h3>
          <ul>
            {section.items.map(guide => {
              const saved = progress[guide.id];
              const status = statusLabel(guide, saved);
              return <li key={guide.id} className="guides-row">
                <button className="guides-row-main" type="button" onClick={() => start(guide.id)} aria-label={`${guide.title}. ${status.tone === "done" ? "Completed. Replay" : status.tone === "resume" ? `Resume at step ${status.label}` : "Start"}`}>
                  <span className="guides-row-text">
                    <strong>{guide.title}</strong>
                    <small>{guide.summary}</small>
                    <small className="guides-row-meta">{guide.steps.length} step{guide.steps.length === 1 ? "" : "s"}</small>
                  </span>
                  <span className={`guides-status ${status.tone}`}>{status.label}</span>
                </button>
                {saved && <button className="icon-button subtle" type="button" aria-label={`Restart ${guide.title}`} title="Restart from the first step" onClick={() => restart(guide.id)}><RotateCcw size={15} aria-hidden="true" /></button>}
              </li>;
            })}
          </ul>
        </section>)}
      </div>
    </aside>
  </div>, document.body);
}
