import { useRef, useState } from "react";

type Point = { x: number; y: number };
type Mode = "browse" | "element" | "region";

/** The image is a view of the host page. All input still passes through Core. */
export function BrowserPageSurface({ frame, mode, connected, send, onCapture }: {
  frame: string; mode: Mode; connected: boolean;
  send: (event: Record<string, unknown>) => void;
  onCapture: (kind: "element" | "region", area: Record<string, number>) => void;
}) {
  const gesture = useRef<{ start: Point; pointerId: number; touch: boolean } | undefined>(undefined);
  const [region, setRegion] = useState<{ left: number; top: number; width: number; height: number }>();
  const point = (event: React.PointerEvent<HTMLImageElement>): Point => {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: (event.clientX - rect.left) * event.currentTarget.naturalWidth / rect.width,
      y: (event.clientY - rect.top) * event.currentTarget.naturalHeight / rect.height };
  };
  const pressKey = (key: string, code = key, modifiers = 0) => {
    send({ kind: "key", type: "keyDown", key, code, modifiers });
    send({ kind: "key", type: "keyUp", key, code, modifiers });
  };
  const finish = (event: React.PointerEvent<HTMLImageElement>, cancelled = false) => {
    const current = gesture.current;
    if (!current || current.pointerId !== event.pointerId) return;
    const end = point(event);
    if (mode === "browse") {
      send(current.touch ? { kind: "touch", type: cancelled ? "touchCancel" : "touchEnd", touchPoints: [] }
        : { kind: "mouse", type: "mouseReleased", button: "left", clickCount: 1, ...end });
    } else if (!cancelled && mode === "element") onCapture("element", end);
    else if (!cancelled && Math.abs(end.x - current.start.x) >= 2 && Math.abs(end.y - current.start.y) >= 2) {
      onCapture("region", { x: Math.min(current.start.x, end.x), y: Math.min(current.start.y, end.y),
        width: Math.abs(end.x - current.start.x), height: Math.abs(end.y - current.start.y) });
    }
    gesture.current = undefined;
    setRegion(undefined);
  };
  return <>
    <div className="managed-browser-screen">
      {frame ? <img src={frame} alt="Live shared browser page. Use Ask about page for accessible controls." draggable={false} tabIndex={0}
        onKeyDown={event => {
          // Tab remains available to leave the stream; focus can move to surrounding browser controls.
          if (!connected || mode !== "browse" || event.key === "Tab" || event.nativeEvent.isComposing) return;
          event.preventDefault();
          const modifiers = (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
          if (event.key.length === 1 && !(modifiers & 7)) send({ kind: "text", text: event.key });
          else pressKey(event.key, event.code, modifiers);
        }}
        onPointerDown={event => {
          if (gesture.current || event.button !== 0) return;
          event.currentTarget.focus({ preventScroll: true });
          const start = point(event);
          gesture.current = { start, pointerId: event.pointerId, touch: event.pointerType === "touch" };
          event.currentTarget.setPointerCapture(event.pointerId);
          if (mode === "browse") send(event.pointerType === "touch"
            ? { kind: "touch", type: "touchStart", touchPoints: [{ ...start, id: 0 }] }
            : { kind: "mouse", type: "mousePressed", button: "left", clickCount: 1, ...start });
        }}
        onPointerMove={event => {
          const current = gesture.current;
          if (!current) {
            if (mode === "browse" && event.pointerType === "mouse") send({ kind: "mouse", type: "mouseMoved", button: "none", ...point(event) });
            return;
          }
          if (current.pointerId !== event.pointerId) return;
          const end = point(event);
          if (mode === "browse") send(current.touch
            ? { kind: "touch", type: "touchMove", touchPoints: [{ ...end, id: 0 }] }
            : { kind: "mouse", type: "mouseMoved", button: "left", ...end });
          else if (mode === "region") setRegion({
            left: 100 * Math.min(current.start.x, end.x) / event.currentTarget.naturalWidth,
            top: 100 * Math.min(current.start.y, end.y) / event.currentTarget.naturalHeight,
            width: 100 * Math.abs(end.x - current.start.x) / event.currentTarget.naturalWidth,
            height: 100 * Math.abs(end.y - current.start.y) / event.currentTarget.naturalHeight,
          });
        }}
        onPointerUp={event => finish(event)} onPointerCancel={event => finish(event, true)}
        onLostPointerCapture={event => finish(event, true)}
        onWheel={event => {
          const rect = event.currentTarget.getBoundingClientRect();
          const scaleX = event.currentTarget.naturalWidth / rect.width;
          const scaleY = event.currentTarget.naturalHeight / rect.height;
          const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1;
          send({ kind: "mouse", type: "mouseWheel", x: (event.clientX - rect.left) * scaleX,
            y: (event.clientY - rect.top) * scaleY, deltaX: event.deltaX * unit * scaleX, deltaY: event.deltaY * unit * scaleY });
        }}
      /> : <p>The page will appear here when the managed browser connects.</p>}
      {region && <div className="managed-browser-region" aria-hidden="true" style={{ left: `${region.left}%`, top: `${region.top}%`, width: `${region.width}%`, height: `${region.height}%` }} />}
    </div>
  </>;
}
