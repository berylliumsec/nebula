import { logCaughtDiagnostic } from "../diagnostics";
import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
interface Recorded {operator_decisions: {id: string; revision: number; text: string; kind: string}[]; message_id: string; sequence: number; attachments: {text: string; source_label: string; truncated: boolean}[]}
export function ChatRecordedContext({api, sessionId, onMessage}: {api: ApiClient; sessionId: string; onMessage: (id: string) => void}) {
  const [instructions, setInstructions] = useState<string>();
  const [note, setNote] = useState<string>();
  const [rows, setRows] = useState<Recorded[]>([]);
  const [offset, setOffset] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState<string>();
  useEffect(() => {const controller = new AbortController(); void api.request<{items: Recorded[]; next_offset: number | null; core_instructions?: string; instruction_note?: string}>(`chat/sessions/${encodeURIComponent(sessionId)}/context-sources?offset=${offset}`, {signal: controller.signal}).then(value => {if (!value || !Array.isArray(value.items)) throw new Error("Core did not return a recorded context page"); setRows(value.items); setNext(value.next_offset); setInstructions(value.core_instructions); setNote(value.instruction_note);}).catch(e => {void logCaughtDiagnostic("interface.assistant_chat.read_failed", "Assistant chat content could not be loaded.", e, "assistant_chat");if(!controller.signal.aborted) setError(e instanceof Error ? e.message : "Recorded context unavailable.");}); return () => controller.abort();}, [api, sessionId, offset]);
  return <section><h3>Recorded prior-turn context</h3>{instructions && <details><summary>Core assistant instructions</summary><pre>{instructions}</pre></details>}{note && <p>{note}</p>}{error && <p role="alert">{error}</p>}{!rows.length && <p>No selected excerpts recorded in these turns.</p>}{rows.map(row => <div key={row.message_id}><button className="button quiet" onClick={() => onMessage(row.message_id)}>Message {row.sequence}</button>{row.operator_decisions?.length > 0 && <details><summary>Operator context submitted with this turn</summary>{row.operator_decisions.map(item => <p key={item.id}>{item.kind} · revision {item.revision}: {item.text}</p>)}</details>}{row.attachments.map((attachment, index) => <details key={index}><summary>{attachment.source_label}{attachment.truncated ? " · excerpt" : ""}</summary><pre>{attachment.text}</pre></details>)}</div>)}{next !== null && <button className="button quiet" onClick={() => setOffset(next)}>Older context</button>}</section>;
}
