import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ThinkingDisclosure } from "./HarnessThinking";

describe("ThinkingDisclosure", () => {
  it("hides model thoughts until the operator expands them", async () => {
    render(<ThinkingDisclosure text="Private chain of thought." />);
    const disclosure = screen.getByLabelText("Thinking");
    expect(disclosure).toBeInTheDocument();
    expect(disclosure).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("Thinking"));
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByText("Private chain of thought.")).toBeVisible();
  });

  it("does not render when the model returned no thoughts", () => {
    const { container } = render(<ThinkingDisclosure text="" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps model paragraph boundaries semantic inside the disclosure", async () => {
    const { container } = render(
      <ThinkingDisclosure text={"First reasoning paragraph.\n\nSecond reasoning paragraph."} />,
    );

    await userEvent.click(screen.getByText("Thinking"));

    const paragraphs = container.querySelectorAll(".assistant-markdown p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0]).toHaveTextContent("First reasoning paragraph.");
    expect(paragraphs[1]).toHaveTextContent("Second reasoning paragraph.");
  });
});
