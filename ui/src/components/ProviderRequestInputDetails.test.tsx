import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { ProviderRequestInputDetails } from "./ProviderRequestInputDetails";

it("shows content-free request estimates and provider-reported usage", async () => {
  render(<ProviderRequestInputDetails request={{
    instructions: 1200,
    conversation: 700,
    toolSchemas: 500,
    toolResults: 300,
    other: 0,
    estimatedTotal: 2700,
    reportedInputTokens: 2510,
    attempt: 2,
  }} />);

  await userEvent.click(screen.getByText("Last provider request"));
  expect(screen.getByText(/2,700 estimated input tokens.*2,510 reported by provider/)).toBeVisible();
  expect(screen.getByText("Tool schemas: 500 estimated")).toBeVisible();
  expect(screen.getByText("Tool results: 300 estimated")).toBeVisible();
  expect(screen.queryByText(/Other:/)).not.toBeInTheDocument();
  expect(screen.getByText(/attempt 2/)).toBeVisible();
});
