import { useCallback, useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { ActivityCenter } from "./ActivityCenter";
import { CommandPalette } from "./CommandPalette";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";

export function AppShell() {
  const [activityOpen, setActivityOpen] = useState(() => window.innerWidth >= 1180);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const toggleActivity = useCallback(() => setActivityOpen((value) => !value), []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
      if (event.key === "Escape" && paletteOpen) setPaletteOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [paletteOpen]);

  return (
    <div className={`app-shell${activityOpen ? " with-activity" : ""}`}>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <SideNav />
      <TopBar
        activityOpen={activityOpen}
        onToggleActivity={toggleActivity}
        onOpenPalette={() => setPaletteOpen(true)}
      />
      <main id="main-content" className="main-content" tabIndex={-1}>
        <Outlet />
      </main>
      <ActivityCenter open={activityOpen} onClose={() => setActivityOpen(false)} />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onToggleActivity={toggleActivity}
      />
    </div>
  );
}
