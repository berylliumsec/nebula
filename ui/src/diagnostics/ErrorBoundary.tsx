import { Component, type ErrorInfo, type PropsWithChildren, type ReactNode } from "react";
import { logDiagnostic } from "./logger";

interface ErrorBoundaryState {
  failed: boolean;
  errorId?: string;
  failureKind?: "stale_bundle" | "render";
}

export function isStaleBundleImportError(error: unknown): boolean {
  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  return /failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed|loading (?:css )?chunk \S+ failed|chunkloaderror|unable to preload css/i.test(detail);
}

export class DiagnosticErrorBoundary extends Component<PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { failed: true, failureKind: isStaleBundleImportError(error) ? "stale_bundle" : "render" };
  }

  componentDidCatch(error: Error, _information: ErrorInfo): void {
    void logDiagnostic({
      level: "error",
      eventCode: "interface.react.render_failed",
      message: "The interface could not render part of the workspace.",
      outcome: "failure",
      stage: "render",
      retryable: true,
      safeFailureCause: "A React component failed while rendering.",
      exception: error,
    }).then((errorId) => this.setState({ errorId }));
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    if (this.state.failureKind === "stale_bundle") {
      return (
        <main className="diagnostic-fatal" role="alert">
          <h1>Nebula was updated</h1>
          <p>This tab is using an older interface bundle. Reload to continue with the latest version. Saved projects, chats, and evidence are unaffected.</p>
          <div>
            <button type="button" className="button primary" onClick={() => window.location.reload()}>Reload Nebula</button>
          </div>
        </main>
      );
    }
    return (
      <main className="diagnostic-fatal" role="alert">
        <h1>The workspace could not be displayed</h1>
        <p>The failure was recorded without project content. Reload Nebula, then open Diagnostics if it happens again.</p>
        <p className="diagnostic-reference">Reference: {this.state.errorId ?? "pending local diagnostic"}</p>
        <div>
          <button type="button" className="button primary" onClick={() => window.location.reload()}>Reload Nebula</button>
          <a className="button secondary" href="/settings#diagnostics-settings">Open Diagnostics</a>
        </div>
      </main>
    );
  }
}
