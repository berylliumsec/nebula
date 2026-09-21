import { useLayoutEffect, useRef } from "react";
import { ExternalLink, SquareArrowOutUpRight } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../../api/client";
import { projectSurface } from "../../resourceRoutes";
import { AgentViewBody, agentViewStatus, useAgentViewStream } from "./AgentViewBody";

interface ChatResultStreamProps {
  api: ApiClient;
  projectId: string;
  sessionId: string;
  /** Float the view over the conversation instead of keeping it here. */
  onPopOut?: () => void;
  /**
   * Set when the operator just docked the floating view here: focus follows
   * it to the control that undoes the move, then this is reported done.
   */
  focusPopOut?: boolean;
  onPopOutFocused?: () => void;
}

/**
 * The Agent view docked in the conversation's details: what the agent
 * published while working on this conversation, following the newest
 * snapshot as it arrives. The same view floats over the conversation when
 * popped out; see AgentViewPanel.
 */
export function ChatResultStream({ api, projectId, sessionId, onPopOut, focusPopOut = false, onPopOutFocused }: ChatResultStreamProps) {
  const stream = useAgentViewStream(api, projectId, sessionId);
  const popOut = useRef<HTMLButtonElement>(null);
  useLayoutEffect(() => {
    if (!focusPopOut || !popOut.current) return;
    popOut.current.focus();
    onPopOutFocused?.();
  }, [focusPopOut, onPopOutFocused]);

  return <section className="chat-result-stream" aria-label="Published results for this conversation">
    <header>
      <div>
        <h3>Agent view</h3>
        <p role="status">{agentViewStatus(stream)}</p>
      </div>
      <div className="chat-result-stream-actions">
        {onPopOut && <button ref={popOut} type="button" className="button quiet" onClick={onPopOut}>
          <SquareArrowOutUpRight size={14} aria-hidden="true" /> Pop out
        </button>}
        <Link className="button quiet" to={projectSurface(projectId, "results", stream.current)}>
          <ExternalLink size={14} aria-hidden="true" /> Open in Results
        </Link>
      </div>
    </header>

    <AgentViewBody stream={stream} />
    {stream.items.length > 0 && <button type="button" className="button quiet" onClick={stream.refresh}>Refresh now</button>}
  </section>;
}
