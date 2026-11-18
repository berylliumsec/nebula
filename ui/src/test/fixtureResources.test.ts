import {describe, expect, it, vi} from "vitest";
import {ResourceScope} from "../../tests/fixtures/resource-scope";
import {withOwnedCleanup} from "../../tests/fixtures/owned-cleanup";

describe("journey resource ownership", () => {
  it("releases an acquired resource when later setup fails", async () => {
    const scope = new ResourceScope();
    const controller = new AbortController();
    scope.own("pending operation", controller, value => value.abort());
    const startupFailure = new Error("Core could not start");
    await expect(withOwnedCleanup(async () => {throw startupFailure;}, [{name: "journey", dispose: () => scope.close()}])).rejects.toBe(startupFailure);
    expect(controller.signal.aborted).toBe(true);
  });

  it("disposes in reverse acquisition order and continues after a failure", async () => {
    const scope = new ResourceScope();
    const order: string[] = [];
    scope.own("model", "model", value => {order.push(value);});
    scope.own("Core", "Core", value => {order.push(value); throw new Error("Core teardown failed");});
    scope.own("API", "API", value => {order.push(value);});
    await expect(scope.close()).rejects.toThrow("Core");
    expect(order).toEqual(["API", "Core", "model"]);
  });

  it("preserves the primary failure while reporting failed cleanup", async () => {
    const scope = new ResourceScope();
    scope.own("Core", null, () => {throw new Error("cleanup failed");});
    const primary = new Error("assertion failed");
    const report = vi.fn();
    await expect(withOwnedCleanup(async () => {throw primary;}, [{name: "journey", dispose: () => scope.close()}], report)).rejects.toBe(primary);
    expect(report).toHaveBeenCalledOnce();
  });

  it("does not dispose twice when cleanup is requested concurrently", async () => {
    const scope = new ResourceScope();
    const dispose = vi.fn();
    scope.own("Core", null, dispose);
    await Promise.all([scope.close(), scope.close()]);
    expect(dispose).toHaveBeenCalledOnce();
    expect(() => scope.own("late resource", null, dispose)).toThrow("after teardown");
  });

  it("bounds diagnostic output and tolerates a failed diagnostic reader", async () => {
    const scope = new ResourceScope();
    scope.own("broken", null, () => {}, () => {throw new Error("unreadable");});
    scope.own("Core", null, () => {}, () => "a".repeat(100_000));
    const output = await scope.diagnostics();
    expect(output).toContain("Diagnostics unavailable.");
    expect(output.endsWith("a".repeat(64_000))).toBe(true);
    expect(output.length).toBeLessThan(65_000);
    await scope.close();
  });
});
