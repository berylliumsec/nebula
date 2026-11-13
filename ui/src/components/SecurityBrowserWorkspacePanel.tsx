import { useEffect, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { useResizableSidePanel } from "./useResizableSidePanel";

interface SecurityBrowserWorkspacePanelProps {
  compact: boolean;
  retiredView?: boolean;
  desktop: boolean;
  toolNavigation: ReactNode;
  transportControls?: ReactNode;
  children: ReactNode;
  onClose: () => void;
  onWidthChange?: (width: number | undefined) => void;
}

export function SecurityBrowserWorkspacePanel({ compact, retiredView: initialRetiredView = false, desktop, toolNavigation, transportControls, children, onClose, onWidthChange }: SecurityBrowserWorkspacePanelProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [retiredView, setRetiredView] = useState(initialRetiredView);
  const size = useResizableSidePanel({
    defaultWidth: 920, enabled: !compact, label: "Resize Security Browser workspace",
    maxWidth: 1280, minPrimaryWidth: 320, minWidth: 640, onWidthChange,
    storageKey: "nebula.security-browser-workspace.width",
  });
  useEffect(() => {
    const retired = searchParams.has("assessment") || searchParams.has("candidate") || ["intruder", "automate"].includes((searchParams.get("tool") ?? searchParams.get("browserTool")) ?? "");
    if (!retired) return;
    const next = new URLSearchParams(searchParams);
    next.delete("assessment"); next.delete("candidate"); next.delete("browserTool"); next.set("tool", "traffic");
    setRetiredView(true);
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  return <aside ref={size.panelRef} style={!desktop ? { ...size.panelStyle, width: "100%" } : size.panelStyle} className="browser-research-panel security-browser-workspace manual-tools" aria-label="Security Browser workspace">
    {size.resizeHandle}
    <header><div><strong>Security Browser</strong><small>Browser tools, traffic, and evidence</small></div><button type="button" aria-label="Close Security Browser" onClick={onClose}><X size={17} /></button></header>
    {transportControls}
    {retiredView && <p role="status">This browser feature is no longer available. Browser tools remain available below.</p>}
    {toolNavigation}
    <div className="security-browser-expert-tools">{children}</div>
  </aside>;
}
