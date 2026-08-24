import { useEffect, useRef, useState } from "react";
import type { DebugProtocol } from "@vscode/debugprotocol";
import {
  Bug,
  ChevronRight,
  CircleStop,
  LoaderCircle,
  Pause,
  Play,
  RotateCcw,
  StepForward,
  X,
} from "lucide-react";
import type { ApiClient } from "../api/client";
import { DebugTransport, type DebugTransportState } from "../api/debugger";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { InlineValidationNotice } from "./InlineValidationNotice";

interface EditorDebuggerPanelProps {
  api: ApiClient;
  engagementId: string;
  path: string;
  expectedSha256?: string;
  dirty: boolean;
  breakpoints: number[];
  cursorLine: number;
  onToggleBreakpoint(line: number): void;
  onReveal(line: number): void;
  onClose(): void;
}

interface VariableGroup {
  name: string;
  variables: DebugProtocol.Variable[];
}

export function EditorDebuggerPanel({
  api,
  engagementId,
  path,
  expectedSha256,
  dirty,
  breakpoints,
  cursorLine,
  onToggleBreakpoint,
  onReveal,
  onClose,
}: EditorDebuggerPanelProps) {
  const transportRef = useRef<DebugTransport | undefined>(undefined);
  const launchRef = useRef<{ threadId?: number; configured: boolean }>({
    configured: false,
  });
  const [state, setState] = useState<
    DebugTransportState | "idle" | "running" | "stopped" | "ended"
  >("idle");
  const [error, setError] = useState<string>();
  const [argumentText, setArgumentText] = useState("[]");
  const [argumentsError, setArgumentsError] = useState<string>();
  const [stack, setStack] = useState<DebugProtocol.StackFrame[]>([]);
  const [variables, setVariables] = useState<VariableGroup[]>([]);
  const [output, setOutput] = useState<string[]>([]);
  const [evaluated, setEvaluated] = useState("");
  const [expression, setExpression] = useState("");
  const [sessionDetail, setSessionDetail] = useState<{
    digest: string;
    expiresAt: string;
  }>();

  useEffect(() => () => transportRef.current?.close(), []);

  const request = async (command: string, args?: object) => {
    try {
      return await transportRef.current?.request(command, args);
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.code_debugger.request",
        "A debugger request failed.",
        caughtError,
        "code_debugger",
      );
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "The debugger request failed.",
      );
      return undefined;
    }
  };

  const inspectStoppedThread = async (threadId: number) => {
    launchRef.current.threadId = threadId;
    const response = (await request("stackTrace", {
      threadId,
      startFrame: 0,
      levels: 50,
    })) as DebugProtocol.StackTraceResponse | undefined;
    const frames = response?.body.stackFrames ?? [];
    setStack(frames);
    if (frames[0]) {
      if (frames[0].line) onReveal(frames[0].line);
      const scopesResponse = (await request("scopes", {
        frameId: frames[0].id,
      })) as DebugProtocol.ScopesResponse | undefined;
      const groups = await Promise.all(
        (scopesResponse?.body.scopes ?? []).map(async (scope) => {
          const variablesResponse = (await request("variables", {
            variablesReference: scope.variablesReference,
            start: 0,
            count: 200,
          })) as DebugProtocol.VariablesResponse | undefined;
          return {
            name: scope.name,
            variables: variablesResponse?.body.variables ?? [],
          };
        }),
      );
      setVariables(groups);
    }
  };

  const configure = async () => {
    if (launchRef.current.configured) return;
    launchRef.current.configured = true;
    await request("setBreakpoints", {
      source: { name: path.split("/").at(-1), path: `/workspace/${path}` },
      breakpoints: breakpoints.map((line) => ({ line })),
      sourceModified: false,
    });
    await request("configurationDone");
  };

  const onEvent = (event: DebugProtocol.Event) => {
    if (event.event === "initialized") void configure();
    else if (event.event === "stopped") {
      const stopped = event as DebugProtocol.StoppedEvent;
      setState("stopped");
      if (stopped.body.threadId)
        void inspectStoppedThread(stopped.body.threadId);
    } else if (event.event === "continued") {
      setState("running");
      setStack([]);
      setVariables([]);
    } else if (event.event === "output") {
      const item = event as DebugProtocol.OutputEvent;
      setOutput((current) => [...current, item.body.output].slice(-500));
    } else if (event.event === "terminated" || event.event === "exited") {
      setState("ended");
    }
  };

  const start = async () => {
    transportRef.current?.close();
    launchRef.current = { configured: false };
    setStack([]);
    setVariables([]);
    setOutput([]);
    setEvaluated("");
    setError(undefined);
    setArgumentsError(undefined);
    if (!expectedSha256 || dirty) {
      setError(
        "Save this Python file before debugging so Nebula can bind the session to exact bytes.",
      );
      return;
    }
    let parsedArguments: string[];
    try {
      const parsed = JSON.parse(argumentText) as unknown;
      if (
        !Array.isArray(parsed) ||
        parsed.some((item) => typeof item !== "string")
      )
        throw new Error();
      parsedArguments = parsed;
    } catch {
      setArgumentsError(
        'Arguments must be a JSON string array, for example ["--target", "sample.bin"].',
      );
      return;
    }
    setState("connecting");
    try {
      const session = await api.startDebugSession(engagementId, {
        path,
        expectedSha256,
        arguments: parsedArguments,
      });
      setSessionDetail({
        digest: session.imageDigest,
        expiresAt: session.expiresAt,
      });
      const transport = new DebugTransport({
        apiBaseUrl: api.baseUrl,
        token: api.getToken(),
        session,
        onEvent,
        onAdapterOutput: (text) =>
          setOutput((current) => [...current, text].slice(-500)),
        onState: (next, detail) => {
          if (next === "failed") setError(detail);
          if (next === "closed" && state !== "ended") setState("ended");
          else if (next === "ready") setState("ready");
        },
      });
      transportRef.current = transport;
      await transport.connect();
      await transport.request("initialize", {
        clientID: "nebula",
        clientName: "Nebula Security Workbench",
        adapterID: "python",
        pathFormat: "path",
        linesStartAt1: true,
        columnsStartAt1: true,
        supportsVariableType: true,
        supportsVariablePaging: true,
        supportsRunInTerminalRequest: false,
      });
      void transport
        .request("launch", {
          name: `Debug ${path}`,
          type: "python",
          request: "launch",
          program: `/workspace/${path}`,
          cwd: "/workspace",
          args: parsedArguments,
          justMyCode: false,
          console: "internalConsole",
        })
        .catch((launchError) =>
          setError(
            launchError instanceof Error
              ? launchError.message
              : "Python launch failed.",
          ),
        );
      setState("running");
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.code_debugger.start",
        "The isolated debugger could not start.",
        caughtError,
        "code_debugger",
      );
      setState("failed");
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "The isolated debugger could not start.",
      );
    }
  };

  const control = async (
    command: "continue" | "next" | "stepIn" | "stepOut" | "pause",
  ) => {
    const threadId = launchRef.current.threadId;
    if (!threadId) return;
    await request(command, { threadId });
    if (command !== "pause") setState("running");
  };

  const stop = async () => {
    await request("disconnect", { restart: false, terminateDebuggee: true });
    transportRef.current?.close();
    setState("ended");
  };

  const evaluate = async () => {
    const frameId = stack[0]?.id;
    if (!expression.trim() || frameId === undefined) return;
    const response = (await request("evaluate", {
      expression,
      frameId,
      context: "repl",
    })) as DebugProtocol.EvaluateResponse | undefined;
    if (response) setEvaluated(response.body.result);
  };

  const active = !["idle", "failed"].includes(state);
  return (
    <section
      className="editor-debugger"
      role="dialog"
      aria-modal="false"
      aria-label="Python debugger"
    >
      <header>
        <span>
          <Bug size={16} />
          <strong>Python debugger</strong>
          <small>{path}</small>
        </span>
        <button
          className="icon-button subtle"
          type="button"
          aria-label="Close debugger"
          onClick={onClose}
        >
          <X size={16} />
        </button>
      </header>
      {!active && (
        <div className="editor-debugger-preflight">
          <div>
            <strong>Isolated launch review</strong>
            <p>
              The saved file runs in Nebula’s prepared Kali runtime. The project
              is read-only, networking is disabled, and the session expires
              after one hour.
            </p>
          </div>
          <dl>
            <div>
              <dt>Source</dt>
              <dd>
                {expectedSha256
                  ? `${expectedSha256.slice(0, 12)}…`
                  : "Save required"}
              </dd>
            </div>
            <div>
              <dt>Breakpoints</dt>
              <dd>{breakpoints.length || "None"}</dd>
            </div>
            <div>
              <dt>Runtime</dt>
              <dd>Prepared Kali · debugpy</dd>
            </div>
          </dl>
          <label>
            Python arguments (JSON array)
            <input
              value={argumentText}
              spellCheck={false}
              onChange={(event) => setArgumentText(event.target.value)}
            />
          </label>
          {argumentsError && (
            <InlineValidationNotice message={argumentsError} />
          )}
          {error && (
            <DiagnosticErrorNotice
              error={error}
              fallback="The debugger could not start."
              compact
            />
          )}
          <div className="dialog-actions">
            <button
              className="button quiet"
              type="button"
              onClick={() => onToggleBreakpoint(cursorLine)}
            >
              Toggle breakpoint at line {cursorLine}
            </button>
            <button
              className="button primary"
              type="button"
              disabled={dirty || !expectedSha256 || state === "connecting"}
              onClick={() => void start()}
            >
              {state === "connecting" ? (
                <LoaderCircle className="spin" size={14} />
              ) : (
                <Play size={14} />
              )}{" "}
              Start isolated debugger
            </button>
          </div>
        </div>
      )}
      {active && (
        <>
          <nav className="editor-debugger-controls" aria-label="Debug controls">
            <button
              type="button"
              aria-label="Continue"
              disabled={state !== "stopped"}
              onClick={() => void control("continue")}
            >
              <Play size={15} />
            </button>
            <button
              type="button"
              aria-label="Pause"
              disabled={state !== "running"}
              onClick={() => void control("pause")}
            >
              <Pause size={15} />
            </button>
            <button
              type="button"
              aria-label="Step over"
              disabled={state !== "stopped"}
              onClick={() => void control("next")}
            >
              <StepForward size={15} />
            </button>
            <button
              type="button"
              aria-label="Step into"
              disabled={state !== "stopped"}
              onClick={() => void control("stepIn")}
            >
              <ChevronRight size={15} />
            </button>
            <button
              type="button"
              aria-label="Step out"
              disabled={state !== "stopped"}
              onClick={() => void control("stepOut")}
            >
              <RotateCcw size={15} />
            </button>
            <button type="button" aria-label="Stop" disabled={state === "ended"} onClick={() => void stop()}>
              <CircleStop size={15} />
            </button>
            {state === "ended" && <button type="button" aria-label="Restart debugger" onClick={() => void start()}><RotateCcw size={15} /></button>}
            <span>
              {state}
              {sessionDetail
                ? ` · ${sessionDetail.digest.slice(0, 18)}… · expires ${new Date(sessionDetail.expiresAt).toLocaleTimeString()}`
                : ""}
            </span>
          </nav>
          {error && (
            <DiagnosticErrorNotice
              error={error}
              fallback="The debugger failed."
              compact
            />
          )}
          <div className="editor-debugger-columns">
            <section>
              <h3>Call stack</h3>
              {stack.length ? (
                stack.map((frame) => (
                  <button
                    type="button"
                    key={frame.id}
                    onClick={() => frame.line && onReveal(frame.line)}
                  >
                    <strong>{frame.name}</strong>
                    <small>
                      {frame.source?.path?.replace("/workspace/", "") ??
                        "Python"}
                      :{frame.line}
                    </small>
                  </button>
                ))
              ) : (
                <p>
                  {state === "stopped"
                    ? "No frames returned."
                    : "Pause or hit a breakpoint to inspect frames."}
                </p>
              )}
            </section>
            <section>
              <h3>Variables</h3>
              {variables.length ? (
                variables.map((group) => (
                  <div key={group.name}>
                    <strong>{group.name}</strong>
                    {group.variables.map((variable, index) => (
                      <p key={`${variable.name}-${index}`}>
                        <code>{variable.name}</code>
                        <span>{variable.value}</span>
                      </p>
                    ))}
                  </div>
                ))
              ) : (
                <p>Variables appear while execution is stopped.</p>
              )}
            </section>
          </div>
          <section className="editor-debugger-console">
            <h3>Debug console</h3>
            <pre aria-live="polite">{output.join("") || "No output yet."}</pre>
            <div>
              <input
                aria-label="Evaluate Python expression"
                value={expression}
                onChange={(event) => setExpression(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void evaluate();
                }}
              />
              <button
                className="button quiet"
                type="button"
                disabled={state !== "stopped" || !expression.trim()}
                onClick={() => void evaluate()}
              >
                Evaluate
              </button>
            </div>
            {evaluated && <output>{evaluated}</output>}
          </section>
        </>
      )}
    </section>
  );
}
