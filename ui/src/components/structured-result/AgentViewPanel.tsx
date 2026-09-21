import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { ExternalLink, GripHorizontal, Minimize2, MoveDiagonal2, Sparkles, X } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../../api/client";
import { projectSurface } from "../../resourceRoutes";
import { IconAction } from "../IconAction";
import { AgentViewBody, agentViewStatus, useAgentViewStream } from "./AgentViewBody";
import {
  arrowDirection,
  clampPoint,
  clampRect,
  currentViewport,
  readLauncher,
  readRect,
  STEP,
  writeLauncher,
  writeRect,
  type AgentViewPoint,
  type AgentViewRect,
} from "./agentViewGeometry";

/** Below this width the view is a sheet: there is no room to float beside. */
const SHEET_QUERY = "(max-width: 760px)";

function useSheet(): boolean {
  const [sheet, setSheet] = useState(() => typeof matchMedia === "function" && matchMedia(SHEET_QUERY).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const media = matchMedia(SHEET_QUERY);
    const update = () => setSheet(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return sheet;
}

/** Re-fit a floating surface whenever the window or the zoomed viewport moves. */
function useViewportChange(onChange: () => void, active: boolean) {
  const latest = useRef(onChange);
  latest.current = onChange;
  useEffect(() => {
    if (!active) return;
    const fit = () => latest.current();
    window.addEventListener("resize", fit);
    window.visualViewport?.addEventListener("resize", fit);
    window.visualViewport?.addEventListener("scroll", fit);
    return () => {
      window.removeEventListener("resize", fit);
      window.visualViewport?.removeEventListener("resize", fit);
      window.visualViewport?.removeEventListener("scroll", fit);
    };
  }, [active]);
}

/**
 * The conversation's toolbar and composer, which a first placement keeps
 * clear. Once the operator moves the panel, where they put it wins.
 */
function pageClearance() {
  const toolbar = document.querySelector(".session-toolbar-actions")?.getBoundingClientRect();
  const composer = document.querySelector("form.chat-composer")?.getBoundingClientRect();
  return {
    below: toolbar && toolbar.height > 0 ? toolbar.bottom : undefined,
    above: composer && composer.height > 0 ? composer.top : undefined,
  };
}

type Gesture = { kind: "move" | "resize"; pointerX: number; pointerY: number; start: AgentViewRect };

export interface AgentViewPanelProps {
  api: ApiClient;
  projectId: string;
  sessionId: string;
  minimized: boolean;
  /** Snapshots published since the operator last looked, for the launcher. */
  unseen: number;
  onMinimize: () => void;
  onRestore: () => void;
  onClose: () => void;
}

/**
 * The Agent view floating over the conversation: moved by its grip, sized by
 * its corner, and remembered on this device. It is not modal, so the
 * transcript and composer stay usable underneath. Minimized, it becomes a
 * launcher that keeps counting new snapshots. On a phone it is a sheet.
 */
export function AgentViewPanel(props: AgentViewPanelProps) {
  const { minimized } = props;
  // The snapshot the operator stopped on outlives minimizing.
  const pin = useState<string>();
  const [rect, setRect] = useState<AgentViewRect>(() => readRect(currentViewport(), pageClearance()));
  const [launcher, setLauncher] = useState<AgentViewPoint | undefined>(() => readLauncher());
  const sheet = useSheet();

  return createPortal(minimized
    ? <AgentViewLauncher unseen={props.unseen} position={launcher} onMove={setLauncher} onRestore={props.onRestore} />
    : <FloatingAgentView {...props} pin={pin} rect={rect} onRect={setRect} sheet={sheet} />,
  document.body);
}

function FloatingAgentView({
  api, projectId, sessionId, onMinimize, onClose, pin, rect, onRect, sheet,
}: AgentViewPanelProps & {
  pin: readonly [string | undefined, (id: string | undefined) => void];
  rect: AgentViewRect;
  onRect: (rect: AgentViewRect) => void;
  sheet: boolean;
}) {
  const titleId = useId();
  const panel = useRef<HTMLElement>(null);
  const gesture = useRef<Gesture | undefined>(undefined);
  const stream = useAgentViewStream(api, projectId, sessionId, pin);

  const place = (next: AgentViewRect, persist: boolean) => {
    const fitted = clampRect(next, currentViewport());
    onRect(fitted);
    if (persist) writeRect(fitted);
  };
  useViewportChange(() => place(rect, false), !sheet);
  // Opening moves focus into the view, so a keyboard user lands where it is.
  useLayoutEffect(() => { panel.current?.focus(); }, []);

  const begin = (kind: Gesture["kind"]) => (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = { kind, pointerX: event.clientX, pointerY: event.clientY, start: rect };
  };
  const track = (event: PointerEvent<HTMLButtonElement>) => {
    const active = gesture.current;
    if (!active) return;
    const dx = event.clientX - active.pointerX;
    const dy = event.clientY - active.pointerY;
    place(active.kind === "move"
      ? { ...active.start, x: active.start.x + dx, y: active.start.y + dy }
      : { ...active.start, width: active.start.width + dx, height: active.start.height + dy }, false);
  };
  const end = () => {
    if (!gesture.current) return;
    gesture.current = undefined;
    writeRect(rect);
  };
  const nudge = (kind: Gesture["kind"]) => (event: KeyboardEvent<HTMLButtonElement>) => {
    const direction = arrowDirection(event.key);
    if (!direction) return;
    event.preventDefault();
    const [dx, dy] = direction.map((unit) => unit * STEP);
    place(kind === "move"
      ? { ...rect, x: rect.x + dx, y: rect.y + dy }
      : { ...rect, width: rect.width + dx, height: rect.height + dy }, true);
  };
  const gestureHandlers = (kind: Gesture["kind"]) => ({
    onPointerDown: begin(kind),
    onPointerMove: track,
    onPointerUp: end,
    onPointerCancel: end,
    onLostPointerCapture: end,
    onKeyDown: nudge(kind),
  });

  return <section
    ref={panel}
    className={`agent-view-panel${sheet ? " sheet" : ""}`}
    role="dialog"
    aria-labelledby={titleId}
    tabIndex={-1}
    style={sheet ? undefined : { left: rect.x, top: rect.y, width: rect.width, height: rect.height }}
    onKeyDown={(event) => {
      // Escape puts the view away without losing it; Close is a choice.
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        onMinimize();
      }
    }}
  >
    <div className="agent-view-header">
      {!sheet && <button
        type="button"
        className="icon-button subtle agent-view-handle"
        aria-label="Move Agent view"
        title="Drag to move · arrow keys to reposition"
        {...gestureHandlers("move")}
      ><GripHorizontal size={18} aria-hidden="true" /></button>}
      <div className="agent-view-title">
        <h2 id={titleId}><Sparkles size={14} aria-hidden="true" /> Agent view</h2>
        <small role="status">{agentViewStatus(stream)}</small>
      </div>
      <IconAction icon={Minimize2} label="Minimize Agent view" title="Minimize and keep following" onClick={onMinimize} />
      <IconAction icon={X} label="Close Agent view" onClick={onClose} />
    </div>

    <div className="agent-view-scroll">
      <AgentViewBody stream={stream} />
    </div>

    <div className="agent-view-footer">
      <Link className="button quiet" to={projectSurface(projectId, "results", stream.current)}>
        <ExternalLink size={14} aria-hidden="true" /> Open in Results
      </Link>
      <span>{stream.position ? `Snapshot ${stream.position} of ${stream.items.length}${stream.pinned ? "" : " · newest"}` : ""}</span>
      {!sheet && <button
        type="button"
        className="icon-button subtle agent-view-resize"
        aria-label="Resize Agent view"
        title="Drag to resize · arrow keys to change the size"
        {...gestureHandlers("resize")}
      ><MoveDiagonal2 size={16} aria-hidden="true" /></button>}
    </div>
  </section>;
}

function AgentViewLauncher({ unseen, position, onMove, onRestore }: {
  unseen: number;
  position?: AgentViewPoint;
  onMove: (point: AgentViewPoint) => void;
  onRestore: () => void;
}) {
  const shell = useRef<HTMLDivElement>(null);
  const show = useRef<HTMLButtonElement>(null);
  const drag = useRef<{ pointerX: number; pointerY: number; left: number; top: number } | undefined>(undefined);
  const status = unseen > 0 ? `${unseen} new ${unseen === 1 ? "snapshot" : "snapshots"}` : "Following newest";

  const place = (point: AgentViewPoint, persist: boolean) => {
    const box = shell.current?.getBoundingClientRect();
    const fitted = clampPoint(point, { width: box?.width ?? 300, height: box?.height ?? 56 }, currentViewport());
    onMove(fitted);
    if (persist) writeLauncher(fitted);
  };
  useViewportChange(() => { if (position) place(position, false); }, Boolean(position));
  // Minimizing hands focus to the way back.
  useLayoutEffect(() => { show.current?.focus(); }, []);

  const origin = () => {
    const box = shell.current?.getBoundingClientRect();
    return { x: box?.left ?? 0, y: box?.top ?? 0 };
  };

  return <div
    ref={shell}
    className="agent-view-launcher"
    style={position ? { left: position.x, top: position.y, right: "auto", bottom: "auto" } : undefined}
  >
    <button
      type="button"
      className="icon-button subtle agent-view-handle"
      aria-label="Move minimized Agent view"
      title="Drag to move · arrow keys to reposition"
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        const start = origin();
        drag.current = { pointerX: event.clientX, pointerY: event.clientY, left: start.x, top: start.y };
      }}
      onPointerMove={(event) => {
        const active = drag.current;
        if (active) place({ x: active.left + event.clientX - active.pointerX, y: active.top + event.clientY - active.pointerY }, false);
      }}
      onPointerUp={() => { drag.current = undefined; if (position) writeLauncher(position); }}
      onPointerCancel={() => { drag.current = undefined; }}
      onLostPointerCapture={() => { drag.current = undefined; }}
      onKeyDown={(event) => {
        const direction = arrowDirection(event.key);
        if (!direction) return;
        event.preventDefault();
        const start = origin();
        place({ x: start.x + direction[0] * STEP, y: start.y + direction[1] * STEP }, true);
      }}
    ><GripHorizontal size={18} aria-hidden="true" /></button>
    <button ref={show} type="button" className="agent-view-launcher-show" aria-label={`Show Agent view, ${status}`} onClick={onRestore}>
      <span>
        <strong><Sparkles size={13} aria-hidden="true" /> Agent view</strong>
        <small role="status">{status}</small>
      </span>
      <em>Show</em>
    </button>
  </div>;
}
