import {render, screen} from "@testing-library/react";
import {describe, expect, it} from "vitest";
import {WebSearchResults, displayHost, webResultsFrom} from "./WebSearchResults";

const receipt = {
  schema: "nebula.tool-result/v2",
  tool_name: "web.search",
  status: "completed",
  observations: [
    {kind: "web_result", rank: 2, title: "CVE-2026-1084 detail", url: "https://nvd.nist.gov/vuln/CVE-2026-1084", snippet: "Out-of-bounds read.", engine: "brave"},
    {kind: "network_port", protocol: "tcp", port: 443, state: "open"},
    {kind: "web_result", rank: 1, title: "OpenSSL Security Advisory", url: "https://www.openssl.org/news/secadv/", snippet: "Two moderate issues.", engine: "duckduckgo", published_at: "2026-02-02"},
  ],
};

describe("web search results in a transcript", () => {
  it("reads only web hits from a receipt and orders them by rank", () => {
    const results = webResultsFrom(receipt);
    expect(results.map((item) => item.rank)).toEqual([1, 2]);
    expect(results[0].title).toBe("OpenSSL Security Advisory");
    expect(results[0].publishedAt).toBe("2026-02-02");
  });

  it("ignores receipts without usable observations", () => {
    expect(webResultsFrom(undefined)).toEqual([]);
    expect(webResultsFrom({observations: "not a list"})).toEqual([]);
    expect(webResultsFrom({observations: [{kind: "web_result", rank: 1}]})).toEqual([]);
  });

  it("renders each hit with its host and snippet", () => {
    render(<WebSearchResults receipt={receipt} />);
    const first = screen.getByRole("link", {name: "OpenSSL Security Advisory"});
    expect(first).toHaveAttribute("href", "https://www.openssl.org/news/secadv/");
    expect(screen.getByText("openssl.org")).toBeInTheDocument();
    expect(screen.getByText("Two moderate issues.")).toBeInTheDocument();
  });

  it("says result text is page content rather than instruction", () => {
    render(<WebSearchResults receipt={receipt} />);
    expect(screen.getByText(/not instructions/)).toBeInTheDocument();
    expect(screen.getByText(/Nebula cannot open these links/)).toBeInTheDocument();
  });

  it("opens links in a new context without handing over the opener", () => {
    render(<WebSearchResults receipt={receipt} />);
    const link = screen.getByRole("link", {name: "OpenSSL Security Advisory"});
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("renders nothing when a search returned no hits", () => {
    const {container} = render(<WebSearchResults receipt={{observations: []}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("falls back to the raw value when a URL cannot be parsed", () => {
    expect(displayHost("https://www.example.com/a")).toBe("example.com");
    expect(displayHost("not a url")).toBe("not a url");
  });
});
