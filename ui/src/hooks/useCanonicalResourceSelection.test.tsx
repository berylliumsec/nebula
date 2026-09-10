import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {useState} from "react";
import {MemoryRouter, Route, Routes, useNavigate} from "react-router-dom";
import {describe, expect, it, vi} from "vitest";
import {useCanonicalResourceSelection} from "./useCanonicalResourceSelection";

vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({engagement: {id: "project"}})}));
const initialItems = [{id: "a", label: "Saved revision one"}];

function Inspector({items = initialItems}: {items?: typeof initialItems}) {
  const [selected, setSelected] = useState<(typeof items)[number]>();
  const {closeResource, missingResourceId, openResource} = useCanonicalResourceSelection("library_item", items, selected, setSelected);
  const navigate = useNavigate();
  return <>
    <output data-testid="selected">{selected?.label ?? "none"}</output>
    <output data-testid="missing">{missingResourceId}</output>
    <button onClick={closeResource}>Close</button>
    <button onClick={() => openResource(items[0])}>Inspect item</button>
    <button onClick={() => navigate(-1)}>Back</button>
    <button onClick={() => navigate(1)}>Forward</button>
    <button onClick={() => navigate("/library/missing")}>Invalid link</button>
  </>;
}

function Harness({items = initialItems}: {items?: typeof initialItems}) {
  return <MemoryRouter initialEntries={["/library", "/library/a"]} initialIndex={1}>
    <Routes><Route path="/library/:resourceId?" element={<Inspector items={items} />} /></Routes>
  </MemoryRouter>;
}

describe("canonical resource selection", () => {
  it("returns keyboard focus to the initiating item when details close", async () => {
    const user = userEvent.setup(); render(<Harness />);
    await user.click(screen.getByRole("button", {name: "Close"}));
    const opener = screen.getByRole("button", {name: "Inspect item"});
    opener.focus(); await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", {name: "Close"}));
    await waitFor(() => expect(opener).toHaveFocus());
  });
  it("closes the inspector with its canonical list route", async () => {
    const user = userEvent.setup(); render(<Harness />);
    expect(screen.getByTestId("selected")).toHaveTextContent("Saved revision one");
    await user.click(screen.getByRole("button", {name: "Close"}));
    expect(screen.getByTestId("selected")).toHaveTextContent("none");
  });
  it("clears on Back and restores the saved record on Forward", async () => {
    const user = userEvent.setup(); render(<Harness />);
    await user.click(screen.getByRole("button", {name: "Back"}));
    expect(screen.getByTestId("selected")).toHaveTextContent("none");
    await user.click(screen.getByRole("button", {name: "Forward"}));
    expect(screen.getByTestId("selected")).toHaveTextContent("Saved revision one");
  });
  it("does not present the previous record under an invalid new identity", async () => {
    const user = userEvent.setup(); render(<Harness />);
    await user.click(screen.getByRole("button", {name: "Invalid link"}));
    expect(screen.getByTestId("missing")).toHaveTextContent("missing");
    expect(screen.getByTestId("selected")).toHaveTextContent("none");
  });
  it("preserves the selected edit snapshot when the same item is refreshed", () => {
    const {rerender} = render(<Harness />);
    rerender(<Harness items={[{id: "a", label: "Another operator saved revision two"}]} />);
    expect(screen.getByTestId("selected")).toHaveTextContent("Saved revision one");
  });
});
