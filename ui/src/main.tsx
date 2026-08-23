import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "@fontsource-variable/geist/wght.css";
import "@fontsource-variable/geist-mono/wght.css";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { PairingGate } from "./components/PairingGate";
import {
  DiagnosticErrorBoundary,
  installGlobalDiagnosticHandlers,
  logDiagnostic,
  nativeDiagnosticSettings,
} from "./diagnostics";
import { isTauriRuntime } from "./api/runtime";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";
import "./ui.css";

installGlobalDiagnosticHandlers();
if (!isTauriRuntime() && "serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js"));
}
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
              <PairingGate><App /></PairingGate>
            </DialogProvider>
          </WorkspaceProvider>
        </ThemeProvider>
      </BrowserRouter>
    </DiagnosticErrorBoundary>
  </StrictMode>,
);

const bootSplash = document.getElementById("nebula-boot");
if (bootSplash) {
  /* The static splash exists only while the JavaScript bundle is loading.
     Animation frames can be throttled while a browser is busy or backgrounded,
     so the handoff must not depend on them once React owns the root. */
  bootSplash.remove();
}
