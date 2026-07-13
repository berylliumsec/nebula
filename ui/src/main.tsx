import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";
import "./styles.css";
import "./workspace.css";
import "./refinement.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <ThemeProvider>
        <WorkspaceProvider>
          <DialogProvider>
            <App />
          </DialogProvider>
        </WorkspaceProvider>
      </ThemeProvider>
    </BrowserRouter>
  </StrictMode>,
);
