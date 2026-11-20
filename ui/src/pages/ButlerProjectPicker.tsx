import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";
import { filterProjectPicker } from "./butlerThreads";
import styles from "./ButlerProjectPicker.module.css";

type PickerProject = { id: string; name: string };

/**
 * Butler mode's project thread picker: a searchable list of every project,
 * with projects whose chats need the operator or are working first. Follows
 * the chat model picker's combobox pattern (search inside the open panel,
 * ↑/↓ to move, Enter to choose, Esc to close).
 */
export function ButlerProjectPicker<T extends PickerProject>({ groups, value, onSelect }: {
  groups: { active: readonly { project: T; label: string }[]; quiet: readonly T[] };
  value?: string;
  onSelect(projectId: string): void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const { options, total } = filterProjectPicker(groups, query);
  const active = options[Math.min(activeIndex, options.length - 1)];
  const selected = value ? [...groups.active.map((item) => item.project), ...groups.quiet].find((project) => project.id === value) : undefined;
  const optionId = (index: number) => `${listId}-${index}`;
  const projectCount = groups.active.length + groups.quiet.length;

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);

  useEffect(() => {
    if (!open || !active) return;
    document.getElementById(optionId(options.indexOf(active)))?.scrollIntoView?.({ block: "nearest" });
  }, [open, active?.project.id, activeIndex]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => { if (!rootRef.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  const show = (seed = "") => { setQuery(seed); setActiveIndex(0); setOpen(true); };
  const close = () => { setOpen(false); triggerRef.current?.focus(); };
  const choose = (projectId: string) => { close(); setQuery(""); onSelect(projectId); };

  return <div ref={rootRef} className={styles.picker}>
    <button ref={triggerRef} type="button" className={styles.trigger} aria-label={`Project thread: ${selected?.name ?? "choose a project"}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined}
      onClick={() => open ? setOpen(false) : show()}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); show(); }
        // Typing on the closed picker starts the search with that letter.
        else if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey && event.key.trim()) { event.preventDefault(); show(event.key); }
      }}>
      <span data-placeholder={selected ? undefined : "true"}>{selected?.name ?? "Choose a project…"}</span><ChevronDown size={15} aria-hidden="true" />
    </button>
    {open && <div className={styles.panel}>
      <div className={styles.search}>
        <Search size={15} aria-hidden="true" />
        <input ref={inputRef} type="search" role="combobox" aria-label="Search projects" aria-autocomplete="list" aria-expanded="true" aria-controls={listId}
          aria-activedescendant={active ? optionId(options.indexOf(active)) : undefined} placeholder="Search projects" value={query}
          onChange={(event) => { setQuery(event.target.value); setActiveIndex(0); }}
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); close(); }
            else if (event.key === "Tab") setOpen(false);
            else if (event.key === "ArrowDown") { event.preventDefault(); setActiveIndex((index) => Math.min(index + 1, Math.max(options.length - 1, 0))); }
            else if (event.key === "ArrowUp") { event.preventDefault(); setActiveIndex((index) => Math.max(index - 1, 0)); }
            else if (event.key === "Enter" && active) { event.preventDefault(); choose(active.project.id); }
          }} />
        {query.trim() && <small className={styles.count} role="status">{total.toLocaleString()} of {projectCount.toLocaleString()}</small>}
      </div>
      <div id={listId} className={styles.list} role="listbox" aria-label="Projects">
        {(["active", "quiet"] as const).map((group) => options.some((option) => option.group === group) && <div key={group} role="group" aria-labelledby={`${listId}-${group}`}>
          <div id={`${listId}-${group}`} className={styles.heading}>{group === "active" ? "Active" : "Quiet"}</div>
          {options.map((option, index) => option.group === group && <button key={option.project.id} id={optionId(index)} type="button" role="option" tabIndex={-1} className={styles.option} aria-selected={option.project.id === value} data-active={index === activeIndex ? "true" : undefined}
            onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(option.project.id)}>
            {option.label && <i className={styles.dot} data-state={option.label.endsWith("need you") ? "waiting" : "working"} aria-hidden="true" />}
            <span className={styles.name}>{option.project.name}</span>
            {option.label && <span className={styles.meta}>{option.label}</span>}
            {option.project.id === value && <Check size={14} aria-hidden="true" />}
          </button>)}
        </div>)}
        {!options.length && <p className={styles.empty}>No projects match “{query.trim()}”.</p>}
      </div>
      {total > options.length && <small className={styles.more}>Showing {options.length} of {total.toLocaleString()} projects. Keep typing to narrow.</small>}
    </div>}
  </div>;
}
