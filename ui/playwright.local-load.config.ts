// Local-only (never committed): the default config with longer time budgets for a host at load ~40.
// Only time budgets change: test.setTimeout() calls are multiplied and the default expect wait is raised.
import base from "./playwright.config";
import { test } from "@playwright/test";

const scale = Number(process.env.NEBULA_LOCAL_TIMEOUT_SCALE ?? 3);
const original = test.setTimeout.bind(test);
test.setTimeout = (timeout: number) => original(timeout * scale);
export default { ...base, timeout: 30_000 * scale, expect: { ...base.expect, timeout: Number(process.env.NEBULA_LOCAL_EXPECT_TIMEOUT ?? 20_000) } };
