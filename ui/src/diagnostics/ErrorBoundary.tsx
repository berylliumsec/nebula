import { Component, type ErrorInfo, type PropsWithChildren, type ReactNode } from "react";
import { logDiagnostic } from "./logger";

interface ErrorBoundaryState {
  failed: boolean;
  errorId?: string;
}

export class DiagnosticErrorBoundary extends Component<PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
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

