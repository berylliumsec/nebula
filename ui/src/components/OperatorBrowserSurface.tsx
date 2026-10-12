import { useEffect, useRef } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

type Point = { x: number; y: number };

export function framePoint(image: HTMLImageElement, clientX: number, clientY: number): Point {
  const rect = image.getBoundingClientRect();
  if (!rect.width || !rect.height || !image.naturalWidth || !image.naturalHeight) return { x: 0, y: 0 };
  return {
    x: Math.max(0, Math.min(image.naturalWidth, (clientX - rect.left) * image.naturalWidth / rect.width)),
    y: Math.max(0, Math.min(image.naturalHeight, (clientY - rect.top) * image.naturalHeight / rect.height)),
  };
}

/** A Chromium screencast. Operator input travels through Core to the same tab. */
export function OperatorBrowserSurface({ frame, connected, send }: {
  frame: string;
  connected: boolean;
  send: (event: Record<string, unknown>) => void;
}) {
  const screen = useRef<HTMLDivElement>(null);
  const pressed = useRef<{ pointerId: number; touch: boolean } | undefined>(undefined);

  useEffect(() => {
    if (!connected || !screen.current) return;
    const surface = screen.current;
    const resize = () => send({
      kind: "resize",
      width: Math.min(1920, Math.max(320, Math.round(surface.clientWidth))),
      height: Math.min(1080, Math.max(320, Math.round(surface.clientHeight))),
    });
    const observer = new ResizeObserver(resize);
    observer.observe(surface);
    resize();
    return () => observer.disconnect();
  }, [connected, send]);

  const finish = (event: ReactPointerEvent<HTMLImageElement>, cancelled = false) => {
    const active = pressed.current;
    if (!active || active.pointerId !== event.pointerId) return;
    const point = framePoint(event.currentTarget, event.clientX, event.clientY);
    send(active.touch
      ? { kind: "touch", type: cancelled ? "touchCancel" : "touchEnd", touchPoints: [] }
      : { kind: "mouse", type: "mouseReleased", button: "left", clickCount: 1, ...point });
    pressed.current = undefined;
  };

  return <div ref={screen} className="operator-browser-screen" role="region" aria-label="Interactive browser page" aria-busy={!connected}>
    {frame ? <img src={frame} alt="Live Chromium page. Click or focus to interact." draggable={false} tabIndex={connected ? 0 : -1}
      onKeyDown={event => {
        if (!connected || event.key === "Tab" || event.nativeEvent.isComposing) return;
        event.preventDefault();
        const modifiers = (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
        if (event.key.length === 1 && !(modifiers & 7)) {
          send({ kind: "text", text: event.key });
        } else {
          send({ kind: "key", type: "keyDown", key: event.key, code: event.code, modifiers });
          send({ kind: "key", type: "keyUp", key: event.key, code: event.code, modifiers });
        }
      }}
      onCompositionEnd={event => { if (connected && event.data) send({ kind: "text", text: event.data }); }}
      onPaste={event => {
        if (!connected) return;
        const content = event.clipboardData.getData("text/plain");
        if (!content) return;
        event.preventDefault();
        for (let offset = 0; offset < content.length; offset += 256) send({ kind: "text", text: content.slice(offset, offset + 256) });
      }}
      onPointerDown={event => {
        if (!connected || pressed.current || event.button !== 0) return;
        event.currentTarget.focus({ preventScroll: true });
        const point = framePoint(event.currentTarget, event.clientX, event.clientY);
        pressed.current = { pointerId: event.pointerId, touch: event.pointerType === "touch" };
        event.currentTarget.setPointerCapture(event.pointerId);
        send(event.pointerType === "touch"
          ? { kind: "touch", type: "touchStart", touchPoints: [{ ...point, id: 0 }] }
          : { kind: "mouse", type: "mousePressed", button: "left", clickCount: 1, ...point });
      }}
      onPointerMove={event => {
        if (!connected || event.pointerType === "touch" && !pressed.current) return;
        const point = framePoint(event.currentTarget, event.clientX, event.clientY);
        if (pressed.current?.pointerId === event.pointerId && pressed.current.touch) {
          send({ kind: "touch", type: "touchMove", touchPoints: [{ ...point, id: 0 }] });
        } else if (event.pointerType === "mouse") {
          send({ kind: "mouse", type: "mouseMoved", button: pressed.current ? "left" : "none", ...point });
        }
      }}
      onPointerUp={event => finish(event)}
      onPointerCancel={event => finish(event, true)}
      onLostPointerCapture={event => finish(event, true)}
      onWheel={event => {
        if (!connected) return;
        event.preventDefault();
        const point = framePoint(event.currentTarget, event.clientX, event.clientY);
        const scale = event.currentTarget.naturalHeight / event.currentTarget.getBoundingClientRect().height;
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? event.currentTarget.clientHeight : 1;
        send({ kind: "mouse", type: "mouseWheel", button: "none", ...point,
          deltaX: Math.max(-2000, Math.min(2000, event.deltaX * unit * scale)),
          deltaY: Math.max(-2000, Math.min(2000, event.deltaY * unit * scale)) });
      }}
    /> : <p>{connected ? "Waiting for the page…" : "Connect to show the page."}</p>}
  </div>;
}
