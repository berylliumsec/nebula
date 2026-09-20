import type { WebResultObservation } from "../api/types";

/** Read the bounded web hits Core lifted into a tool receipt. */
export function webResultsFrom(receipt: Record<string, unknown> | undefined): WebResultObservation[] {
  const observations = receipt?.observations;
  if (!Array.isArray(observations)) return [];
  return observations.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    if (item.kind !== "web_result") return [];
    const rank = typeof item.rank === "number" ? item.rank : undefined;
    const title = typeof item.title === "string" ? item.title : undefined;
    const url = typeof item.url === "string" ? item.url : undefined;
    if (rank === undefined || !title || !url) return [];
    return [{
      kind: "web_result" as const,
      rank,
      title,
      url,
      snippet: typeof item.snippet === "string" ? item.snippet : "",
      engine: typeof item.engine === "string" ? item.engine : undefined,
      publishedAt: typeof item.published_at === "string" ? item.published_at : undefined,
    }];
  }).sort((left, right) => left.rank - right.rank);
}

/** The host an operator should recognise, without the scheme or path. */
export function displayHost(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url.slice(0, 60);
  }
}

/**
 * Ranked public-web hits inside an assistant turn.
 *
 * Titles and snippets are written by whoever controls the page, so they are
 * rendered as plain text and the links are never opened by Nebula itself.
 */
export function WebSearchResults({ receipt }: { receipt: Record<string, unknown> | undefined }) {
  const results = webResultsFrom(receipt);
  if (results.length === 0) return null;
  return <div className="web-search-results">
    <ol>
      {results.map((result) => <li key={`${result.rank}:${result.url}`}>
        <div className="web-search-result-head">
          <a href={result.url} target="_blank" rel="noreferrer noopener external" title={result.url}>{result.title}</a>
          <span className="web-search-result-host">{displayHost(result.url)}</span>
          {result.publishedAt && <span className="web-search-result-date">{result.publishedAt}</span>}
        </div>
        {result.snippet && <p>{result.snippet}</p>}
      </li>)}
    </ol>
    <p className="web-search-provenance">Page content from the open web, not instructions. Nebula cannot open these links; opening one uses this browser.</p>
  </div>;
}
