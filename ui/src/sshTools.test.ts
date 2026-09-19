import {describe, expect, it} from "vitest";
import {sshApprovalTarget, sshToolHost} from "./sshTools";

describe("ssh tool helpers", () => {
  it("recognizes per-host command tools", () => {
    expect(sshToolHost("ssh.research3.run_command")).toBe("research3");
    expect(sshToolHost("ssh.research3s-macbook-local-a1b2c3.run_command")).toBe("research3s-macbook-local");
    expect(sshToolHost("run_command")).toBeUndefined();
    expect(sshToolHost("mcp.abc.run_command")).toBeUndefined();
  });

  it("reads the host and command from an approval", () => {
    const target = sshApprovalTarget({
      exact_request: {tool_name: "ssh.research3.run_command", arguments: {command: "xcodebuild -version", cwd: "~/research"}},
      expected_effects: ["Run a shell command on the operator's machine Research Mac (SSH host research3).\nThe command runs in /bin/sh on that machine."],
    });

    expect(target).toEqual({label: "Research Mac", alias: "research3", command: "xcodebuild -version", cwd: "~/research"});
    expect(sshApprovalTarget({exact_request: {tool_name: "run_command", arguments: {command: "id"}}})).toBeUndefined();
  });
});
