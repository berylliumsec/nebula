import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { readStorage, writeStorage } from "./browserStorage";

export type ThemePreference = "light" | "dark" | "glacier" | "amber" | "zero-light" | "zero-dark" | "zero-classic" | "studio-dark";

interface ThemeContextValue {
  preference: ThemePreference;
  resolvedTheme: ThemePreference;
  setPreference: (preference: ThemePreference) => void;
  cycleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined);
const STORAGE_KEY = "nebula.theme";
const PALETTE_VERSION_KEY = "nebula.theme.paletteVersion";
const PALETTE_VERSION = "2";
const DEFAULT_PREFERENCE: ThemePreference = "dark";
const THEME_ORDER: ThemePreference[] = ["light", "dark", "glacier", "amber", "zero-light", "zero-dark", "zero-classic", "studio-dark"];


function normalizePreference(value: string | null): ThemePreference | undefined {
  if (THEME_ORDER.some((theme) => theme === value)) return value as ThemePreference;
  if (value === "zero") return "zero-classic";
  if (value === "high-contrast" || value === "system") return "dark";
  return undefined;
}

function initialPreference(): ThemePreference {
  const saved = readStorage(STORAGE_KEY);
  // Existing Zero Dark choices belonged to the original charcoal palette.
  // Move those device preferences once, then allow the new Zero Dark to persist.
  const paletteVersion = readStorage(PALETTE_VERSION_KEY);
  const normalized = (saved === "zero-dark" && paletteVersion !== PALETTE_VERSION
    ? "zero-classic"
    : normalizePreference(saved)) ?? DEFAULT_PREFERENCE;
  if (saved && saved !== normalized) writeStorage(STORAGE_KEY, normalized);
  if (paletteVersion !== PALETTE_VERSION) writeStorage(PALETTE_VERSION_KEY, PALETTE_VERSION);
  return normalized;
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [preference, setPreferenceState] = useState<ThemePreference>(initialPreference);
  const resolvedTheme = preference;

  useEffect(() => {
    const syncStoredPreference = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      const normalized = normalizePreference(event.newValue);
      if (normalized) {
        if (event.newValue !== normalized) writeStorage(STORAGE_KEY, normalized);
        setPreferenceState(normalized);
      }
    };
    window.addEventListener("storage", syncStoredPreference);
    return () => window.removeEventListener("storage", syncStoredPreference);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.style.colorScheme = resolvedTheme === "light" || resolvedTheme === "zero-light" ? "light" : "dark";
    // Browser chrome and the iPhone shell (WKWebView.themeColor) follow the page canvas.
    const canvas = getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim();
    if (canvas) document.querySelector('meta[name="theme-color"]')?.setAttribute("content", canvas);
  }, [resolvedTheme]);

  const setPreference = useCallback((value: ThemePreference) => {
    writeStorage(PALETTE_VERSION_KEY, PALETTE_VERSION);
    writeStorage(STORAGE_KEY, value);
    setPreferenceState(value);
  }, []);

  const cycleTheme = useCallback(() => {
    setPreference(THEME_ORDER[(THEME_ORDER.indexOf(preference) + 1) % THEME_ORDER.length]);
  }, [preference, setPreference]);

  const value = useMemo(
    () => ({ preference, resolvedTheme, setPreference, cycleTheme }),
    [cycleTheme, preference, resolvedTheme, setPreference],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/** The resolved theme, or undefined outside ThemeProvider (isolated component tests). */
export function useOptionalResolvedTheme(): ThemePreference | undefined {
  return useContext(ThemeContext)?.resolvedTheme;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside ThemeProvider");
  return context;
}
