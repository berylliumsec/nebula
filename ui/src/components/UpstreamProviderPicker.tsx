export interface UpstreamProviderOption { slug: string; name: string }

export function upstreamCatalog(metadata: Record<string, unknown> | undefined): UpstreamProviderOption[] {
  const raw = metadata?.openrouter_provider_catalog;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => item && typeof item === "object" && typeof (item as UpstreamProviderOption).slug === "string" && typeof (item as UpstreamProviderOption).name === "string"
    ? [{ slug: (item as UpstreamProviderOption).slug, name: (item as UpstreamProviderOption).name }]
    : []);
}

/** OpenRouter routing allowlist: which upstream providers may serve requests. */
export function UpstreamProviderPicker({ catalog, selected, query, onQuery, onChange }: {
  catalog: UpstreamProviderOption[];
  selected: string[];
  query: string;
  onQuery: (value: string) => void;
  onChange: (value: string[]) => void;
}) {
  const known = new Map(catalog.map((item) => [item.slug, item.name]));
  // Keep saved choices visible even before the directory has been loaded.
  const options = [...catalog, ...selected.filter((slug) => !known.has(slug)).map((slug) => ({ slug, name: slug }))];
  const needle = query.trim().toLowerCase();
  const visible = options.filter((item) => !needle || item.name.toLowerCase().includes(needle) || item.slug.includes(needle));
  const summary = selected.length ? `${selected.length} allowed` : "Any";
  return (
    <details className="provider-advanced upstream-providers">
      <summary>Upstream providers · {summary}</summary>
      <p className="provider-dialog-note">Leave everything unchecked to let OpenRouter route to any provider. Checked providers are the only ones OpenRouter may use; models they do not serve cannot be verified.</p>
      {options.length > 8 && <label>Find provider<input type="search" value={query} placeholder="Search providers" onChange={(event) => onQuery(event.target.value)} /></label>}
      <fieldset className="resource-checklist">
        <legend>Allowed upstream providers</legend>
        {visible.length
          ? visible.map((item) => <label key={item.slug}><input type="checkbox" checked={selected.includes(item.slug)} onChange={(event) => onChange(event.target.checked ? [...new Set([...selected, item.slug])] : selected.filter((value) => value !== item.slug))} /><span>{item.name}</span></label>)
          : <p>{catalog.length ? "No providers match this search." : "Save the profile and refresh it to load OpenRouter's provider list."}</p>}
      </fieldset>
      {selected.length > 0 && <button className="button quiet" type="button" onClick={() => onChange([])}>Allow any provider</button>}
    </details>
  );
}
