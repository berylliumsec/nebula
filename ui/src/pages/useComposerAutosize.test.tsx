import {render} from "@testing-library/react";
import {useRef} from "react";
import {afterEach, describe, expect, it, vi} from "vitest";
import {useComposerAutosize} from "./useComposerAutosize";

let width = 240;
let contentHeight = 84;
let notifyResize: () => void;
const disconnect = vi.fn();

function Composer({draft, identity = "chat"}: {draft: string; identity?: string}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useComposerAutosize(ref, draft, 160, identity);
  return <textarea ref={ref} value={draft} readOnly />;
}

function setup() {
  width = 240; contentHeight = 84; disconnect.mockClear();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) {notifyResize = callback;}
    observe() {}
    disconnect = disconnect;
  });
  vi.spyOn(HTMLTextAreaElement.prototype, "getBoundingClientRect").mockImplementation(() => ({width}) as DOMRect);
  return vi.spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get").mockImplementation(() => contentHeight);
}

afterEach(() => {vi.restoreAllMocks(); vi.unstubAllGlobals();});

describe("composer sizing", () => {
  it("resizes an unchanged draft when rotation or a pane changes its width", () => {
    setup();
    const {getByRole} = render(<Composer draft="Keep this exact draft" />);
    const textarea = getByRole("textbox");
    expect(textarea.style.height).toBe("84px");
    width = 700; contentHeight = 44; notifyResize();
    expect(textarea.style.height).toBe("44px");
    expect(textarea).toHaveValue("Keep this exact draft");
  });
  it("does not resize again for a height-only observer notification", () => {
    const reads = setup();
    render(<Composer draft="Keep this exact draft" />);
    reads.mockClear(); notifyResize(); notifyResize();
    expect(reads).not.toHaveBeenCalled();
  });
  it("measures natural content without WebKit's minimum-height padding", () => {
    setup();
    vi.spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLTextAreaElement) {
      expect(this.style.height).toBe("0px");
      expect(this.style.minHeight).toBe("0px");
      return 36;
    });
    const {getByRole} = render(<Composer draft="One line" />);
    expect(getByRole("textbox").style.height).toBe("36px");
    expect(getByRole("textbox").style.minHeight).toBe("");
  });
  it("bounds long drafts and restores CSS sizing when cleared", () => {
    setup(); contentHeight = 400;
    const {getByRole, rerender} = render(<Composer draft="Long draft" />);
    const textarea = getByRole("textbox");
    expect(textarea.style.height).toBe("160px");
    expect(textarea.style.overflowY).toBe("auto");
    rerender(<Composer draft="" />);
    expect(textarea.style.height).toBe("");
    expect(textarea.style.overflowY).toBe("hidden");
  });
  it("cleans up observers and refreshes sizing after a conversation switch", () => {
    setup();
    const {getByRole, rerender, unmount} = render(<Composer draft="Draft" />);
    contentHeight = 44;
    rerender(<Composer draft="Draft" identity="other-chat" />);
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(getByRole("textbox").style.height).toBe("44px");
    unmount();
    expect(disconnect).toHaveBeenCalledTimes(2);
  });
});
