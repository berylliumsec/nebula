import { useEffect, useMemo, useRef, useState } from "react";
import { Command, Contrast, Moon, PanelLeft, PanelRight, Search, Sun } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { navigationItems } from "../navigation";
import { useTheme } from "../state/ThemeContext";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onToggleActivity: () => void;
  onToggleSidebar: () => void;
}

interface PaletteAction {
  id: string;
  label: string;
  description: string;
  icon: typeof Command;
  keywords: string;
  run: () => void;
}

export function CommandPalette({ open, onClose, onToggleActivity, onToggleSidebar }: CommandPaletteProps) {
  const navigate = useNavigate();
  const { setPreference } = useTheme();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

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
      {
        id: "theme-light",
        label: "Use light theme",
        description: "Set a persistent light appearance",
        icon: Sun,
        keywords: "appearance theme light",
        run: () => setPreference("light"),
      },
      {
        id: "theme-dark",
        label: "Use dark theme",
        description: "Set a persistent dark appearance",
        icon: Moon,
        keywords: "appearance theme dark",
        run: () => setPreference("dark"),
      },
      {
        id: "theme-contrast",
        label: "Use high-contrast theme",
        description: "Increase borders, text contrast, and focus visibility",
        icon: Contrast,
        keywords: "appearance accessibility contrast",
        run: () => setPreference("high-contrast"),
      },
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
    ],
    [navigate, onToggleActivity, onToggleSidebar, setPreference],
  );

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle
      ? actions.filter((action) => `${action.label} ${action.keywords}`.toLowerCase().includes(needle))
      : actions;
  }, [actions, query]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => setSelected(0), [query]);

  if (!open) return null;

  const execute = (action: PaletteAction | undefined) => {
    if (!action) return;
    action.run();
    onClose();
  };

  return (
    <div className="palette-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <label className="palette-search">
          <Search size={19} aria-hidden="true" />
          <span className="sr-only">Search commands</span>
          <input
            ref={inputRef}
            aria-label="Search commands"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search pages and actions…"
            onKeyDown={(event) => {
              if (event.key === "Escape") onClose();
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setSelected((value) => Math.min(value + 1, results.length - 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setSelected((value) => Math.max(value - 1, 0));
              }
              if (event.key === "Enter") execute(results[selected]);
            }}
          />
          <kbd>Esc</kbd>
        </label>
        <div className="palette-results" role="listbox" aria-label="Commands">
          {results.map((action, index) => {
            const Icon = action.icon;
            return (
              <button
                key={action.id}
                type="button"
                role="option"
                aria-selected={selected === index}
                onMouseEnter={() => setSelected(index)}
                onClick={() => execute(action)}
              >
                <span className="palette-icon">
                  <Icon size={17} aria-hidden="true" />
                </span>
                <span>
                  <strong>{action.label}</strong>
                  <small>{action.description}</small>
                </span>
              </button>
            );
          })}
          {results.length === 0 && <p className="palette-empty">No matching commands</p>}
        </div>
        <footer>
          <span><kbd>↑</kbd><kbd>↓</kbd> Navigate</span>
          <span><kbd>↵</kbd> Open</span>
        </footer>
      </div>
    </div>
  );
}
