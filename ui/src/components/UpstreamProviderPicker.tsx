import { useState } from "react";
import { RefreshCw, X } from "lucide-react";
import type { UpstreamProviderOption } from "../api/types";

export type { UpstreamProviderOption };

const SLUG = /^[a-z0-9][a-z0-9._-]{0,199}$/;
const MAX_ALLOWED = 200;

export function upstreamCatalog(metadata: Record<string, unknown> | undefined): UpstreamProviderOption[] {
  const raw = metadata?.openrouter_provider_catalog;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    if (typeof row.slug !== "string" || typeof row.name !== "string") return [];
    const datacenters = Array.isArray(row.datacenters) ? row.datacenters.filter((code): code is string => typeof code === "string") : [];
    return [{
      slug: row.slug,
      name: row.name,
      ...(typeof row.headquarters === "string" ? { headquarters: row.headquarters } : {}),
      ...(datacenters.length ? { datacenters } : {}),
    }];
  });
}

/** Where a provider serves from: listed datacenters first, otherwise its headquarters. */
export function upstreamLocation(item: UpstreamProviderOption): string {
  if (item.datacenters?.length) return item.datacenters.join(" · ");
  return item.headquarters ? `${item.headquarters} HQ` : "";
}

function locationCodes(item: UpstreamProviderOption): string[] {
  return [...(item.datacenters ?? []), ...(item.headquarters ? [item.headquarters] : [])].map((code) => code.toLowerCase());
}

export function matchesUpstreamQuery(item: UpstreamProviderOption, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  // Country codes match exactly so "us" does not also match every name containing "us".
  if (needle.length <= 3 && locationCodes(item).includes(needle)) return true;
  return item.name.toLowerCase().includes(needle) || item.slug.includes(needle);
}

/**
 * Parse an allowlist written as JSON: an array of provider slugs, or `{"only": [...]}`
 * (OpenRouter's own routing shape). Returns normalized slugs or an error message.
 */
export function parseUpstreamAllowlist(text: string): { slugs: string[] } | { error: string } {
  let value: unknown;
  try {
    value = JSON.parse(text.trim() || "[]");
  } catch {
    // diagnostic-expected: invalid operator input is shown inline next to the editor
    return { error: "This is not valid JSON." };
  }
  if (value && typeof value === "object" && !Array.isArray(value)) value = (value as Record<string, unknown>).only;
  if (!Array.isArray(value)) return { error: "Use an array of provider slugs, or an object with an \"only\" array." };
  if (value.some((item) => typeof item !== "string")) return { error: "Every entry must be a provider slug string." };
  const slugs = [...new Set((value as string[]).map((item) => item.trim().toLowerCase()).filter(Boolean))];
  const invalid = slugs.find((slug) => !SLUG.test(slug));
  if (invalid) return { error: `"${invalid}" is not a valid provider slug.` };
  if (slugs.length > MAX_ALLOWED) return { error: `Allow at most ${MAX_ALLOWED} providers.` };
  return { slugs };
}

type DirectoryState = "idle" | "loading" | "ready" | "failed";

/** OpenRouter routing allowlist: which upstream providers may serve requests. */
export function UpstreamProviderPicker({ catalog, selected, query, onQuery, onChange, directoryState = "ready", onReloadDirectory }: {
  catalog: UpstreamProviderOption[];
  selected: string[];
  query: string;
  onQuery: (value: string) => void;
  onChange: (value: string[]) => void;
  directoryState?: DirectoryState;
  onReloadDirectory?: () => void;
}) {
  const [mode, setMode] = useState<"list" | "json">("list");
  // Start expanded when an allowlist exists; afterwards the operator controls it.
  const [initiallyOpen] = useState(selected.length > 0);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const known = new Map(catalog.map((item) => [item.slug, item]));
  // Keep saved choices visible even before the directory has been loaded.
  const options: UpstreamProviderOption[] = [...catalog, ...selected.filter((slug) => !known.has(slug)).map((slug) => ({ slug, name: slug }))];
  const visible = options.filter((item) => matchesUpstreamQuery(item, query));
  const unknown = catalog.length ? selected.filter((slug) => !known.has(slug)) : [];
  const summary = selected.length ? `${selected.length} allowed` : "Any provider";

  const toggle = (slug: string, allowed: boolean) => onChange(allowed ? [...new Set([...selected, slug])] : selected.filter((value) => value !== slug));
  const openJson = () => {
    setDraft(JSON.stringify(selected, null, 2));
    setDraftError(null);
    setMode("json");
  };
  const editJson = (text: string) => {
    setDraft(text);
    const parsed = parseUpstreamAllowlist(text);
    if ("error" in parsed) {
      setDraftError(parsed.error);
      return;
    }
    setDraftError(null);
    onChange(parsed.slugs);
  };

  return (
    <details className="provider-advanced upstream-providers" open={initiallyOpen || undefined}>
      <summary><span>Upstream providers</span><span className="provider-section-badge">{summary}</span></summary>
      <div className="provider-section-body">
        <p className="provider-dialog-note">Leave everything unchecked to let OpenRouter route to any provider. Checked providers are the only ones OpenRouter may use; models they do not serve cannot be verified.</p>
        <div className="upstream-toolbar">
          {mode === "list"
            ? <input type="search" aria-label="Search upstream providers" value={query} placeholder="Search, or a country like US" onChange={(event) => onQuery(event.target.value)} />
            : <span className="upstream-toolbar-label">Allowlist as JSON</span>}
          <div className="upstream-mode" role="group" aria-label="Allowlist editor">
            <button type="button" aria-pressed={mode === "list"} onClick={() => setMode("list")}>List</button>
            <button type="button" aria-pressed={mode === "json"} onClick={openJson}>JSON</button>
          </div>
        </div>
        {mode === "list" ? (
          <>
            {selected.length > 0 && (
              <ul className="upstream-chips" aria-label="Allowed upstream providers">
                {selected.map((slug) => (
                  <li key={slug}>
                    <span>{known.get(slug)?.name ?? slug}</span>
                    <button type="button" aria-label={`Remove ${known.get(slug)?.name ?? slug}`} onClick={() => toggle(slug, false)}><X size={12} aria-hidden="true" /></button>
                  </li>
                ))}
              </ul>
            )}
            <fieldset className="upstream-list" aria-busy={directoryState === "loading" || undefined}>
              <legend className="sr-only">Upstream providers</legend>
              {visible.length
                ? visible.map((item) => {
                  const location = upstreamLocation(item);
                  return (
                    <label key={item.slug}>
                      <input type="checkbox" aria-label={location ? `${item.name}, ${location}` : item.name} checked={selected.includes(item.slug)} onChange={(event) => toggle(item.slug, event.target.checked)} />
                      <span className="upstream-name"><strong>{item.name}</strong>{item.name !== item.slug && <small>{item.slug}</small>}</span>
                      {location && <span className="upstream-location" title={item.datacenters?.length ? "Listed datacenters" : "Headquarters; OpenRouter lists no datacenters"}>{location}</span>}
                    </label>
                  );
                })
                : <p className="upstream-empty">{directoryState === "loading" ? "Loading OpenRouter's provider list…" : catalog.length ? "No providers match this search." : directoryState === "failed" ? "OpenRouter's provider list could not be loaded." : "OpenRouter's provider list is not loaded yet."}</p>}
            </fieldset>
            {(catalog.length > 0 || selected.length > 0 || directoryState === "failed") && <div className="upstream-footer">
              <small>{catalog.length ? `${visible.length} of ${options.length} providers` : ""}</small>
              {directoryState === "failed" && onReloadDirectory && <button className="button quiet" type="button" onClick={onReloadDirectory}><RefreshCw size={13} aria-hidden="true" /> Retry</button>}
              {selected.length > 0 && <button className="button quiet" type="button" onClick={() => onChange([])}>Allow any provider</button>}
            </div>}
          </>
        ) : (
          <>
            <textarea className="upstream-json" aria-label="Upstream provider allowlist JSON" aria-invalid={Boolean(draftError) || undefined} rows={7} spellCheck={false} autoCapitalize="none" value={draft} onChange={(event) => editJson(event.target.value)} />
            <p className={draftError ? "form-error" : "provider-dialog-note"} role={draftError ? "alert" : undefined}>
              {draftError ?? <>An array of provider slugs, for example <code>["gmicloud", "together"]</code>, or OpenRouter's <code>{"{\"only\": [...]}"}</code>. <code>[]</code> allows any provider.</>}
            </p>
            {!draftError && unknown.length > 0 && <p className="provider-dialog-note">Not in OpenRouter's current directory: {unknown.join(", ")}.</p>}
          </>
        )}
      </div>
    </details>
  );
}
