import { useEffect, useRef, useState, type FormEvent } from "react";
import { Clipboard, Copy, FilePenLine, Trash2, X } from "lucide-react";
import type { WorkspaceEntry } from "../api/types";

export interface WorkspaceEntryMenuState {
  entry: WorkspaceEntry;
  x: number;
  y: number;
}

interface Props {
  menu: WorkspaceEntryMenuState;
  onClose(): void;
  onCopyPath(entry: WorkspaceEntry): Promise<void>;
  onCopyContents(entry: WorkspaceEntry): Promise<void>;
  onRename(entry: WorkspaceEntry, newName: string): Promise<void>;
  onDelete(entry: WorkspaceEntry): Promise<void>;
}

export function WorkspaceEntryContextMenu({ menu, onClose, onCopyPath, onCopyContents, onRename, onDelete }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(menu.entry.name);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) onClose();
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [onClose]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try { await action(); onClose(); }
    finally { setBusy(false); }
  };

  const rename = async (event: FormEvent) => {
    event.preventDefault();
    const next = name.trim();
    if (!next || next === menu.entry.name) return;
    await run(() => onRename(menu.entry, next));
  };

  const left = Math.min(menu.x || 12, globalThis.innerWidth - 250);
  const top = Math.min(menu.y || 12, globalThis.innerHeight - (renaming ? 230 : 205));
  return <div ref={rootRef} className="workspace-entry-menu" role="menu" aria-label={`Actions for ${menu.entry.name}`} style={{ left: Math.max(8, left), top: Math.max(8, top) }}>
    <header><span><strong>{menu.entry.name}</strong><small>/workspace/{menu.entry.path}</small></span><button type="button" aria-label="Close file actions" onClick={onClose}><X size={13} /></button></header>
    {renaming ? <form onSubmit={(event) => void rename(event)}><label>New name<input autoFocus maxLength={255} value={name} onChange={(event) => setName(event.target.value)} /></label><footer><button type="button" onClick={() => setRenaming(false)}>Cancel</button><button className="primary" type="submit" disabled={busy || !name.trim() || name.trim() === menu.entry.name}>Rename</button></footer></form> : <>
      <button role="menuitem" type="button" disabled={busy} onClick={() => void run(() => onCopyPath(menu.entry))}><Clipboard size={14} /> Copy path</button>
      <button role="menuitem" type="button" disabled={busy || menu.entry.kind !== "file"} onClick={() => void run(() => onCopyContents(menu.entry))}><Copy size={14} /> Copy file contents</button>
      <button role="menuitem" type="button" disabled={busy || menu.entry.kind === "other"} onClick={() => setRenaming(true)}><FilePenLine size={14} /> Rename</button>
      <div className="separator" />
      <button className="danger" role="menuitem" type="button" disabled={busy || menu.entry.kind === "other"} onClick={() => void run(() => onDelete(menu.entry))}><Trash2 size={14} /> Delete{menu.entry.kind === "directory" ? " empty folder" : ""}</button>
    </>}
  </div>;
}
