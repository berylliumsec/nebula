import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ToolArtifactReference } from "../api/types";
import type { NebulaDraftRequest } from "../state/WorkbenchDraftContext";
import { useConfirmation } from "./DialogSystem";

function ResultImage({api, id}: {api: ApiClient; id: string}) {
  const [url, setUrl] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {const controller = new AbortController(); let local: string | undefined; void api.fetchChatImagePreview(id, controller.signal).then(blob => {if(!controller.signal.aborted) {local = URL.createObjectURL(blob); setUrl(local);}}).catch(() => {if(!controller.signal.aborted) setFailed(true);}); return () => {controller.abort(); if(local) URL.revokeObjectURL(local);};}, [api, id]);
  return failed ? <p>Image preview unavailable; its retained reference remains linked to the source message.</p> : url ? <img style={{maxWidth: "100%"}} src={url} alt="Retained assistant output" /> : <p>Loading image…</p>;
}
export interface ChatResult {id: string; message_id: string; kind: string; label: string; text?: string; artifact_id?: string; tool_call_id?: string; source_id?: string}
export function ChatResults({api, sessionId, onMessage, onAttach}: {api: ApiClient; sessionId: string; onMessage: (id: string) => void; onAttach: (value: NebulaDraftRequest) => void}) {
  const [items, setItems] = useState<ChatResult[]>([]);
  const [offset, setOffset] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [artifacts, setArtifacts] = useState<ToolArtifactReference[]>([]);
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<ChatResult>();
  const previewRef = useRef<HTMLPreElement>(null);
  const confirm = useConfirmation();
  useEffect(() => {const controller = new AbortController(); setBusy(true); setError(undefined); void api.request<{items: ChatResult[]; next_offset: number | null}>(`chat/sessions/${encodeURIComponent(sessionId)}/results?offset=${offset}`, {signal: controller.signal}).then(value => {setItems(value.items); setNext(value.next_offset);}).catch(e => {if(!controller.signal.aborted) setError(e instanceof Error ? e.message : "Results unavailable.");}).finally(() => {if(!controller.signal.aborted) setBusy(false);}); return () => controller.abort();}, [api, sessionId, offset]);
  const run = async (action: () => Promise<void>) => {setError(undefined); setBusy(true); try {await action();} catch(e) {setError(e instanceof Error ? e.message : "Preview unavailable. The source message remains available.");} finally {setBusy(false);}};
  const inspect = (item: ChatResult) => run(async () => {setSelected(item); setText(item.text ?? ""); setArtifacts([]); if(item.tool_call_id) setArtifacts(await api.listToolCallArtifacts(item.tool_call_id));});
  return <section aria-label="Conversation results"><h3>Retained results</h3>{busy && <p role="status">Loading results…</p>}{error && <p role="alert">{error}</p>}{!busy && !items.length && <p>No retained outputs in these turns.</p>}<ul>{items.map(item => <li key={item.id}><button className="button quiet" onClick={() => void inspect(item)}>{item.label} · {item.kind}</button><button className="button quiet" onClick={() => onMessage(item.message_id)}>Source message</button></li>)}</ul><nav>{offset > 0 && <button className="button quiet" onClick={() => setOffset(Math.max(0, offset - 40))}>Previous turns</button>}{next !== null && <button className="button quiet" onClick={() => setOffset(next)}>More turns</button>}</nav>
  {selected && <section><h3>{selected.label}</h3>{selected.kind === "image" && selected.artifact_id && <ResultImage key={selected.artifact_id} api={api} id={selected.artifact_id} />}{selected.artifact_id && !selected.tool_call_id && !selected.text && <p>The artifact reference is retained. Open its source message for the available preview.</p>}{artifacts.map(artifact => <div key={artifact.artifactId}><strong>{artifact.filename ?? artifact.kind}</strong>{artifact.searchable && <button className="button quiet" onClick={() => void run(async () => {const result = await api.readToolOutput(artifact.artifactId, 1); setText(result.lines.map(line => line.text).join("\n"));})}>Read bounded excerpt</button>}<button className="button quiet" onClick={() => void run(async () => {if (!await confirm({title: "Save raw artifact?", message: "Raw tool output may contain sensitive or untrusted data. Save the retained original?", confirmLabel: "Save original"})) return; const result = await api.downloadToolArtifact(artifact.artifactId); const url = URL.createObjectURL(result.blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = result.filename ?? artifact.filename ?? "artifact"; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);})}>Save original</button></div>)}{text && <><pre ref={previewRef} tabIndex={0}>{text}</pre><button className="button quiet" onClick={() => {const selection = window.getSelection(); const selectedText = selection && previewRef.current?.contains(selection.anchorNode) && previewRef.current.contains(selection.focusNode) ? selection.toString() : ""; onAttach({text: selectedText || text, sourceKind: "assistant_message", sourceId: selected.message_id, sourceLabel: selected.label});}}>Add selection or excerpt to next message</button></>}</section>}</section>;
}
