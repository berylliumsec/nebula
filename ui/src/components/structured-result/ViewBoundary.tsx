import { Component, type ErrorInfo, type ReactNode } from "react";
import { logCaughtDiagnostic } from "../../diagnostics";

interface ViewBoundaryProps {
  /** Which view this guards, used in the message and the diagnostic. */
  view: string;
  children: ReactNode;
  /** Offered when a view fails: the tree always renders. */
  onFallback?: () => void;
}

interface ViewBoundaryState {
  failure?: Error;
}

/**
 * A failure in one renderer must not take the dashboard with it. The view is
 * replaced in place with what went wrong and a route to the tree, which shows
 * the same result without any specialised presentation.
 */
export class ViewBoundary extends Component<ViewBoundaryProps, ViewBoundaryState> {
  state: ViewBoundaryState = {};

  static getDerivedStateFromError(failure: Error): ViewBoundaryState {
    return { failure };
  }

  componentDidCatch(failure: Error, info: ErrorInfo): void {
    void logCaughtDiagnostic(
      "interface.structured_result.view_failed",
      `The ${this.props.view} view of a structured result could not render: ${info.componentStack?.split("\n")[1]?.trim() ?? "unknown component"}.`,
      failure,
      "structured_result",
    );
  }

  componentDidUpdate(previous: ViewBoundaryProps): void {
    // A different view (or a different result) deserves a fresh attempt.
    if (previous.view !== this.props.view && this.state.failure) this.setState({ failure: undefined });
  }

  render(): ReactNode {
    if (!this.state.failure) return this.props.children;
    return <div className="structured-view-failure" role="alert">
      <h3>The {this.props.view} view could not render</h3>
      <p>{this.state.failure.message || "The renderer stopped with an unexpected error."}</p>
      <p>The result itself is unchanged. The tree and raw views show it without this presentation.</p>
      <div className="structured-view-failure-actions">
        {this.props.onFallback && <button type="button" className="button primary" onClick={this.props.onFallback}>Show the tree</button>}
        <button type="button" className="button quiet" onClick={() => this.setState({ failure: undefined })}>Try again</button>
      </div>
    </div>;
  }
}
