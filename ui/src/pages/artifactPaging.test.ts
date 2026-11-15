import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { appendSearchPage, readContinuation } from "./artifactPaging";

function client(...bodies: unknown[]) {
  const fetchMock = vi.fn<typeof fetch>();
  for (const body of bodies) fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200 }));
  return { api: new ApiClient({ baseUrl: "http://core.test", fetch: fetchMock }), fetchMock };
}
const sent = (fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, call: number) => JSON.parse(String(fetchMock.mock.calls[call][1]?.body));
const page = (lines: Array<[number, string, boolean?]>, extra: Record<string, unknown>) => ({
  schema: "nebula.tool-output.read/v1", artifact_id: "artifact-a", filename: "scan.txt", searchable: true,
  lines: lines.map(([line, text, cut]) => ({ line, text, ...(cut ? { line_truncated: true } : {}) })), ...extra,
});

describe("artifact read continuation", () => {
  it("resumes a page that stopped inside one long line with Core's cursor", async () => {
    // Core's _read_page: a split inside line 6 names only a cursor.
    const { api, fetchMock } = client(
      page([[5, "short"], [6, "x".repeat(40), true]], { starting_line: 5, truncated: true, continuation: { continuation_cursor: "CUR-1" }, continuation_cursor: "CUR-1" }),
      page([[6, "y".repeat(40)], [7, "next"]], { starting_line: 6, truncated: true, continuation: { starting_line: 8 }, continuation_cursor: "CUR-2" }),
      page([[8, "last"]], { starting_line: 8, truncated: false, continuation: null, continuation_cursor: null }),
    );
    const first = await api.readToolOutput("artifact-a", 5);
    expect(sent(fetchMock, 0)).toEqual({ starting_line: 5, line_count: 100 });
    expect(first.continuationStartingLine).toBeUndefined();
    const next = readContinuation(first);
    expect(next).toEqual({ label: "Continue line 6", startingLine: 6, cursor: "CUR-1" });

    const second = await api.readToolOutput("artifact-a", next!.startingLine, undefined, next!.cursor);
    expect(sent(fetchMock, 1)).toEqual({ line_count: 100, continuation_cursor: "CUR-1" });
    expect(second.lines[0]).toEqual({ line: 6, text: "y".repeat(40), lineTruncated: undefined });
    const after = readContinuation(second);
    expect(after).toEqual({ label: "Read next lines", startingLine: 8, cursor: "CUR-2" });

    const last = await api.readToolOutput("artifact-a", after!.startingLine, undefined, after!.cursor);
    expect(sent(fetchMock, 2)).toEqual({ line_count: 100, continuation_cursor: "CUR-2" });
    expect(readContinuation(last)).toBeUndefined();
  });

  it("falls back to the starting line when Core sends no cursor", () => {
    expect(readContinuation({ artifactId: "a", searchable: true, lines: [{ line: 100, text: "z" }], truncated: true, continuationStartingLine: 101 }))
      .toEqual({ label: "Read next lines", startingLine: 101, cursor: undefined });
  });
});

describe("artifact search continuation", () => {
  it("sends Core's cursor for the next page and appends its matches", async () => {
    const match = (line: number) => ({ artifact_id: "artifact-a", filename: "scan.txt", line, context: [{ line, text: `open ${line}/tcp` }] });
    const { api, fetchMock } = client(
      { matches: [match(1), match(2)], skipped: [{ artifact_id: "bin", reason: "binary_or_non_text" }], truncated: true, continuation_cursor: "S-1" },
      { matches: [match(30)], skipped: [{ artifact_id: "bin", reason: "binary_or_non_text" }], truncated: false, continuation_cursor: null },
    );
    const first = await api.searchToolOutput("call-a", "open");
    expect(sent(fetchMock, 0).cursor).toBeNull();
    expect(first.continuationCursor).toBe("S-1");
    const second = await api.searchToolOutput("call-a", "open", { cursor: first.continuationCursor });
    expect(sent(fetchMock, 1)).toMatchObject({ query: "open", cursor: "S-1" });
    const shown = appendSearchPage(first, second);
    expect(shown.matches.map((item) => item.line)).toEqual([1, 2, 30]);
    expect(shown.skipped).toEqual([{ artifactId: "bin", reason: "binary_or_non_text" }]);
    expect(shown.truncated).toBe(false);
    expect(shown.continuationCursor).toBeUndefined();
    expect(appendSearchPage(undefined, first)).toBe(first);
  });
});
