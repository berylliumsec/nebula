import { useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, ChevronRight, Folder, FolderOpen, FolderPlus, LoaderCircle, Server, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import { logCaughtDiagnostic } from "../diagnostics";
import { ModalSurface } from "./DialogSystem";

interface FolderListing {
  path: string;
  parent?: string;
  directories: Array<{ name: string; path: string }>;
  truncated: boolean;
  nextOffset?: number;
}

export function HostFolderPicker({ api, value, onSelect }: {
  api?: ApiClient;
  value?: string;
  onSelect: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [listing, setListing] = useState<FolderListing>();
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string>();
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [createError, setCreateError] = useState<string>();
  const [loadMoreError, setLoadMoreError] = useState<string>();
  const browseButtonRef = useRef<HTMLButtonElement>(null);
  const folderListRef = useRef<HTMLDivElement>(null);
  const newFolderInputRef = useRef<HTMLInputElement>(null);
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const retryLoadMoreButtonRef = useRef<HTMLButtonElement>(null);
  const selectButtonRef = useRef<HTMLButtonElement>(null);

  const load = async (path?: string, moveFocus = false) => {
    if (!api) return;
    if (moveFocus) {
      setNewFolderOpen(false);
      setNewFolderName("");
      setCreateError(undefined);
    }
    setLoading(true);
    setError(undefined);
    setLoadMoreError(undefined);
    try {
      setListing(await api.listHostWorkspaceFolders(path));
      if (moveFocus) requestAnimationFrame(() => {
        const firstFolder = folderListRef.current?.querySelector<HTMLButtonElement>("button");
        if (firstFolder) firstFolder.focus();
        else selectButtonRef.current?.focus();
      });
    } catch (caught) {
      logCaughtDiagnostic("interface.host_folder.list_failed", "A host workspace folder could not be listed.", caught, "host-folder-picker");
      setError(caught instanceof Error ? caught.message : "Folder could not be listed.");
      requestAnimationFrame(() => retryButtonRef.current?.focus());
    } finally {
      setLoading(false);
    }
  };

  const loadMore = async () => {
    if (!api || !listing?.truncated || listing.nextOffset === undefined) return;
    const { path, nextOffset } = listing;
    setLoadingMore(true);
    setLoadMoreError(undefined);
    try {
      const page = await api.listHostWorkspaceFolders(path, nextOffset);
      setListing((current) => {
        if (!current || current.path !== page.path) return current;
        const known = new Set(current.directories.map((directory) => directory.path));
        return {
          ...page,
          directories: [...current.directories, ...page.directories.filter((directory) => !known.has(directory.path))],
        };
      });
    } catch (caught) {
      logCaughtDiagnostic("interface.host_folder.page_failed", "More host workspace folders could not be listed.", caught, "host-folder-picker");
      setLoadMoreError(caught instanceof Error ? caught.message : "More folders could not be listed.");
      requestAnimationFrame(() => retryLoadMoreButtonRef.current?.focus());
    } finally {
      setLoadingMore(false);
    }
  };

  const openBrowser = () => {
    setOpen(true);
    void load(value?.trim() || listing?.path);
  };

  const closeBrowser = () => {
    setOpen(false);
    setError(undefined);
    setNewFolderOpen(false);
    setNewFolderName("");
    setCreateError(undefined);
    setLoadMoreError(undefined);
    requestAnimationFrame(() => browseButtonRef.current?.focus());
  };

  const beginCreateFolder = () => {
    setNewFolderOpen(true);
    setCreateError(undefined);
    requestAnimationFrame(() => newFolderInputRef.current?.focus());
  };

  const cancelCreateFolder = () => {
    setNewFolderOpen(false);
    setNewFolderName("");
    setCreateError(undefined);
  };

  const createFolder = async (event: FormEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (!api || !listing || creatingFolder || !newFolderName.trim()) return;
    setCreatingFolder(true);
    setCreateError(undefined);
    try {
      const created = await api.createHostWorkspaceFolder(listing.path, newFolderName);
      setListing(created);
      setNewFolderOpen(false);
      setNewFolderName("");
      requestAnimationFrame(() => selectButtonRef.current?.focus());
    } catch (caught) {
      logCaughtDiagnostic("interface.host_folder.create_failed", "A host workspace folder could not be created.", caught, "host-folder-picker");
      setCreateError(caught instanceof Error ? caught.message : "Folder could not be created.");
      requestAnimationFrame(() => newFolderInputRef.current?.focus());
    } finally {
      setCreatingFolder(false);
    }
  };

  const selectCurrentFolder = () => {
    if (!listing || listing.path === "/") return;
    onSelect(listing.path);
    closeBrowser();
  };

  return <div className="host-folder-picker">
    <button ref={browseButtonRef} className="button secondary" type="button" disabled={!api || loading} onClick={openBrowser}>
      {loading ? <LoaderCircle className="spin" size={14} /> : <FolderOpen size={14} />} Browse folders
    </button>
    {!open && error && <small role="alert">{error}</small>}
    {open && createPortal(
      <ModalSurface labelledBy="host-folder-dialog-title" className="host-folder-dialog" onClose={closeBrowser}>
        <header>
          <div>
            <span className="host-folder-dialog-kicker"><Server size={14} /> Nebula host</span>
            <h2 id="host-folder-dialog-title">Choose project folder</h2>
            <p>This folder becomes the shared working directory for Grok, Codex, and Kali.</p>
          </div>
          <button className="icon-button subtle" type="button" aria-label="Close folder browser" onClick={closeBrowser}><X size={18} /></button>
        </header>
        <div className="host-folder-location">
          <span>Current folder</span>
          <code title={listing?.path}>{(listing?.path ?? value?.trim()) || "Loading home folder…"}</code>
        </div>
        <div className="host-folder-create">
          {newFolderOpen ? <form onSubmit={(event) => void createFolder(event)}>
            <label><span>New folder name</span><input ref={newFolderInputRef} required maxLength={255} value={newFolderName} onChange={(event) => { setNewFolderName(event.target.value); if (createError) setCreateError(undefined); }} /></label>
            <button className="button quiet" type="button" disabled={creatingFolder} onClick={cancelCreateFolder}>Cancel</button>
            <button className="button primary" type="submit" disabled={creatingFolder || !newFolderName.trim()}>{creatingFolder ? <LoaderCircle className="spin" size={14} /> : <FolderPlus size={14} />} {creatingFolder ? "Creating…" : "Create folder"}</button>
          </form> : <button className="button secondary" type="button" disabled={!listing || loading} onClick={beginCreateFolder}><FolderPlus size={14} /> New folder</button>}
          {createError && <small role="alert">{createError}</small>}
        </div>
        <div ref={folderListRef} className="host-folder-list" aria-busy={loading}>
          {loading && <div className="host-folder-state" role="status"><LoaderCircle className="spin" size={20} /><span>Loading folders…</span></div>}
          {!loading && error && <div className="host-folder-state error" role="alert"><strong>Folder unavailable</strong><span>{error}</span><button ref={retryButtonRef} className="button secondary" type="button" onClick={() => void load(listing?.path, true)}>Try again</button></div>}
          {!loading && !error && listing?.directories.map((directory) => <button type="button" key={directory.path} title={directory.path} onClick={() => void load(directory.path, true)}><Folder size={18} /><span>{directory.name}</span><ChevronRight size={16} /></button>)}
          {!loading && !error && listing && !listing.directories.length && <div className="host-folder-state"><FolderOpen size={20} /><span>This folder has no child folders.</span></div>}
        </div>
        {!loading && !error && listing?.truncated && <div className={`host-folder-truncated${loadMoreError ? " error" : ""}`}>
          {loadMoreError
            ? <><span role="alert">More folders could not be loaded. {loadMoreError}</span><button ref={retryLoadMoreButtonRef} className="button quiet" type="button" aria-label="Try loading more again" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? <LoaderCircle className="spin" size={14} /> : null} Try again</button></>
            : <><span>{listing.directories.length.toLocaleString()} folders shown.</span><button className="button quiet" type="button" aria-label="Load more folders" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? <LoaderCircle className="spin" size={14} /> : null} {loadingMore ? "Loading more…" : "Load more"}</button></>}
        </div>}
        <footer>
          <button className="button secondary" type="button" disabled={!listing?.parent || loading} onClick={() => void load(listing?.parent, true)}><ArrowUp size={16} /> Up one level</button>
          <span>{listing?.path === "/" ? "The filesystem root cannot be used as a project folder." : "Select the folder shown above."}</span>
          <button ref={selectButtonRef} className="button primary" type="button" disabled={!listing || listing.path === "/" || loading} onClick={selectCurrentFolder}>Select folder</button>
        </footer>
      </ModalSurface>,
      document.body,
    )}
  </div>;
}
