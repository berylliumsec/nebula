import { describe, expect, it } from "vitest";
import { TERMINAL_KEYS, withControl } from "./terminalKeys";

describe("terminal key row", () => {
  it("maps latched Control letters to C0 control codes", () => {
    expect(withControl("c")).toBe("\x03");
    expect(withControl("C")).toBe("\x03");
    expect(withControl("d")).toBe("\x04");
    expect(withControl("[")).toBe("\x1b");
    expect(withControl(" ")).toBe("\x00");
  });

  it("passes through input a Control latch cannot modify", () => {
    expect(withControl("ls")).toBe("ls");
    expect(withControl("5")).toBe("5");
  });

  it("sends standard arrow and escape sequences", () => {
    const sequence = Object.fromEntries(TERMINAL_KEYS.map((key) => [key.id, key.sequence]));
    expect(sequence.esc).toBe("\x1b");
    expect(sequence.tab).toBe("\t");
    expect(sequence.up).toBe("\x1b[A");
    expect(sequence.left).toBe("\x1b[D");
  });
});
