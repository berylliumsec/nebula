import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { BookOpenCheck, Search } from "lucide-react";
import { expect, test, vi } from "vitest";

test("renders mapped Phosphor icons through the Lucide-compatible surface", () => {
  const ref = createRef<SVGSVGElement>();
  const clicked = vi.fn();
  render(<Search ref={ref} size={18} color="rebeccapurple" className="spin" aria-label="Find" onClick={clicked} />);
  const icon = screen.getByRole("img", { name: "Find" });
  expect(icon).toBe(ref.current);
  expect(icon).toHaveAttribute("data-icon-family", "phosphor");
  expect(icon).toHaveAttribute("data-icon-weight", "regular");
  expect(icon).toHaveAttribute("width", "18");
  expect(icon).toHaveAttribute("color", "rebeccapurple");
  expect(icon).toHaveClass("lucide", "lucide-search", "spin");
  fireEvent.click(icon);
  expect(clicked).toHaveBeenCalledOnce();
});

test("selects light artwork at display sizes and gives composed definitions unique ids", () => {
  const { container } = render(<><BookOpenCheck size={24} /><BookOpenCheck size={16} strokeWidth={1.4} /></>);
  const icons = [...container.querySelectorAll("svg")];
  expect(icons.map(icon => icon.dataset.iconWeight)).toEqual(["light", "light"]);
  const ids = icons.map(icon => icon.querySelector("clipPath")?.id);
  expect(ids.every(Boolean)).toBe(true);
  expect(new Set(ids).size).toBe(2);
  icons.forEach(icon => expect(icon.querySelector("g[clip-path]")?.getAttribute("clip-path")).toBe(`url(#${icon.querySelector("clipPath")?.id})`));
});

test("leaves an icon's artwork untouched when its parent renders again", () => {
  const view = render(<button type="button"><Search size={14} /></button>);
  const artwork = view.container.querySelector("g")!;
  const records: MutationRecord[] = [];
  const observer = new MutationObserver((items) => records.push(...items));
  observer.observe(view.container, { childList: true, subtree: true });
  view.rerender(<button type="button"><Search size={14} /></button>);
  records.push(...observer.takeRecords());
  expect(records).toEqual([]);
  expect(view.container.querySelector("g")).toBe(artwork);

  // A different weight still swaps the artwork.
  view.rerender(<button type="button"><Search size={14} weight="light" /></button>);
  records.push(...observer.takeRecords());
  observer.disconnect();
  expect(records.filter((record) => record.target === artwork)).not.toHaveLength(0);
  expect(view.container.querySelector("svg")).toHaveAttribute("data-icon-weight", "light");
});
