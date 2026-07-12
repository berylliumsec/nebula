import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  type TerminalConnectionState,
  type TerminalTransport,
  UnavailableTerminalTransport,
} from "../api/terminal";

interface TerminalPanelProps {
  sessionId: string;
  transport?: TerminalTransport;
}

export function TerminalPanel({ sessionId, transport }: TerminalPanelProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const fallbackTransport = useRef<TerminalTransport>(new UnavailableTerminalTransport());
  const [connection, setConnection] = useState<TerminalConnectionState>("disconnected");
  const activeTransport = transport ?? fallbackTransport.current;

  useEffect(() => {
    if (!hostRef.current) return;
    const controller = new AbortController();
    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      lineHeight: 1.35,
      scrollback: 10_000,
      screenReaderMode: true,
      theme: {
        background: "#0a0f18",
        foreground: "#c9d7e7",
        cursor: "#72d5ff",
        selectionBackground: "#254466",
        black: "#0a0f18",
        brightBlack: "#607187",
        red: "#ff7087",
        green: "#55d6a8",
        yellow: "#f4c86a",
        blue: "#72a7ff",
        magenta: "#ba8cff",
        cyan: "#72d5ff",
        white: "#dce7f2",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(hostRef.current);
    terminal.textarea?.setAttribute("aria-label", "Nebula terminal input");
    requestAnimationFrame(() => {
      fit.fit();
      activeTransport.connect({
        sessionId,
        columns: terminal.cols,
        rows: terminal.rows,
        signal: controller.signal,
        onData: (data) => terminal.write(data),
        onStateChange: (state) => setConnection(state),
      });
    });

    const input = terminal.onData((data) => activeTransport.send(data));
    const resize = new ResizeObserver(() => {
      fit.fit();
      activeTransport.resize(terminal.cols, terminal.rows);
    });
    resize.observe(hostRef.current);

    return () => {
      controller.abort();
      resize.disconnect();
      input.dispose();
      activeTransport.disconnect();
      terminal.dispose();
    };
  }, [activeTransport, sessionId]);

  return (
    <section className="terminal-panel" aria-label="Terminal session">
      <header>
        <div className="terminal-dots" aria-hidden="true"><span /><span /><span /></div>
        <strong>human-session-01</strong>
        <span className={`terminal-state ${connection}`}><span /> {connection}</span>
      </header>
      <div className="terminal-host" ref={hostRef} />
    </section>
  );
}
