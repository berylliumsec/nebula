import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EditorMarkdownPreview } from "./EditorMarkdownPreview";

describe("EditorMarkdownPreview", () => {
  it("renders GFM as reading content without activating embedded HTML or unsafe links", () => {
    render(<EditorMarkdownPreview filePath="README.markdown" content={'# Guide\n\n| Name | State |\n| --- | --- |\n| Preview | Ready |\n\n- [x] Done\n\n<script>alert(1)</script>\n\n[unsafe](javascript:alert(1)) [external](https://example.com)'} />);
    const preview = screen.getByRole("region", { name: "Markdown preview: README.markdown" });
    expect(within(preview).getByRole("heading", { name: "Guide" })).toBeVisible();
    expect(within(preview).getByRole("table")).toHaveTextContent("PreviewReady");
    expect(within(preview).getByRole("checkbox", { name: "Done" })).toBeDisabled();
    expect(preview.querySelector("script")).toBeNull();
    expect(within(preview).getByText("unsafe").closest("a")).toBeNull();
    expect(within(preview).getByRole("link", { name: "external" })).toHaveAttribute("rel", "noopener noreferrer");
  });
});
