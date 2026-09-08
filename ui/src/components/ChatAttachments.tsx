import { Paperclip } from "lucide-react";
import { logCaughtDiagnostic } from "../diagnostics";
import { useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { KnowledgeSource, WorkspaceListing } from "../api/types";
import type { NebulaDraftRequest } from "../state/WorkbenchDraftContext";
import { ModalSurface } from "./DialogSystem";

type Preview = {text: string; truncated: boolean; label: string; sourceKind: string; sourceId: string};
export function ChatAttachments({api, projectId, onAttach, onImages, imagesEnabled}: {api: ApiClient; projectId: string; onAttach: (value: NebulaDraftRequest) => void; onImages: () => void; imagesEnabled: boolean}) {
  const [open, setOpen] = useState(false);
  const [listing, setListing] = useState<WorkspaceListing>();
  const [sources, setSources] = useState<KnowledgeSource[]>();
  const [preview, setPreview] = useState<Preview>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const input = useRef<HTMLInputElement>(null);
  const run = async (action: () => Promise<void>) => {setBusy(true); setError(undefined); try {await action();} catch(e) { void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat");setError(e instanceof Error ? e.message : "Could not read this attachment. Try another file.");} finally {setBusy(false);}};
  const browse = (path = "", offset = 0) => run(async () => {setSources(undefined); setPreview(undefined); setListing(await api.listWorkspace(projectId, path, offset));});
  const sourcePreview = async (source: KnowledgeSource) => {
    const value = await api.request<{text: string; truncated: boolean; label: string}>(`chat/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(source.id)}/preview`);
    setPreview({...value, sourceKind: "knowledge", sourceId: source.id});
  };
  return <><button className="button quiet chat-composer-icon" type="button" aria-label="Attach files" title="Attach files" onClick={() => setOpen(true)}><Paperclip size={18} aria-hidden="true" /></button>{open && <ModalSurface as="section" className="provider-dialog assistant-attachment-dialog" labelledBy="chat-attachment-title" onClose={() => setOpen(false)}><header><h2 id="chat-attachment-title">Attach to next message</h2><button type="button" className="button quiet" onClick={() => setOpen(false)}>Close attachments</button></header><p>Preview the exact text before attaching it. Documents upload to this project’s knowledge library.</p><nav>{imagesEnabled && <button type="button" className="button quiet" disabled={busy} onClick={() => {setOpen(false); onImages();}}>Images from this device</button>}<button type="button" className="button quiet" disabled={busy} onClick={() => input.current?.click()}>Document from this device</button><button type="button" className="button quiet" disabled={busy} onClick={() => void browse()}>Browse project files</button><button type="button" className="button quiet" disabled={busy} onClick={() => void run(async () => {setListing(undefined); setPreview(undefined); setSources((await api.listKnowledgeSources(projectId)).items);})}>Choose knowledge source</button></nav>
    <input ref={input} type="file" hidden accept=".txt,.md,.pdf,.docx,.xlsx,.csv,.json,.jsonl,.html,.py,.sh,.js,.ts,.tsx,.yaml,.yml,.toml,.xml,.log,.rst" onChange={event => {const file = event.target.files?.[0]; event.target.value = ""; if (!file) return; void run(async () => {if (file.size > 20 * 1024 * 1024) throw new Error("Choose a document smaller than 20 MiB."); const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ""; for(let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192)); const source = await api.ingestKnowledgeSource({engagementId: projectId, filename: file.name, mediaType: file.type || undefined, contentBase64: btoa(binary)}); await sourcePreview(source);});}} />
    {busy && <p role="status">Loading attachment…</p>}{error && <p role="alert">{error}</p>}
    {listing && <section><h3>Project host files: {listing.path || "/"}</h3>{listing.path && <button type="button" className="button quiet" onClick={() => void browse(listing.path.split("/").slice(0, -1).join("/"))}>Parent folder</button>}<ul>{listing.entries.map(entry => <li key={entry.path}><button type="button" className="button quiet" disabled={busy || !["file", "directory"].includes(entry.kind)} onClick={() => entry.kind === "directory" ? void browse(entry.path) : void run(async () => {const value = await api.previewWorkspaceFile(projectId, entry.path); setPreview({text: value.text, truncated: value.truncated, label: entry.path, sourceKind: "workspace_file", sourceId: entry.path});})}>{entry.name}{entry.kind === "directory" ? "/" : ""}</button></li>)}</ul>{!listing.entries.length && <p>This folder is empty.</p>}{listing.nextOffset !== undefined && <button type="button" onClick={() => void browse(listing.path, listing.nextOffset)}>More files</button>}</section>}
    {sources && <section><h3>Project knowledge</h3>{sources.length ? <ul>{sources.map(source => <li key={source.id}><button type="button" className="button quiet" disabled={busy} onClick={() => void run(() => sourcePreview(source))}>{source.name}</button></li>)}</ul> : <p>No sources yet. Upload a document above.</p>}</section>}
    {preview && <section><h3>{preview.label}</h3>{preview.truncated && <p>Bounded excerpt; the complete source is not included.</p>}<pre tabIndex={0}>{preview.text}</pre><button type="button" className="button primary" disabled={busy || !preview.text} onClick={() => {onAttach({text: preview.text, sourceKind: preview.sourceKind, sourceId: preview.sourceId, sourceLabel: preview.label, truncated: preview.truncated}); setOpen(false);}}>Attach this excerpt</button></section>}
  </ModalSurface>}</>;
}
