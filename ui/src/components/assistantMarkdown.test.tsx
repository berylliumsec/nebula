import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AssistantMarkdown } from "./AssistantMarkdown";
import { parseExactFences } from "./assistantMarkdown";

describe("exact assistant Markdown", () => {
  it("retains exact closed fence offsets and makes an unclosed fence inert", () => {
    const exact = "before\r\n```python meta\r\n\tprint('λ')\r\n```\r\nafter";
    const parsed = parseExactFences(exact);
    expect(parsed.unmatchedStart).toBeUndefined();
    expect(parsed.blocks[0].source).toBe("\tprint('λ')\r\n");
    expect(exact.slice(parsed.blocks[0].sourceStart, parsed.blocks[0].sourceEnd)).toBe(parsed.blocks[0].source);

    const malformed = parseExactFences("ok\n```sh\necho no close\n");
    expect(malformed.blocks).toEqual([]);
    expect(malformed.unmatchedStart).toBe(3);
  });

  it("blocks raw HTML, images, and unsafe link protocols", () => {
    const onRun = vi.fn();
    const { container } = render(
      <AssistantMarkdown
        content={'<img src=x onerror="alert(1)">\n\n![remote](https://example.test/pixel.png)\n\n[unsafe](javascript:alert(1)) [safe](https://example.test/)'}
        durable
        messageId="message-1"
        runnableLanguages={new Set(["python"])}
        onRun={onRun}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("unsafe").closest("a")).not.toHaveAttribute("href");
    expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute("href", "https://example.test/");
  });

  it("copies and runs only immutable selected source with UTF-8 byte offsets", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const onRun = vi.fn();
    const source = "aλb\n";
    const { container } = render(
      <AssistantMarkdown
        content={`\`\`\`python\n${source}\`\`\`\n`}
        durable
        messageId="message-1"
        runnableLanguages={new Set(["python"])}
        onRun={onRun}
      />,
    );
    const code = container.querySelector(".assistant-code-block code");
    expect(code?.textContent).toBe(source);
    const walker = document.createTreeWalker(code as Node, NodeFilter.SHOW_TEXT);
    let lambdaNode: Text | undefined;
    while (walker.nextNode()) {
      const node = walker.currentNode as Text;
      if (node.data.includes("λ")) { lambdaNode = node; break; }
    }
    expect(lambdaNode).toBeDefined();
    const offset = lambdaNode?.data.indexOf("λ") ?? 0;
    const range = document.createRange();
    range.setStart(lambdaNode as Text, offset);
    range.setEnd(lambdaNode as Text, offset + 1);
    const selection = globalThis.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);

    const block = container.querySelector(".assistant-code-block") as HTMLElement;
    await user.click(within(block).getByRole("button", { name: "Copy exact code" }));
    expect(writeText).toHaveBeenCalledWith("λ");
    await user.click(within(block).getByRole("button", { name: "Review and run python code" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    expect(onRun.mock.calls[0][0]).toMatchObject({
      source: "λ",
      language: "python",
      origin: {
        messageId: "message-1",
        selectionStartByte: 1,
        selectionEndByte: 3,
      },
    });
  });

  it("shows Copy but not Run for unsupported, unavailable, or non-durable fences", () => {
    const onRun = vi.fn();
    const { rerender } = render(
      <AssistantMarkdown content={'```json\n{"safe": true}\n```'} durable messageId="m" runnableLanguages={new Set(["python"])} onRun={onRun} />,
    );
    expect(screen.getByRole("button", { name: "Copy exact code" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Review and run/ })).toBeNull();
    rerender(<AssistantMarkdown content={'```python\nprint(1)\n```'} durable messageId="m" runnableLanguages={new Set()} onRun={onRun} />);
    expect(screen.queryByRole("button", { name: /Review and run/ })).toBeNull();
    rerender(<AssistantMarkdown content={'```python\nprint(1)\n```'} durable={false} messageId="m" runnableLanguages={new Set(["python"])} onRun={onRun} />);
    expect(screen.queryByRole("button", { name: /Review and run/ })).toBeNull();
  });
});
