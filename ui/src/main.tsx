import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import {
  DiagnosticErrorBoundary,
  installGlobalDiagnosticHandlers,
  logDiagnostic,
  nativeDiagnosticSettings,
} from "./diagnostics";
import { isTauriRuntime } from "./api/runtime";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";
import "./styles.css";
import "./workspace.css";
import "./refinement.css";
import "./zero-theme.css";

installGlobalDiagnosticHandlers();
if (isTauriRuntime()) {
  void nativeDiagnosticSettings().catch((error: unknown) => logDiagnostic({
    level: "error",
    eventCode: "interface.diagnostics.settings_load_failed",
    message: "The interface could not load diagnostics preferences.",
    outcome: "fallback",
    stage: "bootstrap",
    retryable: true,
    exception: error,
  }));
}

const root = document.getElementById("root");
if (!root) {
  void logDiagnostic({
    level: "critical",
    eventCode: "interface.bootstrap.root_missing",
    message: "The interface root element is missing.",
    outcome: "failure",
    stage: "bootstrap",
    retryable: false,
  });
  throw new Error("Nebula interface root element is missing");
}
void logDiagnostic({
  level: "info",
  eventCode: "interface.bootstrap.started",
  message: "The Nebula interface bootstrap started.",
  outcome: "started",
  stage: "bootstrap",
});

createRoot(root).render(
  <StrictMode>
    <DiagnosticErrorBoundary>
      <BrowserRouter>
        <ThemeProvider>
          <WorkspaceProvider>
            <DialogProvider>
              <App />
            </DialogProvider>
          </WorkspaceProvider>
        </ThemeProvider>
      </BrowserRouter>
    </DiagnosticErrorBoundary>
  </StrictMode>,
);

const bootSplash = document.getElementById("nebula-boot");
if (bootSplash) {
  const minimumDisplayMs = 1_100;
  const elapsed = performance.now();
  window.setTimeout(() => {
    window.requestAnimationFrame(() => {
      bootSplash.classList.add("nebula-boot--leaving");
      window.setTimeout(() => bootSplash.remove(), 450);
    });
  }, Math.max(0, minimumDisplayMs - elapsed));
}
