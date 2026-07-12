import { useMemo, useState } from "react";
import {
  Braces,
  MessageSquare,
  MoreHorizontal,
  Paperclip,
  Plus,
  Send,
  ShieldCheck,
  SquareTerminal,
} from "lucide-react";
import { ApiTerminalTransport } from "../api/terminal";
import { PageHeader } from "../components/PageHeader";
import { TerminalPanel } from "../components/TerminalPanel";
import { useWorkspace } from "../state/WorkspaceContext";

type SessionView = "terminal" | "chat";

export function SessionsPage() {
  const [view, setView] = useState<SessionView>("terminal");
  const { api, coreState, engagement, health, previewMode } = useWorkspace();
  const terminalTransport = useMemo(
    () => (api && coreState === "online" ? new ApiTerminalTransport(api.baseUrl, api.getToken()) : undefined),
    [api, coreState],
  );

  return (
    <div className="page sessions-page">
      <PageHeader
        eyebrow="Operator workspace"
        title="Sessions"
        description="Human-operated terminals and cited agent conversations stay separate from sandboxed tool calls."
        actions={<button className="button primary" type="button"><Plus size={16} /> New session</button>}
      />

      <div className="session-toolbar">
        <div className="session-tabs" role="tablist" aria-label="Session views">
          <button type="button" role="tab" aria-selected={view === "terminal"} onClick={() => setView("terminal")}>
            <SquareTerminal size={16} /> Human terminal
          </button>
          <button type="button" role="tab" aria-selected={view === "chat"} onClick={() => setView("chat")}>
            <MessageSquare size={16} /> Analyst chat
          </button>
        </div>
        <div className="session-scope"><ShieldCheck size={15} /> Human controlled · {engagement?.name ?? (previewMode ? "ACME-EXT preview" : "no engagement")}</div>
      </div>

      <div className="session-layout">
        <section className="session-workspace">
          {view === "terminal" ? (
            <TerminalPanel sessionId="human-session-01" transport={terminalTransport} />
          ) : (
            <div className="chat-panel">
              <div className="chat-scroll">
                {previewMode ? <>
                <article className="chat-message assistant">
                  <span className="chat-avatar">N</span>
                  <div>
                    <header><strong>Nebula assistant</strong><span>19:07</span></header>
                    <p>I found two prior observations relevant to the gateway. Both are cited below; no command was executed.</p>
                    <button className="citation-chip" type="button"><Braces size={13} /> Observation #184 · Nmap import</button>
                    <button className="citation-chip" type="button"><Braces size={13} /> Advisory · CVE record</button>
                  </div>
                </article>
                <article className="chat-message operator">
                  <span className="chat-avatar">JD</span>
                  <div>
                    <header><strong>You</strong><span>19:08</span></header>
                    <p>Summarize the applicability evidence and list what still needs independent verification.</p>
                  </div>
                </article>
                <div className="chat-thinking"><span /><span /><span /> Retrieving approved sources</div>
                </> : <div className="empty-state compact"><MessageSquare size={23} /><strong>No conversation loaded</strong><p>Analyst conversation history will appear when the Core chat resource is connected.</p></div>}
              </div>
              <form className="chat-composer" onSubmit={(event) => event.preventDefault()}>
                <label className="sr-only" htmlFor="analyst-message">Message the analyst assistant</label>
                <textarea id="analyst-message" placeholder="Ask about this engagement…" rows={3} />
                <footer>
                  <button className="icon-button subtle" type="button" aria-label="Attach evidence"><Paperclip size={17} /></button>
                  <span>Sources are cited · executable tools disabled in chat</span>
                  <button className="button primary square" type="submit" aria-label="Send message"><Send size={16} /></button>
                </footer>
              </form>
            </div>
          )}
        </section>

        <aside className="session-inspector" aria-label="Session inspector">
          <header><div><span>Context</span><strong>Session details</strong></div><button className="icon-button subtle" type="button" aria-label="More session actions"><MoreHorizontal size={17} /></button></header>
          <dl>
            <div><dt>Owner</dt><dd>{previewMode ? "Jordan Diaz" : "Current operator"}</dd></div>
            <div><dt>Started</dt><dd>{previewMode ? "18 minutes ago" : "No session selected"}</dd></div>
            <div><dt>Working directory</dt><dd><code>{previewMode ? "/workspace/acme-ext" : "Not mounted"}</code></dd></div>
            <div><dt>PTY runner</dt><dd><span className={`status-dot ${health?.runner === "ready" ? "healthy" : "unavailable"}`} /> {health?.runner ?? "Core unavailable"}</dd></div>
          </dl>
          <section>
            <h3>Scope guard</h3>
            <div className="scope-chip-list">{previewMode ? <><span>*.acme.test</span><span>10.42.16.0/24</span><span>TCP 80, 443, 8443</span></> : <span>Scope details are managed by Core</span>}</div>
          </section>
          <section>
            <h3>Session evidence</h3>
            <div className="empty-state mini"><Braces size={19} /><p>Captured output will be hashed and linked here.</p></div>
          </section>
        </aside>
      </div>
    </div>
  );
}
