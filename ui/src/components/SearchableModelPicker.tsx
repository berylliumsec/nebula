import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";

export interface ModelPickerOption {
  id: string;
  label: string;
  searchText?: string | null;
  group: "allowed" | "more" | "saved";
}

export function SearchableModelPicker({ options, value, providerName, disabled, busy, onSelect }: {
  options: ModelPickerOption[];
  value: string;
  providerName: string;
  disabled?: boolean;
  busy?: boolean;
  onSelect(value: string): void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const matches = options.filter(option => `${option.label} ${option.id} ${option.searchText ?? ""}`.toLocaleLowerCase().includes(normalizedQuery));
  const ordered = normalizedQuery ? matches : [
    ...matches.filter(option => option.id === value),
    ...matches.filter(option => option.id !== value),
  ];
  const visible = ordered.slice(0, 80);
  const selected = options.find(option => option.id === value);
  const active = visible[Math.min(activeIndex, visible.length - 1)];

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open || !active) return;
    document.getElementById(`${listId}-${visible.indexOf(active)}`)?.scrollIntoView?.({ block: "nearest" });
  }, [open, active?.id, activeIndex, listId]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  const choose = (id: string) => {
    setOpen(false);
    setQuery("");
    setActiveIndex(0);
    onSelect(id);
    triggerRef.current?.focus();
  };

  return <div ref={rootRef} className="chat-model-picker">
    <button ref={triggerRef} type="button" className="chat-model-picker-trigger" aria-label={`Chat model: ${selected?.label ?? (value || "Select model")}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined} aria-busy={busy} disabled={disabled} onClick={() => { setQuery(""); setActiveIndex(0); setOpen(previous => !previous); }} onKeyDown={(event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); }
    }}><span title={selected?.label ?? value}>{selected?.label ?? (value || "Select model")}</span><ChevronDown size={14} aria-hidden="true" /></button>
    {open && <div className="chat-model-picker-panel">
      <div className="chat-model-picker-search"><Search size={15} aria-hidden="true" /><input ref={inputRef} type="search" role="combobox" aria-label="Search models" aria-autocomplete="list" aria-expanded="true" aria-controls={listId} aria-activedescendant={active ? `${listId}-${visible.indexOf(active)}` : undefined} placeholder="Search name or model ID" value={query} onChange={event => { setQuery(event.target.value); setActiveIndex(0); }} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); setOpen(false); triggerRef.current?.focus(); }
        if (event.key === "Tab") setOpen(false);
        if (event.key === "ArrowDown") { event.preventDefault(); setActiveIndex(index => Math.min(index + 1, Math.max(visible.length - 1, 0))); }
        if (event.key === "ArrowUp") { event.preventDefault(); setActiveIndex(index => Math.max(index - 1, 0)); }
        if (event.key === "Enter" && active) { event.preventDefault(); choose(active.id); }
      }} /></div>
      {normalizedQuery && <small className="chat-model-picker-count" role="status">{ordered.length} matching model{ordered.length === 1 ? "" : "s"}</small>}
      <div id={listId} className="chat-model-picker-list" role="listbox" aria-label="Models">
        {visible.map((option, index) => <button id={`${listId}-${index}`} key={option.id} type="button" role="option" className="chat-model-picker-option" aria-selected={option.id === value} data-active={index === activeIndex} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(option.id)}><span>{option.label}<small>{option.group === "more" ? `Add to ${providerName}'s allowed models` : option.group === "saved" ? "Saved model" : "Allowed model"}</small></span>{option.id === value && <Check size={15} aria-hidden="true" />}</button>)}
        {!visible.length && <p className="chat-model-picker-empty">No matching models</p>}
      </div>
      {ordered.length > visible.length && <small className="chat-model-picker-count">Showing {visible.length} of {ordered.length} models. Refine the search to see more.</small>}
    </div>}
  </div>;
}
