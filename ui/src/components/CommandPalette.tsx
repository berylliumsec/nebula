import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { ChevronRight, Command, FileSearch, PanelLeft, PanelRight, Search, SlidersHorizontal } from "lucide-react";
import { useNavigate } from "react-router-dom";
import type { ApiClient } from "../api/client";
import type { ActionDescriptor, SearchResult } from "../api/types";
import { navigationItems } from "../navigation";
import { resourcePath } from "../resourceRoutes";
import { logCaughtDiagnostic } from "../diagnostics";
import { settingCatalog, settingCatalogText, type SettingCatalogEntry } from "../settingsCatalog";
import type { ContextualCommand } from "../state/ChromeContext";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onToggleActivity: () => void;
  onToggleSidebar: () => void;
  onOpenSetting: (entry: SettingCatalogEntry, returnFocus: HTMLElement | null) => void;
  contextualCommands?: ContextualCommand[];
  api?: ApiClient;
  activeProjectId?: string;
}

interface PaletteAction {
  id: string;
  label: string;
  description: string;
  icon: typeof Command;
  keywords: string;
  shortcut?: string;
  meta?: string;
  kind?: "command" | "setting";
  resource?: SearchResult;
  disabled?: boolean;
  run: () => void;
}

const FOCUSABLE = [
  "button:not([disabled])",
  "input:not([disabled])",
  "[href]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function CommandPalette({ open, onClose, onToggleActivity, onToggleSidebar, onOpenSetting, contextualCommands = [], api, activeProjectId }: CommandPaletteProps) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const [scope, setScope] = useState<"active" | "all">("active");
  const [remoteResults, setRemoteResults] = useState<SearchResult[]>([]);
  const [searchState, setSearchState] = useState<"idle" | "loading" | "ready" | "offline">("idle");
  const [partialIndex, setPartialIndex] = useState(false);
  const [actionResult, setActionResult] = useState<SearchResult>();
  const inputRef = useRef<HTMLInputElement>(null);
  const paletteRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const actions = useMemo<PaletteAction[]>(
    () => [
      ...navigationItems.map((item) => ({
        id: item.commandId,
        label: `Go to ${item.label}`,
        description: item.description,
        icon: item.icon,
        keywords: `${item.label} ${item.legacyLabel ?? ""} ${item.aliases.join(" ")} ${item.description}`,
        run: () => navigate(item.path),
      })),
      ...settingCatalog.map((entry) => ({
        id: entry.id,
        label: entry.label,
        description: entry.description,
        icon: SlidersHorizontal,
        keywords: settingCatalogText(entry),
        meta: `${entry.category} · ${entry.scope}`,
        kind: "setting" as const,
        run: () => onOpenSetting(entry, returnFocusRef.current),
      })),
      {
        id: "sidebar",
        label: "Toggle sidebar",
        description: "Show or hide workspace navigation",
        icon: PanelLeft,
        keywords: "sidebar navigation hide show",
        run: onToggleSidebar,
      },
      {
        id: "activity",
        label: "Toggle activity inspector",
        description: "Show run events and approval requests",
        icon: PanelRight,
        keywords: "activity approvals inspector drawer panel",
        run: onToggleActivity,
      },
      ...contextualCommands.map((command) => ({
        ...command,
        icon: Command,
        keywords: `${command.keywords ?? ""} editor code`,
      })),
    ],
    [contextualCommands, navigate, onOpenSetting, onToggleActivity, onToggleSidebar],
  );

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return actions;
    const terms = needle.split(/\s+/).filter(Boolean);
    const local = actions
      .filter((action) => {
        const haystack = `${action.label} ${action.keywords}`.toLowerCase();
        return terms.every((term) => haystack.includes(term));
      })
      .sort((left, right) => {
        const leftLabel = left.label.toLowerCase();
        const rightLabel = right.label.toLowerCase();
        const leftScore = leftLabel.startsWith(needle) ? 0 : left.kind === "setting" ? 1 : 2;
        const rightScore = rightLabel.startsWith(needle) ? 0 : right.kind === "setting" ? 1 : 2;
        return leftScore - rightScore;
      });
    const resources: PaletteAction[] = remoteResults.map((result) => ({
      id: `resource:${result.ref.kind}:${result.ref.id}`,
      label: result.label,
      description: result.description || result.snippet || result.breadcrumb,
      icon: FileSearch,
      keywords: `${result.label} ${result.description} ${result.snippet}`,
      meta: `${result.project} · ${result.breadcrumb}`,
      resource: result,
      run: () => navigate(resourcePath(result.ref.projectId, result.ref.kind, result.ref.id)),
    }));
    const fileSearch: PaletteAction[] = activeProjectId && needle.length >= 2 ? [{
      id: "workspace-file-search",
      label: `Search project files for “${query.trim()}”`,
      description: "Run the bounded workspace-content search in Code",
      icon: FileSearch,
      keywords: query,
      meta: "Current project · Files",
      run: () => {
        const parameters = new URLSearchParams({ view: "code", workspaceSearch: query.trim() });
        navigate(`/projects/${encodeURIComponent(activeProjectId)}/workbench?${parameters}`);
      },
    }] : [];
    return [...resources, ...fileSearch, ...local];
  }, [actions, activeProjectId, navigate, query, remoteResults]);

  useEffect(() => {
    if (!open || !api || query.trim().length < 2) {
      setRemoteResults([]);
      setSearchState("idle");
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSearchState("loading");
      void api.searchResources({ query: query.trim(), activeProject: activeProjectId, scope }, controller.signal)
        .then((response) => {
          setRemoteResults(response.items);
          setPartialIndex(response.partialIndex);
          setSearchState("ready");
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) {
            void logCaughtDiagnostic("interface.omnibox.search_failed", "Federated omnibox search could not reach Core.", error, "omnibox");
            setSearchState("offline");
          }
        });
    }, 160);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeProjectId, api, open, query, scope]);

  useEffect(() => {
    if (open) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setQuery("");
      setSelected(0);
      setScope("active");
      setActionResult(undefined);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
    return () => {
      if (open) returnFocusRef.current?.focus();
    };
  }, [open]);

  useEffect(() => setSelected(0), [query]);

  if (!open) return null;

  const execute = (action: PaletteAction | undefined) => {
    if (!action || action.disabled) return;
    action.run();
    onClose();
  };

  const trapFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const items = [...(paletteRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="palette-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        ref={paletteRef}
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={trapFocus}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <label className="palette-search">
          <Search size={19} aria-hidden="true" />
          <span className="sr-only">Search pages, actions, and settings</span>
          <input
            ref={inputRef}
            aria-label="Search pages, actions, and settings"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search pages, actions, and settings…"
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setSelected((value) => Math.min(value + 1, results.length - 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setSelected((value) => Math.max(value - 1, 0));
              }
              if (event.key === "Enter") execute(results[selected]);
              if (event.key === "ArrowRight" && results[selected]?.resource) {
                event.preventDefault();
                setActionResult(results[selected].resource);
              }
            }}
          />
          <kbd>Esc</kbd>
        </label>
        <div className="palette-scope" aria-label="Search scope">
          <button type="button" aria-pressed={scope === "active"} onClick={() => setScope("active")}>Current project</button>
          <button type="button" aria-pressed={scope === "all"} onClick={() => setScope("all")}>All projects</button>
        </div>
        {actionResult && <div className="palette-action-menu" role="menu" aria-label={`Actions for ${actionResult.label}`}>
          <header><strong>{actionResult.label}</strong><button type="button" onClick={() => setActionResult(undefined)}>Back</button></header>
          {actionResult.actions.map((descriptor: ActionDescriptor) => <button
            key={descriptor.id}
            type="button"
            role="menuitem"
            disabled={!descriptor.available || descriptor.id !== "open"}
            title={descriptor.disabledReason ?? (descriptor.id === "open" ? undefined : "Open the resource to use this action in context.")}
            onClick={() => execute(results.find((item) => item.resource === actionResult))}
          >{descriptor.id.replaceAll("_", " ")}<small>{descriptor.authority}</small></button>)}
        </div>}
        <div className="palette-results" role="listbox" aria-label="Commands">
          {results.map((action, index) => {
            const Icon = action.icon;
            return (
              <button
                key={action.id}
                type="button"
                role="option"
                aria-selected={selected === index}
                disabled={action.disabled}
                onMouseEnter={() => setSelected(index)}
                onClick={() => execute(action)}
              >
                <span className="palette-icon">
                  <Icon size={17} aria-hidden="true" />
                </span>
                <span>
                  <strong>{action.label}</strong>
                  <small>{action.description}</small>
                  {action.meta && <em className="palette-result-meta">{action.meta}</em>}
                </span>
                {action.shortcut && <kbd>{action.shortcut}</kbd>}
                {action.resource && <ChevronRight size={17} aria-label="Show actions" />}
              </button>
            );
          })}
          {searchState === "loading" && <p className="palette-empty" role="status">Searching Nebula…</p>}
          {searchState === "offline" && <p className="palette-empty" role="status">Core search is offline. Pages, settings, and local actions are still available.</p>}
          {partialIndex && <p className="palette-empty" role="status">Search index was refreshed; results are current.</p>}
          {results.length === 0 && searchState !== "loading" && <p className="palette-empty">No results. The item may have moved or been deleted.</p>}
        </div>
        <footer>
          <span><kbd>↑</kbd><kbd>↓</kbd> Navigate</span>
          <span><kbd>↵</kbd> Open</span>
          <span><kbd>→</kbd> Actions</span>
        </footer>
      </div>
    </div>
  );
}
